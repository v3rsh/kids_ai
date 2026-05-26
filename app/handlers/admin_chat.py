"""
Хендлеры раздела «Чат модерации» админки.
"""
from __future__ import annotations

from uuid import UUID

from loguru import logger
from pybotx import Bot, HandlerCollector, IncomingMessage

from fsm import cleanup_middleware, fsm_middleware
from handlers.admin import _format_roles_block, _sender_huid
from keyboards import admin_chat_menu_bubbles
from services import access, discovery
from services.access import admin_only
from services.notifications import _send_to_moderation_chat
from states import AdminAction, AdminFlow
from utils.bot_utils import reply_to_user, resolve_bot_id


collector = HandlerCollector()

_ADMIN_CHAT_TEST_DEFAULT = (
    "🔧 Тестовое сообщение от администратора бота «Безопасные рисунки».\n"
    "Если вы видите это — чат модерации настроен корректно."
)


def _btn_data(message: IncomingMessage) -> dict:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


@collector.command(
    "/admin_chat_status",
    description="Статус чата модерации (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_chat_status(message: IncomingMessage, bot: Bot) -> None:
    """Диагностика текущего чата модерации."""
    await message.state.fsm.set_state(AdminFlow.admin_menu)
    roles_block = await _format_roles_block(bot, _sender_huid(message))
    mod_chat = access.get_moderation_chat_id()
    header = (
        "**Чат модерации не настроен.**"
        if mod_chat is None
        else f"**Текущий chat_id:** `{mod_chat}`"
    )
    await reply_to_user(
        message,
        bot,
        f"{header}\n{roles_block}",
        bubbles=admin_chat_menu_bubbles(),
    )


@collector.command(
    "/admin_chat_test",
    description="Тестовое сообщение в чат модерации (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_chat_test(message: IncomingMessage, bot: Bot) -> None:
    """Отправить тест в чат модерации или запросить свой текст."""
    data = _btn_data(message)
    custom = (data.get("text") or message.argument or "").strip()
    if not custom and not message.argument:
        await message.state.fsm.set_state(AdminAction.admin_action_chat_test_text)
        await reply_to_user(
            message,
            bot,
            (
                "Отправьте текст тестового сообщения следующим сообщением "
                "или нажмите «Отправить шаблон»."
            ),
            bubbles=admin_chat_menu_bubbles(),
        )
        return

    body = custom or _ADMIN_CHAT_TEST_DEFAULT
    await _send_to_moderation_chat(
        bot,
        body,
        purpose="admin_chat_test",
    )
    await reply_to_user(
        message,
        bot,
        "✅ Тестовое сообщение отправлено в чат модерации "
        "(если чат настроен и бот в нём).",
        bubbles=admin_chat_menu_bubbles(),
    )


@collector.command(
    "/admin_chat_rediscover",
    description="Повтор discovery чата модерации (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_chat_rediscover(message: IncomingMessage, bot: Bot) -> None:
    """Сбросить дедуп discovery и отправить карточку админу."""
    mod_chat = access.get_moderation_chat_id()
    if mod_chat is None:
        await reply_to_user(
            message,
            bot,
            (
                "ℹ️ Чат модерации не задан. Добавьте бота в групповой чат — "
                "карточка discovery придёт автоматически."
            ),
            bubbles=admin_chat_menu_bubbles(),
        )
        return

    discovery._notified_at.pop(("chat", str(mod_chat).lower()), None)

    chat_name = "—"
    bot_id = resolve_bot_id(bot)
    if bot_id is not None:
        try:
            info = await bot.chat_info(bot_id=bot_id, chat_id=mod_chat)
            chat_name = (
                getattr(info, "name", None)
                or getattr(info, "chat_name", None)
                or "—"
            )
        except Exception as exc:
            logger.warning(
                "admin_chat_rediscover: chat_info упал",
                chat_id=str(mod_chat),
                error=str(exc),
            )

    await discovery.notify_admin_moderation_chat_candidate(
        bot,
        chat_id=mod_chat,
        chat_name=chat_name,
        creator_huid=message.sender.huid,
    )
    await reply_to_user(
        message,
        bot,
        "✅ Карточка discovery отправлена в ваш личный чат с ботом.",
        bubbles=admin_chat_menu_bubbles(),
    )


async def state_handle_chat_test_text(
    message: IncomingMessage, bot: Bot
) -> None:
    """FSM: текст тестового сообщения в чат модерации."""
    text = (message.body or "").strip()
    await message.state.fsm.clear()
    if not text:
        await reply_to_user(
            message,
            bot,
            "Текст не может быть пустым.",
            bubbles=admin_chat_menu_bubbles(),
        )
        return
    await _send_to_moderation_chat(
        bot,
        text,
        purpose="admin_chat_test_custom",
    )
    await reply_to_user(
        message,
        bot,
        "✅ Сообщение отправлено в чат модерации.",
        bubbles=admin_chat_menu_bubbles(),
    )


from handlers.common import register_state_handler

register_state_handler(
    AdminAction.admin_action_chat_test_text.value,
    state_handle_chat_test_text,
)


__all__ = ["collector"]
