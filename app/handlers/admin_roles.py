"""
Хендлеры администрирования ролей и чата модерации.

Реализует кнопки discovery-карточек (``services/discovery.py``) и
диагностические команды:

- ``/admin_role_approve`` — назначить юзера модератором или жюри (по
  ``data["role"]`` / ``data["huid"]``). После записи в БД:
    * для модератора — попытка добавить его в чат модерации
      (``bot.add_users_to_chat``);
    * попытка отправить welcome-DM, если у юзера известен chat_id.
- ``/admin_role_reject`` — мягкое «❌ Отклонено» (редактирует карточку).
- ``/admin_chat_approve`` — установить chat_id как ``moderation_chat_id``
  и отправить в этот чат короткое подтверждение.
- ``/admin_chat_reject`` — «❌ Отклонено».
- ``/admin_roles`` — диагностика: список активных модераторов, жюри и
  текущий ``moderation_chat_id`` с кнопками «Отозвать».
- ``/admin_role_revoke`` — отозвать роль (через кнопку из ``/admin_roles``
  или вручную ``/admin_role_revoke <huid> <role>``).

Все команды скрытые (``visible=False``) и защищены ``@admin_only``.
Все они живут в личном чате админа — chat-gate пропускает только
PERSONAL_CHAT, так что групповые клики не сработают.
"""
from __future__ import annotations

from uuid import UUID

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage
from sqlalchemy import select

from database.db import get_session
from database.models import JuryMember, Moderator
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import (
    admin_add_role_role_bubbles,
    admin_confirm_bubbles,
    admin_resend_welcome_role_bubbles,
    admin_roles_menu_bubbles,
    back_to_admin_menu_bubbles,
)
from services import access, discovery
from services.access import admin_only
from states import AdminAction, AdminFlow
from utils.bot_utils import reply_to_user, resolve_bot_id


collector = HandlerCollector()


# =====================================================================
# Утилиты парсинга payload кнопок и аргументов
# =====================================================================


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


async def _assign_role(
    message: IncomingMessage,
    bot: Bot,
    role: str,
    huid: UUID,
    *,
    initial_name: str = "",
) -> str:
    """Назначить модератора или жюри; вернуть текст ответа."""
    profile = await discovery.fetch_user_profile(bot, huid)
    full_name = initial_name or (
        profile.get("public_name")
        or profile.get("username")
        or profile.get("ad_login")
        or ""
    )

    if role == "moderator":
        await access.add_moderator(
            huid,
            full_name=full_name,
            username=profile.get("username"),
            by_huid=message.sender.huid,
        )
        _chat_ok, chat_status = await discovery.add_moderator_to_chat(bot, huid)
        dm_ok = await discovery.send_welcome_dm_to_moderator(bot, huid)
        return (
            f"✅ Назначен модератором: **{full_name or huid}**.\n"
            f"• Чат модерации: {chat_status}\n"
            f"• Welcome-DM: "
            f"{'отправлен' if dm_ok else 'не отправлен (пусть напишет боту /start)'}"
        )

    await access.add_jury_member(
        huid,
        full_name=full_name,
        username=profile.get("username"),
        by_huid=message.sender.huid,
    )
    dm_ok = await discovery.send_welcome_dm_to_jury(bot, huid)
    return (
        f"✅ Назначен членом жюри: **{full_name or huid}**.\n"
        f"• Welcome-DM: "
        f"{'отправлен' if dm_ok else 'не отправлен (пусть напишет боту /start)'}"
    )


# =====================================================================
# Назначение / отзыв роли (модератор | жюри)
# =====================================================================


@collector.command(
    "/admin_role_approve",
    description="Назначить роль (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_role_approve(message: IncomingMessage, bot: Bot) -> None:
    """Назначить юзера модератором или жюри по нажатию кнопки из
    discovery-карточки.

    После записи в БД делает два дополнительных шага (не критичных):
    1. для модератора — добавление в чат модерации (если он настроен);
    2. отправка welcome-DM назначенному (если у него есть ``chat_id``).
    """
    data = _btn_data(message)
    role = (data.get("role") or "").lower()
    huid = _parse_uuid(data.get("huid"))
    initial_name = (data.get("name") or "").strip()

    if role not in ("moderator", "jury") or huid is None:
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось обработать кнопку: повреждены данные карточки.",
            bubbles=back_to_admin_menu_bubbles(),
        )
        return

    body = await _assign_role(
        message,
        bot,
        role,
        huid,
        initial_name=initial_name,
    )

    logger.info(
        "Админ назначил роль",
        role=role,
        huid=str(huid),
        by_huid=str(message.sender.huid),
    )
    await reply_to_user(
        message, bot, body, bubbles=back_to_admin_menu_bubbles()
    )


@collector.command(
    "/admin_role_reject",
    description="Отклонить запрос роли (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_role_reject(message: IncomingMessage, bot: Bot) -> None:
    """Отклонить кандидата на роль (только редактирует карточку)."""
    data = _btn_data(message)
    role = (data.get("role") or "").lower()
    name = (data.get("name") or "").strip() or data.get("huid") or "—"
    role_label = "модератора" if role == "moderator" else "члена жюри"
    await reply_to_user(
        message,
        bot,
        f"❌ Запрос на роль {role_label} отклонён: **{name}**.",
        bubbles=back_to_admin_menu_bubbles(),
    )


# =====================================================================
# Назначение / отклонение чата модерации
# =====================================================================


_MODERATION_CHAT_WELCOME = (
    "Чат настроен как чат модерации конкурса «Безопасные рисунки».\n"
    "Сюда будут приходить уведомления о новых заявках, событиях жюри "
    "и алёртах диска."
)


@collector.command(
    "/admin_chat_approve",
    description="Сделать чат чатом модерации (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_chat_approve(message: IncomingMessage, bot: Bot) -> None:
    """Записать chat_id как текущий moderation_chat_id."""
    data = _btn_data(message)
    chat_id = _parse_uuid(data.get("chat_id"))
    chat_name = (data.get("chat_name") or "").strip()
    if chat_id is None:
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось обработать кнопку: повреждены данные карточки.",
            bubbles=back_to_admin_menu_bubbles(),
        )
        return

    await access.set_moderation_chat(chat_id, by_huid=message.sender.huid)

    welcome_ok = True
    bot_id = resolve_bot_id(bot)
    try:
        send_kwargs = {
            "chat_id": chat_id,
            "body": _MODERATION_CHAT_WELCOME,
            "wait_callback": False,
        }
        if bot_id is not None:
            send_kwargs["bot_id"] = bot_id
        await bot.send_message(**send_kwargs)
    except Exception:
        logger.exception(
            "Не удалось отправить welcome в чат модерации",
            chat_id=str(chat_id),
        )
        welcome_ok = False

    body = (
        f"✅ Чат «{chat_name or chat_id}» назначен чатом модерации.\n"
        f"• Подтверждение в чат: {'отправлено' if welcome_ok else 'не отправлено'}"
    )
    await reply_to_user(
        message, bot, body, bubbles=back_to_admin_menu_bubbles()
    )


@collector.command(
    "/admin_chat_reject",
    description="Отклонить чат модерации (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_chat_reject(message: IncomingMessage, bot: Bot) -> None:
    """Отклонить кандидата на чат модерации."""
    data = _btn_data(message)
    chat_name = (data.get("chat_name") or "").strip() or data.get("chat_id") or "—"
    await reply_to_user(
        message,
        bot,
        f"❌ Отклонён чат-кандидат на чат модерации: **{chat_name}**.",
        bubbles=back_to_admin_menu_bubbles(),
    )


# =====================================================================
# Диагностика и отзыв ролей
# =====================================================================


@collector.command(
    "/admin_role_add",
    description="Назначить роль по HUID (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_role_add(message: IncomingMessage, bot: Bot) -> None:
    """FSM: выбор роли → ввод HUID → назначение."""
    data = _btn_data(message)
    role = (data.get("role") or "").lower()

    if role in ("moderator", "jury"):
        await message.state.fsm.set_state(AdminAction.admin_action_add_role_huid)
        await message.state.fsm.update_data(admin_add_role=role)
        role_label = "модератора" if role == "moderator" else "жюри"
        await reply_to_user(
            message,
            bot,
            f"Введите HUID (UUID) пользователя для назначения **{role_label}** "
            "следующим сообщением.",
            bubbles=admin_add_role_role_bubbles(),
        )
        return

    await message.state.fsm.set_state(AdminAction.admin_action_add_role_role)
    await reply_to_user(
        message,
        bot,
        "Выберите роль для назначения или нажмите кнопку ниже.",
        bubbles=admin_add_role_role_bubbles(),
    )


async def _state_handle_add_role_huid(
    message: IncomingMessage, bot: Bot
) -> None:
    """FSM: ввод HUID при назначении роли."""
    huid = _parse_uuid((message.body or "").strip())
    data = await message.state.fsm.get_data()
    role = (data.get("admin_add_role") or "").lower()
    await message.state.fsm.clear()

    if huid is None:
        await reply_to_user(
            message,
            bot,
            "Невалидный UUID. Повторите через /admin_role_add.",
            bubbles=admin_roles_menu_bubbles(),
        )
        return
    if role not in ("moderator", "jury"):
        await reply_to_user(
            message,
            bot,
            "Роль не выбрана. Начните заново через /admin_role_add.",
            bubbles=admin_roles_menu_bubbles(),
        )
        return

    body = await _assign_role(message, bot, role, huid)
    logger.info(
        "Админ назначил роль через /admin_role_add",
        role=role,
        huid=str(huid),
        by_huid=str(message.sender.huid),
    )
    await reply_to_user(message, bot, body, bubbles=admin_roles_menu_bubbles())


@collector.command(
    "/admin_role_resend_welcome",
    description="Повторить welcome-DM (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_role_resend_welcome(
    message: IncomingMessage, bot: Bot
) -> None:
    """Повторная отправка welcome-DM модератору или жюри."""
    data = _btn_data(message)
    huid = _parse_uuid(data.get("huid") or message.argument)
    role = (data.get("role") or "").lower()

    if huid and role in ("moderator", "jury"):
        if role == "moderator":
            if not access.is_moderator(huid):
                body = f"ℹ️ `{huid}` не является активным модератором."
            else:
                dm_ok = await discovery.send_welcome_dm_to_moderator(bot, huid)
                body = (
                    f"✅ Welcome-DM модератору отправлен."
                    if dm_ok
                    else f"ℹ️ Welcome-DM не отправлен (нет chat_id у `{huid}`)."
                )
        else:
            if not access.is_jury(huid):
                body = f"ℹ️ `{huid}` не является активным членом жюри."
            else:
                dm_ok = await discovery.send_welcome_dm_to_jury(bot, huid)
                body = (
                    f"✅ Welcome-DM жюри отправлен."
                    if dm_ok
                    else f"ℹ️ Welcome-DM не отправлен (нет chat_id у `{huid}`)."
                )
        bubbles = (
            admin_resend_welcome_role_bubbles(str(huid))
            if huid
            else admin_roles_menu_bubbles()
        )
        await reply_to_user(message, bot, body, bubbles=bubbles)
        return

    if huid:
        await reply_to_user(
            message,
            bot,
            f"Выберите роль для повторной welcome-DM пользователя `{huid}`.",
            bubbles=admin_resend_welcome_role_bubbles(str(huid)),
        )
        return

    async with get_session()() as session:
        mods = (
            await session.execute(
                select(Moderator).where(Moderator.is_active.is_(True))
            )
        ).scalars().all()
        jury = (
            await session.execute(
                select(JuryMember).where(JuryMember.is_active.is_(True))
            )
        ).scalars().all()

    if not mods and not jury:
        await reply_to_user(
            message,
            bot,
            "Активных модераторов и жюри нет.",
            bubbles=admin_roles_menu_bubbles(),
        )
        return

    lines = [
        "**Повторить welcome-DM**\n",
        "Выберите пользователя:",
    ]
    bubbles = BubbleMarkup()
    for m in mods:
        name = m.full_name or m.username or str(m.huid)[:8]
        bubbles.add_button(
            command="/admin_role_resend_welcome",
            label=f"🛡 {name}",
            data={"huid": str(m.huid)},
            new_row=True,
        )
    for j in jury:
        name = j.full_name or j.username or str(j.huid)[:8]
        bubbles.add_button(
            command="/admin_role_resend_welcome",
            label=f"⚖️ {name}",
            data={"huid": str(j.huid)},
            new_row=True,
        )
    bubbles.add_button(command="/admin", label="◀ В админку", new_row=True)
    await reply_to_user(message, bot, "\n".join(lines), bubbles=bubbles)


@collector.command(
    "/admin_roles",
    description="Список ролей и текущий чат модерации (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_roles(message: IncomingMessage, bot: Bot) -> None:
    """Диагностика: активные модераторы, жюри, moderation_chat_id."""
    await message.state.fsm.set_state(AdminFlow.admin_menu)
    async with get_session()() as session:
        mods = (
            await session.execute(
                select(Moderator).where(Moderator.is_active.is_(True))
            )
        ).scalars().all()
        jury = (
            await session.execute(
                select(JuryMember).where(JuryMember.is_active.is_(True))
            )
        ).scalars().all()

    moderation_chat = access.get_moderation_chat_id()

    lines = ["🛠 Текущее состояние ролей:"]
    lines.append(
        f"\nЧат модерации: `{moderation_chat}`"
        if moderation_chat else "\nЧат модерации: не настроен"
    )

    lines.append(f"\nМодераторы (активные): {len(mods)}")
    for m in mods:
        name = m.full_name or m.username or m.huid
        lines.append(f"• {name} — `{m.huid}`")
    lines.append(f"\nЧлены жюри (активные): {len(jury)}")
    for j in jury:
        name = j.full_name or j.username or j.huid
        lines.append(f"• {name} — `{j.huid}`")

    bubbles = BubbleMarkup()
    for m in mods:
        bubbles.add_button(
            command="/admin_role_revoke",
            label=f"🗑 Отозвать модератора: {m.full_name or m.username or str(m.huid)[:8]}",
            data={"role": "moderator", "huid": str(m.huid)},
            new_row=True,
        )
    for j in jury:
        bubbles.add_button(
            command="/admin_role_revoke",
            label=f"🗑 Отозвать жюри: {j.full_name or j.username or str(j.huid)[:8]}",
            data={"role": "jury", "huid": str(j.huid)},
            new_row=True,
        )

    bubbles.add_button(command="/admin", label="◀ В админку", new_row=True)
    if not mods and not jury:
        await reply_to_user(
            message,
            bot,
            "\n".join(lines),
            bubbles=admin_roles_menu_bubbles(),
        )
        return
    await reply_to_user(message, bot, "\n".join(lines), bubbles=bubbles)


@collector.command(
    "/admin_role_revoke",
    description="Отозвать роль (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_role_revoke(message: IncomingMessage, bot: Bot) -> None:
    """Первый шаг: запрос подтверждения отзыва роли."""
    data = _btn_data(message)
    role = (data.get("role") or "").lower()
    huid = _parse_uuid(data.get("huid"))

    if not role or huid is None:
        arg = (message.argument or "").strip().split()
        if len(arg) >= 2:
            huid = _parse_uuid(arg[0])
            role = arg[1].lower()

    if role not in ("moderator", "jury") or huid is None:
        await reply_to_user(
            message,
            bot,
            "Использование: `/admin_role_revoke <huid> <moderator|jury>` "
            "или нажмите кнопку в `/admin_roles`.",
            bubbles=admin_roles_menu_bubbles(),
        )
        return

    role_label = "модератора" if role == "moderator" else "члена жюри"
    await reply_to_user(
        message,
        bot,
        f"⚠️ Отозвать роль **{role_label}** у `{huid}`?",
        bubbles=admin_confirm_bubbles(
            action="revoke_role",
            payload={"role": role, "huid": str(huid)},
            confirm_command="/admin_role_revoke_confirm",
        ),
    )


@collector.command(
    "/admin_role_revoke_confirm",
    description="Подтверждение отзыва роли (admin only)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_role_revoke_confirm(
    message: IncomingMessage, bot: Bot
) -> None:
    """Второй шаг: выполнить или отменить отзыв роли."""
    data = _btn_data(message)
    confirm = (data.get("confirm") or "").strip().lower()
    role = (data.get("role") or "").lower()
    huid = _parse_uuid(data.get("huid"))

    if confirm != "yes":
        await reply_to_user(
            message,
            bot,
            "❌ Отзыв роли отменён.",
            bubbles=admin_roles_menu_bubbles(),
        )
        return

    if role not in ("moderator", "jury") or huid is None:
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось обработать подтверждение: повреждены данные.",
            bubbles=admin_roles_menu_bubbles(),
        )
        return

    if role == "moderator":
        changed = await access.revoke_moderator(huid)
    else:
        changed = await access.revoke_jury(huid)

    role_label = "модератора" if role == "moderator" else "члена жюри"
    if changed:
        body = f"✅ Роль {role_label} отозвана: `{huid}`."
        logger.info(
            "Админ отозвал роль",
            role=role,
            huid=str(huid),
            by_huid=str(message.sender.huid),
        )
    else:
        body = f"ℹ️ Активная роль {role_label} у `{huid}` не найдена."
    await reply_to_user(message, bot, body, bubbles=admin_roles_menu_bubbles())


register_state_handler(
    AdminAction.admin_action_add_role_huid.value,
    _state_handle_add_role_huid,
)


__all__ = ["collector"]
