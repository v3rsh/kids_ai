"""
Стаб сервиса режима приёма заявок (Wave 1 → ветка D / admin).

Переключение между ``files`` (основной) и ``links`` (резервный, §33.6)
происходит:
- вручную модератором: ``/intake_mode files|links``;
- автоматически при заполнении диска ≥ 95 % (§28.1, §33.6).

Состояние режима хранится в таблице ``app_settings`` (key=``intake_mode``,
value=``files``/``links``), чтобы переключение пережило рестарт
контейнера. Дефолт при первом старте — ``INTAKE_MODE_DEFAULT``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from database.models import IntakeMode


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка D (admin)"


async def get_intake_mode() -> "IntakeMode":
    """Текущий режим приёма (читается из app_settings, дефолт — из config)."""
    raise NotImplementedError(_STUB_MSG)


async def set_intake_mode(
    mode: "IntakeMode",
    *,
    by_huid: UUID,
    reason: str | None = None,
) -> None:
    """Установить режим приёма (по команде модератора или автопереключению).

    ``by_huid`` — кто переключил (модератор или системный UUID при
    автопереключении). ``reason`` опционально пишется в лог; в БД
    не хранится отдельно, история восстанавливается из логов.
    """
    raise NotImplementedError(_STUB_MSG)


async def maybe_auto_switch_to_links() -> bool:
    """Автопереключение в ``links`` при достижении DISK_BLOCK_PCT (§28.1).

    Возвращает True, если режим был сменён. Идемпотентно: если уже
    в ``links`` — ничего не делает. Триггерит уведомление в чат
    модерации и общую рассылку участникам (§33.6).
    """
    raise NotImplementedError(_STUB_MSG)


__all__ = ["get_intake_mode", "set_intake_mode", "maybe_auto_switch_to_links"]
