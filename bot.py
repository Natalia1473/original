import os
import logging
import sqlite3
import tempfile
import zipfile
import xml.etree.ElementTree as ET
import base64
import uuid
from datetime import datetime
from difflib import SequenceMatcher

import requests
from dotenv import load_dotenv
from flask import Flask, request
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters

# --------------------------------------------
#  Загрузка переменных окружения
# --------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8443"))

COPYLEAKS_EMAIL = os.getenv("COPYLEAKS_EMAIL")
COPYLEAKS_API_KEY = os.getenv("COPYLEAKS_API_KEY")

DB_PATH = "submissions.db"
LOCAL_SIMILARITY_THRESHOLD = 0.7
INTERNET_SIMILARITY_THRESHOLD = 20.0  # % совпадений

# --------------------------------------------
#  Логирование
# --------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --------------------------------------------
#  Работа с локальной БД
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
            internet_score REAL
        )
    """
    )
    conn.commit()
    conn.close()

def save_submission(user_id: int, username: str, text: str, internet_score: float):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO submissions (user_id, username, text, ts, internet_score) VALUES (?, ?, ?, ?, ?)",
        (user_id, username, text, datetime.utcnow().isoformat(), internet_score)
    )
    conn.commit()
    conn.close()

def fetch_all_submissions():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, username, text, ts, internet_score FROM submissions"
    ).fetchall()
    conn.close()
    return rows

def calculate_max_similarity_locally(new_text: str):
    submissions = fetch_all_submissions()
    best_ratio, best_id, best_user = 0.0, None, None
    for rec_id, rec_user_id, rec_username, rec_text, rec_ts, rec_score in submissions:
        ratio = SequenceMatcher(None, new_text, rec_text).ratio()
        if ratio > best_ratio:
            best_ratio, best_id, best_user = ratio, rec_id, rec_username or str(rec_user_id)
    return best_ratio, best_id, best_user

# --------------------------------------------
#  Извлечение текста из .docx
# --------------------------------------------
def extract_text_from_docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path, 'r') as z:
            xml_content = z.read('word/document.xml')
    except Exception as e:
        raise RuntimeError(f"Ошибка при открытии DOCX как ZIP: {e}")

    try:
        namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        root = ET.fromstring(xml_content)
        texts = [node.text for node in root.findall('.//w:t', namespace) if node.text]
        return "\n".join(texts)
    except Exception as e:
        raise RuntimeError(f"Ошибка при разборе XML document.xml: {e}")

# --------------------------------------------
#  Copyleaks API
# --------------------------------------------
def get_copyleaks_token() -> str:
    url = "https://id.copyleaks.com/v3/account/login/api"
    data = {"email": COPYLEAKS_EMAIL, "key": COPYLEAKS_API_KEY}
    headers = {"Content-Type": "application/json"}
    resp = requests.post(url, json=data, headers=headers)
    if resp.status_code != 200:
        logger.error(f"Copyleaks login failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Copyleaks login: {resp.status_code} {resp.text}")
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError("В ответе Copyleaks не найден access_token")
    return token

def submit_to_copyleaks(access_token: str, content: str) -> str:
    scan_id = str(uuid.uuid4())
    b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    url = f"https://api.copyleaks.com/v3/scans/submit/file/{scan_id}"
    payload = {
        "base64": b64,
        "filename": "submission.txt",
        "properties": {"sandbox": False, "startScan": True, "webhooks": []}
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"}
    resp = requests.put(url, json=payload, headers=headers)
    if resp.status_code not in (200, 201):
        logger.error(f"Copyleaks submit failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Copyleaks submit: {resp.status_code} {resp.text}")
    return scan_id

def poll_copyleaks_result(access_token: str, scan_id: str) -> dict:
    """
    Опрашивает статус сканирования до завершения.
    """
    url = f"https://api.copyleaks.com/v3/scans/{scan_id}/status"
    headers = {"Authorization": f"Bearer {access_token}"}
    while True:
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            logger.error(f"Copyleaks status failed: {resp.status_code} {resp.text}")
            raise RuntimeError(f"Copyleaks status: {resp.status_code} {resp.text}")
        data = resp.json()
        status = data.get("status")
        if status == "completed":
            return data
        if status == "failed":
            raise RuntimeError("Copyleaks scan failed (status=failed)")
        import time; time.sleep(2)

def get_copyleaks_report(access_token: str, scan_id: str) -> dict:
    url = f"https://api.copyleaks.com/v3/scans/{scan_id}/results"
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        logger.error(f"Copyleaks report failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Copyleaks report: {resp.status_code} {resp.text}")
    return resp.json()

def check_internet_plagiarism(text: str) -> float:
    access_token = get_copyleaks_token()
    scan_id = submit_to_copyleaks(access_token, text)
    poll_copyleaks_result(access_token, scan_id)
    report = get_copyleaks_report(access_token, scan_id)
    return report.get("summary", {}).get("percentage", 0.0)

# --------------------------------------------
#  Обработчики Telegram
# --------------------------------------------
def start(update: Update, context):
    update.message.reply_text(
        "Привет! Я бот для проверки оригинальности работ.\n"
        "Отправь текст или загрузи .docx — проверю по интернету через Copyleaks."
    )

def help_cmd(update: Update, context):
    update.message.reply_text(
        "/start — приветствие\n"
        "/help — справка"
    )

def check_text(update: Update, context):
    user = update.effective_user
    raw_text = update.message.text.strip()
    if not raw_text:
        update.message.reply_text("Пустого текста не принимаю.")
        return
    local_ratio, local_id, local_user = calculate_max_similarity_locally(raw_text)
    if local_id and local_ratio >= LOCAL_SIMILARITY_THRESHOLD:
        update.message.reply_text(
            f"⚠ Локальное совпадение: {round(local_ratio*100,1)}% с @{local_user}.\nПроверяю по интернету..."
        )
    else:
        update.message.reply_text("Локальных совпадений нет. Проверяю по интернету...")
    try:
        internet_score = check_internet_plagiarism(raw_text)
    except Exception as e:
        err = str(e)
        logger.error(f"Copyleaks error in check_text: {err}")
        update.message.reply_text(f"❌ Не удалось проверить через Copyleaks:\n{err}")
        return
    if internet_score >= INTERNET_SIMILARITY_THRESHOLD:
        update.message.reply_text(f"❌ В интернете найдено {internet_score:.1f}% совпадений.")
    else:
        update.message.reply_text(f"✅ В интернете только {internet_score:.1f}% совпадений. Оригинально.")
    save_submission(user.id, user.username or "", raw_text, internet_score)

def handle_document(update: Update, context):
    user = update.effective_user
    doc = update.message.document
    if not doc.file_name.lower().endswith(".docx"):
        update.message.reply_text("Поддерживаются только файлы .docx")
        return
    new_file = context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
        temp_path = tf.name
        new_file.download(custom_path=temp_path)
    try:
        raw_text = extract_text_from_docx(temp_path)
    except Exception as e:
        err = str(e)
        logger.error(f"DOCX read error: {err}")
        update.message.reply_text(f"❌ Не удалось извлечь текст из .docx:\n{err}")
        os.remove(temp_path)
        return
    os.remove(temp_path)
    if not raw_text.strip():
        update.message.reply_text("Файл пустой или не содержит текста.")
        return
    local_ratio, local_id, local_user = calculate_max_similarity_locally(raw_text)
    if local_id and local_ratio >= LOCAL_SIMILARITY_THRESHOLD:
        update.message.reply_text(
            f"⚠ Локальное совпадение: {round(local_ratio*100,1)}% с @{local_user}.\nПроверяю по интернету..."
        )
    else:
        update.message.reply_text("Локальных совпадений нет. Проверяю по интернету...")
    try:
        internet_score = check_internet_plagiarism(raw_text)
    except Exception as e:
        err = str(e)
        logger.error(f"Copyleaks error in handle_document: {err}")
        update.message.reply_text(f"❌ Не удалось проверить через Copyleaks:\n{err}")
        return
    if internet_score >= INTERNET_SIMILARITY_THRESHOLD:
        update.message.reply_text(f"❌ В интернете найдено {internet_score:.1f}% совпадений.")
    else:
        update.message.reply_text(f"✅ В интернете только {internet_score:.1f}% совпадений. Оригинально.")
    save_submission(user.id, user.username or "", raw_text, internet_score)

# --------------------------------------------
#  Настройка Flask и запуск
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

if __name__ == "__main__":
    init_db()
    if not TOKEN or not WEBHOOK_URL or not COPYLEAKS_EMAIL or not COPYLEAKS_API_KEY:
        logger.error("TELEGRAM_TOKEN, WEBHOOK_URL, COPYLEAKS_EMAIL или COPYLEAKS_API_KEY не заданы!")
        exit(1)
    bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")
    app.run(host="0.0.0.0", port=PORT)
