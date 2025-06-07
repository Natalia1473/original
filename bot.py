import os
import logging
import sqlite3
import tempfile
import zipfile
import xml.etree.ElementTree as ET
import uuid
from datetime import datetime
from difflib import SequenceMatcher

from copyleaks import Copyleaks
from copyleaks.models.scan_properties import ScanProperties, Webhooks
from copyleaks.models.source import SourceText
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
#  Локальная БД (SQLite)
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


def calculate_max_similarity_locally(new_text: str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT user_id, username, text FROM submissions"
    ).fetchall()
    conn.close()
    best_ratio, best_user = 0.0, None
    for user_id, username, old_text in rows:
        ratio = SequenceMatcher(None, new_text, old_text).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_user = username or str(user_id)
    return best_ratio, best_user

# --------------------------------------------
#  Извлечение текста из .docx
# --------------------------------------------
def extract_text_from_docx(path: str) -> str:
    with zipfile.ZipFile(path, 'r') as z:
        xml_content = z.read('word/document.xml')
    namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    root = ET.fromstring(xml_content)
    texts = [node.text for node in root.findall('.//w:t', namespace) if node.text]
    return "\n".join(texts)

# --------------------------------------------
#  Copyleaks SDK
# --------------------------------------------
# Инициализация SDK и логин
copyleaks = Copyleaks()
login = copyleaks.login(COPYLEAKS_EMAIL, COPYLEAKS_API_KEY)
access_token = login.access_token


def check_internet_plagiarism(text: str) -> float:
    """
    Проверяет текст через Copyleaks и возвращает % совпадений.
    """
    scan_id = str(uuid.uuid4())
    props = ScanProperties(
        sandbox=False,
        start_scan=True,
        webhooks=Webhooks(status=[])
    )
    source = SourceText(content=text, filename="submission.txt")
    copyleaks.create_scan_by_text(
        token=access_token,
        scan_id=scan_id,
        properties=props,
        source=source
    )
    # SDK ждёт завершения и получает результаты
    report = copyleaks.get_scan_results(token=access_token, scan_id=scan_id)
    return report.summary.percentage

# --------------------------------------------
#  Обработчики Telegram
# --------------------------------------------
def start(update: Update, context):
    update.message.reply_text(
        "Привет! Я проверяю оригинальность работ через Copyleaks. "
        "Отправь текст или .docx."
    )


def help_cmd(update: Update, context):
    update.message.reply_text(
        "/start — приветствие\n"
        "/help — справка"
    )


def check_text(update: Update, context):
    raw_text = update.message.text.strip()
    if not raw_text:
        update.message.reply_text("Пустого текста не принимаю.")
        return
    # Локальная проверка
    ratio, user = calculate_max_similarity_locally(raw_text)
    if ratio >= LOCAL_SIMILARITY_THRESHOLD:
        update.message.reply_text(f"⚠ Локальное совпадение: {ratio*100:.1f}% с @{user}")
    update.message.reply_text("Проверяю по интернету...")
    try:
        internet_score = check_internet_plagiarism(raw_text)
    except Exception as e:
        update.message.reply_text(f"Ошибка Copyleaks: {e}")
        return
    if internet_score >= INTERNET_SIMILARITY_THRESHOLD:
        update.message.reply_text(f"❌ Найдено {internet_score:.1f}% совпадений в интернете.")
    else:
        update.message.reply_text(f"✅ В интернете только {internet_score:.1f}% совпадений.")
    save_submission(update.effective_user.id, update.effective_user.username or "", raw_text, internet_score)


def handle_document(update: Update, context):
    doc = update.message.document
    if not doc.file_name.lower().endswith('.docx'):
        update.message.reply_text("Только .docx")
        return
    new_file = context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix='.docx', delete=False) as tf:
        path = tf.name
        new_file.download(custom_path=path)
    raw_text = extract_text_from_docx(path)
    os.remove(path)
    # Локальная проверка
    ratio, user = calculate_max_similarity_locally(raw_text)
    if ratio >= LOCAL_SIMILARITY_THRESHOLD:
        update.message.reply_text(f"⚠ Локальное совпадение: {ratio*100:.1f}% с @{user}")
    update.message.reply_text("Проверяю по интернету...")
    try:
        internet_score = check_internet_plagiarism(raw_text)
    except Exception as e:
        update.message.reply_text(f"Ошибка Copyleaks: {e}")
        return
    if internet_score >= INTERNET_SIMILARITY_THRESHOLD:
        update.message.reply_text(f"❌ Найдено {internet_score:.1f}% совпадений в интернете.")
    else:
        update.message.reply_text(f"✅ В интернете только {internet_score:.1f}% совпадений.")
    save_submission(update.effective_user.id, update.effective_user.username or "", raw_text, internet_score)

# --------------------------------------------
#  Flask + Webhook
# --------------------------------------------
app = Flask(__name__)
bot = Bot(token=TOKEN)
dispatcher = Dispatcher(bot, None, workers=4, use_context=True)

dispatcher.add_handler(CommandHandler('start', start))
dispatcher.add_handler(CommandHandler('help', help_cmd))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, check_text))
dispatcher.add_handler(MessageHandler(Filters.document, handle_document))

@app.route('/', methods=['GET'])
def root():
    return 'Bot is running', 200

@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    dispatcher.process_update(update)
    return 'OK', 200

if __name__ == '__main__':
    init_db()
    if not all([TOKEN, WEBHOOK_URL, COPYLEAKS_EMAIL, COPYLEAKS_API_KEY]):
        logger.error('Не заданы все переменные окружения')
        exit(1)
    bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")
    app.run(host='0.0.0.0', port=PORT)
