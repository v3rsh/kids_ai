"""
Общие хендлеры (не привязаны к ветке сценария).

Здесь — /start, /help, on_chat_created, default_message_handler.
"""
from loguru import logger
from pybotx import (
    Bot,
    ChatCreatedEvent,
    HandlerCollector,
    IncomingMessage,
)

from fsm import cleanup_middleware, fsm_middleware


collector = HandlerCollector()


@collector.chat_created
async def on_chat_created(event: ChatCreatedEvent, bot: Bot) -> None:
    """Первое сообщение, когда чат с ботом создан."""
    logger.info(
        "Создан новый чат",
        chat_id=str(event.chat.id),
        chat_type=event.chat.type,
    )
    await bot.send_message(
        bot_id=event.bot.id,
        chat_id=event.chat.id,
        body=(
            "Привет! Я бот kids_ai.\n\n"
            "Используй команду /start, чтобы начать."
        ),
        wait_callback=False,
    )


@collector.command("/start", description="Начать работу с ботом")
async def cmd_start(message: IncomingMessage, bot: Bot) -> None:
    """Точка входа в бот."""
    await bot.answer_message(
        "Бот kids_ai работает. Каркас готов, бизнес-логика добавляется по мере развития.",
        wait_callback=False,
    )


@collector.command("/help", description="Справка")
async def cmd_help(message: IncomingMessage, bot: Bot) -> None:
    """Краткая справка по командам."""
    await bot.answer_message(
        "Доступные команды:\n"
        "/start — начать работу\n"
        "/help — эта справка",
        wait_callback=False,
    )


@collector.default_message_handler(middlewares=[fsm_middleware])
async def default_handler(message: IncomingMessage, bot: Bot) -> None:
    """Обработка свободного текста — заглушка.

    По мере роста проекта здесь будет роутинг по текущему FSM-состоянию.
    """
    await bot.answer_message(
        "Я понимаю только команды. Используй /start или /help.",
        wait_callback=False,
    )
