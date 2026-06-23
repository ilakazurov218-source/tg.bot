def main():
    try:
        test = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=10)
        print(f"Тест Telegram API: {test.status_code} - {test.text}")
    except Exception as e:
        print(f"Не могу подключиться к Telegram: {e}")
    
    threading.Thread(target=run_server, daemon=True).start()
    print("HTTP сервер запущен")
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from groq import AsyncGroq
from pymongo import MongoClient
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
MONGODB_URI = os.environ.get("MONGODB_URI")

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

# Заглушка для Render
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running")
    def log_message(self, format, *args):
        pass

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

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
            model="llama-3.3-70b-versatile",
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

def main():
    # Запускаем веб-сервер в отдельном потоке
    threading.Thread(target=run_server, daemon=True).start()

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
