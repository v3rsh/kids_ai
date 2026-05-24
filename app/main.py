"""
kids_ai (pybotx)

Точка входа приложения на базе Starlette и pybotx.
"""
import asyncio
from contextlib import asynccontextmanager
from uuid import UUID

from loguru import logger
from starlette.applications import Starlette
from pybotx import Bot, BotAccountWithSecret, IncomingMessage, lifespan_wrapper

from config import (
    BOT_ID,
    CTS_URL,
    BOT_SECRET_KEY,
    DEBUG,
    DISK_CHECK_INTERVAL_SEC,
    ENABLE_SCHEDULER,
    JURY_POOLS_CONFIG,
    UVICORN_WORKERS,
)
from database.db import engine, get_session
from database.models import Base
from database.migrations import run_auto_migrations
from fsm import init_fsm_storage, close_fsm_storage
from handlers import get_all_collectors
from routes import routes


# ===== Глобальный обработчик ошибок =====

async def _global_error_handler(
    message: IncomingMessage,
    bot: Bot,
    exc: Exception,
) -> None:
    """Safety net: ловит все необработанные исключения из хендлеров."""
    logger.exception("Необработанная ошибка в хендлере:")
    from utils.bot_utils import send_with_retry
    await send_with_retry(
        bot,
        "Произошла ошибка. Попробуй ещё раз или начни сначала – команда **/start**",
    )


# ===== Инициализация бота =====

def create_bot() -> Bot:
    """Создаёт и настраивает экземпляр бота."""
    if not all([BOT_ID, CTS_URL, BOT_SECRET_KEY]):
        raise ValueError(
            "Не заданы обязательные переменные окружения: "
            "BOT_ID, CTS_URL, BOT_SECRET_KEY"
        )

    return Bot(
        collectors=get_all_collectors(),
        bot_accounts=[
            BotAccountWithSecret(
                id=UUID(BOT_ID),
                cts_url=CTS_URL,
                secret_key=BOT_SECRET_KEY,
            ),
        ],
        exception_handlers={Exception: _global_error_handler},
    )


# ===== Lifespan =====

@asynccontextmanager
async def lifespan(app: Starlette):
    """Управление жизненным циклом приложения."""
    logger.info("Запуск приложения kids_ai...")

    if ENABLE_SCHEDULER and UVICORN_WORKERS > 1:
        raise RuntimeError(
            "Некорректная конфигурация: ENABLE_SCHEDULER=true при "
            f"UVICORN_WORKERS={UVICORN_WORKERS}. "
            "При нескольких workers выключи scheduler в web-процессе "
            "(ENABLE_SCHEDULER=false) и запусти отдельный scheduler-контейнер."
        )

    # Создание новых таблиц (если модели добавлены)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Автомиграция: добавление колонок, индексов, enum-значений
    await run_auto_migrations()

    # Синхронизация справочников ролей и распределения судей по пулам.
    # Делается в одной короткой сессии до старта FSM/бота, чтобы первый
    # клик модератора/жюри сразу попадал в готовую БД.
    from services.access import sync_role_directories_from_config
    from services.pools import sync_pool_assignments_from_config

    async with get_session()() as session:
        await sync_role_directories_from_config(session)
        await sync_pool_assignments_from_config(JURY_POOLS_CONFIG, session=session)

    await init_fsm_storage()

    bot = create_bot()
    app.state.bot = bot

    disk_monitor_task: asyncio.Task | None = None
    async with lifespan_wrapper(bot) as bot_wrapper:
        # Фоновый мониторинг диска. Запускается только в
        # одном web-процессе: при UVICORN_WORKERS>1 + ENABLE_SCHEDULER=true
        # выше уже бросается RuntimeError.
        if ENABLE_SCHEDULER:
            from services.storage import start_disk_monitor_task

            disk_monitor_task = start_disk_monitor_task(
                bot, DISK_CHECK_INTERVAL_SEC
            )
            logger.info(
                "Фоновый монитор диска включён (ENABLE_SCHEDULER=true)",
                interval_sec=DISK_CHECK_INTERVAL_SEC,
            )
        else:
            logger.info(
                "ENABLE_SCHEDULER=false → фоновый монитор диска НЕ запущен; "
                "используй ручную команду /disk и помни про auto-switch в LINKS"
            )

        logger.info("Бот успешно запущен и готов к работе!")
        yield

    # Shutdown: остановить фоновые задачи и flush'нуть aggregator
    # уведомлений жюри (иначе теряем pending-event'ы агрегации).
    if disk_monitor_task is not None:
        disk_monitor_task.cancel()
        try:
            await disk_monitor_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        from services.notifications import flush_jury_event_aggregator

        await flush_jury_event_aggregator()
    except Exception:
        logger.exception("Не удалось корректно остановить агрегатор jury-событий")

    await close_fsm_storage()
    from utils.message_tracking import close_redis
    await close_redis()
    app.state.bot = None
    logger.info("Завершение работы приложения...")


# ===== Приложение Starlette =====

app = Starlette(
    debug=DEBUG,
    routes=routes,
    lifespan=lifespan,
)


# ===== Точка входа для uvicorn =====

if __name__ == "__main__":
    import uvicorn
    from config import SERVER_HOST, SERVER_PORT

    uvicorn.run(
        "main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=DEBUG,
    )
