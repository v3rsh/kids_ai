"""
Глобальный middleware: апсерт пользователя в ``users`` при каждом
входящем сообщении из личного чата.

Зачем: без записи юзера в таблицу ``users`` любая попытка отправить ему
проактивный DM (например, «заявка принята», «новая задача жюри»)
получает WARNING ``нет chat_id`` и тихо умирает
(``services.notifications._send_to_user`` /
``services.discovery._resolve_user_chat_id``).

Контракт:
- Подключается ОДИН раз через
  ``Bot(middlewares=[chat_gate_middleware, user_sync_middleware])``
  в ``app/main.py.create_bot()`` после ``chat_gate_middleware`` —
  то есть мы заведомо в личном чате.
- Делает один апсерт ``huid + chat_id + ad_login`` и идёт дальше.
  Ошибки сети/БД глушим в самом сервисе (см.
  ``services.users.upsert_user_from_sender``), чтобы временный сбой
  на этом этапе не блокировал обработку команды.
- НЕ ходит в CTS. Тяжёлый CTS-вызов делается отдельно из ``cmd_apply``
  (``ensure_user_profile_loaded``) и fire-and-forget из
  ``cmd_start`` / ``on_chat_created``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger
from pybotx import Bot, IncomingMessage

from services import users as users_service


IncomingMessageHandlerFunc = Callable[[IncomingMessage, Bot], Awaitable[Any]]


async def user_sync_middleware(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    """Апсертит юзера и пропускает обработку дальше.

    Не блокирует основной flow: на любых ошибках просто логируем
    и продолжаем работу — упасть с UnhandledException на каждом сообщении
    при сетевом сбое БД мы не хотим.
    """
    try:
        await users_service.upsert_user_from_message(message)
    except Exception:
        logger.exception(
            "user_sync_middleware: апсерт пользователя упал, продолжаем",
            huid=str(getattr(message.sender, "huid", "")),
        )
    await call_next(message, bot)


__all__ = ["user_sync_middleware"]
