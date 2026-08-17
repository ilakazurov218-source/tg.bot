from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from groq import AsyncGroq
from pymongo import MongoClient
import os
import asyncio
import threading

TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MONGODB_URI = os.environ.get("MONGODB_URI")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # Например: https://your-app.onrender.com

groq_client = AsyncGroq(api_key=GROQ_API_KEY)

try:
    mongo_client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True, serverSelectionTimeoutMS=30000)
    mongo_client.admin.command('ping')
    print("MongoDB подключена успешно!")
except Exception as e:
    print(f"Ошибка подключения к MongoDB: {e}")
    exit(1)

db = mongo_client["telegram_bot"]
conversations = db["conversations"]
MAX_HISTORY = 20

# Flask приложение
flask_app = Flask(__name__)

# Telegram приложение
telegram_app = Application.builder().token(TOKEN).build()

# --- Постоянный фоновый event loop ---
# Решает проблему "Event loop is closed": раньше каждый запрос от Flask
# мог выполняться в новом event loop, а объект telegram_app был привязан
# к старому. Теперь все асинхронные операции идут через один и тот же loop,
# который живёт в отдельном потоке всё время работы приложения.
background_loop = asyncio.new_event_loop()


def _start_background_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


loop_thread = threading.Thread(target=_start_background_loop, args=(background_loop,), daemon=True)
loop_thread.start()


def run_coro(coro):
    """Запускает корутину в общем фоновом event loop и ждёт результат."""
    future = asyncio.run_coroutine_threadsafe(coro, background_loop)
    return future.result()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conversations.update_one({"user_id": user_id}, {"$set": {"messages": []}}, upsert=True)
    await update.message.reply_text("Привет! я Болтунчик")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_message = update.message.text
    user_id = update.message.from_user.id

    if "погода" in user_message.lower():
        city = user_message.lower().replace("погода", "").strip()
        if not city:
            await update.message.reply_text("Напиши город, например: погода Москва")
            return
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
        response = requests.get(url)
        data = response.json()
        if response.status_code == 200:
            temp = data["main"]["temp"]
            description = data["weather"][0]["description"]
            await update.message.reply_text(f"Погода в {city}:\nТемпература: {temp}°C\nОписание: {description}")
        else:
            await update.message.reply_text("Город не найден. Проверь написание.")
        return

    user_doc = conversations.find_one({"user_id": user_id})
    history = user_doc["messages"] if user_doc else []
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    try:
        ai_response = await groq_client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": "Ты дружелюбный помощник в телеграм-боте. Отвечай кратко и по-русски."},
                *history
            ]
        )
        reply_text = ai_response.choices[0].message.content
        history.append({"role": "assistant", "content": reply_text})
        conversations.update_one({"user_id": user_id}, {"$set": {"messages": history}}, upsert=True)
        await update.message.reply_text(reply_text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при обращении к AI: {e}")


# Регистрируем обработчики
telegram_app.add_handler(CommandHandler("start", start))
telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Инициализируем telegram_app один раз в фоновом loop при старте процесса,
# а не при каждом запросе (раньше это делал "async with telegram_app" в вебхуке).
run_coro(telegram_app.initialize())


@flask_app.route("/")
def index():
    return "Bot is running!"


@flask_app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    update = Update.de_json(data, telegram_app.bot)
    run_coro(telegram_app.process_update(update))
    return "OK", 200


def setup_webhook():
    run_coro(telegram_app.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook/{TOKEN}"))
    print(f"Webhook установлен: {WEBHOOK_URL}/webhook/{TOKEN}")


if __name__ == "__main__":
    # Устанавливаем webhook при старте
    setup_webhook()

    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
