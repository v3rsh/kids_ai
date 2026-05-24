"""
Сервис discovery: автоматическое обнаружение кандидатов на роли
и автоматизация рутины при назначении.

Контракт:
- ``notify_admin_role_candidate(bot, huid, role)`` — когда юзер,
  не имеющий роли, дёрнул точку входа (``/moderator`` / ``/jury``),
  бот шлёт админу карточку с профилем + двумя кнопками («Назначить» /
  «Отклонить»). Дедуп по ``(huid, role)`` с TTL 1 час, чтобы повторные
  попытки не флудили.
- ``notify_admin_moderation_chat_candidate(bot, chat_id, ...)`` — когда
  бот добавлен в новый групповой чат (не ``moderation_chat_id``), та же
  механика, кнопки «Сделать чатом модерации» / «Отклонить».
- ``add_moderator_to_chat(bot, huid)`` — после одобрения модератора
  пытается затащить его в ``moderation_chat_id`` через
  ``bot.add_users_to_chat``. Возвращает ``(ok, human_status)``.
- ``send_welcome_dm_to_moderator(bot, huid)`` / ``_to_jury(bot, huid)``
  — короткое приветственное DM назначенному. Если у него ещё нет
  ``users.chat_id`` — WARNING и no-op.

Карточки админу отправляются в его личный чат. ``chat_id`` админа
берётся из таблицы ``users`` (заполняется при первом ``/start``).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from loguru import logger
from pybotx import BubbleMarkup

from services import access
from utils.bot_utils import resolve_bot_id

if TYPE_CHECKING:
    from pybotx import Bot


# =====================================================================
# Внутренние утилиты
# =====================================================================


_RoleKind = Literal["moderator", "jury"]


# Дедуп: ключ -> монотоник timestamp последнего уведомления.
# Ключи:
#   ("role", role, str(huid)) для discovery ролей;
#   ("chat", str(chat_id)) для discovery чатов.
_DEDUP_TTL_SECONDS = 3600  # 1 час
_notified_at: dict[tuple, float] = {}


def _dedup_should_skip(key: tuple) -> bool:
    """True, если уже шлём это сообщение в окне TTL."""
    now = time.monotonic()
    last = _notified_at.get(key)
    if last is not None and (now - last) < _DEDUP_TTL_SECONDS:
        return True
    _notified_at[key] = now
    return False


async def _get_admin_huids() -> list[UUID]:
    """Список UUID администраторов (из env, через services.access.ADMIN)."""
    from config import ADMIN_HUIDS

    out: list[UUID] = []
    for raw in ADMIN_HUIDS:
        try:
            out.append(UUID(str(raw).strip()))
        except (TypeError, ValueError):
            logger.warning("ADMIN_HUID невалидный UUID", value=raw)
    return out


async def _resolve_user_chat_id(huid: UUID) -> UUID | None:
    """Найти ``users.chat_id`` для пользователя."""
    from sqlalchemy import select

    from database.db import get_session
    from database.models import User

    async with get_session()() as session:
        result = await session.execute(
            select(User.chat_id).where(User.huid == huid)
        )
        row = result.first()
        return row[0] if row else None


# =====================================================================
# Поиск профиля
# =====================================================================


async def fetch_user_profile(bot: "Bot", huid: UUID) -> dict:
    """Получить технический профиль пользователя через CTS.

    Возвращает dict с человекочитаемыми полями. Ошибки CTS (юзер не
    найден / нет прав) → пустой профиль с одним huid, без исключения.
    """
    bot_id = resolve_bot_id(bot)
    profile = {
        "huid": str(huid),
        "username": None,
        "ad_login": None,
        "ad_domain": None,
        "company": None,
        "company_position": None,
        "department": None,
        "emails": [],
        "public_name": None,
    }
    if bot_id is None:
        logger.warning("fetch_user_profile: не удалось определить bot_id")
        return profile
    try:
        user = await bot.search_user_by_huid(bot_id=bot_id, huid=huid)
    except Exception:
        logger.exception("fetch_user_profile: search_user_by_huid упал", huid=str(huid))
        return profile
    profile.update(
        username=user.username,
        ad_login=user.ad_login,
        ad_domain=user.ad_domain,
        company=user.company,
        company_position=user.company_position,
        department=user.department,
        emails=list(user.emails or []),
        public_name=user.public_name,
    )
    return profile


def _format_profile(profile: dict) -> str:
    """Многострочное описание профиля для карточки админу."""
    lines = [
        f"HUID: `{profile['huid']}`",
    ]
    name = profile.get("username") or profile.get("public_name")
    if name:
        lines.append(f"Имя: {name}")
    ad = profile.get("ad_login")
    if ad:
        if profile.get("ad_domain"):
            lines.append(f"AD: {ad}@{profile['ad_domain']}")
        else:
            lines.append(f"AD: {ad}")
    if profile.get("company_position"):
        lines.append(f"Должность: {profile['company_position']}")
    if profile.get("department"):
        lines.append(f"Подразделение: {profile['department']}")
    if profile.get("company"):
        lines.append(f"Компания: {profile['company']}")
    if profile.get("emails"):
        lines.append(f"Email: {', '.join(profile['emails'])}")
    return "\n".join(lines)


def _profile_full_name(profile: dict) -> str:
    """Лучшее доступное имя пользователя."""
    return (
        profile.get("public_name")
        or profile.get("username")
        or profile.get("ad_login")
        or profile.get("huid", "—")
    )


# =====================================================================
# Отправка карточек админу
# =====================================================================


async def _send_to_admin(
    bot: "Bot",
    *,
    body: str,
    bubbles: BubbleMarkup,
    purpose: str,
) -> int:
    """Отправить сообщение всем админам в их личные чаты. Возвращает
    количество успешных доставок."""
    admins = await _get_admin_huids()
    if not admins:
        logger.warning(
            "Discovery: ADMIN_HUIDS не задан, некому слать карточку",
            purpose=purpose,
        )
        return 0

    bot_id = resolve_bot_id(bot)
    delivered = 0

    for admin_huid in admins:
        chat_id = await _resolve_user_chat_id(admin_huid)
        if chat_id is None:
            logger.warning(
                "Discovery: у админа нет chat_id (пусть напишет боту /start)",
                purpose=purpose,
                admin_huid=str(admin_huid),
            )
            continue
        kwargs = {
            "chat_id": chat_id,
            "body": body,
            "bubbles": bubbles,
            "wait_callback": False,
        }
        if bot_id is not None:
            kwargs["bot_id"] = bot_id
        try:
            await bot.send_message(**kwargs)
            delivered += 1
        except Exception:
            logger.exception(
                "Discovery: не удалось отправить карточку админу",
                purpose=purpose,
                admin_huid=str(admin_huid),
            )

    logger.info(
        "Discovery: карточка админу отправлена",
        purpose=purpose,
        delivered=delivered,
        admins_total=len(admins),
    )
    return delivered


async def notify_admin_role_candidate(
    bot: "Bot",
    *,
    huid: UUID,
    role: _RoleKind,
) -> None:
    """Карточка админу: «новый кандидат на роль X»."""
    dedup_key = ("role", role, str(huid).lower())
    if _dedup_should_skip(dedup_key):
        logger.debug(
            "Discovery role: дедуп, не шлём повторно",
            huid=str(huid),
            role=role,
        )
        return

    profile = await fetch_user_profile(bot, huid)

    role_label = "модератора" if role == "moderator" else "члена жюри"
    body = (
        f"🔔 Запрос доступа к роли {role_label}.\n\n"
        f"{_format_profile(profile)}\n\n"
        "Подтверди или отклони запрос."
    )

    bubbles = BubbleMarkup()
    payload = {
        "role": role,
        "huid": str(huid),
        "name": _profile_full_name(profile),
    }
    bubbles.add_button(
        command="/admin_role_approve",
        label=(
            "✅ Назначить модератором"
            if role == "moderator"
            else "✅ Назначить членом жюри"
        ),
        data=payload,
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_reject",
        label="❌ Отклонить",
        data=payload,
        new_row=True,
    )

    await _send_to_admin(
        bot,
        body=body,
        bubbles=bubbles,
        purpose=f"discovery_role_{role}",
    )


async def notify_admin_moderation_chat_candidate(
    bot: "Bot",
    *,
    chat_id: UUID,
    chat_name: str,
    creator_huid: UUID | None,
) -> None:
    """Карточка админу: «бот добавлен в новый групповой чат — сделать чатом модерации?»."""
    dedup_key = ("chat", str(chat_id).lower())
    if _dedup_should_skip(dedup_key):
        logger.debug(
            "Discovery chat: дедуп, не шлём повторно",
            chat_id=str(chat_id),
        )
        return

    creator_block = ""
    if creator_huid is not None:
        profile = await fetch_user_profile(bot, creator_huid)
        creator_block = "\nСоздатель чата:\n" + _format_profile(profile) + "\n"

    body = (
        "🔔 Бота добавили в новый групповой чат.\n\n"
        f"Название: {chat_name or '—'}\n"
        f"chat_id: `{chat_id}`\n"
        f"{creator_block}\n"
        "Сделать этот чат чатом модерации конкурса?"
    )

    bubbles = BubbleMarkup()
    payload = {"chat_id": str(chat_id), "chat_name": chat_name or ""}
    bubbles.add_button(
        command="/admin_chat_approve",
        label="✅ Сделать чатом модерации",
        data=payload,
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_chat_reject",
        label="❌ Отклонить",
        data=payload,
        new_row=True,
    )

    await _send_to_admin(
        bot,
        body=body,
        bubbles=bubbles,
        purpose="discovery_moderation_chat",
    )


# =====================================================================
# Постдействия после одобрения админом
# =====================================================================


async def add_moderator_to_chat(bot: "Bot", huid: UUID) -> tuple[bool, str]:
    """Затащить модератора в текущий чат модерации.

    Если чат не настроен или бот не имеет права добавлять — возвращает
    ``(False, причина)``. Если HUID уже в чате — ``(True, "уже в чате")``.

    Returns:
        (ok, человекочитаемый статус для admin reply)
    """
    moderation_chat = access.get_moderation_chat_id()
    if moderation_chat is None:
        return False, "чат модерации не настроен"

    bot_id = resolve_bot_id(bot)
    if bot_id is None:
        return False, "не удалось определить bot_id"

    try:
        info = await bot.chat_info(bot_id=bot_id, chat_id=moderation_chat)
        already = any(
            getattr(m, "huid", None) == huid for m in (info.members or [])
        )
        if already:
            return True, "уже в чате модерации"
    except Exception as exc:
        logger.warning(
            "add_moderator_to_chat: не удалось получить chat_info",
            chat_id=str(moderation_chat),
            error=str(exc),
        )

    try:
        await bot.add_users_to_chat(
            bot_id=bot_id, chat_id=moderation_chat, huids=[huid]
        )
        logger.info(
            "Модератор добавлен в чат модерации",
            huid=str(huid),
            chat_id=str(moderation_chat),
        )
        return True, "добавлен в чат модерации"
    except Exception as exc:
        logger.warning(
            "add_users_to_chat упал — добавьте модератора вручную",
            huid=str(huid),
            chat_id=str(moderation_chat),
            error=str(exc),
        )
        return False, f"не удалось добавить в чат модерации ({exc})"


_WELCOME_MODERATOR_TEXT = (
    "👋 Тебя назначили модератором конкурса «Безопасные рисунки».\n\n"
    "Чтобы открыть меню модератора, введи команду /moderator.\n"
    "Команды по очереди и действиям — в подсказке /m_help."
)

_WELCOME_JURY_TEXT = (
    "👋 Тебя назначили членом жюри конкурса «Безопасные рисунки».\n\n"
    "Открой меню жюри командой /jury — там список твоих задач и "
    "прогресс по голосованию."
)


async def send_welcome_dm_to_moderator(bot: "Bot", huid: UUID) -> bool:
    """Послать модератору короткое приветственное DM. Возвращает успех."""
    return await _send_welcome_dm(
        bot, huid, _WELCOME_MODERATOR_TEXT, "welcome_moderator"
    )


async def send_welcome_dm_to_jury(bot: "Bot", huid: UUID) -> bool:
    """Послать судье короткое приветственное DM. Возвращает успех."""
    return await _send_welcome_dm(bot, huid, _WELCOME_JURY_TEXT, "welcome_jury")


async def _send_welcome_dm(
    bot: "Bot", huid: UUID, body: str, purpose: str
) -> bool:
    chat_id = await _resolve_user_chat_id(huid)
    if chat_id is None:
        logger.warning(
            "Welcome DM: у пользователя нет chat_id (не писал боту /start)",
            purpose=purpose,
            huid=str(huid),
        )
        return False
    bot_id = resolve_bot_id(bot)
    kwargs = {
        "chat_id": chat_id,
        "body": body,
        "wait_callback": False,
    }
    if bot_id is not None:
        kwargs["bot_id"] = bot_id
    try:
        await bot.send_message(**kwargs)
        logger.info(
            "Welcome DM отправлен", purpose=purpose, huid=str(huid)
        )
        return True
    except Exception:
        logger.exception(
            "Welcome DM не удался", purpose=purpose, huid=str(huid)
        )
        return False


__all__ = [
    "fetch_user_profile",
    "notify_admin_role_candidate",
    "notify_admin_moderation_chat_candidate",
    "add_moderator_to_chat",
    "send_welcome_dm_to_moderator",
    "send_welcome_dm_to_jury",
]
