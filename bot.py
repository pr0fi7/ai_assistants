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

INITIAL_QUESTIONS = [
    "Сколько лет собеседнику?",
    "Откуда собеседник?",
    "Где и кем работает собеседник?",
    "Какая у собеседника зарплата?",
    "С кем живёт собеседник?",
    "У собеседника свой дом или съёмное жильё?",
    "Есть ли у собеседника машина?",
    "Был ли у собеседника опыт работы на бирже?",
    "Как собеседник относится к криптовалюте?"
    ]

def setup_logging():
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
        level=logging.INFO
    )
    logging.getLogger(__name__).setLevel(logging.INFO)


def maybe_split(text: str, split_chance: float = 0.3):
    if random.random() > split_chance:
        return [text]
    parts = __import__('re').split(r'(?<=[\.!?])\s+|\n', text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if len(parts) >= 2 else [text]


async def process_pending(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    # wait to batch incoming messages
    await asyncio.sleep(10)
    pending = context.user_data.pop('pending_messages', [])
    context.user_data.pop('pending_task', None)
    if not pending:
        return
    questions = context.user_data.get('questions')
    if questions is None:
        questions = INITIAL_QUESTIONS.copy()
    context.user_data['questions'] = questions

    # call multi-agent chat
    final_response = multi_agent_chat(
        user_prompt="\n".join(pending),
        conversation=context.user_data.get('conversation'),
        questions=questions
    )
    conv = final_response.get('conversation', [])
    photo_status = final_response.get('photo_status', False)
    context.user_data['questions'] = final_response.get('questions', [])
    context.user_data['conversation'] = conv
    context.user_data['photo_status'] = photo_status

    # handle business_connection_id if present
    business_conn = context.user_data.pop('business_connection_id', None)
    extra = {}
    if business_conn:
        extra['business_connection_id'] = business_conn

    # send assistant reply in chunks
    last_message = conv[-1]['content']
    for part in maybe_split(last_message, split_chance=0.3):
        await context.bot.send_message(chat_id, part, **extra)
        await asyncio.sleep(random.uniform(1.0, 3.0))

    # if photo_status is True, send a random unused image
    if photo_status:
        # track used images for this user
        used = context.user_data.setdefault('used_images', [])
        # define available image names
        all_images = [f"{i}.jpg" for i in range(1, 10)]
        # filter out already used images
        available = [img for img in all_images if img not in used]
        # if none left, reset
        if not available:
            used.clear()
            available = all_images.copy()
        # choose one at random
        selected = random.choice(available)
        used.append(selected)
        # send the photo
        image_path = os.path.join('images', selected)
        try:
            with open(image_path, 'rb') as photo_file:
                await context.bot.send_photo(chat_id, photo=photo_file, **extra)
        except FileNotFoundError:
            logging.error(f"Image file not found: {image_path}")

async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # use effective_message
    if update.business_message:
        logging.info("GOT BUSINESS_MESSAGE, conn_id=%s", 
                    update.business_message.business_connection_id)

    msg = update.effective_message  
    if not msg or not msg.text:
        return

    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(msg.text.strip())
    if not context.user_data.get('pending_task'):
        context.user_data['pending_task'] = asyncio.create_task(
            process_pending(context, update.effective_chat.id)
        )

async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.business_message:
        logging.info(
            "GOT BUSINESS_MESSAGE, conn_id=%s", 
            update.business_message.business_connection_id
        )

    msg = update.effective_message  
    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    photo = msg.photo
    if not photo:
        return
    file = await context.bot.get_file(photo[-1].file_id)
    image_bytes = await file.download_as_bytearray()

    description = await getWhatOnImage(image_bytes)

    caption = msg.caption or ""

    combined = (
        "Пользователь отправил фото с следующим описанием: "
        + description
        + (f" и таким текстом: {caption}" if caption else "")
    )

    buffer = context.user_data.setdefault('pending_messages', [])
    buffer.append(combined)
    if not context.user_data.get('pending_task'):
        context.user_data['pending_task'] = asyncio.create_task(
            process_pending(context, msg.chat.id)
        )

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.business_message:
        logging.info(
            "GOT BUSINESS_MESSAGE, conn_id=%s",
            update.business_message.business_connection_id
        )

    msg = update.effective_message
    if update.business_message:
        context.user_data['business_connection_id'] = msg.business_connection_id

    media = msg.voice or msg.audio
    if not media:
        return

    file = await context.bot.get_file(media.file_id)
    raw_bytes = await file.download_as_bytearray()

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

    try:
        transcription = await getWhatonAudio(wav_io)
    except BadRequestError as e:
        logging.error(f"Transcription error: {e}")
        await context.bot.send_message(
            msg.chat.id,
            f"⚠️ Transcription failed: {e}"
        )
        return

    caption = msg.caption or ""
    combined = (
        transcription
        + (f" Пользователь отправил следующее голосовое сообщение(голосом не текстом): {caption}"
           if caption else "")
    )

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

    file = await context.bot.get_file(media.file_id)
    vid_bytes = await file.download_as_bytearray()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_vid:
        tmp_vid.write(vid_bytes)
        tmp_vid_path = tmp_vid.name

    audio = AudioSegment.from_file(tmp_vid_path, format="mp4")
    audio = audio.set_frame_rate(16000).set_channels(1)
    buf = io.BytesIO()
    audio.export(buf, format="wav")
    buf.seek(0)
    buf.name = "audio.wav"

    try:
        transcription = await getWhatonAudio(buf)
    except Exception as e:
        transcription = f"[Audio transcription failed: {e}]"
        logging.error(f"Video audio transcription error: {e}")

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

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
