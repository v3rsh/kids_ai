"""
Конфигурация приложения kids_ai.

Загружает переменные окружения и настраивает логирование.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ===== Базовые пути =====
BASE_DIR = Path(__file__).resolve().parent
LOGS_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"

LOGS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# ===== Настройки логирования =====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
JSON_LOGS = os.getenv("JSON_LOGS", "False").lower() == "true"

# Инициализация loguru
from logging_config import setup_logging, logger
setup_logging(log_level=LOG_LEVEL, json_logs=JSON_LOGS, logs_dir=LOGS_DIR)

# ===== Настройки бота (pybotx) =====
BOT_ID = os.getenv("BOT_ID")
CTS_URL = os.getenv("CTS_URL")
BOT_SECRET_KEY = os.getenv("BOT_SECRET_KEY")

# ===== Настройки администраторов =====
# Поддерживается несколько HUID через запятую; первый — главный (получает уведомления)
_admin_huid_env = os.getenv("ADMIN_HUID", "")
ADMIN_HUIDS: list[str] = [h.strip() for h in _admin_huid_env.split(",") if h.strip()]
ADMIN_HUID: str | None = ADMIN_HUIDS[0] if ADMIN_HUIDS else None

# ===== Настройки базы данных PostgreSQL =====
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "kids_ai")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

DATABASE_URL = f"postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# ===== FSM Storage =====
# SQLite (fallback, если REDIS_URL не задан)
STATES_DB_PATH = os.path.join(DATA_DIR, "states.sqlite3")
# Redis (приоритетное хранилище FSM)
REDIS_URL = os.getenv("REDIS_URL")  # например redis://172.20.0.4:6379/0
FSM_TTL_DAYS = int(os.getenv("FSM_TTL_DAYS", "30"))

# ===== Настройки веб-сервера =====
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# ===== Отладочный режим =====
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# ===== Scheduler =====
# Если True — scheduler запускается в этом процессе.
# При появлении app/scheduler.py подключи его в main.py lifespan.
# Для multi-worker деплоя scheduler выносится в отдельный контейнер (ENABLE_SCHEDULER=false у web).
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "false").lower() == "true"
UVICORN_WORKERS = max(1, int(os.getenv("UVICORN_WORKERS", "1")))
