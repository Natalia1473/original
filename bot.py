import logging
import sqlite3
import os
from datetime import datetime
from difflib import SequenceMatcher

from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters
from flask import Flask, request

# --------------- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---------------
load_dotenv()  # ожидает .env в корне проекта
TOKEN = os.getenv("TELEGRAM_TOKEN")        # токен бота
WEBHOOK_URL = os.getenv("WEBHOOK_URL")     # HTTPS URL вашего сервиса без слэша в конце, например https://mybot.onrender.com
PORT = int(os.getenv("PORT", "8443"))      # порт, на котором будет слушать Flask
DB_PATH = "submissions.db"                 # файл базы данных SQLite
SIMILARITY_THRESHOLD = 0.7                 # порог похожести (0.7 = 70%)

# ------------- ЛОГИРОВАНИЕ --------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------- ФУНКЦИИ РАБОТЫ С БАЗОЙ -------------
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
    """
    Возвращает (max_ratio, id_похожей_записи, username_похожей_записи).
    Если записей нет, вернет (0.0, None, None).
    """
    submissions = fetch_all_submissions()
    best_ratio, best_id, best_user = 0.0, None, None
    for rec_id, rec_user_id, rec_username, rec_text, rec_ts in submissions:
        ratio = SequenceMatcher(None, new_text, rec_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = rec_id
            best_user = rec_username or str(rec_user_id)
    return best_ratio, best_id, best_user

# ------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -------------
def start(update: Update, context):
    update.message.reply_text(
        "Привет! Я бот для проверки оригинальности работ.\n"
        "Отправь текст работы — я проверю её на схожесть с ранее присланными."
    )

def help_cmd(update: Update, context):
    update.message.reply_text("/start — показать приветствие\n/help — показать справку")

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
            f"Найдена похожая работа пользователя @{matched_user}.\n"
            "Если ты не списывал(а), просто проигнорируй сообщение.\n"
            "Твоя работа сохранена."
        )
    else:
        reply = "✅ Похоже, что работа оригинальная. Сохраняю."

    update.message.reply_text(reply)
    save_submission(user.id, user.username or "", text)

# ------------- НАСТРОЙКА BOT и DISPATCHER -------------
app = Flask(__name__)
bot = Bot(token=TOKEN)
# Dispatcher с минимум 1 воркером (например, 4), чтобы не было warning
dispatcher = Dispatcher(bot, None, workers=4, use_context=True)

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, check_text))

# ------------- ROUTES ДЛЯ FLASK -------------
@app.route("/", methods=["GET"])
def root():
    return "Bot is running", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    dispatcher.process_update(update)
    return "OK", 200

# ------------- ЗАПУСК -------------
if __name__ == "__main__":
    init_db()
    if not TOKEN or not WEBHOOK_URL:
        logger.error("TELEGRAM_TOKEN или WEBHOOK_URL не заданы в окружении!")
        exit(1)
    # Устанавливаем webhook
    bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")
    # Запускаем Flask
    app.run(host="0.0.0.0", port=PORT)
