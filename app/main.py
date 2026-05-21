"""
kids_ai (pybotx)

Точка входа приложения на базе Starlette и pybotx.
"""
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
    ENABLE_SCHEDULER,
    UVICORN_WORKERS,
)
from database.db import engine
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

    await init_fsm_storage()

    bot = create_bot()
    app.state.bot = bot

    async with lifespan_wrapper(bot) as bot_wrapper:
        if ENABLE_SCHEDULER:
            # При появлении app/scheduler.py подключай его здесь:
            #   from scheduler import setup_scheduler
            #   setup_scheduler(bot)
            logger.info(
                "ENABLE_SCHEDULER=true, но app/scheduler.py ещё не создан — пропускаем"
            )
        else:
            logger.info("Scheduler отключён (ENABLE_SCHEDULER=false)")

        logger.info("Бот успешно запущен и готов к работе!")
        yield

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
