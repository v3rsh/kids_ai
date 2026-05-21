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

# ===== Безопасные рисунки =====
# Конвенция env-переменных проекта — UPPER_SNAKE_CASE без общего префикса
# (см. ADMIN_HUID, REDIS_URL и т.п.). Все имена ниже подчиняются ей.

# Списки HUID модераторов и членов жюри (через запятую), §5.2, §5.4, §27.2.
_moderator_huids_env = os.getenv("MODERATOR_HUIDS", "")
MODERATOR_HUIDS: list[str] = [
    h.strip() for h in _moderator_huids_env.split(",") if h.strip()
]
_jury_huids_env = os.getenv("JURY_HUIDS", "")
JURY_HUIDS: list[str] = [h.strip() for h in _jury_huids_env.split(",") if h.strip()]

# UUID группового чата «Безопасные рисунки — модерация» (§19).
MODERATION_CHAT_ID: str | None = os.getenv("MODERATION_CHAT_ID") or None

# Локальное хранилище файлов конкурса (§21, §33.1).
# В контейнере — именованный том attachments_volume (см. docker-compose.yml).
ATTACHMENTS_DIR = Path(
    os.getenv("ATTACHMENTS_DIR", str(DATA_DIR / "attachments"))
)
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

# Лимит размера одного файла (§11.4, §16) и пороги мониторинга диска (§28.1).
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
DISK_WARN_PCT = int(os.getenv("DISK_WARN_PCT", "80"))
DISK_BLOCK_PCT = int(os.getenv("DISK_BLOCK_PCT", "95"))
# Интервал фонового мониторинга диска (§28.1). 1800 c = 30 мин.
# Сам алёрт дедуплицируется внутри services.storage.check_and_alert_disk
# (раз в 24 часа на каждый порог), так что 30 мин — безопасный дефолт.
DISK_CHECK_INTERVAL_SEC = int(os.getenv("DISK_CHECK_INTERVAL_SEC", "1800"))

# Режим приёма заявок по умолчанию (§33.6): "files" | "links".
INTAKE_MODE_DEFAULT = os.getenv("INTAKE_MODE_DEFAULT", "files")

# Параметры жюри (§35).
# TOP_N — размер шорт-листа на пул (по умолчанию 10, §35.1).
# JURY_ROUNDS — максимальное число раундов до автоматического жребия (§35.2).
# JURY_ROUND_DEADLINE_HOURS — дедлайн одного раунда (§35.6, по умолчанию 48 ч).
# JURY_POOLS_CONFIG — JSON-конфиг распределения судей по пулам;
#   пустая строка = все судьи во всех 12 пулах (поведение по умолчанию).
TOP_N = int(os.getenv("TOP_N", "10"))
JURY_ROUNDS = int(os.getenv("JURY_ROUNDS", "3"))
JURY_ROUND_DEADLINE_HOURS = int(os.getenv("JURY_ROUND_DEADLINE_HOURS", "48"))
JURY_POOLS_CONFIG = os.getenv("JURY_POOLS_CONFIG", "")

# Год проведения конкурса — используется в формировании BR-ID (§20).
COMPETITION_YEAR = int(os.getenv("COMPETITION_YEAR", "2026"))

# Текст экрана «Контакты организаторов» (§7). Вынесен из хардкода
# `app/handlers/user.py` в env-переменную, чтобы заказчик мог поправить
# формулировку без диффа в коде. Многострочный текст в .env передаётся
# через `\n` (dotenv разворачивает их в реальные переводы строк).
# Пустая строка / отсутствие переменной → используется дефолтный текст
# Wave 2 (см. CONTACTS_TEXT_DEFAULT ниже).
CONTACTS_TEXT_DEFAULT = (
    "Контакты организаторов:\n\n"
    "• Организатор конкурса — команда ИБ.\n"
    "• Основной модератор — Екатерина Винокурова.\n"
    "• Резервный модератор / владелец проекта — Анастасия Иванова.\n\n"
    "По всем вопросам пишите в чат «Безопасные рисунки — модерация»."
)
CONTACTS_TEXT: str = os.getenv("CONTACTS_TEXT") or CONTACTS_TEXT_DEFAULT
