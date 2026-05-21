"""
Стаб сервиса пулов жюри (Wave 1 → ветка C / jury).

Пул = пара «трек × возрастная категория». Всего 12 пулов
(3 трека × 4 категории, §35.1). По умолчанию все судьи во всех
12 пулах; конфиг ``JURY_POOLS_CONFIG`` сужает участие (§35.6).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import Application, JuryMember
    from utils.contracts import PoolKey


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка C (jury)"


def all_pools() -> list["PoolKey"]:
    """Все 12 пулов конкурса в стабильном порядке.

    Порядок: трек × возрастная категория (cross-product). Используется
    в `/jury_state`, генерации шорт-листа и при открытии раундов
    одновременно во всех пулах.
    """
    raise NotImplementedError(_STUB_MSG)


async def get_pool_applications(pool: "PoolKey") -> list["Application"]:
    """Все заявки пула, допущенные модерацией (``moderation_status = ДОПУЩЕНО``).

    Сортировка ``(created_at ASC, id ASC)`` — единый порядок работ
    для жюри (Wave 0, §35.3).
    """
    raise NotImplementedError(_STUB_MSG)


async def get_jury_for_pool(pool: "PoolKey") -> list["JuryMember"]:
    """Назначенные на пул судьи.

    Если в ``JuryPoolAssignment`` нет записей — fallback на «все
    активные ``JuryMember``» (поведение по умолчанию из §35.6).
    """
    raise NotImplementedError(_STUB_MSG)


__all__ = ["all_pools", "get_pool_applications", "get_jury_for_pool"]
