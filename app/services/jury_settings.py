"""
Runtime-настройки логики жюри.

Хранятся в таблице ``app_settings`` (как и режим приёма), чтобы
изменения переживали рестарт контейнера. Поверх БД — простой
in-process кэш с инвалидацией на ``set_*`` (значения читаются в
``services.jury.close_round`` и не должны порождать лишний SELECT
на каждый клик судьи).

Ключи в ``app_settings``:

- ``jury_max_round`` — int как строка (1..50). Порог раунда, начиная
  с которого срабатывает автоматический жребий, если включён
  ``jury_auto_lot``.
- ``jury_auto_lot`` — ``on`` / ``off``. ``off`` отключает жребий
  совсем: раунды продолжаются, пока ничья не разрешится сама.

Дефолты при отсутствии записи — ``JURY_MAX_ROUND_DEFAULT`` и
``JURY_AUTO_LOT_DEFAULT`` из ``config``.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from config import JURY_AUTO_LOT_DEFAULT, JURY_MAX_ROUND_DEFAULT
from database.db import get_session
from database.models import AppSetting

JURY_MAX_ROUND_KEY = "jury_max_round"
JURY_AUTO_LOT_KEY = "jury_auto_lot"
SHORTLIST_ANNOUNCED_KEY = "shortlist_announced"

#: Жёсткий верхний предел значения, которое можно записать через UI.
#: Защищает от опечаток вроде «500». 50 раундов — заведомо больше,
#: чем понадобится в любой реалистичной конфигурации.
JURY_MAX_ROUND_HARD_LIMIT = 50

_cache: dict[str, object] = {}


def _invalidate() -> None:
    _cache.clear()


async def get_jury_max_round() -> int:
    """Текущий порог раундов жюри (читается из ``app_settings``).

    При отсутствии записи или мусоре в БД — возврат
    ``JURY_MAX_ROUND_DEFAULT``.
    """
    cached = _cache.get(JURY_MAX_ROUND_KEY)
    if isinstance(cached, int):
        return cached

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting.value).where(AppSetting.key == JURY_MAX_ROUND_KEY)
        )
        row = result.first()

    value = JURY_MAX_ROUND_DEFAULT
    if row is not None and row[0]:
        try:
            parsed = int(row[0].strip())
            if 1 <= parsed <= JURY_MAX_ROUND_HARD_LIMIT:
                value = parsed
            else:
                logger.warning(
                    "jury_max_round вне допустимого диапазона — fallback на дефолт",
                    stored=row[0],
                    default=JURY_MAX_ROUND_DEFAULT,
                )
        except ValueError:
            logger.warning(
                "jury_max_round не парсится как int — fallback на дефолт",
                stored=row[0],
                default=JURY_MAX_ROUND_DEFAULT,
            )

    _cache[JURY_MAX_ROUND_KEY] = value
    return value


async def get_jury_auto_lot() -> bool:
    """Текущее состояние тумблера автожребия.

    ``True`` — после ``max_round`` ничья разрешается жребием;
    ``False`` — раунды продолжаются неограниченно.
    """
    cached = _cache.get(JURY_AUTO_LOT_KEY)
    if isinstance(cached, bool):
        return cached

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting.value).where(AppSetting.key == JURY_AUTO_LOT_KEY)
        )
        row = result.first()

    if row is None or not row[0]:
        value = bool(JURY_AUTO_LOT_DEFAULT)
    else:
        normalized = row[0].strip().lower()
        if normalized in ("on", "true", "1", "yes"):
            value = True
        elif normalized in ("off", "false", "0", "no"):
            value = False
        else:
            logger.warning(
                "jury_auto_lot не распознан — fallback на дефолт",
                stored=row[0],
                default=JURY_AUTO_LOT_DEFAULT,
            )
            value = bool(JURY_AUTO_LOT_DEFAULT)

    _cache[JURY_AUTO_LOT_KEY] = value
    return value


async def set_jury_max_round(value: int, *, by_huid: Optional[UUID] = None) -> None:
    """UPSERT порога раундов в ``app_settings``.

    Raises:
        ValueError: ``value`` вне диапазона 1..``JURY_MAX_ROUND_HARD_LIMIT``.
    """
    if not isinstance(value, int) or value < 1 or value > JURY_MAX_ROUND_HARD_LIMIT:
        raise ValueError(
            f"Порог раундов должен быть целым числом 1..{JURY_MAX_ROUND_HARD_LIMIT}"
        )

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == JURY_MAX_ROUND_KEY)
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(AppSetting(key=JURY_MAX_ROUND_KEY, value=str(value)))
        else:
            setting.value = str(value)
        await session.commit()

    _invalidate()
    logger.info(
        "Порог раундов жюри изменён",
        new_value=value,
        by_huid=str(by_huid) if by_huid is not None else "",
    )


async def set_jury_auto_lot(enabled: bool, *, by_huid: Optional[UUID] = None) -> None:
    """UPSERT тумблера автожребия в ``app_settings``."""
    stored_value = "on" if enabled else "off"

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == JURY_AUTO_LOT_KEY)
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(AppSetting(key=JURY_AUTO_LOT_KEY, value=stored_value))
        else:
            setting.value = stored_value
        await session.commit()

    _invalidate()
    logger.info(
        "Тумблер автожребия жюри переключён",
        new_value=stored_value,
        by_huid=str(by_huid) if by_huid is not None else "",
    )


async def get_shortlist_announced() -> bool:
    """True, если событие ``shortlist_ready`` уже отправлено в чат модерации."""
    cached = _cache.get(SHORTLIST_ANNOUNCED_KEY)
    if isinstance(cached, bool):
        return cached

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting.value).where(AppSetting.key == SHORTLIST_ANNOUNCED_KEY)
        )
        row = result.first()

    value = False
    if row is not None and row[0]:
        value = row[0].strip().lower() in ("true", "1", "yes", "on")

    _cache[SHORTLIST_ANNOUNCED_KEY] = value
    return value


async def set_shortlist_announced(*, announced: bool, by_huid: Optional[UUID] = None) -> None:
    """UPSERT флага ``shortlist_announced`` в ``app_settings``."""
    stored_value = "true" if announced else "false"

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == SHORTLIST_ANNOUNCED_KEY)
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(AppSetting(key=SHORTLIST_ANNOUNCED_KEY, value=stored_value))
        else:
            setting.value = stored_value
        await session.commit()

    _invalidate()
    logger.info(
        "shortlist_announced изменён",
        announced=announced,
        by_huid=str(by_huid) if by_huid is not None else "",
    )


async def reset_shortlist_announced(*, by_huid: Optional[UUID] = None) -> None:
    """Сбросить флаг для повторной отправки ``shortlist_ready``."""
    await set_shortlist_announced(announced=False, by_huid=by_huid)


def reset_cache() -> None:
    """Сбросить кэш (для тестов и при ручной правке БД)."""
    _invalidate()


__all__ = [
    "JURY_MAX_ROUND_KEY",
    "JURY_AUTO_LOT_KEY",
    "SHORTLIST_ANNOUNCED_KEY",
    "JURY_MAX_ROUND_HARD_LIMIT",
    "get_jury_max_round",
    "get_jury_auto_lot",
    "get_shortlist_announced",
    "set_shortlist_announced",
    "reset_shortlist_announced",
    "set_jury_max_round",
    "set_jury_auto_lot",
    "reset_cache",
]
