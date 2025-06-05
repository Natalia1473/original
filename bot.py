import os
import logging
import sqlite3
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from difflib import SequenceMatcher

import requests
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters

# --------------------------------------------
#  1. Загрузка переменных окружения из .env
# --------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # например: https://mybot.onrender.com
PORT = int(os.getenv("PORT", "8443"))

# Данные для Copyleaks (антиплагиат)
COPYLEAKS_EMAIL = os.getenv("COPYLEAKS_EMAIL")
COPYLEAKS_API_KEY = os.getenv("COPYLEAKS_API_KEY")

# Путь к локальной SQLite-базе (по-прежнему используем, 
# чтобы хранить историю проверок и сохранять тексты)
DB_PATH = "submissions.db"
# Порог “локального” сравнения (для тех случаев, если хотим сначала сверяться
# со своей БД, но ниже мы ориентируемся на Copyleaks).
LOCAL_SIMILARITY_THRESHOLD = 0.7

# Если нужно, можно ввести порог “интересного” процента из Copyleaks,
# но Copyleaks возвращает уже свои проценты совпадения.
# Здесь просто храним константу, но конкретно с ней будем работать в логике.
INTERNET_SIMILARITY_THRESHOLD = 20.0  # в процентах (например, 20% совпадений)

# --------------------------------------------
#  Логирование
# --------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --------------------------------------------
#  Функции для работы с локальной БД (SQLite)
# --------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            text TEXT NOT NULL,
            ts TEXT NOT NULL,
            internet_score REAL  -- процент совпадения из Copyleaks
        )
    """)
    conn.commit()
    conn.close()

def save_submission(user_id: int, username: str, text: str, internet_score: float):
    """
    Сохраняем каждую проверенную работу, включая процент совпадения по интернету.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO submissions (user_id, username, text, ts, internet_score) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, text, datetime.utcnow().isoformat(), internet_score)
    )
    conn.commit()
    conn.close()

def fetch_all_submissions():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT id, user_id, username, text, ts, internet_score FROM submissions").fetchall()
    conn.close()
    return rows

def calculate_max_similarity_locally(new_text: str):
    """
    (ОСТАВЛЕНА для примера) Возвращает max_ratio, id_записи, username из локальной БД.
    """
    submissions = fetch_all_submissions()
    best_ratio, best_id, best_user = 0.0, None, None
    for rec_id, rec_user_id, rec_username, rec_text, rec_ts, rec_score in submissions:
        ratio = SequenceMatcher(None, new_text, rec_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = rec_id
            best_user = rec_username or str(rec_user_id)
    return best_ratio, best_id, best_user

# --------------------------------------------
#  Функции для извлечения текста из .docx
# --------------------------------------------
def extract_text_from_docx(path: str) -> str:
    """
    Открываем .docx как ZIP, читаем word/document.xml и вытаскиваем все узлы <w:t>.
    Это быстрее и надёжнее, чем python-docx, если в документе могут быть «битые» изображения.
    """
    try:
        with zipfile.ZipFile(path, 'r') as z:
            xml_content = z.read('word/document.xml')
    except Exception as e:
        raise RuntimeError(f"Не удалось открыть DOCX: {e}")

    try:
        namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        root = ET.fromstring(xml_content)
        texts = []
        for node in root.findall('.//w:t', namespace):
            if node.text:
                texts.append(node.text)
        return "\n".join(texts)
    except Exception as e:
        raise RuntimeError(f"Ошибка разбора XML document.xml: {e}")

# --------------------------------------------
#  Интеграция с Copyleaks API
#  (https://api.copyleaks.com/documentation/apis-rest)
# --------------------------------------------
def get_copyleaks_token() -> str:
    """
    Шаг 1: Получаем OAuth-токен от Copyleaks.
    Возвращает строку access_token.
    """
    url = "https://id.copyleaks.com/v3/account/login/api"
    data = {
        "email": COPYLEAKS_EMAIL,
        "apiKey": COPYLEAKS_API_KEY
    }
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Не удалось получить Copyleaks токен: {resp.status_code} {resp.text}")
    token = resp.json().get("access_token")
    return token

def submit_to_copyleaks(access_token: str, content: str) -> dict:
    """
    Шаг 2: Отправляем контент на проверку. Copyleaks возвращает info о JobId и другие поля.
    Важно: Copyleaks ожидает, что мы укажем:
      - language: язык текста (например, "ru" или "en"),
      - webhooks: куда Copyleaks пришлёт результат (мы можем не указывать, тогда будем опрашивать статус).
    Пример запроса «On-the-fly» (без webhooks) – мы будем затем сами опрашивать статус.
    """
    url = "https://api.copyleaks.com/v3/scans/submit/text"
    # Загружаем текст напрямую
    payload = {
        "base64": False,
        "properties": {
            "webhooks": [],
            "sandbox": False,
            "startScan": True,
            "webhooks": []
        },
        "content": content,
        "metadata": {
            "filename": "telegram_submission.txt",
            "title": "Telegram Bot Submission"
        },
        "options": {
            "language": "ru"  # или "en", в зависимости от языка работы
        }
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    resp = requests.post(url, json=payload, headers=headers)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Не удалось отправить текст в Copyleaks: {resp.status_code} {resp.text}")
    return resp.json()

def poll_copyleaks_result(access_token: str, scan_id: str) -> dict:
    """
    Шаг 3: Опрашиваем статус сканирования. Как только статус == "completed", возвращаем результат.
    Метод: GET https://api.copyleaks.com/v3/scans/{scan_id}/status
    """
    url = f"https://api.copyleaks.com/v3/scans/{scan_id}/status"
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"Ошибка при опросе статуса Copyleaks: {resp.status_code} {resp.text}")
        data = resp.json()
        status = data.get("status")
        # Возможные статусы: "in-progress", "queued", "completed", "failed"
        if status == "completed":
            return data
        if status == "failed":
            raise RuntimeError("Copyleaks scan failed")
        # Если всё ещё in-progress, подождём пару секунд и попробуем ещё раз
        import time
        time.sleep(2)

def get_copyleaks_report(access_token: str, scan_id: str) -> dict:
    """
    Шаг 4: Получаем итоговый отчёт (с процентами совпадений и списком источников).
    Метод: GET https://api.copyleaks.com/v3/scans/{scan_id}/results
    """
    url = f"https://api.copyleaks.com/v3/scans/{scan_id}/results"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"Не удалось получить отчет Copyleaks: {resp.status_code} {resp.text}")
    return resp.json()

def check_internet_plagiarism(text: str) -> float:
    """
    Функция-обёртка: возвращает процент совпадений по Copyleaks (0.0–100.0).
    1) Получаем OAuth-токен
    2) Отправляем текст
    3) Ожидаем окончания сканирования (poll)
    4) Получаем отчет и извлекаем общий процент совпадений
    """
    # 1) Открываем сессию
    access_token = get_copyleaks_token()

    # 2) Отправляем текст на проверку
    submission = submit_to_copyleaks(access_token, text)
    scan_id = submission.get("scan"); 
    if not scan_id:
        raise RuntimeError("Не получили scan_id из Copyleaks в ответе")

    # 3) Ждём завершения
    status_data = poll_copyleaks_result(access_token, scan_id)
    # (В status_data могут быть детали, но общий прогресс проверен)

    # 4) Забираем отчёт
    report = get_copyleaks_report(access_token, scan_id)
    # В отчёте есть поле "results" → список совпадений.
    # Самое простое: посмотреть поле "percentage" в summary (если есть).
    summary = report.get("summary", {})
    overall_score = summary.get("percentage", 0.0)
    return overall_score

# --------------------------------------------
#  Обработчики Telegram-сообщений
# --------------------------------------------
def start(update: Update, context):
    update.message.reply_text(
        "Привет! Я бот для проверки оригинальности работ.\n"
        "Отправь текст или загрузи .docx-файл — я проверю его по интернету."
    )

def help_cmd(update: Update, context):
    update.message.reply_text(
        "/start — приветствие\n"
        "/help — эта справка\n\n"
        "Можно отправить чистый текст или .docx."
    )

def check_text(update: Update, context):
    user = update.effective_user
    raw_text = update.message.text.strip()
    if not raw_text:
        update.message.reply_text("Пустого текста не принимаю.")
        return

    # 1) Сначала проверяем “быстро” по локальной БД (необязательно, но покажу, как можно)
    local_ratio, local_id, local_user = calculate_max_similarity_locally(raw_text)
    if local_id and local_ratio >= LOCAL_SIMILARITY_THRESHOLD:
        perc_local = round(local_ratio * 100, 1)
        update.message.reply_text(
            f"⚠ Найдена похожая работа из локальной базы: {perc_local}% совпадения с @{local_user}.\n"
            "Чтобы убедиться, что текст уникален в интернете, сейчас проверю Copyleaks..."
        )
    else:
        update.message.reply_text("Локальных совпадений не найдено. Проверяю по интернету через Copyleaks...")

    # 2) Проверяем через Copyleaks по интернету
    try:
        internet_score = check_internet_plagiarism(raw_text)
    except Exception as e:
        logger.error(f"Ошибка при проверке в интернете: {e}")
        update.message.reply_text("Произошла ошибка при проверке в Copyleaks.")
        return

    # 3) Формируем ответ в зависимости от процента
    if internet_score >= INTERNET_SIMILARITY_THRESHOLD:
        update.message.reply_text(
            f"❌ По интернет-базе найдено {internet_score:.1f}% совпадений. "
            "Возможно, текст не оригинальный."
        )
    else:
        update.message.reply_text(
            f"✅ В интернете совпадений лишь {internet_score:.1f}%. Работа, скорее всего, оригинальная."
        )

    # 4) Сохраняем в локальную БД вместе с процентом из Copyleaks
    save_submission(user.id, user.username or "", raw_text, internet_score)

def handle_document(update: Update, context):
    user = update.effective_user
    doc = update.message.document

    if not doc.file_name.lower().endswith(".docx"):
        update.message.reply_text("Поддерживаются только файлы .docx")
        return

    # 1) Скачиваем .docx во временный файл
    file_id = doc.file_id
    new_file = context.bot.get_file(file_id)
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
        temp_path = tf.name
        new_file.download(custom_path=temp_path)

    # 2) Извлекаем текст, игнорируя медиа
    try:
        raw_text = extract_text_from_docx(temp_path)
    except Exception as e:
        logger.error(f"Ошибка чтения DOCX: {e}")
        update.message.reply_text("Не удалось извлечь текст из .docx.")
        os.remove(temp_path)
        return

    os.remove(temp_path)

    if not raw_text.strip():
        update.message.reply_text("Файл пустой или не содержит текста.")
        return

    # 3) Проверяем “быстро” по локальной БД
    local_ratio, local_id, local_user = calculate_max_similarity_locally(raw_text)
    if local_id and local_ratio >= LOCAL_SIMILARITY_THRESHOLD:
        perc_local = round(local_ratio * 100, 1)
        update.message.reply_text(
            f"⚠ Локальное совпадение: {perc_local}% с @{local_user}.\n"
            "Сейчас проверяю по интернету..."
        )
    else:
        update.message.reply_text("Локальных совпадений не найдено. Проверяю по интернету через Copyleaks...")

    # 4) Проверяем через Copyleaks
    try:
        internet_score = check_internet_plagiarism(raw_text)
    except Exception as e:
        logger.error(f"Ошибка при проверке в Copyleaks: {e}")
        update.message.reply_text("Произошла ошибка при проверке в Copyleaks.")
        return

    # 5) Формируем ответ по интернет-результату
    if internet_score >= INTERNET_SIMILARITY_THRESHOLD:
        update.message.reply_text(
            f"❌ По интернет-базе найдено {internet_score:.1f}% совпадений. "
            "Возможно, текст не оригинальный."
        )
    else:
        update.message.reply_text(
            f"✅ В интернете совпадений лишь {internet_score:.1f}%. Скорее всего, оригинал."
        )

    # 6) Сохраняем в базу
    save_submission(user.id, user.username or "", raw_text, internet_score)

# --------------------------------------------
#  Настройка Flask + Telegram Webhook
# --------------------------------------------
app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, workers=4, use_context=True)

dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("help", help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, check_text))
dispatcher.add_handler(MessageHandler(Filters.document, handle_document))

@app.route("/", methods=["GET"])
def root():
    return "Bot is running", 200

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    dispatcher.process_update(update)
    return "OK", 200

# --------------------------------------------
#  Запуск приложения
# --------------------------------------------
if __name__ == "__main__":
    init_db()
    if not TOKEN or not WEBHOOK_URL or not COPYLEAKS_EMAIL or not COPYLEAKS_API_KEY:
        logger.error("OTPIONS: TELEGRAM_TOKEN, WEBHOOK_URL, COPYLEAKS_EMAIL или COPYLEAKS_API_KEY не заданы!")
        exit(1)
    # Устанавливаем webhook
    bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")
    # Запускаем Flask
    app.run(host="0.0.0.0", port=PORT)
