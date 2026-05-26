"""
Сервис проверки ролей, доступа и in-memory кэш ролей конкурса
«Безопасные рисунки».

Источник правды:
- БД (таблицы ``moderators``, ``jury_members``, ``app_settings``) для
  списков модераторов, жюри и UUID чата модерации.
- ``ADMIN_HUIDS`` (env) — техническая роль разработчика. Не управляется
  через бот; меняется только через перезапуск с новой переменной.

Рантайм:
- ``is_moderator()`` / ``is_jury()`` / ``get_moderation_chat_id()``
  читают **in-memory кэш**, без обращения в PostgreSQL. Это важно для
  hot path (chat-gate middleware дёргает кэш на каждое входящее).
- Кэш перезагружается через ``reload_access_cache(session)`` на старте
  бота и после каждой операции add/revoke/set.

Bootstrap из env: **отключён**. Модераторы / жюри / чат модерации
управляются только через discovery-флоу — никаких env-переменных для
этих ролей больше нет (см. ``handlers.admin_roles`` и
``services.discovery``). ``seed_access_from_config_if_empty`` оставлен
как no-op ради обратной совместимости вызова из ``main.py`` lifespan.

Сравнение UUID идёт через нормализованную строку (``str(uuid).lower()``).
"""
from __future__ import annotations

import asyncio
import functools
from typing import Awaitable, Callable
from uuid import UUID

from loguru import logger

try:  # config может быть недоступен в unit-тестах без env
    from config import ADMIN_HUIDS
except ImportError:  # pragma: no cover - safety net
    ADMIN_HUIDS = []


# Тип хендлера pybotx (без жёсткого импорта Bot/IncomingMessage —
# чтобы не тащить pybotx в unit-тесты этого модуля).
_HandlerFunc = Callable[..., Awaitable[None]]


# =====================================================================
# In-memory кэш
# =====================================================================

_MODERATION_CHAT_SETTING_KEY = "moderation_chat_id"

_moderator_huids: set[str] = set()
_jury_huids: set[str] = set()
_moderation_chat_id: UUID | None = None
_cache_lock = asyncio.Lock()


def _normalize(huid: UUID | str | None) -> str:
    """Привести HUID к ``str(uuid)`` в lowercase для сравнений."""
    if huid is None:
        return ""
    if isinstance(huid, UUID):
        return str(huid).lower()
    return str(huid).strip().lower()


def _huid_in(huid: UUID | str | None, allowlist: set[str] | list[str]) -> bool:
    if not allowlist:
        return False
    target = _normalize(huid)
    if not target:
        return False
    if isinstance(allowlist, set):
        return target in allowlist
    return any(_normalize(item) == target for item in allowlist)


def is_moderator(huid: UUID | str | None) -> bool:
    """True, если HUID — активный модератор (lookup по кэшу)."""
    return _huid_in(huid, _moderator_huids)


def is_jury(huid: UUID | str | None) -> bool:
    """True, если HUID — активный судья (lookup по кэшу)."""
    return _huid_in(huid, _jury_huids)


def is_admin(huid: UUID | str | None) -> bool:
    """True, если HUID есть в ``ADMIN_HUIDS`` (разработчик / тех. админ).

    Не путать с модератором: ``is_admin`` — это техническая роль для
    диагностических команд (``/disk``, аварийные операции). Источник —
    env, в БД не хранится.
    """
    return _huid_in(huid, ADMIN_HUIDS)


def get_moderation_chat_id() -> UUID | None:
    """Текущий UUID чата модерации (из кэша) или None."""
    return _moderation_chat_id


def get_moderator_huids() -> set[str]:
    """Снапшот множества HUID активных модераторов (только для диагностики)."""
    return set(_moderator_huids)


def get_jury_huids() -> set[str]:
    """Снапшот множества HUID активных судей (только для диагностики)."""
    return set(_jury_huids)


# =====================================================================
# Перезагрузка кэша
# =====================================================================


async def reload_access_cache(session=None) -> None:
    """Перечитать таблицы ``moderators`` / ``jury_members`` / ``app_settings``
    и атомарно подменить in-memory кэш.

    Вызывается:
    - на старте бота (lifespan, после seed);
    - после каждой операции add/revoke/set_moderation_chat.

    Args:
        session: AsyncSession. Если None — открывает свою.
    """
    from sqlalchemy import select

    from database.models import AppSetting, JuryMember, Moderator

    async def _do(s) -> tuple[set[str], set[str], UUID | None]:
        mod_rows = (
            await s.execute(
                select(Moderator.huid).where(Moderator.is_active.is_(True))
            )
        ).scalars().all()
        jury_rows = (
            await s.execute(
                select(JuryMember.huid).where(JuryMember.is_active.is_(True))
            )
        ).scalars().all()

        mods = {_normalize(h) for h in mod_rows}
        jurys = {_normalize(h) for h in jury_rows}

        setting = (
            await s.execute(
                select(AppSetting.value).where(
                    AppSetting.key == _MODERATION_CHAT_SETTING_KEY
                )
            )
        ).scalar_one_or_none()
        chat_id: UUID | None = None
        if setting:
            try:
                chat_id = UUID(setting.strip())
            except (TypeError, ValueError):
                logger.warning(
                    "app_settings.moderation_chat_id содержит невалидный UUID",
                    value=setting,
                )
        return mods, jurys, chat_id

    if session is not None:
        mods, jurys, chat_id = await _do(session)
    else:
        from database.db import get_session

        async with get_session()() as s:
            mods, jurys, chat_id = await _do(s)

    global _moderator_huids, _jury_huids, _moderation_chat_id
    async with _cache_lock:
        _moderator_huids = mods
        _jury_huids = jurys
        _moderation_chat_id = chat_id

    logger.info(
        "Кэш ролей перезагружен",
        moderators=len(mods),
        jury=len(jurys),
        moderation_chat_id=str(chat_id) if chat_id else None,
    )


# =====================================================================
# Декораторы для хендлеров pybotx
# =====================================================================


def _make_role_decorator(
    check: Callable[[UUID | str | None], bool],
    deny_message: str,
    role_log: str,
) -> Callable[[_HandlerFunc], _HandlerFunc]:
    """Фабрика декораторов на основе функции проверки роли."""

    def decorator(handler: _HandlerFunc) -> _HandlerFunc:
        @functools.wraps(handler)
        async def wrapper(message, bot, *args, **kwargs):
            huid = getattr(getattr(message, "sender", None), "huid", None)
            if not check(huid):
                logger.info(
                    "Отказ доступа: пользователь не в роли",
                    role=role_log,
                    huid=str(huid) if huid else None,
                )
                from keyboards import main_menu_bubbles

                kwargs = {"wait_callback": False}
                menu = main_menu_bubbles(huid=huid)
                if menu is not None:
                    kwargs["bubbles"] = menu
                await bot.answer_message(deny_message, **kwargs)
                return None
            return await handler(message, bot, *args, **kwargs)

        return wrapper

    return decorator


moderator_only = _make_role_decorator(
    is_moderator,
    "Команда доступна только модераторам.",
    "moderator",
)
"""Защита хендлера от не-модераторов."""

jury_only = _make_role_decorator(
    is_jury,
    "Команда доступна только членам жюри.",
    "jury",
)
"""Защита хендлера от не-жюри."""

admin_only = _make_role_decorator(
    is_admin,
    "Команда доступна только администраторам.",
    "admin",
)
"""Защита хендлера от не-админов (разработчик / тех. админ)."""


# =====================================================================
# Управление списками (add / revoke / set_moderation_chat)
# =====================================================================


async def add_moderator(
    huid: UUID,
    *,
    full_name: str = "",
    username: str | None = None,
    by_huid: UUID | None = None,
    session=None,
) -> bool:
    """Добавить модератора (upsert + reactivate). Возвращает True, если
    запись новая или была деактивирована, False — если уже активна.

    После записи перечитывает кэш.
    """
    return await _upsert_role(
        "moderator",
        huid=huid,
        full_name=full_name,
        username=username,
        by_huid=by_huid,
        session=session,
    )


async def add_jury_member(
    huid: UUID,
    *,
    full_name: str = "",
    username: str | None = None,
    by_huid: UUID | None = None,
    session=None,
) -> bool:
    """Добавить судью (upsert + reactivate). Возвращает True, если
    запись новая или была деактивирована.

    После записи перечитывает кэш.
    """
    return await _upsert_role(
        "jury",
        huid=huid,
        full_name=full_name,
        username=username,
        by_huid=by_huid,
        session=session,
    )


async def revoke_moderator(huid: UUID, *, session=None) -> bool:
    """Деактивировать модератора (is_active=False, история сохраняется)."""
    return await _deactivate_role("moderator", huid=huid, session=session)


async def revoke_jury(huid: UUID, *, session=None) -> bool:
    """Деактивировать судью (is_active=False, история голосов сохраняется)."""
    return await _deactivate_role("jury", huid=huid, session=session)


async def set_moderation_chat(
    chat_id: UUID,
    *,
    by_huid: UUID | None = None,
    session=None,
) -> None:
    """Записать ``app_settings.moderation_chat_id`` и перечитать кэш."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from database.models import AppSetting

    async def _do(s) -> None:
        stmt = pg_insert(AppSetting).values(
            key=_MODERATION_CHAT_SETTING_KEY, value=str(chat_id)
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": str(chat_id)},
        )
        await s.execute(stmt)
        await s.commit()

    if session is not None:
        await _do(session)
        await reload_access_cache(session)
    else:
        from database.db import get_session

        async with get_session()() as s:
            await _do(s)
            await reload_access_cache(s)

    logger.info(
        "Назначен чат модерации",
        chat_id=str(chat_id),
        by_huid=str(by_huid) if by_huid else None,
    )


async def _upsert_role(
    role: str,
    *,
    huid: UUID,
    full_name: str,
    username: str | None,
    by_huid: UUID | None,
    session,
) -> bool:
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from database.models import JuryMember, Moderator

    model = Moderator if role == "moderator" else JuryMember

    async def _do(s) -> bool:
        existing = (
            await s.execute(select(model).where(model.huid == huid))
        ).scalar_one_or_none()
        was_inactive_or_new = existing is None or not existing.is_active

        values = {
            "huid": huid,
            "full_name": full_name or "",
            "username": username,
            "added_by_huid": by_huid,
            "is_active": True,
        }
        stmt = pg_insert(model).values(values)
        update_set = {
            "is_active": True,
            "added_by_huid": by_huid,
        }
        if full_name:
            update_set["full_name"] = full_name
        if username is not None:
            update_set["username"] = username
        stmt = stmt.on_conflict_do_update(
            index_elements=[model.huid], set_=update_set
        )
        await s.execute(stmt)
        await s.commit()
        return was_inactive_or_new

    if session is not None:
        changed = await _do(session)
        await reload_access_cache(session)
    else:
        from database.db import get_session

        async with get_session()() as s:
            changed = await _do(s)
            await reload_access_cache(s)

    logger.info(
        "Назначена роль",
        role=role,
        huid=str(huid),
        by_huid=str(by_huid) if by_huid else None,
        changed=changed,
    )
    return changed


async def _deactivate_role(role: str, *, huid: UUID, session) -> bool:
    from sqlalchemy import update

    from database.models import JuryMember, Moderator

    model = Moderator if role == "moderator" else JuryMember

    async def _do(s) -> bool:
        result = await s.execute(
            update(model)
            .where(model.huid == huid, model.is_active.is_(True))
            .values(is_active=False)
        )
        await s.commit()
        return (result.rowcount or 0) > 0

    if session is not None:
        changed = await _do(session)
        await reload_access_cache(session)
    else:
        from database.db import get_session

        async with get_session()() as s:
            changed = await _do(s)
            await reload_access_cache(s)

    logger.info("Отозвана роль", role=role, huid=str(huid), changed=changed)
    return changed


# =====================================================================
# Bootstrap из env (отключён — управление через discovery)
# =====================================================================


async def seed_access_from_config_if_empty(
    session=None,
) -> tuple[int, int, bool]:
    """No-op. Env-переменные MODERATOR_HUIDS / JURY_HUIDS / MODERATION_CHAT_ID
    больше не используются — модераторы, члены жюри и чат модерации
    назначаются только через discovery-флоу:

    - ``/moderator`` / ``/jury`` от не-роли → карточка админу с кнопками
      «Назначить / Отклонить» (``handlers.admin_roles``);
    - добавление бота в групповой чат → карточка админу с кнопкой
      «Сделать чатом модерации».

    Функция оставлена ради обратной совместимости вызова из ``main.py``
    lifespan: возвращает нули и логирует факт вызова.
    """
    del session  # параметр оставлен ради сигнатуры
    logger.info(
        "seed_access_from_config_if_empty: env-seed отключён, "
        "роли и чат модерации управляются через discovery (см. /moderator, /jury)",
    )
    return (0, 0, False)


# =====================================================================
# Сброс moderation_chat_id (валидация на старте, ручной reset)
# =====================================================================


async def clear_moderation_chat(*, session=None) -> bool:
    """Снять текущий ``moderation_chat_id`` (удалить из ``app_settings``).

    Используется, когда `bot.chat_info(moderation_chat_id)` на старте
    показывает, что бот больше не участник чата (или чат не существует),
    либо когда админ явно сбрасывает настройку. После сброса
    ``get_moderation_chat_id()`` начинает возвращать ``None``, а
    ``notify_moderation_chat_*`` тихо no-op'ает с WARNING.

    Returns:
        True, если запись была удалена; False, если её и так не было.
    """
    from sqlalchemy import delete

    from database.models import AppSetting

    async def _do(s) -> bool:
        result = await s.execute(
            delete(AppSetting).where(
                AppSetting.key == _MODERATION_CHAT_SETTING_KEY
            )
        )
        await s.commit()
        return (result.rowcount or 0) > 0

    if session is not None:
        changed = await _do(session)
        await reload_access_cache(session)
    else:
        from database.db import get_session

        async with get_session()() as s:
            changed = await _do(s)
            await reload_access_cache(s)

    logger.info("Сброшен chat модерации", changed=changed)
    return changed


__all__ = [
    # Проверки роли (sync, lookup в кэше)
    "is_moderator",
    "is_jury",
    "is_admin",
    "get_moderation_chat_id",
    "get_moderator_huids",
    "get_jury_huids",
    # Декораторы
    "moderator_only",
    "jury_only",
    "admin_only",
    # Управление списками
    "add_moderator",
    "add_jury_member",
    "revoke_moderator",
    "revoke_jury",
    "set_moderation_chat",
    "clear_moderation_chat",
    # Lifespan
    "reload_access_cache",
    "seed_access_from_config_if_empty",
]
