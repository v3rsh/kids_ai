"""
Глобальный chat-gate middleware.

Бот реагирует на входящие сообщения **в личных чатах** и в **чате
модерации** (только от пользователей с ролью модератора). Остальные
групповые чаты — outbound only: бот шлёт уведомления, но клики и текст
из них молча игнорируются.

В чате модерации модератор может нажимать кнопки на служебных
уведомлениях (например, «📄 Карточка» → ``/find BR-…``).

Подключается ОДИН раз через ``Bot(middlewares=[chat_gate_middleware])``
в ``app/main.py.create_bot()``. Применяется ДО всех остальных middleware
и хендлеров.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger
from pybotx import Bot, ChatTypes, IncomingMessage

from services.access import get_moderation_chat_id, is_moderator


IncomingMessageHandlerFunc = Callable[[IncomingMessage, Bot], Awaitable[Any]]


def _is_moderation_chat_for_moderator(message: IncomingMessage) -> bool:
    """Разрешить inbound из чата модерации только для модераторов."""
    mod_chat = get_moderation_chat_id()
    if mod_chat is None:
        return False
    if message.chat.id != mod_chat:
        return False
    return is_moderator(message.sender.huid)


async def chat_gate_middleware(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    """Пропускает PERSONAL_CHAT и чат модерации (для модераторов)."""
    if message.chat.type == ChatTypes.PERSONAL_CHAT:
        await call_next(message, bot)
        return
    if _is_moderation_chat_for_moderator(message):
        await call_next(message, bot)
        return
    logger.debug(
        "chat_gate: входящее из неличного чата проигнорировано",
        chat_id=str(message.chat.id),
        chat_type=str(message.chat.type),
    )


__all__ = ["chat_gate_middleware"]
