import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Режим работы: local (файл + мок LLM) или prod (Redis + OpenAI)
APP_ENV = os.getenv("APP_ENV", "local").lower()

# --- OpenAI ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GPT_MODEL = os.getenv("GPT_MODEL", "gpt-4o-mini")
PROXY_SERVICE_URL = os.getenv("PROXY_SERVICE_URL") or None

# --- Bitrix24 ---
BITRIX_WEBHOOK = os.getenv("BITRIX_WEBHOOK")
BOT_ID = os.getenv("BOT_ID")
CLIENT_ID = os.getenv("CLIENT_ID")
OPERATOR_ID = os.getenv("OPERATOR_ID") or None

# --- Промпт ---
PROMPT_FILE = os.getenv("PROMPT_FILE", "src/prompt.txt")

# --- Хранилище ---
STORAGE_FILE = os.getenv("STORAGE_FILE", str(BASE_DIR / "data" / "storage.json"))
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

# Ключи хранилища 
REDIS_PREFIX = "bot:bsk_ai_2"
REDIS_LIMIT_KEY = f"{REDIS_PREFIX}:limit"
REDIS_COUNT_KEY = f"{REDIS_PREFIX}:count"
REDIS_DIALOGS_KEY = f"{REDIS_PREFIX}:dialogs"
REDIS_HISTORY_PREFIX = f"{REDIS_PREFIX}:history"
REDIS_PENDING_PREFIX = f"{REDIS_PREFIX}:pending"

# --- Логирование ---
LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "logs" / "bot.log"))

# --- Поведение диалога ---
MAX_HISTORY_PAIRS = 60
WORKER_TIMEOUT = 300
MAX_CONSECUTIVE_ERRORS = 5
OPENAI_TIMEOUT = int(os.getenv("OPENAI_TIMEOUT", "60"))
OPENAI_RETRIES = int(os.getenv("OPENAI_RETRIES", "3"))

# --- Сбор пачки сообщений ---
MESSAGE_COLLECTION_WINDOW = float(os.getenv("MESSAGE_COLLECTION_WINDOW", "20.0"))
MAX_COLLECTION_WINDOW = float(os.getenv("MAX_COLLECTION_WINDOW", "60.0"))
MESSAGE_CHECK_INTERVAL = float(os.getenv("MESSAGE_CHECK_INTERVAL", "0.5"))

# --- Поведение ответов ---
COMBINE_MULTIPLE_MESSAGES = os.getenv("COMBINE_MULTIPLE_MESSAGES", "true").lower() == "true"
HUMANIZE_MODE = os.getenv("HUMANIZE_MODE", "true").lower() == "true"

# --- Перевод на оператора ---
PHONE_TRANSFER_DELAY = int(os.getenv("PHONE_TRANSFER_DELAY", "1800"))


def _resolve(path: str) -> str:
    """Приводит относительный путь к абсолютному относительно корня проекта."""
    if os.path.isabs(path):
        return path
    return str(BASE_DIR / path)


LOG_FILE = _resolve(LOG_FILE)
STORAGE_FILE = _resolve(STORAGE_FILE)


def _load_prompt(path: str) -> str:
    candidates = [_resolve(path), str(BASE_DIR / "src" / path)]
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            continue
    return "Вы — помощник в чате. Отвечайте вежливо и по существу."


PROMPT = _load_prompt(PROMPT_FILE)


def is_local() -> bool:
    return APP_ENV == "local"