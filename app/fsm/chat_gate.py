"""
Глобальный chat-gate middleware.

Бот реагирует на входящие сообщения **только в личных чатах**. Любые
групповые чаты (включая чат модерации) — outbound only: бот туда шлёт
уведомления через ``bot.send_message``, но клики и текст из этих чатов
молча игнорируются. Это сознательное проектное решение — модерация
ведётся только в личных DM модератора с ботом.

Подключается ОДИН раз через ``Bot(middlewares=[chat_gate_middleware])``
в ``app/main.py.create_bot()``. Применяется ДО всех остальных middleware
и хендлеров.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger
from pybotx import Bot, ChatTypes, IncomingMessage


IncomingMessageHandlerFunc = Callable[[IncomingMessage, Bot], Awaitable[Any]]


async def chat_gate_middleware(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    """Пропускает только PERSONAL_CHAT. Всё остальное молча дропает."""
    if message.chat.type == ChatTypes.PERSONAL_CHAT:
        await call_next(message, bot)
        return
    logger.debug(
        "chat_gate: входящее из неличного чата проигнорировано",
        chat_id=str(message.chat.id),
        chat_type=str(message.chat.type),
    )
    # Сознательно ничего не отвечаем: moderation chat тоже только outbound.


__all__ = ["chat_gate_middleware"]
