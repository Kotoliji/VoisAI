import os
import openai
import logging
import speech_recognition as sr
from gtts import gTTS
from io import BytesIO
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import pyogg
import ctypes
import numpy as np
import tempfile

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = ""
openai.api_key = ""

DISHES = [
    {"name": "Піца 'Маргарита'", "ingredients": ["томат", "моцарела", "базилік"], "price": 150},
    {"name": "Суші з лососем", "ingredients": ["рис", "лосось", "норі"], "price": 200},
    {"name": "Паста Карбонара", "ingredients": ["спагеті", "бекон", "яйця", "пармезан"], "price": 180},
    {"name": "Салат Цезар", "ingredients": ["салат ромен", "курка", "пармезан", "сухарики"], "price": 120}
]

user_orders = {}

messages = [
    {"role": "system", "content": (
        "Ти - розумний голосовий асистент для замовлення їжі. "
        "Коли користувач хоче щось замовити, ти повинен дати чітку інструкцію в одному з рядків відповіді. "
        "Формат для додавання страви: 'Додати до замовлення: <назва страви>'. "
        "Формат для виключення інгредієнта: 'Виключити інгредієнт: <назва інгредієнта>'. "
    )}
]

def transcribe_audio(ogg_data: BytesIO):
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_file:
        tmp_file.write(ogg_data.getvalue())
        tmp_file_name = tmp_file.name

    try:
        opus_file = pyogg.OpusFile(tmp_file_name)
        channels = opus_file.channels
        buffer_length = opus_file.buffer_length
        total_samples = buffer_length * channels

        c_short_array_type = ctypes.c_short * total_samples
        c_short_array = c_short_array_type.from_address(ctypes.addressof(opus_file.buffer.contents))

        pcm_array = np.ctypeslib.as_array(c_short_array)
        raw_data = pcm_array.tobytes()

        r = sr.Recognizer()
        sample_rate = opus_file.frequency
        audio_data = sr.AudioData(raw_data, sample_rate, 2)
        try:
            text = r.recognize_google(audio_data, language="uk-UA")
            return text.strip()
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            logger.error(f"Помилка розпізнавання: {e}")
            return None
    finally:
        if os.path.exists(tmp_file_name):
            os.remove(tmp_file_name)

async def send_voice_response(update: Update, context: ContextTypes.DEFAULT_TYPE, text_response: str):
    tts = gTTS(text=text_response, lang='uk')
    voice_io = BytesIO()
    tts.write_to_fp(voice_io)
    voice_io.seek(0)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_io, caption=text_response)

def get_chatgpt_response(user_id: int, user_text: str) -> str:
    user_history = user_orders.get(user_id, {"items": [], "history": []})
    user_order_items = user_history["items"]
    order_description = "Поточне замовлення: " + ", ".join([item["name"] for item in user_order_items]) if user_order_items else "Поточне замовлення порожнє."

    convo = messages.copy()
    convo.append({"role": "system", "content": order_description})
    convo.append({"role": "user", "content": user_text})

    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=convo
    )
    reply = response.choices[0].message.content.strip()
    return reply

def process_gpt_reply(user_id: int, reply: str) -> str:
    user_history = user_orders.setdefault(user_id, {"items": [], "history": []})
    lines = reply.split('\n')
    for line in lines:
        line_lower = line.lower()
        if "додати до замовлення" in line_lower:
            for dish in DISHES:
                if dish["name"].lower() in line_lower:
                    user_history["items"].append(dish)
                    break
        if "виключити інгредієнт" in line_lower:
            words = line_lower.split(':')
            if len(words) > 1:
                ingr = words[1].strip()
                for dish in user_history["items"]:
                    dish["ingredients"] = [i for i in dish["ingredients"] if i.lower() != ingr]

    user_orders[user_id] = user_history
    return reply

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    response_text = get_chatgpt_response(user_id, user_text)
    processed_reply = process_gpt_reply(user_id, response_text)
    await update.message.reply_text(processed_reply)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    file_id = update.message.voice.file_id
    voice_file = await context.bot.get_file(file_id)
    voice_bytes = await voice_file.download_as_bytearray()

    recognized_text = transcribe_audio(BytesIO(voice_bytes))

    if recognized_text is None:
        await update.message.reply_text("Вибачте, не вдалося розпізнати ваш голос. Спробуйте ще раз.")
        return

    response_text = get_chatgpt_response(user_id, recognized_text)
    processed_reply = process_gpt_reply(user_id, response_text)
    await send_voice_response(update, context, processed_reply)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "Привіт! Я бот для замовлення їжі.\n"
        "Можете попросити мене порадити страву, додати її до вашого замовлення, "
        "виключити інгредієнти. Надішліть голосове або текстове повідомлення.\n"
        "Скористайтесь /checkout для формування чеку."
    )
    await update.message.reply_text(welcome_text)

async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_history = user_orders.get(user_id, {"items": [], "history": []})
    items = user_history["items"]

    if not items:
        await update.message.reply_text("Ваше замовлення порожнє. Додайте страви перед формуванням чеку.")
        return

    total = 0
    receipt_lines = []
    for dish in items:
        line = f"{dish['name']}: {dish['price']} грн"
        receipt_lines.append(line)
        total += dish['price']

    receipt_text = "Ваш чек:\n" + "\n".join(receipt_lines) + f"\nЗагальна сума: {total} грн"
    await update.message.reply_text(receipt_text)

def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("checkout", checkout))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))

    application.run_polling()

if __name__ == '__main__':
    main()
