import logging
import sqlite3
import os
import tempfile
from datetime import datetime
from difflib import SequenceMatcher

from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters
from flask import Flask, request
from docx import Document  # pip install python-docx

# --------------- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---------------
load_dotenv()  # ищет .env в корне проекта
TOKEN = os.getenv("TELEGRAM_TOKEN")         # токен бота
WEBHOOK_URL = os.getenv("WEBHOOK_URL")      # без слэша в конце, например https://mybot.onrender.com
PORT = int(os.getenv("PORT", "8443"))       # порт для Flask
DB_PATH = "submissions.db"                  # SQLite-файл
SIMILARITY_THRESHOLD = 0.7                  # порог похожести (0.7 = 70%)

# ------------- ЛОГИРОВАНИЕ --------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------- ФУНКЦИИ РАБОТЫ С БАЗОЙ -------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            ts TEXT NOT NULL
        )
        """
    )
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
    Если записей нет, вернёт (0.0, None, None).
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

# ------------- ФУНКЦИЯ ДЛЯ ЧТЕНИЯ .docx -------------
def extract_text_from_docx(path: str) -> str:
    doc = Document(path)
    paragraphs = [p.text for p in doc.paragraphs if p.text]
    return "\n".join(paragraphs)

# ------------- ОБРАБОТЧИКИ СООБЩЕНИЙ -------------
def start(update: Update, context):
    update.message.reply_text(
        "Привет! Я бот для проверки оригинальности работ.\n"
        "Можно отправить текст как сообщение или загрузить .docx-файл."
    )

def help_cmd(update: Update, context):
    update.message.reply_text(
        "/start — приветствие\n"
        "/help — справка\n\n"
        "Отправь текст или загрузите .docx."
    )

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
            "Если ты не списывал(а), просто проигнорируй.\n"
            "Работа сохранена."
        )
    else:
        reply = "✅ Похоже, что работа оригинальная. Сохраняю."

    update.message.reply_text(reply)
    save_submission(user.id, user.username or "", text)

def handle_document(update: Update, context):
    user = update.effective_user
    doc = update.message.document

    if not doc.file_name.lower().endswith(".docx"):
        update.message.reply_text("Поддерживаются только файлы .docx")
        return

    # Скачиваем в временный файл
    file_id = doc.file_id
    new_file = context.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
        temp_path = tf.name
        new_file.download(custom_path=temp_path)

    try:
        text = extract_text_from_docx(temp_path)
    except Exception as e:
        logger.error(f"Ошибка чтения DOCX: {e}")
        update.message.reply_text("Не смог прочитать .docx-файл.")
        os.remove(temp_path)
        return

    os.remove(temp_path)

    if not text.strip():
        update.message.reply_text("Файл пустой или не содержит текста.")
        return

    ratio, matched_id, matched_user = calculate_max_similarity(text)
    if matched_id and ratio >= SIMILARITY_THRESHOLD:
        perc = round(ratio * 100, 1)
        reply = (
            f"⚠ Похожесть: {perc}%\n"
            f"Найдена похожая работа пользователя @{matched_user}.\n"
            "Если ты не списывал(а), просто проигнорируй.\n"
            "Работа сохранена."
        )
    else:
        reply = "✅ Похоже, что работа оригинальная. Сохраняю."

    update.message.reply_text(reply)
    save_submission(user.id, user.username or "", text)

# ------------- НАСТРОЙКА BOT и DISPATCHER -------------
app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, workers=4, use_context=True)

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, check_text))
dispatcher.add_handler(MessageHandler(Filters.document, handle_document))

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
    bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")
    app.run(host="0.0.0.0", port=PORT)
