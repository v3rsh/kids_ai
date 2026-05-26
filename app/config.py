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
# Единственное хранилище FSM — Redis (контейнер из docker-compose.yml,
# AOF на named volume `redisdata`). Переменная обязательна.
REDIS_URL = os.getenv("REDIS_URL")  # например redis://172.20.0.4:6379/0
if not REDIS_URL:
    raise RuntimeError(
        "REDIS_URL не задан. FSM работает только с Redis "
        "(см. docker-compose.yml: контейнер redis на 172.20.0.4). "
        "Заполни REDIS_URL в .env."
    )
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
#
# Роли модераторов и членов жюри, а также UUID чата модерации
# в env больше НЕ задаются. Управление полностью через discovery:
# - не-модератор дёргает /moderator → админу приходит карточка с
#   кнопками «Назначить модератором / Отклонить»;
# - не-жюри дёргает /jury → аналогично, карточка с «Назначить жюри»;
# - бот добавлен в групповой чат → админу приходит карточка
#   «Сделать чатом модерации?»;
# - диагностика: /admin_roles, /admin_state.
# Источник истины — таблица ``app_settings`` (chat) и
# ``moderators`` / ``jury_members`` (роли).

# Шаблон URL-deeplink на DM с ботом для кнопок в чате модерации.
# Точный синтаксис зависит от версии eXpress CTS — уточняйте у админов.
# Плейсхолдеры: {bot_id}, {cts_url}; для новой заявки также {br_id},
# {command} (/find BR-…), {command_encoded} (URL-encoded).
# Если переменная пуста — кнопка-ссылка не добавляется.
EXPRESS_DEEPLINK_TEMPLATE: str = os.getenv("EXPRESS_DEEPLINK_TEMPLATE", "")

# Локальное хранилище файлов конкурса.
# В контейнере — bind-mount ./data/attachments хоста (см. docker-compose.yml).
ATTACHMENTS_DIR = Path(
    os.getenv("ATTACHMENTS_DIR", str(DATA_DIR / "attachments"))
)
ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

# Лимит размера одного файла и пороги мониторинга диска.
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "10"))
DISK_WARN_PCT = int(os.getenv("DISK_WARN_PCT", "80"))
DISK_BLOCK_PCT = int(os.getenv("DISK_BLOCK_PCT", "95"))
# Интервал фонового мониторинга диска. 1800 c = 30 мин.
# Сам алёрт дедуплицируется внутри services.storage.check_and_alert_disk
# (раз в 24 часа на каждый порог), так что 30 мин — безопасный дефолт.
DISK_CHECK_INTERVAL_SEC = int(os.getenv("DISK_CHECK_INTERVAL_SEC", "1800"))

# Режим приёма заявок по умолчанию: "files" | "links".
INTAKE_MODE_DEFAULT = os.getenv("INTAKE_MODE_DEFAULT", "files")

# Параметры жюри.
# TOP_N — размер шорт-листа на пул (по умолчанию 10).
# JURY_ROUNDS — максимальное число раундов до автоматического жребия.
# JURY_ROUND_DEADLINE_HOURS — дедлайн одного раунда (по умолчанию 48 ч).
# JURY_POOLS_CONFIG — JSON-конфиг распределения судей по пулам;
#   пустая строка = все судьи во всех 9 пулах (поведение по умолчанию).
TOP_N = int(os.getenv("TOP_N", "10"))
JURY_ROUNDS = int(os.getenv("JURY_ROUNDS", "3"))
JURY_ROUND_DEADLINE_HOURS = int(os.getenv("JURY_ROUND_DEADLINE_HOURS", "48"))
JURY_POOLS_CONFIG = os.getenv("JURY_POOLS_CONFIG", "")

# Год проведения конкурса — используется в формировании BR-ID.
COMPETITION_YEAR = int(os.getenv("COMPETITION_YEAR", "2026"))

# Текст экрана «Контакты организаторов». Вынесен из хардкода
# `app/handlers/user.py` в env-переменную, чтобы заказчик мог поправить
# формулировку без диффа в коде. Многострочный текст в .env передаётся
# через `\n` (dotenv разворачивает их в реальные переводы строк).
# Пустая строка / отсутствие переменной → используется дефолтный текст
# из ``CONTACTS_TEXT_DEFAULT`` ниже.
CONTACTS_TEXT_DEFAULT = (
    "**Контакты организаторов**\n\n"
    "• Организатор конкурса — команда ИБ.\n"
    "• Основной модератор — Екатерина Винокурова.\n"
    "• Резервный модератор / владелец проекта — Анастасия Иванова.\n\n"
    "По всем вопросам пишите в чат «Безопасные рисунки — модерация»."
)
CONTACTS_TEXT: str = os.getenv("CONTACTS_TEXT") or CONTACTS_TEXT_DEFAULT
