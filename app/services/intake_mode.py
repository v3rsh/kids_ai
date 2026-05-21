"""
Сервис управления режимом приёма заявок (ветка D1).

Поддерживает два режима (§33.6):
- ``FILES`` — основной: файлы загружаются на сервер бота;
- ``LINKS`` — резервный: родитель присылает ссылку на облако.

Переключение:
- вручную модератором (``/intake_mode files|links`` — Wave 2 / ветка B);
- администратором (``/intake_mode`` — Wave 2 / ветка D, см. ``handlers/admin.py``);
- автоматически при заполнении диска ≥ ``DISK_BLOCK_PCT`` (см.
  ``services.storage.check_and_alert_disk`` → ``maybe_auto_switch_to_links``).

Состояние хранится в таблице ``app_settings`` (key=``intake_mode``,
value=``files`` / ``links``), чтобы переключение пережило рестарт
контейнера. Дефолт при первом старте — ``INTAKE_MODE_DEFAULT`` (env).
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from loguru import logger
from sqlalchemy import select

from config import INTAKE_MODE_DEFAULT
from database.db import get_session
from database.models import AppSetting, IntakeMode

if TYPE_CHECKING:
    pass


INTAKE_MODE_KEY = "intake_mode"
"""Ключ в таблице ``app_settings`` для режима приёма (§33.6)."""

#: UUID-«отправитель» для авто-переключения (нужен в логах и аудит-метках).
#: Технический ноль-UUID, чтобы отличать system-action от модератора.
SYSTEM_HUID = UUID("00000000-0000-0000-0000-000000000000")


def _parse_default() -> IntakeMode:
    """Прочитать ``INTAKE_MODE_DEFAULT`` из конфига; неизвестное → FILES."""
    raw = (INTAKE_MODE_DEFAULT or "files").strip().lower()
    try:
        return IntakeMode(raw)
    except ValueError:
        logger.warning(
            "Неизвестный INTAKE_MODE_DEFAULT — fallback на FILES",
            value=raw,
        )
        return IntakeMode.FILES


async def get_intake_mode() -> IntakeMode:
    """Текущий режим приёма (читается из ``app_settings``).

    Если запись отсутствует — возвращает дефолт из конфига (``INTAKE_MODE_DEFAULT``).
    Чтение из БД делается одной короткой выборкой; кэширование не вводим
    специально — переключений мало и они переживают рестарт через БД.
    """
    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting.value).where(AppSetting.key == INTAKE_MODE_KEY)
        )
        row = result.first()
        if row is None or not row[0]:
            return _parse_default()
        try:
            return IntakeMode(row[0].strip().lower())
        except ValueError:
            logger.warning(
                "Неизвестное значение intake_mode в БД — fallback на дефолт",
                stored=row[0],
            )
            return _parse_default()


async def set_intake_mode(
    mode: IntakeMode,
    *,
    by_huid: UUID,
    reason: str | None = None,
) -> None:
    """UPSERT режима приёма в ``app_settings``.

    Args:
        mode: новый режим.
        by_huid: HUID того, кто переключил (модератор / админ / SYSTEM_HUID
            при автопереключении). Используется только в логах — в БД
            историю не пишем (§33.6 не требует, история восстанавливается
            из loguru-логов).
        reason: опциональная пояснительная строка для лога.
    """
    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == INTAKE_MODE_KEY)
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(AppSetting(key=INTAKE_MODE_KEY, value=mode.value))
        else:
            setting.value = mode.value
        await session.commit()

    logger.info(
        "Режим приёма заявок переключён",
        new_mode=mode.value,
        by_huid=str(by_huid),
        reason=reason or "",
    )


async def maybe_auto_switch_to_links(bot=None) -> bool:
    """Авто-переход в ``LINKS`` при заполнении диска ≥ ``DISK_BLOCK_PCT``.

    Идемпотентно: если уже в ``LINKS`` — возвращает False, без записи.
    При переключении отправляет уведомление в чат модерации (§28.1 +
    §33.6), если передан ``bot``.

    Returns:
        True, если режим был сменён; False — если переключать не нужно
        (порог не достигнут или уже ``LINKS``).
    """
    # Локальный импорт, чтобы избежать циклической зависимости с storage.
    from services.storage import should_block_intake

    if not should_block_intake():
        return False

    current = await get_intake_mode()
    if current is IntakeMode.LINKS:
        return False

    await set_intake_mode(
        IntakeMode.LINKS,
        by_huid=SYSTEM_HUID,
        reason="auto-switch on disk usage >= DISK_BLOCK_PCT",
    )

    if bot is not None:
        try:
            from services.notifications import (
                INTAKE_MODE_LINKS_NOTICE_TEMPLATE,
                _send_to_moderation_chat,
            )

            await _send_to_moderation_chat(
                bot,
                INTAKE_MODE_LINKS_NOTICE_TEMPLATE,
                purpose="auto_switch_to_links",
            )
        except Exception:
            logger.exception(
                "Не удалось отправить уведомление об автопереключении в LINKS"
            )

    return True


__all__ = [
    "INTAKE_MODE_KEY",
    "SYSTEM_HUID",
    "get_intake_mode",
    "set_intake_mode",
    "maybe_auto_switch_to_links",
]
