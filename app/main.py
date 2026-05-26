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
from fsm import chat_gate_middleware, init_fsm_storage, close_fsm_storage
from handlers import get_all_collectors
from handlers._user_sync_middleware import user_sync_middleware
from routes import routes


# ===== Глобальный обработчик ошибок =====

async def _global_error_handler(
    message: IncomingMessage,
    bot: Bot,
    exc: Exception,
) -> None:
    """Safety net: ловит все необработанные исключения из хендлеров."""
    logger.exception("Необработанная ошибка в хендлере:")
    from keyboards import main_menu_bubbles
    from utils.bot_utils import send_with_retry

    huid = getattr(getattr(message, "sender", None), "huid", None)
    await send_with_retry(
        bot,
        "Произошла ошибка. Попробуй ещё раз или начни сначала – команда **/start**",
        bubbles=main_menu_bubbles(huid=huid),
    )


# ===== Валидация чата модерации =====

async def _validate_moderation_chat(bot: Bot) -> None:
    """Проверить, что бот реально является участником ``moderation_chat_id``.

    Если в ``app_settings.moderation_chat_id`` лежит UUID, а бот в этом
    чате не присутствует (или чата вообще нет), уведомления в чат
    модерации будут уходить в никуда — pybotx с ``wait_callback=False``
    не возвращает ошибку, и логи остаются «чистыми».

    Решение: на старте дёрнуть ``bot.chat_info(chat_id=mod_chat)``. При
    ошибке — сбросить настройку (``clear_moderation_chat``) и
    дополнительно записать ERROR-лог, чтобы факт сброса был заметен.
    """
    from services import access
    from utils.bot_utils import resolve_bot_id

    mod_chat = access.get_moderation_chat_id()
    if mod_chat is None:
        logger.info(
            "Валидация чата модерации: не настроен, пропускаю "
            "(добавьте бота в групповой чат и одобрите карточку)"
        )
        return

    bot_id = resolve_bot_id(bot)
    if bot_id is None:
        logger.error(
            "Валидация чата модерации: не удалось определить bot_id, пропускаю",
            chat_id=str(mod_chat),
        )
        return

    try:
        info = await bot.chat_info(bot_id=bot_id, chat_id=mod_chat)
    except Exception as exc:
        logger.error(
            "Валидация чата модерации: бот не участник или чат недоступен, "
            "сбрасываю moderation_chat_id",
            chat_id=str(mod_chat),
            error=repr(exc),
        )
        try:
            await access.clear_moderation_chat()
        except Exception:
            logger.exception(
                "Не удалось сбросить moderation_chat_id",
                chat_id=str(mod_chat),
            )
        return

    chat_name = getattr(info, "name", None) or getattr(info, "chat_name", None) or "—"
    logger.info(
        "Чат модерации валиден",
        chat_id=str(mod_chat),
        chat_name=chat_name,
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
        # Глобальный chat-gate: всё, что прилетает из не-личных чатов
        # (включая чат модерации), молча игнорируется. См. fsm/chat_gate.py.
        # После него — user_sync_middleware: апсертит юзера в `users`
        # (huid + chat_id + ad_*) на каждом входящем из личного чата.
        # Это гарантирует, что у нас есть `chat_id` для проактивных DM
        # (notifications/discovery), даже если юзер ещё ни разу не дёргал
        # `/start`. См. handlers/_user_sync_middleware.py.
        middlewares=[chat_gate_middleware, user_sync_middleware],
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

    # Bootstrap ролей и кэша доступа:
    # 1) reload in-memory кэша (services.access._moderator_huids и др.) —
    #    hot path (chat-gate, /moderator, /jury) после этого отвечает
    #    без походов в БД. Env-seed модераторов/жюри/чата отключён,
    #    управление через discovery (см. handlers/admin_roles.py).
    # 2) sync пулов жюри.
    from services.access import reload_access_cache
    from services.pools import sync_pool_assignments_from_config

    async with get_session()() as session:
        await reload_access_cache(session)
        await sync_pool_assignments_from_config(JURY_POOLS_CONFIG, session=session)

    await init_fsm_storage()

    bot = create_bot()
    app.state.bot = bot

    disk_monitor_task: asyncio.Task | None = None
    async with lifespan_wrapper(bot) as bot_wrapper:
        # Валидация чата модерации: если в БД лежит UUID, проверяем
        # фактическое членство бота через chat_info. Это ловит «болванки»
        # из старого env-seed и случаи, когда бота выгнали — иначе
        # ``_send_to_moderation_chat`` уходит в null без сигналов в логах.
        await _validate_moderation_chat(bot)

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
