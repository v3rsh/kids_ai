"""
Сервис проверки ролей и доступа к командам.

Источники истины:
- ``config.MODERATOR_HUIDS`` (§5.2, §27.2);
- ``config.JURY_HUIDS`` (§5.4, §35.4);
- ``config.ADMIN_HUIDS`` — техническая роль (разработчик/тех. админ).

Списки HUID прошиваются на старте бота из переменных окружения
(``MODERATOR_HUIDS``, ``JURY_HUIDS``, ``ADMIN_HUID``). Справочники в
БД (``moderators``, ``jury_members``) заполняются Wave 2 / D из этих
же списков — но проверка доступа всегда идёт по конфигу, чтобы:
- не делать запрос в PostgreSQL на каждый клик кнопки модератора;
- избежать рекурсивных DB-зависимостей в FSM-middleware.

Сравнение делается по нормализованному строковому представлению UUID
(``str(uuid)`` в lowercase).
"""
from __future__ import annotations

import functools
from typing import Awaitable, Callable
from uuid import UUID

from loguru import logger

try:  # config может быть недоступен в unit-тестах без env
    from config import ADMIN_HUIDS, JURY_HUIDS, MODERATOR_HUIDS
except ImportError:  # pragma: no cover - safety net
    ADMIN_HUIDS = []
    JURY_HUIDS = []
    MODERATOR_HUIDS = []


# Тип хендлера pybotx (без жёсткого импорта Bot/IncomingMessage —
# чтобы не тащить pybotx в unit-тесты этого модуля).
_HandlerFunc = Callable[..., Awaitable[None]]


def _normalize(huid: UUID | str | None) -> str:
    """Привести HUID к ``str(uuid)`` в lowercase для сравнений."""
    if huid is None:
        return ""
    if isinstance(huid, UUID):
        return str(huid).lower()
    return str(huid).strip().lower()


def _huid_in(huid: UUID | str | None, allowlist: list[str]) -> bool:
    if not allowlist:
        return False
    target = _normalize(huid)
    if not target:
        return False
    return any(_normalize(item) == target for item in allowlist)


def is_moderator(huid: UUID | str | None) -> bool:
    """True, если HUID есть в ``MODERATOR_HUIDS`` (§5.2, §27.2)."""
    return _huid_in(huid, MODERATOR_HUIDS)


def is_jury(huid: UUID | str | None) -> bool:
    """True, если HUID есть в ``JURY_HUIDS`` (§5.4, §35.4)."""
    return _huid_in(huid, JURY_HUIDS)


def is_admin(huid: UUID | str | None) -> bool:
    """True, если HUID есть в ``ADMIN_HUIDS`` (разработчик / тех. админ).

    Не путать с модератором: ``is_admin`` — это техническая роль для
    диагностических команд (``/disk``, аварийные операции). Может
    пересекаться с модератором, может не пересекаться.
    """
    return _huid_in(huid, ADMIN_HUIDS)


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
                await bot.answer_message(deny_message, wait_callback=False)
                return None
            return await handler(message, bot, *args, **kwargs)

        return wrapper

    return decorator


moderator_only = _make_role_decorator(
    is_moderator,
    "Команда доступна только модераторам.",
    "moderator",
)
"""Защита хендлера от не-модераторов (§27.2)."""

jury_only = _make_role_decorator(
    is_jury,
    "Команда доступна только членам жюри.",
    "jury",
)
"""Защита хендлера от не-жюри (§27.4)."""

admin_only = _make_role_decorator(
    is_admin,
    "Команда доступна только администраторам.",
    "admin",
)
"""Защита хендлера от не-админов (разработчик / тех. админ)."""


# =====================================================================
# Синхронизация справочников Moderator/JuryMember (Wave 3, lifespan)
# =====================================================================


async def sync_role_directories_from_config(
    session=None,
) -> tuple[int, int]:
    """Идемпотентный upsert ``MODERATOR_HUIDS`` / ``JURY_HUIDS`` в БД.

    Заполняет таблицы ``moderators`` и ``jury_members`` из переменных
    окружения. Помечает ``is_active=False`` для HUID, которые есть
    в БД, но отсутствуют в конфиге (мягкое удаление: история голосов
    и комментариев сохраняется, новых задач/команд они уже не получат).

    Вызывается из ``app/main.py`` (lifespan) на каждом старте, чтобы
    изменение списков в `.env` сразу отражалось в БД без миграций.

    Returns:
        (mods_active, jury_active) — итоговое число активных записей.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from database.models import JuryMember, Moderator

    async def _do(s) -> tuple[int, int]:
        mod_uuids: set[UUID] = set()
        for raw in MODERATOR_HUIDS:
            try:
                mod_uuids.add(UUID(str(raw).strip()))
            except (TypeError, ValueError):
                logger.warning(
                    "MODERATOR_HUIDS: пропускаю невалидный UUID",
                    value=raw,
                )

        jury_uuids: set[UUID] = set()
        for raw in JURY_HUIDS:
            try:
                jury_uuids.add(UUID(str(raw).strip()))
            except (TypeError, ValueError):
                logger.warning(
                    "JURY_HUIDS: пропускаю невалидный UUID",
                    value=raw,
                )

        if mod_uuids:
            stmt = pg_insert(Moderator).values(
                [
                    {"huid": h, "full_name": "", "is_active": True}
                    for h in sorted(mod_uuids, key=str)
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[Moderator.huid],
                set_={"is_active": True},
            )
            await s.execute(stmt)

        if jury_uuids:
            stmt = pg_insert(JuryMember).values(
                [
                    {"huid": h, "full_name": "", "is_active": True}
                    for h in sorted(jury_uuids, key=str)
                ]
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[JuryMember.huid],
                set_={"is_active": True},
            )
            await s.execute(stmt)

        existing_mods = (await s.execute(select(Moderator.huid))).scalars().all()
        for h in existing_mods:
            if h not in mod_uuids:
                await s.execute(
                    Moderator.__table__
                    .update()
                    .where(Moderator.huid == h)
                    .values(is_active=False)
                )

        existing_jury = (await s.execute(select(JuryMember.huid))).scalars().all()
        for h in existing_jury:
            if h not in jury_uuids:
                await s.execute(
                    JuryMember.__table__
                    .update()
                    .where(JuryMember.huid == h)
                    .values(is_active=False)
                )

        await s.commit()

        return len(mod_uuids), len(jury_uuids)

    if session is not None:
        result = await _do(session)
    else:
        from database.db import get_session

        async with get_session()() as s:
            result = await _do(s)

    logger.info(
        "Справочники ролей синхронизированы из конфига",
        moderators=result[0],
        jury_members=result[1],
    )
    return result


__all__ = [
    "is_moderator",
    "is_jury",
    "is_admin",
    "moderator_only",
    "jury_only",
    "admin_only",
    "sync_role_directories_from_config",
]
