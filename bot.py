import logging
import sqlite3
from datetime import datetime
import os

from difflib import SequenceMatcher
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# --------------- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---------------
load_dotenv()  # ищет .env в текущей папке
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
DB_PATH = "submissions.db"
SIMILARITY_THRESHOLD = 0.7  # 70%

# ------------- ЛОГИРОВАНИЕ --------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ------------- ИНИЦИАЛИЗАЦИЯ БД -------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            text   TEXT NOT NULL,
            ts     TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ------------- ФУНКЦИИ РАБОТЫ С БД -------------
def save_submission(user_id: int, username: str, text: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO submissions (user_id, username, text, ts) VALUES (?, ?, ?, ?)",
        (user_id, username, text, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def fetch_all_submissions():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, username, text, ts FROM submissions")
    rows = cur.fetchall()
    conn.close()
    return rows  # список кортежей (id, user_id, username, text, ts)


# ------------- УТИЛИТА: СРАВНЕНИЕ ТЕКСТА -------------
def calculate_max_similarity(new_text: str):
    """
    Возвращает кортеж (max_ratio, id_похожей_записи, username_похожей_записи).
    Если записей нет, вернёт (0.0, None, None).
    """
    submissions = fetch_all_submissions()
    best_ratio = 0.0
    best_id = None
    best_username = None

    for rec in submissions:
        rec_id, rec_user_id, rec_username, rec_text, rec_ts = rec
        ratio = SequenceMatcher(None, new_text, rec_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = rec_id
            best_username = rec_username or str(rec_user_id)
    return best_ratio, best_id, best_username


# ------------- ОБРАБОТЧИКИ КОМАНД -------------
def start_handler(update: Update, context: CallbackContext):
    text = (
        "Привет! Я бот для проверки оригинальности работ.\n"
        "Отправь текст работы целиком (просто как сообщение), "
        "я проверю степень схожести с ранее присланными и сохраню твою работу."
    )
    update.message.reply_text(text)


def help_handler(update: Update, context: CallbackContext):
    text = (
        "/start — показать это сообщение\n"
        "Просто отправь текст работы как обычное сообщение."
    )
    update.message.reply_text(text)


# ------------- ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ -------------
def text_handler(update: Update, context: CallbackContext):
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    text = update.message.text.strip()

    if not text:
        update.message.reply_text("Пустого текста не принимаю.")
        return

    # Сначала считаем схожесть с предыдущими работами
    max_ratio, matched_id, matched_username = calculate_max_similarity(text)

    # Форматируем ответ в зависимости от результата
    if matched_id is not None and max_ratio >= SIMILARITY_THRESHOLD:
        perc = round(max_ratio * 100, 1)
        reply = (
            f"⚠ Похожесть: {perc}%\n"
            f"Ближайшее совпадение с работой пользователя @{matched_username}.\n"
            "Если ты не списывал(а), просто проигнорируй это сообщение.\n"
            "Твоя работа будет сохранена."
        )
    else:
        reply = "✅ Похоже, что работа оригинальна (нет явных совпадений). Сохраняю её."

    # Отправляем ответ и сохраняем новую работу
    update.message.reply_text(reply)
    save_submission(user_id, username, text)


# ------------- ГЛАВНАЯ ФУНКЦИЯ -------------
def main():
    # Инициализируем базу
    init_db()

    # Убедимся, что токен взялся
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не найден в окружении!")
        return

    # Создаём бота
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Регистрируем обработчики
    dp.add_handler(CommandHandler("start", start_handler))
    dp.add_handler(CommandHandler("help", help_handler))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_handler))

    # Запускаем
    updater.start_polling()
    logger.info("Бот запущен, ожидаю сообщений...")
    updater.idle()


if __name__ == "__main__":
    main()
