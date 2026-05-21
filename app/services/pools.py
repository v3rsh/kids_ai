"""
Сервис пулов жюри (Wave 2 / ветка C).

Пул = пара «трек × возрастная категория». Всего 12 пулов
(3 трека × 4 категории, §35.1). По умолчанию все судьи во всех
12 пулах; конфиг ``JURY_POOLS_CONFIG`` сужает участие (§35.6).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    AgeCategory,
    Application,
    JuryMember,
    JuryPoolAssignment,
    ModerationStatus,
    Track,
)
from utils.contracts import PoolKey

if TYPE_CHECKING:  # pragma: no cover
    pass


# =====================================================================
# Базовые операции с пулами
# =====================================================================


def all_pools() -> list[PoolKey]:
    """Все 12 пулов конкурса в стабильном порядке (§35.1).

    Порядок: внешний цикл — ``Track`` (в порядке определения в enum),
    внутренний — ``AgeCategory`` (в порядке определения). Этот же
    порядок используется в `/jury_state`, при генерации шорт-листа и
    при одновременном открытии раундов во всех пулах.
    """
    return [
        PoolKey(track=track, age_category=age)
        for track in Track
        for age in AgeCategory
    ]


async def get_pool_applications(
    pool: PoolKey,
    *,
    session: AsyncSession,
    status_filter: Optional[ModerationStatus] = ModerationStatus.DOPUSHCHENO,
) -> list[Application]:
    """Заявки пула (§35.1, §35.3).

    По умолчанию возвращает только заявки со статусом модерации
    ``ДОПУЩЕНО`` (`status_filter=DOPUSHCHENO`) — именно они уходят
    в раунд 1 жюри. ``status_filter=None`` — все заявки пула без
    фильтра (используется в шорт-листе для проставления
    «не оценивалась» работ, выпавших на модерации).

    Сортировка ``(created_at ASC, id ASC)`` — единый порядок работ
    для всех судей (Wave 0, §35.3).
    """
    stmt = select(Application).where(
        Application.track == pool.track,
        Application.age_category == pool.age_category,
    )
    if status_filter is not None:
        stmt = stmt.where(Application.moderation_status == status_filter)
    stmt = stmt.order_by(Application.created_at.asc(), Application.id.asc())

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_jury_for_pool(
    pool: PoolKey,
    *,
    session: AsyncSession,
) -> list[JuryMember]:
    """Назначенные на пул судьи (§35.6).

    Если в ``JuryPoolAssignment`` есть хотя бы одна запись для данного
    пула — возвращает только тех ``JuryMember`` (активных), что в этом
    списке. Если назначений нет — fallback на «все активные
    ``JuryMember``» (поведение по умолчанию из §35.6).

    Один запрос; N+1 не возникает (см. `performance.mdc`).
    """
    assigned_stmt = (
        select(JuryMember)
        .join(
            JuryPoolAssignment,
            JuryPoolAssignment.jury_huid == JuryMember.huid,
        )
        .where(
            JuryPoolAssignment.track == pool.track,
            JuryPoolAssignment.age_category == pool.age_category,
            JuryMember.is_active.is_(True),
        )
        .order_by(JuryMember.huid)
    )
    result = await session.execute(assigned_stmt)
    assigned = list(result.scalars().all())
    if assigned:
        return assigned

    fallback_stmt = (
        select(JuryMember)
        .where(JuryMember.is_active.is_(True))
        .order_by(JuryMember.huid)
    )
    result = await session.execute(fallback_stmt)
    return list(result.scalars().all())


# =====================================================================
# Синхронизация конфига JURY_POOLS_CONFIG
# =====================================================================


def _parse_pools_config(raw: str) -> list[tuple[UUID, Track, AgeCategory]]:
    """Разобрать JSON-конфиг JURY_POOLS_CONFIG.

    Формат (выбран в Wave 2 / C, фиксируется в этом docstring):

        [
            {
                "huid": "11111111-1111-1111-1111-111111111111",
                "pools": [
                    {"track": "TRADITIONAL", "age_category": "AGE_4_6"},
                    {"track": "AI",          "age_category": "AGE_7_10"}
                ]
            },
            {
                "huid": "22222222-2222-2222-2222-222222222222",
                "pools": "all"
            }
        ]

    - ``huid`` — UUID члена жюри (должен совпадать с записью
      в ``jury_members``);
    - ``pools`` — либо список объектов с ключами ``track`` /
      ``age_category`` (значения — ``Enum.name`` UPPER_SNAKE_CASE,
      см. ``Track``/``AgeCategory``), либо строка ``"all"`` —
      сокращение для «все 12 пулов»;
    - судьи, отсутствующие в JSON, **не получают** ни одной записи
      в ``JuryPoolAssignment``, а значит для них работает fallback
      из §35.6 («все 12 пулов»). Это соответствует поведению по
      умолчанию.

    Пустая строка / ``None`` / отсутствие конфига → пустой результат
    (никаких записей, все судьи во всех пулах).

    Возвращает плоский список троек ``(huid, track, age)``,
    готовый к upsert'у в БД.

    Raises:
        ValueError — конфиг невалидный JSON, или содержит
        неизвестные значения трека / возрастной категории,
        или дубликаты (один и тот же ``(huid, track, age)``).
    """
    if not raw or not raw.strip():
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JURY_POOLS_CONFIG: невалидный JSON — {exc}") from exc

    if not isinstance(parsed, list):
        raise ValueError(
            "JURY_POOLS_CONFIG: ожидается список объектов в корне"
        )

    triples: list[tuple[UUID, Track, AgeCategory]] = []
    seen: set[tuple[UUID, Track, AgeCategory]] = set()

    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError(
                f"JURY_POOLS_CONFIG: каждый элемент — объект, получено {type(entry).__name__}"
            )
        huid_raw = entry.get("huid")
        if not huid_raw:
            raise ValueError("JURY_POOLS_CONFIG: пропущено поле 'huid' у элемента")
        try:
            huid = UUID(str(huid_raw))
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"JURY_POOLS_CONFIG: невалидный UUID 'huid'={huid_raw!r}"
            ) from exc

        pools_field = entry.get("pools", "all")
        if pools_field == "all":
            pool_pairs: list[tuple[Track, AgeCategory]] = [
                (t, a) for t in Track for a in AgeCategory
            ]
        elif isinstance(pools_field, list):
            pool_pairs = []
            for p in pools_field:
                if not isinstance(p, dict):
                    raise ValueError(
                        "JURY_POOLS_CONFIG: элемент 'pools' — объект {track, age_category}"
                    )
                track_name = p.get("track")
                age_name = p.get("age_category")
                if track_name not in Track.__members__:
                    raise ValueError(
                        f"JURY_POOLS_CONFIG: неизвестный track={track_name!r}; "
                        f"допустимые: {sorted(Track.__members__)}"
                    )
                if age_name not in AgeCategory.__members__:
                    raise ValueError(
                        f"JURY_POOLS_CONFIG: неизвестная age_category={age_name!r}; "
                        f"допустимые: {sorted(AgeCategory.__members__)}"
                    )
                pool_pairs.append(
                    (Track[track_name], AgeCategory[age_name])
                )
        else:
            raise ValueError(
                "JURY_POOLS_CONFIG: 'pools' должно быть либо 'all', либо списком"
            )

        for track, age in pool_pairs:
            key = (huid, track, age)
            if key in seen:
                raise ValueError(
                    f"JURY_POOLS_CONFIG: дубликат пары (huid={huid}, "
                    f"track={track.name}, age={age.name})"
                )
            seen.add(key)
            triples.append(key)

    return triples


async def sync_pool_assignments_from_config(
    raw_config: str,
    *,
    session: AsyncSession,
) -> int:
    """Применить ``JURY_POOLS_CONFIG`` к таблице ``JuryPoolAssignment``.

    Идемпотентная операция: при каждом старте бота полностью
    переписывает таблицу под актуальный конфиг. Формат JSON описан
    в docstring ``_parse_pools_config``.

    Возвращает количество созданных назначений (после очистки).
    Вызывается из ``app/main.py`` (Wave 3, см. ``# WAVE3-TODO``) в
    рамках lifespan-блока, после регистрации справочника
    ``JuryMember``.

    Безопасность: транзакция оборачивает delete+bulk insert одним
    блоком, чтобы при ошибке парсинга/вставки не остаться с
    полупустой таблицей.
    """
    triples = _parse_pools_config(raw_config)

    await session.execute(delete(JuryPoolAssignment))

    if not triples:
        await session.commit()
        logger.info(
            "JURY_POOLS_CONFIG: пуст → все назначения сняты, fallback «все судьи во всех пулах»"
        )
        return 0

    session.add_all(
        [
            JuryPoolAssignment(
                jury_huid=huid,
                track=track,
                age_category=age,
            )
            for (huid, track, age) in triples
        ]
    )
    await session.commit()
    logger.info(
        "JURY_POOLS_CONFIG применён: создано назначений",
        count=len(triples),
    )
    return len(triples)


__all__ = [
    "all_pools",
    "get_pool_applications",
    "get_jury_for_pool",
    "sync_pool_assignments_from_config",
]
