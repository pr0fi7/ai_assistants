import logging
import os
import random
import asyncio
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes
# Import your multi-agent chat function
from test import multi_agent_chat
from utils import getWhatOnImage, getWhatonAudio
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from pydub import AudioSegment
import io
from openai import BadRequestError

import moviepy as mp
import tempfile
from PIL import Image


# Load environment variables
load_dotenv()
TOKEN = os.getenv("TOKEN")

# Set up logging
def setup_logging():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
        level=logging.INFO
    )
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

# Utility to maybe split long assistant replies
def maybe_split(text: str, split_chance: float = 0.3):
    """
    With probability `split_chance`, split `text` on sentence or newline boundaries.
    Otherwise, return [text] as a single chunk.
    """
    if random.random() > split_chance:
        return [text]
    parts = __import__('re').split(r'(?<=[\.\!?])\s+|\n', text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if len(parts) >= 2 else [text]

async def process_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    # Wait to batch multi-turn user messages
    await asyncio.sleep(10)
    pending = context.user_data.pop('pending_messages', [])
    context.user_data.pop('pending_task', None)
    if not pending:
        return

    # Combine all buffered user prompts
    user_prompt = "\n".join(pending)
    # Call your multi-agent function once
    conv, phone_flag = multi_agent_chat(
        user_prompt=user_prompt,
        conversation=context.user_data.get('conversation'),
        phone_number=context.user_data.get('phone_number_received', False)
    )
    # Save state
    context.user_data['conversation'] = conv
    context.user_data['phone_number_received'] = phone_flag

    # Send assistant reply in chunks
    last_message = conv[-1]['content']
    parts = maybe_split(last_message, split_chance=0.3)
    for part in parts:
        await context.bot.send_message(chat_id, part)
        await asyncio.sleep(random.uniform(1.0, 3.0))

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    if not text:
        return
    # Buffer incoming user messages
    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(text)
    # Schedule batching if not already scheduled
    if not context.user_data.get('pending_task'):
        task = asyncio.create_task(process_pending(context, update.effective_chat.id))
        context.user_data['pending_task'] = task

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Download the highest-resolution photo sent
    photo = update.message.photo
    if not photo:
        return
    file_id = photo[-1].file_id
    file = await context.bot.get_file(file_id)
    image_bytes = await file.download_as_bytearray()
    # Describe the image (await the coroutine)
    description = await getWhatOnImage(image_bytes)
    # Include any accompanying caption
    caption = update.message.caption or ""
    combined = 'Пользователь отправил фото с следующим описанием' + description + (f"и таким текстом: {caption}" if caption else "")

    # Buffer description as user input
    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(combined)
    if not context.user_data.get('pending_task'):
        task = asyncio.create_task(process_pending(context, update.effective_chat.id))
        context.user_data['pending_task'] = task

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Handle both voice notes (.oga) and uploaded audio files
    media = update.message.voice or update.message.audio
    if not media:
        return
    file = await context.bot.get_file(media.file_id)
    raw_bytes = await file.download_as_bytearray()

    # Convert to WAV (PCM 16-bit) for OpenAI transcription
    try:
        # Let pydub auto-detect format; codec ensures compatible WAV
        audio_seg = AudioSegment.from_file(io.BytesIO(bytes(raw_bytes)))
        buf = io.BytesIO()
        audio_seg.export(buf, format='wav', codec='pcm_s16le')
        buf.seek(0)
        wav_io = buf
        wav_io.name = 'audio.wav'
    except FileNotFoundError:
        logging.error("ffmpeg/ffprobe not found. Audio conversion failed.")
        await context.bot.send_message(
            update.effective_chat.id,
            "⚠️ Audio conversion failed. Please install ffmpeg and ensure it's in PATH or set FFMPEG_PATH."
        )
        return
    except Exception as e:
        logging.error(f"Audio conversion error: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"⚠️ Audio conversion error: {e}"  
        )
        return

    # Transcribe with OpenAI
    try:
        transcription = await getWhatonAudio(wav_io)
    except BadRequestError as e:
        logging.error(f"Transcription error: {e}")
        await context.bot.send_message(
            update.effective_chat.id,
            f"⚠️ Transcription failed: {e}\nEnsure the audio is clear and in a supported format (WAV/MP3/OGG)."
        )
        return

    caption = update.message.caption or ""
    combined = transcription + (f" Пользователь отправил следующее голосовое сообщение: {caption}" if caption else "")
    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(combined)
    if not context.user_data.get('pending_task'):
        context.user_data['pending_task'] = asyncio.create_task(
            process_pending(context, update.effective_chat.id)
        )
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    media = update.message.video or update.message.video_note
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
    combined += "\n\n" + (update.message.caption or "")
    pending = context.user_data.setdefault("pending_messages", [])
    pending.append(combined)
    if not context.user_data.get("pending_task"):
        context.user_data["pending_task"] = asyncio.create_task(
            process_pending(context, update.effective_chat.id)
        )


def main():
    setup_logging()
    application = Application.builder().token(TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_image))
    application.add_handler(
        MessageHandler((filters.VOICE | filters.AUDIO) & ~filters.COMMAND, handle_audio)
    )
    application.add_handler(MessageHandler((filters.VIDEO | filters.VIDEO_NOTE) & ~filters.COMMAND, handle_video))

    application.run_polling(allowed_updates=Update.ALL_TYPES)   

if __name__ == "__main__":
    main()