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
#  Загрузка переменных окружения из .env
# --------------------------------------------
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "8443"))

COPYLEAKS_EMAIL = os.getenv("COPYLEAKS_EMAIL")
COPYLEAKS_API_KEY = os.getenv("COPYLEAKS_API_KEY")

DB_PATH = "submissions.db"
LOCAL_SIMILARITY_THRESHOLD = 0.7
INTERNET_SIMILARITY_THRESHOLD = 20.0  # процент совпадений

# --------------------------------------------
#  Логирование
# --------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --------------------------------------------
#  Функции работы с локальной SQLite-БД
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
    rows = conn.execute("SELECT id, user_id, username, text, ts, internet_score FROM submissions").fetchall()
    conn.close()
    return rows

def calculate_max_similarity_locally(new_text: str):
    submissions = fetch_all_submissions()
    best_ratio, best_id, best_user = 0.0, None, None
    for rec in submissions:
        rec_id, rec_user_id, rec_username, rec_text, rec_ts, rec_score = rec
        ratio = SequenceMatcher(None, new_text, rec_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_id = rec_id
            best_user = rec_username or str(rec_user_id)
    return best_ratio, best_id, best_user

# --------------------------------------------
#  Функция для извлечения текста из .docx
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
#  Интеграция с Copyleaks API
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
    """
    Отправляем свободный текст на проверку через endpoint Submit by file.
    Возвращаем scan_id.
    """
    # Генерируем уникальный scan_id
    scan_id = str(uuid.uuid4())

    # Кодируем текст в base64
    b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')

    # Формируем URL для отправки файла
    url = f"https://api.copyleaks.com/v3/scans/submit/file/{scan_id}"
    # Формируем payload с обязательными полями
    payload = {
        "base64": b64,
        "filename": "submission.txt",
        "properties": {
            "sandbox": False,
            "startScan": True,
            "webhooks": []
        }
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    # Отправляем PUT-запрос
    resp = requests.put(url, json=payload, headers=headers)
    if resp.status_code not in (200, 201):
        logger.error(f"Copyleaks submit failed: {resp.status_code} {resp.text}")
        raise RuntimeError(f"Copyleaks submit: {resp.status_code} {resp.text}")
    return scan_id

# --------------------------------------------
def poll_copyleaks_result(access_token: str, scan_id: str) -> dict:
