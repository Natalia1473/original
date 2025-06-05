import logging
import sqlite3
import os
from datetime import datetime
from difflib import SequenceMatcher

from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters
from flask import Flask, request

load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # без слэша в конце, например: https://mybot.onrender.com
PORT = int(os.getenv("PORT", "8443"))
DB_PATH = "submissions.db"
SIMILARITY_THRESHOLD = 0.7

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            ts TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def save_submission(user_id: int, username: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO submissions (user_id, username, text, ts) VALUES (?, ?, ?, ?)",
        (user_id, username, text, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()

def fetch_all_submissions():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, user_id, username, text, ts FROM submissions").fetchall()
    conn.close()
    return rows

def calculate_max_similarity(new_text: str):
    submissions = fetch_all_submissions()
    best_ratio, best_id, best_user = 0.0, None, None
    for rec_id, rec_user_id, rec_username, rec_text, rec_ts in submissions:
        ratio = SequenceMatcher(None, new_text, rec_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = rec_id
            best_user = rec_username or str(rec_user_id)
    return best_ratio, best_id, best_user

def start(update: Update, context):
    update.message.reply_text(
        "Привет! Я бот для проверки оригинальности. Отправь текст работы."
    )

def help_cmd(update: Update, context):
    update.message.reply_text("/start   /help")

def check_text(update: Update, context):
    user = update.effective_user
    text = update.message.text.strip()
    if not text:
        update.message.reply_text("Пустой текст не принимаю.")
        return
    ratio, matched_id, matched_user = calculate_max_similarity(text)
    if matched_id and ratio >= SIMILARITY_THRESHOLD:
        perc = round(ratio * 100, 1)
        reply = (
            f"⚠ Похожесть: {perc}%\n"
            f"Ближайшая работа: @{matched_user}\n"
            "Твоя работа будет сохранена."
        )
    else:
        reply = "✅ Оригинально. Сохраняю."
    update.message.reply_text(reply)
    save_submission(user.id, user.username or "", text)

# Flask-приложение для webhook
app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, workers=0)

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, check_text))

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    dispatcher.process_update(update)
    return "OK", 200

if __name__ == "__main__":
    init_db()
    # Устанавливаем webhook в Telegram
    bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")
    # Запускаем Flask
    app.run(host="0.0.0.0", port=PORT)
