import os
import base64
import cv2
import numpy as np
from io import BytesIO
from PIL import Image
import dotenv
import aiohttp

from openai import OpenAI

dotenv.load_dotenv()
openai_api_key = os.getenv("OPENAI_API_KEY")


def load_image(image_bytes):
    # Ensure the image file is opened correctly
    print("Type of image_bytes:", type(image_bytes))

    image = Image.open(BytesIO(image_bytes))
    image = image.convert('RGB')  # Ensure image is in RGB format
    image_array = np.array(image)
    return image_array

async def fetch_image_text(session, api_key, base64_image):
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Тебе нужно описать, что изображено на фото с наибольшей детализацией. "
                                    "Постарайся дать как можно больше информации о том, что изображено на фото и важных деталях. "
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}
                    }
                ]
            }
        ],
        "max_tokens": 300
    }

    async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as response:
        result = await response.json()
        return result['choices'][0]['message']['content']

async def getWhatOnImage(image_bytes):
    # OpenAI API Key

    image = load_image(image_bytes)
    _, buffer = cv2.imencode('.jpg', image)
    base64_image = base64.b64encode(buffer.tobytes()).decode('utf-8')

    # async with aiohttp.ClientSession() as session:
    #     tasks = fetch_image_text(session, api_key, base64_image)
    #     results = await asyncio.gather(*tasks)
    async with aiohttp.ClientSession() as session:
        results = await fetch_image_text(session, openai_api_key, base64_image)
    return results

async def getWhatonAudio(audio_bytes):
    # OpenAI API Key
    client = OpenAI(api_key=openai_api_key)
    transcription = client.audio.transcriptions.create(
        model="gpt-4o-transcribe", 
        file=audio_bytes
    )
    return transcription.text
