"""
Хендлеры раздела «Пользователи» админки.
"""
from __future__ import annotations

from uuid import UUID

from loguru import logger
from pybotx import Bot, HandlerCollector, IncomingMessage
from sqlalchemy import select

from database.db import get_session
from database.models import User
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import admin_user_card_bubbles, admin_users_menu_bubbles
from services import access, discovery
from services.access import admin_only
from services.admin import apps_by_parent_huid
from services.users import sync_user_from_cts
from states import AdminAction, AdminFlow
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


def _btn_data(message: IncomingMessage) -> dict:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


def _parse_uuid(raw) -> UUID | None:
    if not raw:
        return None
    try:
        return UUID(str(raw).strip())
    except (TypeError, ValueError):
        return None


async def _format_user_card(bot: Bot, huid: UUID) -> str:
    """Собрать текст карточки пользователя."""
    profile = await discovery.fetch_user_profile(bot, huid)
    async with get_session()() as session:
        row = (
            await session.execute(select(User).where(User.huid == huid))
        ).scalar_one_or_none()

    lines = [
        f"**Профиль пользователя**",
        "",
        discovery._format_profile(profile),
    ]
    if row:
        lines.extend(
            [
                "",
                f"**chat_id:** `{row.chat_id}`" if row.chat_id else "**chat_id:** не зафиксирован",
                f"**last_activity:** {row.last_activity or '—'}",
                f"**cts_synced_at:** {row.cts_synced_at or '—'}",
            ]
        )
    roles: list[str] = []
    if access.is_admin(huid):
        roles.append("админ")
    if access.is_moderator(huid):
        roles.append("модератор")
    if access.is_jury(huid):
        roles.append("жюри")
    lines.append("")
    lines.append(f"**Роли:** {', '.join(roles) if roles else '—'}")
    return "\n".join(lines)


async def _show_user_card(
    message: IncomingMessage, bot: Bot, huid: UUID
) -> None:
    body = await _format_user_card(bot, huid)
    await reply_to_user(
        message,
        bot,
        body,
        bubbles=admin_user_card_bubbles(str(huid)),
    )


@collector.command(
    "/admin_user_find",
    description="Найти пользователя по HUID (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_user_find(message: IncomingMessage, bot: Bot) -> None:
    """FSM или аргумент: поиск пользователя."""
    arg = (message.argument or "").strip()
    huid = _parse_uuid(arg)
    if huid is None:
        await message.state.fsm.set_state(AdminAction.admin_action_find_user_huid)
        await reply_to_user(
            message,
            bot,
            "Введите HUID пользователя (UUID) следующим сообщением.",
            bubbles=admin_users_menu_bubbles(),
        )
        return
    await _show_user_card(message, bot, huid)


async def _state_handle_find_user_huid(
    message: IncomingMessage, bot: Bot
) -> None:
    huid = _parse_uuid((message.body or "").strip())
    await message.state.fsm.clear()
    if huid is None:
        await reply_to_user(
            message,
            bot,
            "Невалидный UUID. Попробуйте снова через /admin_user_find.",
            bubbles=admin_users_menu_bubbles(),
        )
        return
    await _show_user_card(message, bot, huid)


@collector.command(
    "/admin_user_resync",
    description="Resync CTS профиля (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_user_resync(message: IncomingMessage, bot: Bot) -> None:
    """Принудительный sync_user_from_cts."""
    data = _btn_data(message)
    huid = _parse_uuid(data.get("huid") or message.argument)
    if huid is None:
        await reply_to_user(
            message,
            bot,
            "Укажите HUID через кнопку на карточке или `/admin_user_resync <huid>`.",
            bubbles=admin_users_menu_bubbles(),
        )
        return
    try:
        await sync_user_from_cts(bot, huid)
        body = f"✅ CTS-профиль обновлён для `{huid}`."
    except Exception:
        logger.exception("admin_user_resync упал", huid=str(huid))
        body = f"❌ Не удалось обновить профиль `{huid}`. См. логи."
    await reply_to_user(
        message,
        bot,
        body + "\n\n" + await _format_user_card(bot, huid),
        bubbles=admin_user_card_bubbles(str(huid)),
    )


@collector.command(
    "/admin_user_apps",
    description="Заявки родителя по HUID (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_user_apps(message: IncomingMessage, bot: Bot) -> None:
    """Список заявок родителя."""
    data = _btn_data(message)
    huid = _parse_uuid(data.get("huid") or message.argument)
    if huid is None:
        await reply_to_user(
            message,
            bot,
            "Укажите HUID через кнопку на карточке.",
            bubbles=admin_users_menu_bubbles(),
        )
        return

    apps = await apps_by_parent_huid(huid)
    if not apps:
        body = f"У пользователя `{huid}` заявок нет."
    else:
        lines = [f"**Заявки родителя** `{huid}`:", ""]
        for app in apps[:30]:
            lines.append(
                f"• **{app.br_id}** · {app.track.value} · "
                f"{app.moderation_status.value} · «{app.title}»"
            )
        if len(apps) > 30:
            lines.append(f"\n… и ещё {len(apps) - 30} заявок.")
        body = "\n".join(lines)

    await reply_to_user(
        message,
        bot,
        body,
        bubbles=admin_user_card_bubbles(str(huid)),
    )


register_state_handler(
    AdminAction.admin_action_find_user_huid.value,
    _state_handle_find_user_huid,
)


__all__ = ["collector"]
