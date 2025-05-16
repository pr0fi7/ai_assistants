import logging
import os
import random
import asyncio
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    # we need Update again for allowed_updates
)

# your imports…
from test import multi_agent_chat
from utils import getWhatOnImage, getWhatonAudio
from openai import BadRequestError
from pydub import AudioSegment
import io, tempfile
import moviepy as mp
from PIL import Image

load_dotenv()
TOKEN = os.getenv("TOKEN")

def setup_logging():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
        level=logging.INFO
    )
    logging.getLogger(__name__).setLevel(logging.INFO)

def maybe_split(text: str, split_chance: float = 0.3):
    if random.random() > split_chance:
        return [text]
    parts = __import__('re').split(r'(?<=[\.\!?])\s+|\n', text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if len(parts) >= 2 else [text]

async def process_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    await asyncio.sleep(10)
    pending = context.user_data.pop('pending_messages', [])
    context.user_data.pop('pending_task', None)
    if not pending:
        return

    # … your multi-agent call …
    conv, phone_flag = multi_agent_chat(
        user_prompt="\n".join(pending),
        conversation=context.user_data.get('conversation'),
        phone_number=context.user_data.get('phone_number_received', False)
    )
    context.user_data['conversation'] = conv
    context.user_data['phone_number_received'] = phone_flag

    # **If there was a business_connection_id stashed, grab it here:**
    business_conn = context.user_data.pop('business_connection_id', None)
    extra = {}
    if business_conn:
        extra['business_connection_id'] = business_conn

    # Send assistant reply in chunks, including business_connection_id if needed
    last_message = conv[-1]['content']
    for part in maybe_split(last_message, split_chance=0.3):
        await context.bot.send_message(chat_id, part, **extra)
        await asyncio.sleep(random.uniform(1.0, 3.0))


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # **Use effective_message so that business_message → .effective_message**
    if update.business_message:
        logging.info("GOT BUSINESS_MESSAGE, conn_id=%s", 
                    update.business_message.business_connection_id)

    msg = update.effective_message  
    if not msg or not msg.text:
        return

    # **If this was a business_message, stash its connection ID**  
    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    # Buffer and schedule exactly as before
    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(msg.text.strip())
    if not context.user_data.get('pending_task'):
        context.user_data['pending_task'] = asyncio.create_task(
            process_pending(context, update.effective_chat.id)
        )


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Debug log
    if update.business_message:
        logging.info(
            "GOT BUSINESS_MESSAGE, conn_id=%s", 
            update.business_message.business_connection_id
        )

    # Grab the Message object (works for both normal & business)
    msg = update.effective_message  

    # If it’s a business message, stash the connection ID
    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    # Download the highest-resolution photo sent
    photo = msg.photo
    if not photo:
        return
    file = await context.bot.get_file(photo[-1].file_id)
    image_bytes = await file.download_as_bytearray()

    # Describe the image
    description = await getWhatOnImage(image_bytes)

    # **Use msg.caption** instead of update.message.caption
    caption = msg.caption or ""

    combined = (
        "Пользователь отправил фото с следующим описанием: "
        + description
        + (f" и таким текстом: {caption}" if caption else "")
    )

    # Buffer description as user input
    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(combined)
    if not context.user_data.get('pending_task'):
        context.user_data['pending_task'] = asyncio.create_task(
            process_pending(context, msg.chat.id)
        )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Log for debugging
    if update.business_message:
        logging.info(
            "GOT BUSINESS_MESSAGE, conn_id=%s",
            update.business_message.business_connection_id
        )

    # Grab the actual Message object (works for both normal & business)
    msg = update.effective_message

    # If it's a business message, stash its connection_id
    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    # Now handle voice or audio attachments
    media = msg.voice or msg.audio
    if not media:
        return

    file = await context.bot.get_file(media.file_id)
    raw_bytes = await file.download_as_bytearray()

    # Convert to WAV for transcription
    try:
        audio_seg = AudioSegment.from_file(io.BytesIO(raw_bytes))
        buf = io.BytesIO()
        audio_seg.export(buf, format='wav', codec='pcm_s16le')
        buf.seek(0)
        wav_io = buf
        wav_io.name = 'audio.wav'
    except FileNotFoundError:
        logging.error("ffmpeg/ffprobe not found.")
        await context.bot.send_message(
            msg.chat.id,
            "⚠️ Audio conversion failed. Please install ffmpeg."
        )
        return
    except Exception as e:
        logging.error(f"Audio conversion error: {e}")
        await context.bot.send_message(
            msg.chat.id,
            f"⚠️ Audio conversion error: {e}"
        )
        return

    # Transcribe with OpenAI
    try:
        transcription = await getWhatonAudio(wav_io)
    except BadRequestError as e:
        logging.error(f"Transcription error: {e}")
        await context.bot.send_message(
            msg.chat.id,
            f"⚠️ Transcription failed: {e}"
        )
        return

    # **Use msg.caption** instead of update.message.caption
    caption = msg.caption or ""
    combined = (
        transcription
        + (f" Пользователь отправил следующее голосовое сообщение(голосом не текстом): {caption}"
           if caption else "")
    )

    # Buffer and schedule batching just like before
    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(combined)
    if not context.user_data.get('pending_task'):
        context.user_data['pending_task'] = asyncio.create_task(
            process_pending(context, msg.chat.id)
        )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.business_message:
        logging.info("GOT BUSINESS_MESSAGE, conn_id=%s", 
                    update.business_message.business_connection_id)

    msg = update.effective_message
    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    media = msg.video or msg.video_note
    if not media:
        return

    # 1) Download the circular or normal video
    file = await context.bot.get_file(media.file_id)
    vid_bytes = await file.download_as_bytearray()

    # 2) Write video out to a temp file
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_vid:
        tmp_vid.write(vid_bytes)
        tmp_vid_path = tmp_vid.name

    # 3) Pull audio with pydub, resample to 16kHz mono, export PCM16 WAV
    audio = AudioSegment.from_file(tmp_vid_path, format="mp4")
    audio = audio.set_frame_rate(16000).set_channels(1)
    buf = io.BytesIO()
    audio.export(buf, format="wav")       # default codec is pcm_s16le
    buf.seek(0)
    buf.name = "audio.wav"

    # 4) Transcribe with Whisper
    try:
        transcription = await getWhatonAudio(buf)
    except Exception as e:
        transcription = f"[Audio transcription failed: {e}]"
        logging.error(f"Video audio transcription error: {e}")

    # 5) Load video again to sample frames
    clip = mp.VideoFileClip(tmp_vid_path)
    duration = min(clip.duration, 30)
    interval = 2 if duration <= 10 else 5

    frames_desc = []
    for t in range(0, int(duration) + 1, interval):
        frame = clip.get_frame(t)
        img = Image.fromarray(frame)
        img_buf = io.BytesIO()
        img.save(img_buf, format="JPEG")
        img_bytes = img_buf.getvalue()

        try:
            desc = await getWhatOnImage(img_bytes)
        except Exception as e:
            desc = f"[Frame at {t}s failed: {e}]"

        frames_desc.append(f"At {t}s: {desc}")

    # 6) Buffer up a single combined message
    combined = f"Пользователь отправил видео с в котором он говорит: {transcription}\n"
    combined += "\n и на котором видно:\n"
    combined += "\n".join(frames_desc)
    combined += "\n\n" + (msg.caption or "")
    pending = context.user_data.setdefault("pending_messages", [])
    pending.append(combined)
    if not context.user_data.get("pending_task"):
        context.user_data["pending_task"] = asyncio.create_task(
            process_pending(context, msg.chat.id)
        )

def main():
    setup_logging()
    application = Application.builder().token(TOKEN).build()
    
    # Text + Business-Text
    application.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND)
            | (filters.UpdateType.BUSINESS_MESSAGE & filters.TEXT),
            echo
        )
    )

    # Photo + Business-Photo
    application.add_handler(
        MessageHandler(
            (filters.PHOTO & ~filters.COMMAND)
            | (filters.UpdateType.BUSINESS_MESSAGE & filters.PHOTO),
            handle_image
        )
    )

    # Voice/Audio + Business-Voice/Audio
    application.add_handler(
        MessageHandler(
            ((filters.VOICE | filters.AUDIO) & ~filters.COMMAND)
            | (filters.UpdateType.BUSINESS_MESSAGE & (filters.VOICE | filters.AUDIO)),
            handle_audio
        )
    )

    # Video/VideoNote + Business-Video/VideoNote
    application.add_handler(
        MessageHandler(
            ((filters.VIDEO | filters.VIDEO_NOTE) & ~filters.COMMAND)
            | (filters.UpdateType.BUSINESS_MESSAGE & (filters.VIDEO | filters.VIDEO_NOTE)),
            handle_video
        )
    )

    # tell Telegram we want *every* update type (including business_message, deleted_business_messages…)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()