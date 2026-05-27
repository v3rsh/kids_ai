"""
Сервис жюри-голосования.

Реализует:
- алгоритм отбора по раундам с **инкрементальной фиксацией** топ-N
  (см. ``docs/architecture.md`` → «Сервис жюри»): после каждого
  закрытого раунда above_tie сразу попадает в ``jury_status=V_TOP_10``,
  а в следующий раунд уходит только зона ничьи как новая задача;
- runtime-настройку числа раундов и тумблер автоматического жребия
  через ``services.jury_settings`` (хранится в ``app_settings``,
  переживает рестарт);
- формирование шорт-листа по итогам всех пулов;
- синхронизацию полей реестра, относящихся к раундам жюри, и
  «Статуса жюри»;
- закрытие раунда по полноте / дедлайну / команде модератора;
- получение списка задач для конкретного судьи (``/jury_tasks``).

DTO ``JuryTaskDTO``, ``RoundResult`` и ``PoolKey`` живут в
``utils/contracts.py`` — реализация импортирует их оттуда, чтобы
смежные сервисы пользовались теми же типами.

Сервис принимает ``session: AsyncSession`` явным kwarg'ом, но допускает
``None`` — тогда создаёт собственную сессию через ``get_session()``
(удобно для сценариев вне хендлера: scheduler / migrations / тесты).
Если сессию передаёт хендлер — используется одна сессия на запрос
(см. ``.cursor/rules/performance.mdc``).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Mapping, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import JURY_ROUND_DEADLINE_HOURS, TOP_N
from database.db import get_session
from database.models import (
    AgeCategory,
    Application,
    JuryRound,
    JuryRoundAggregate,
    JuryRoundStatus,
    JuryStatus,
    JuryVote,
    JuryVoteState,
    JuryVoteValue,
    ModerationStatus,
    Track,
)
from services.jury_settings import get_jury_auto_lot, get_jury_max_round
from services.pools import (
    all_pools,
    get_jury_for_pool,
    get_pool_applications,
)
from utils.contracts import JuryTaskDTO, PoolKey, RoundCloseReport, RoundResult

if TYPE_CHECKING:
    from pybotx import Bot


# =====================================================================
# Внутренние структуры и хелперы
# =====================================================================


@dataclass
class _RoundOutcome:
    """Результат прогона алгоритма отбора для одного раунда.

    ``is_tied=False`` — все ``top_ids`` фиксируются как ``V_TOP_10``.
    ``is_tied=True`` — ничья на границе оставшихся вакансий:
    ``above_tie_ids`` фиксируется сразу, ``tie_ids`` уходит в
    следующий раунд (или закрывается жребием — см. ``close_round``).
    """

    counts: dict[UUID, int]
    sorted_app_ids: list[UUID]
    top_ids: list[UUID]
    above_tie_ids: list[UUID]
    tie_ids: list[UUID]
    is_tied: bool


def _open_session_ctx(session: Optional[AsyncSession]):
    """Контекст-менеджер: используем переданную сессию или создаём свою.

    Поведение:
    - если ``session`` передана — возвращает thin-wrapper, который **не**
      коммитит и **не** закрывает её (вызов её владельца);
    - если ``None`` — открывает новую сессию через ``get_session()``.
    """
    if session is not None:
        class _Passthrough:
            async def __aenter__(self):
                return session

            async def __aexit__(self, exc_type, exc, tb):
                return False

        return _Passthrough()
    return get_session()()


async def _get_round(
    round_id: UUID,
    *,
    session: AsyncSession,
) -> JuryRound | None:
    return (
        await session.execute(select(JuryRound).where(JuryRound.id == round_id))
    ).scalar_one_or_none()


async def _get_round_by_pool_no(
    *,
    track: Track,
    age_category: AgeCategory,
    round_no: int,
    session: AsyncSession,
) -> JuryRound | None:
    stmt = select(JuryRound).where(
        JuryRound.track == track,
        JuryRound.age_category == age_category,
        JuryRound.round_no == round_no,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _count_yes_per_app(
    round_id: UUID,
    *,
    session: AsyncSession,
) -> dict[UUID, int]:
    """Подсчёт SUBMITTED ``YES``-голосов по каждой работе раунда.

    Черновики не учитываются. Один SQL-запрос с ``GROUP BY``.
    """
    stmt = (
        select(JuryVote.application_id, func.count())
        .where(
            JuryVote.round_id == round_id,
            JuryVote.state == JuryVoteState.SUBMITTED,
            JuryVote.vote == JuryVoteValue.YES,
        )
        .group_by(JuryVote.application_id)
    )
    rows = (await session.execute(stmt)).all()
    return {row[0]: int(row[1]) for row in rows}


async def _count_fixed_top_in_pool(
    *,
    track: Track,
    age_category: AgeCategory,
    session: AsyncSession,
) -> int:
    """Сколько мест в топ-N пула уже зафиксировано (``jury_status=V_TOP_10``)."""
    stmt = select(func.count(Application.id)).where(
        Application.track == track,
        Application.age_category == age_category,
        Application.jury_status == JuryStatus.V_TOP_10,
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def _get_round_candidates(
    round_obj: JuryRound,
    *,
    session: AsyncSession,
) -> list[Application]:
    """Список заявок-кандидатов раунда.

    - Раунд 1: все ``ДОПУЩЕНО``-заявки пула, созданные не позже
      ``round_obj.opened_at`` (фиксированный снапшот на момент открытия).
    - Раунды 2+: заявки пула со статусом ``НА_ГОЛОСОВАНИИ`` (то есть
      ещё не зафиксированные ни как ``V_TOP_10``, ни как
      ``NE_VOSHLO_V_TOP_10``), которые **участвовали в предыдущем
      раунде** (есть запись в ``jury_round_aggregates`` для round N-1).
      Сортировка ``(created_at ASC, id ASC)``.

    Этот метод **детерминирован**: повторный вызов даёт тот же список
    при тех же данных в БД.
    """
    if round_obj.round_no == 1:
        apps = await get_pool_applications(
            PoolKey(track=round_obj.track, age_category=round_obj.age_category),
            session=session,
            status_filter=ModerationStatus.DOPUSHCHENO,
        )
        return [a for a in apps if a.created_at <= round_obj.opened_at]

    prior = await _get_round_by_pool_no(
        track=round_obj.track,
        age_category=round_obj.age_category,
        round_no=round_obj.round_no - 1,
        session=session,
    )
    if prior is None:
        logger.warning(
            "Нет предыдущего раунда — кандидатов нет",
            round_id=str(round_obj.id),
            round_no=round_obj.round_no,
        )
        return []

    stmt = (
        select(Application)
        .join(
            JuryRoundAggregate,
            JuryRoundAggregate.application_id == Application.id,
        )
        .where(
            Application.track == round_obj.track,
            Application.age_category == round_obj.age_category,
            Application.jury_status == JuryStatus.NA_GOLOSOVANII,
            JuryRoundAggregate.round_id == prior.id,
        )
        .order_by(Application.created_at.asc(), Application.id.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


def _compute_outcome_from_data(
    candidates: list[Application],
    counts: Mapping[UUID, int],
    *,
    top_n: int = TOP_N,
) -> _RoundOutcome:
    """Чистая версия алгоритма отбора для одного раунда — без I/O.

    Выделена для unit-тестов (см. tests/test_jury_algorithm.py).
    Сортировка по голосам ``YES`` DESC, тай-брейк — ``(created_at, id)`` ASC.

    ``top_n`` — число свободных вакансий в шорт-листе пула на момент
    раунда (для раундов 2+ оно меньше ``TOP_N``, т.к. часть мест уже
    зафиксирована above_tie прошлых раундов).
    """
    sorted_apps = sorted(
        candidates,
        key=lambda a: (-counts.get(a.id, 0), a.created_at, a.id),
    )
    sorted_ids = [a.id for a in sorted_apps]

    if top_n <= 0:
        return _RoundOutcome(
            counts=dict(counts),
            sorted_app_ids=sorted_ids,
            top_ids=[],
            above_tie_ids=[],
            tie_ids=[],
            is_tied=False,
        )

    if len(sorted_apps) <= top_n:
        return _RoundOutcome(
            counts=dict(counts),
            sorted_app_ids=sorted_ids,
            top_ids=sorted_ids,
            above_tie_ids=sorted_ids,
            tie_ids=[],
            is_tied=False,
        )

    votes_at_n = counts.get(sorted_apps[top_n - 1].id, 0)
    votes_at_n_plus_1 = counts.get(sorted_apps[top_n].id, 0)
    if votes_at_n > votes_at_n_plus_1:
        top_ids = sorted_ids[:top_n]
        return _RoundOutcome(
            counts=dict(counts),
            sorted_app_ids=sorted_ids,
            top_ids=top_ids,
            above_tie_ids=top_ids,
            tie_ids=[],
            is_tied=False,
        )

    tie_votes = votes_at_n
    above_tie = [a.id for a in sorted_apps if counts.get(a.id, 0) > tie_votes]
    tie_zone = [a.id for a in sorted_apps if counts.get(a.id, 0) == tie_votes]
    return _RoundOutcome(
        counts=dict(counts),
        sorted_app_ids=sorted_ids,
        top_ids=[],
        above_tie_ids=above_tie,
        tie_ids=tie_zone,
        is_tied=True,
    )


async def _compute_round_outcome(
    round_obj: JuryRound,
    *,
    session: AsyncSession,
    top_n_override: Optional[int] = None,
) -> _RoundOutcome:
    """Применить алгоритм отбора к раунду: вернуть отсортированный результат + tie-зону.

    Тонкая I/O-обёртка над ``_compute_outcome_from_data``.

    ``top_n_override`` — если передан, используется как число вакансий;
    иначе считается автоматически: ``TOP_N - count(V_TOP_10 в пуле)``.
    """
    candidates = await _get_round_candidates(round_obj, session=session)
    counts = await _count_yes_per_app(round_obj.id, session=session)

    if top_n_override is not None:
        top_n = top_n_override
    else:
        fixed = await _count_fixed_top_in_pool(
            track=round_obj.track,
            age_category=round_obj.age_category,
            session=session,
        )
        top_n = TOP_N - fixed

    return _compute_outcome_from_data(candidates, counts, top_n=top_n)


async def _upsert_round_aggregates(
    round_obj: JuryRound,
    counts: Mapping[UUID, int],
    candidates: list[Application],
    *,
    session: AsyncSession,
) -> None:
    """Записать ``yes_count`` каждой работы раунда в ``jury_round_aggregates``.

    Идемпотентно: повторный вызов перезаписывает существующие записи.
    Включает **все** кандидаты раунда (в том числе с 0 ``YES`` — это
    важно для Excel-реестра, чтобы отличать «0 голосов» от «не
    участвовала в раунде»).
    """
    existing_rows = (
        await session.execute(
            select(JuryRoundAggregate).where(
                JuryRoundAggregate.round_id == round_obj.id
            )
        )
    ).scalars().all()
    existing_by_app = {r.application_id: r for r in existing_rows}

    for app in candidates:
        yes = int(counts.get(app.id, 0))
        row = existing_by_app.get(app.id)
        if row is None:
            session.add(
                JuryRoundAggregate(
                    round_id=round_obj.id,
                    application_id=app.id,
                    yes_count=yes,
                )
            )
        else:
            row.yes_count = yes


async def _fix_above_tie_in_top(
    *,
    track: Track,
    age_category: AgeCategory,
    above_tie_ids: list[UUID],
    sorted_app_ids: list[UUID],
    round_no: int,
    decided_by_lot_ids: Optional[set[UUID]] = None,
    session: AsyncSession,
) -> None:
    """Зафиксировать выбранные заявки в ``V_TOP_10`` инкрементально.

    Позиции (``pool_position``) считаются с продолжением: первая
    «новая» заявка получает номер ``current_max_pool_position + 1``,
    дальше — в порядке ``sorted_app_ids`` (отфильтрованном до
    ``above_tie_ids``).
    """
    if not above_tie_ids:
        return

    decided_by_lot_ids = decided_by_lot_ids or set()

    max_pos = await session.execute(
        select(func.coalesce(func.max(Application.pool_position), 0)).where(
            Application.track == track,
            Application.age_category == age_category,
            Application.pool_position.is_not(None),
        )
    )
    next_position = int(max_pos.scalar() or 0) + 1

    above_set = set(above_tie_ids)
    ordered = [aid for aid in sorted_app_ids if aid in above_set]
    # Fallback: если sorted_app_ids не покрыл (например, при жребии
    # tie_zone не отсортирован по голосам) — добавим остальные в
    # детерминированном порядке UUID.
    ordered += sorted([aid for aid in above_tie_ids if aid not in set(ordered)])

    for app_id in ordered:
        await session.execute(
            update(Application)
            .where(Application.id == app_id)
            .values(
                jury_status=JuryStatus.V_TOP_10,
                jury_final_round=round_no,
                jury_decided_by_lot=app_id in decided_by_lot_ids,
                pool_position=next_position,
            )
        )
        next_position += 1


async def _fix_losers_in_round(
    *,
    track: Track,
    age_category: AgeCategory,
    candidates: list[Application],
    survivors: set[UUID],
    round_no: int,
    session: AsyncSession,
) -> int:
    """Заявки раунда, не выжившие в этом раунде → ``NE_VOSHLO_V_TOP_10``.

    Выживший = в ``above_tie_ids`` (зафиксирован) **или** в
    ``tie_ids`` (уйдёт в следующий раунд / жребий). Все прочие
    кандидаты раунда — проиграли.

    Возвращает число помеченных как «не вошло».
    """
    losers = [a.id for a in candidates if a.id not in survivors]
    if not losers:
        return 0

    result = await session.execute(
        update(Application)
        .where(Application.id.in_(losers))
        .values(
            jury_status=JuryStatus.NE_VOSHLO_V_TOP_10,
            jury_final_round=round_no,
        )
    )
    return int(result.rowcount or len(losers))


async def _count_dopushcheno_in_pool(
    pool: PoolKey,
    *,
    session: AsyncSession,
) -> int:
    stmt = select(func.count(Application.id)).where(
        Application.track == pool.track,
        Application.age_category == pool.age_category,
        Application.moderation_status == ModerationStatus.DOPUSHCHENO,
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def is_pool_done(
    pool: PoolKey,
    *,
    session: AsyncSession,
) -> bool:
    """Пул «отработан» для глобального shortlist_ready."""
    top_n_count = await _count_fixed_top_in_pool(
        track=pool.track,
        age_category=pool.age_category,
        session=session,
    )
    voting_count = int(
        (
            await session.execute(
                select(func.count(Application.id)).where(
                    Application.track == pool.track,
                    Application.age_category == pool.age_category,
                    Application.jury_status == JuryStatus.NA_GOLOSOVANII,
                )
            )
        ).scalar()
        or 0
    )
    open_rounds = int(
        (
            await session.execute(
                select(func.count(JuryRound.id)).where(
                    JuryRound.track == pool.track,
                    JuryRound.age_category == pool.age_category,
                    JuryRound.status == JuryRoundStatus.OPEN,
                )
            )
        ).scalar()
        or 0
    )
    if top_n_count >= 1 and voting_count == 0:
        return True
    if open_rounds == 0 and voting_count == 0:
        return True
    return False


async def is_global_shortlist_ready(
    *,
    session: Optional[AsyncSession] = None,
) -> bool:
    """True, если все 9 пулов отработаны."""
    async with _open_session_ctx(session) as s:
        for pool in all_pools():
            if not await is_pool_done(pool, session=s):
                return False
        return True


async def maybe_notify_shortlist_ready(
    bot: "Bot | None",
    *,
    session: Optional[AsyncSession] = None,
) -> None:
    if bot is None:
        return
    from services.jury_settings import get_shortlist_announced

    if await get_shortlist_announced():
        return
    if not await is_global_shortlist_ready(session=session):
        return
    from services import notifications

    await notifications.notify_moderation_chat_jury_event(
        bot,
        event_kind="shortlist_ready",
        pools=[],
        round_no=None,
    )


async def purge_inactive_jury_votes_in_open_rounds(
    jury_huid: UUID,
    *,
    session: AsyncSession,
) -> list[UUID]:
    """Удалить голоса отозванного судьи только в OPEN-раундах."""
    open_round_ids = (
        await session.execute(
            select(JuryRound.id).where(JuryRound.status == JuryRoundStatus.OPEN)
        )
    ).scalars().all()
    if not open_round_ids:
        return []

    affected = (
        await session.execute(
            select(JuryVote.round_id)
            .where(
                JuryVote.jury_huid == jury_huid,
                JuryVote.round_id.in_(open_round_ids),
            )
            .group_by(JuryVote.round_id)
        )
    ).scalars().all()

    await session.execute(
        delete(JuryVote).where(
            JuryVote.jury_huid == jury_huid,
            JuryVote.round_id.in_(open_round_ids),
        )
    )
    return list(affected)


async def try_auto_close_rounds_after_revoke(
    round_ids: list[UUID],
    *,
    bot: "Bot | None" = None,
    session: Optional[AsyncSession] = None,
) -> None:
    """Пересчитать all_submitted и закрыть OPEN-раунды при необходимости."""
    if not round_ids:
        return
    async with _open_session_ctx(session) as s:
        for round_id in round_ids:
            round_obj = await _get_round(round_id, session=s)
            if round_obj is None or round_obj.status != JuryRoundStatus.OPEN:
                continue
            pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)
            jury_for_pool = await get_jury_for_pool(pool, session=s)
            if not jury_for_pool:
                continue
            submitted_huids = (
                await s.execute(
                    select(JuryVote.jury_huid)
                    .where(
                        JuryVote.round_id == round_id,
                        JuryVote.state == JuryVoteState.SUBMITTED,
                    )
                    .group_by(JuryVote.jury_huid)
                )
            ).scalars().all()
            pool_huids = {m.huid for m in jury_for_pool}
            if pool_huids.issubset(set(submitted_huids)):
                if session is None:
                    await s.commit()
                await close_round(round_id, session=session, bot=bot)


async def auto_shortlist_undersized_pool(
    pool: PoolKey,
    *,
    session: Optional[AsyncSession] = None,
    bot: "Bot | None" = None,
) -> int:
    """Закрыть пул без голосования (< TOP_N работ). Возвращает число работ."""
    async with _open_session_ctx(session) as s:
        fixed = await _count_fixed_top_in_pool(
            track=pool.track,
            age_category=pool.age_category,
            session=s,
        )
        if fixed > 0:
            logger.info(
                "auto_shortlist_undersized_pool: уже финализирован",
                pool=pool.as_label(),
                fixed=fixed,
            )
            return fixed

        apps = await get_pool_applications(
            pool,
            session=s,
            status_filter=ModerationStatus.DOPUSHCHENO,
        )
        if not apps:
            return 0

        apps_sorted = sorted(apps, key=lambda a: (a.created_at, a.id))
        for position, app in enumerate(apps_sorted, start=1):
            await s.execute(
                update(Application)
                .where(Application.id == app.id)
                .values(
                    jury_status=JuryStatus.V_TOP_10,
                    pool_position=position,
                    jury_final_round=None,
                    jury_decided_by_lot=False,
                )
            )

        if session is None:
            await s.commit()

        count = len(apps_sorted)
        logger.info(
            "auto_shortlist_undersized_pool: пул закрыт без голосования",
            pool=pool.as_label(),
            works=count,
        )

    if bot is not None:
        from services import notifications

        await notifications.notify_moderation_chat_undersized_pool(
            bot,
            pool_label=pool.as_label(),
            works_n=count,
            top_n=TOP_N,
        )
        await maybe_notify_shortlist_ready(bot, session=session)
    return count


async def _notify_round_opened(
    bot: "Bot | None",
    *,
    pool: PoolKey,
    round_obj: JuryRound,
    session: AsyncSession,
    is_new_round: bool,
) -> None:
    if bot is None:
        logger.info(
            "round_opened без bot — пропуск нотификаций",
            pool=pool.as_label(),
            round_no=round_obj.round_no,
        )
        return

    candidates = await _get_round_candidates(round_obj, session=session)
    fixed = await _count_fixed_top_in_pool(
        track=pool.track,
        age_category=pool.age_category,
        session=session,
    )
    slots = TOP_N - fixed
    from services import notifications

    await notifications.notify_moderation_chat_jury_event(
        bot,
        event_kind="round_opened",
        pools=[(pool.track.value, pool.age_category.value)],
        round_no=round_obj.round_no,
        candidates_n=len(candidates),
        slots_n=slots,
    )
    if is_new_round:
        from services import jury_notifications

        jury_members = await get_jury_for_pool(pool, session=session)
        for member in jury_members:
            await jury_notifications.maybe_announce_next_task_to_judge(
                bot,
                jury_huid=member.huid,
                trigger="open_round",
                session=session,
            )


async def _build_close_report(
    round_obj: JuryRound,
    *,
    outcome: _RoundOutcome,
    candidates: list[Application],
    losers_count: int,
    fixed_before: int,
    pool_completed: bool,
    lot_applied: bool,
    next_round_opened: bool,
    session: AsyncSession,
) -> RoundCloseReport:
    remaining_after = TOP_N - fixed_before - len(outcome.above_tie_ids)
    if lot_applied:
        remaining_after = 0
    pool_top_n = await _count_fixed_top_in_pool(
        track=round_obj.track,
        age_category=round_obj.age_category,
        session=session,
    )
    next_candidates_n = 0
    next_slots_n = 0
    if next_round_opened:
        next_round = await _get_round_by_pool_no(
            track=round_obj.track,
            age_category=round_obj.age_category,
            round_no=round_obj.round_no + 1,
            session=session,
        )
        if next_round is not None:
            next_candidates = await _get_round_candidates(next_round, session=session)
            next_fixed = await _count_fixed_top_in_pool(
                track=round_obj.track,
                age_category=round_obj.age_category,
                session=session,
            )
            next_candidates_n = len(next_candidates)
            next_slots_n = TOP_N - next_fixed

    return RoundCloseReport(
        candidates_n=len(candidates),
        slots_n=TOP_N - fixed_before,
        fixed_top_n_in_round=len(outcome.above_tie_ids),
        tie_n=len(outcome.tie_ids),
        losers_n=losers_count,
        remaining_slots_after=max(remaining_after, 0),
        pool_completed=pool_completed,
        lot_applied=lot_applied,
        next_round_opened=next_round_opened,
        next_round_candidates_n=next_candidates_n,
        next_round_slots_n=next_slots_n,
        pool_top_n=pool_top_n,
    )


async def _notify_round_closed(
    bot: "Bot | None",
    *,
    pool: PoolKey,
    round_obj: JuryRound,
    report: RoundCloseReport,
) -> None:
    if bot is None:
        return
    from services import notifications

    await notifications.notify_moderation_chat_jury_event(
        bot,
        event_kind="round_closed",
        pools=[(pool.track.value, pool.age_category.value)],
        round_no=round_obj.round_no,
        close_report=report,
    )


# =====================================================================
# Открытие раунда
# =====================================================================


async def open_round(
    *,
    track: Track,
    age_category: AgeCategory,
    round_no: int,
    candidates: list[Application],
    session: Optional[AsyncSession] = None,
    bot: "Bot | None" = None,
) -> JuryRound:
    """Открыть новый раунд по пулу.

    Создаёт запись ``JuryRound`` со ``status=OPEN``, ``opened_at=now()``,
    ``deadline_at = now() + JURY_ROUND_DEADLINE_HOURS``. На раунде 1
    дополнительно переводит все заявки пула со статусом ``ДОПУЩЕНО`` в
    ``jury_status = НА_ГОЛОСОВАНИИ`` (синхронизация «Статуса жюри»
    в реестре).

    ``candidates`` для раунда 1 игнорируется (берётся всё из
    ``get_pool_applications`` на момент открытия — единый снапшот
    через ``opened_at``); для раундов 2+ список нужен **только**
    в логе/sanity-check — кандидаты пересчитываются детерминированно
    из ``_get_round_candidates`` по предыдущему раунду. Это сделано
    специально: даже если caller передаст устаревший список, бот
    использует консистентные данные из БД.

    Идемпотентность: если раунд для (track, age, round_no) уже
    существует — возвращает его без правок.
    """
    async with _open_session_ctx(session) as s:
        existing = await _get_round_by_pool_no(
            track=track,
            age_category=age_category,
            round_no=round_no,
            session=s,
        )
        if existing is not None:
            logger.info(
                "open_round: раунд уже существует — возврат текущего",
                track=track.name,
                age_category=age_category.name,
                round_no=round_no,
                status=existing.status.name,
            )
            return existing

        now = datetime.utcnow()
        round_obj = JuryRound(
            track=track,
            age_category=age_category,
            round_no=round_no,
            status=JuryRoundStatus.OPEN,
            opened_at=now,
            deadline_at=now + timedelta(hours=JURY_ROUND_DEADLINE_HOURS),
        )
        s.add(round_obj)
        await s.flush()

        if round_no == 1:
            await s.execute(
                update(Application)
                .where(
                    Application.track == track,
                    Application.age_category == age_category,
                    Application.moderation_status == ModerationStatus.DOPUSHCHENO,
                    Application.jury_status == JuryStatus.NE_PEREDANO_ZHYURI,
                )
                .values(jury_status=JuryStatus.NA_GOLOSOVANII)
            )

        if session is None:
            await s.commit()
        logger.info(
            "Раунд жюри открыт",
            round_id=str(round_obj.id),
            track=track.name,
            age_category=age_category.name,
            round_no=round_no,
            deadline_at=round_obj.deadline_at.isoformat(),
            candidates_hint=len(candidates) if candidates else None,
        )
        pool = PoolKey(track=track, age_category=age_category)
        await _notify_round_opened(
            bot,
            pool=pool,
            round_obj=round_obj,
            session=s,
            is_new_round=True,
        )
        return round_obj


# =====================================================================
# Черновики голосов + отправка
# =====================================================================


async def upsert_draft_vote(
    *,
    round_id: UUID,
    application_id: UUID,
    jury_huid: UUID,
    vote: JuryVoteValue,
    session: Optional[AsyncSession] = None,
) -> JuryVote:
    """Сохранить черновик голоса судьи.

    UPSERT по уникальному ключу ``(round_id, application_id, jury_huid)``:
    - если записи нет — создаёт новую с ``state=DRAFT``;
    - если есть — обновляет ``vote``, оставляя ``state`` как был.
      Если запись уже ``SUBMITTED`` — RuntimeError (после отправки
      повторная подача в раунде невозможна).

    Черновик хранится в PostgreSQL и переживает рестарт бота.
    """
    async with _open_session_ctx(session) as s:
        existing = (
            await s.execute(
                select(JuryVote).where(
                    JuryVote.round_id == round_id,
                    JuryVote.application_id == application_id,
                    JuryVote.jury_huid == jury_huid,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            existing = JuryVote(
                round_id=round_id,
                application_id=application_id,
                jury_huid=jury_huid,
                vote=vote,
                state=JuryVoteState.DRAFT,
            )
            s.add(existing)
        else:
            if existing.state == JuryVoteState.SUBMITTED:
                raise RuntimeError(
                    "Голос уже отправлен (SUBMITTED) — повторная подача"
                    " в этом раунде запрещена"
                )
            existing.vote = vote

        if session is None:
            await s.commit()
        return existing


async def submit_votes(
    *,
    round_id: UUID,
    jury_huid: UUID,
    votes: Mapping[UUID, JuryVoteValue],
    session: Optional[AsyncSession] = None,
    bot: "Bot | None" = None,
) -> None:
    """Зафиксировать оценки судьи в раунде.

    Принимает словарь ``app_id → JuryVoteValue`` со всеми работами пула
    в карусели. Алгоритм:

    1. Проверить, что ``votes`` покрывает **все** кандидаты раунда.
    2. Проверить правило разброса: есть и YES, и NO (если работ
       больше одной).
    3. UPSERT каждой записи ``(round_id, app_id, jury_huid)`` с
       ``state=SUBMITTED``, ``submitted_at=now()``. Это перезаписывает
       любые остаточные DRAFT.
    4. Если **все** назначенные на пул судьи отправили оценки —
       автоматически закрыть раунд.

    Raises:
        ValueError — пропущены работы / нарушено правило разброса.
        LookupError — раунд не найден / не в статусе OPEN.
    """
    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")
        if round_obj.status != JuryRoundStatus.OPEN:
            raise LookupError(
                f"Раунд {round_id} закрыт (status={round_obj.status.name})"
            )

        candidates = await _get_round_candidates(round_obj, session=s)
        candidate_ids = {a.id for a in candidates}
        missing = candidate_ids - set(votes.keys())
        if missing:
            raise ValueError(
                f"Не все работы оценены: пропущено {len(missing)} из {len(candidate_ids)}"
            )
        if len(candidate_ids) > 1:
            values = {votes[app_id] for app_id in candidate_ids}
            if JuryVoteValue.YES not in values or JuryVoteValue.NO not in values:
                raise ValueError(
                    "Правило разброса: должна быть хотя бы одна"
                    " оценка YES и хотя бы одна оценка NO"
                )

        existing_rows = (
            await s.execute(
                select(JuryVote).where(
                    JuryVote.round_id == round_id,
                    JuryVote.jury_huid == jury_huid,
                )
            )
        ).scalars().all()
        existing_by_app = {v.application_id: v for v in existing_rows}

        now = datetime.utcnow()
        for app_id in candidate_ids:
            value = votes[app_id]
            row = existing_by_app.get(app_id)
            if row is None:
                s.add(
                    JuryVote(
                        round_id=round_id,
                        application_id=app_id,
                        jury_huid=jury_huid,
                        vote=value,
                        state=JuryVoteState.SUBMITTED,
                        submitted_at=now,
                    )
                )
            else:
                row.vote = value
                row.state = JuryVoteState.SUBMITTED
                row.submitted_at = now

        await s.flush()

        from services.access import is_jury

        if not is_jury(jury_huid):
            await s.rollback()
            raise PermissionError(
                "Судья отозван во время отправки оценок"
            )

        pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)
        jury_for_pool = await get_jury_for_pool(pool, session=s)
        submitted_huids = (
            await s.execute(
                select(JuryVote.jury_huid)
                .where(
                    JuryVote.round_id == round_id,
                    JuryVote.state == JuryVoteState.SUBMITTED,
                )
                .group_by(JuryVote.jury_huid)
            )
        ).scalars().all()
        submitted_set = set(submitted_huids)
        pool_huids = {m.huid for m in jury_for_pool}
        all_submitted = bool(pool_huids) and pool_huids.issubset(submitted_set)

        if session is None:
            await s.commit()

        logger.info(
            "Судья отправил оценки",
            round_id=str(round_id),
            jury_huid=str(jury_huid),
            votes_count=len(candidate_ids),
            all_submitted=all_submitted,
        )

    if bot is not None:
        from services import jury_notifications

        await jury_notifications.maybe_announce_next_task_to_judge(
            bot,
            jury_huid=jury_huid,
            trigger="submit_votes",
            session=session,
        )

    if all_submitted:
        logger.info(
            "Все назначенные судьи проголосовали — автоматическое закрытие раунда",
            round_id=str(round_id),
        )
        await close_round(round_id, session=session, bot=bot)


# =====================================================================
# Закрытие раунда, расчёт топ-N, жребий
# =====================================================================


async def close_round(
    round_id: UUID,
    *,
    session: Optional[AsyncSession] = None,
    bot: "Bot | None" = None,
) -> RoundResult:
    """Закрыть раунд по триггеру (полнота / дедлайн / команда модератора).

    Алгоритм (инкрементальная фиксация):

    1. UPDATE ... SET status=CLOSED WHERE id=:id AND status=OPEN —
       идемпотентно: если кто-то уже закрыл, ничего не делаем.
    2. Подсчёт SUBMITTED-голосов + UPSERT в ``JuryRoundAggregate``.
    3. Прогон ``_compute_round_outcome`` с ``top_n = TOP_N - fixed`` —
       определение above_tie, tie-зоны и проигравших.
    4. **Сразу фиксировать** above_tie как ``V_TOP_10`` (инкремент:
       часть мест в шорт-листе занята уже сейчас).
    5. Проигравших раунда — ``NE_VOSHLO_V_TOP_10``.
    6. Решение, что дальше:
       - нет ничьи и все вакансии закрыты → пул завершён;
       - есть ничья + ``round_no >= max_round`` + ``auto_lot=True``
         → ``apply_lot_if_needed``;
       - есть ничья (иначе) → открыть ``round_no + 1`` с tie-зоной
         как новой задачей всем судьям.

    Возвращает ``RoundResult`` для логирования / нотификаций.
    """
    max_round = await get_jury_max_round()
    auto_lot = await get_jury_auto_lot()

    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")

        now = datetime.utcnow()
        result = await s.execute(
            update(JuryRound)
            .where(
                JuryRound.id == round_id,
                JuryRound.status == JuryRoundStatus.OPEN,
            )
            .values(status=JuryRoundStatus.CLOSED, closed_at=now)
        )
        if result.rowcount == 0:
            logger.info(
                "close_round: раунд уже закрыт — пропускаем",
                round_id=str(round_id),
                status=round_obj.status.name,
            )
            fixed_now = await _count_fixed_top_in_pool(
                track=round_obj.track,
                age_category=round_obj.age_category,
                session=s,
            )
            outcome = await _compute_round_outcome(
                round_obj,
                session=s,
                top_n_override=TOP_N - fixed_now,
            )
            return RoundResult(
                pool=PoolKey(track=round_obj.track, age_category=round_obj.age_category),
                round_no=round_obj.round_no,
                top_ids=tuple(outcome.top_ids),
                tie_ids=tuple(outcome.tie_ids),
                decided_by_lot=(),
                needs_next_round=outcome.is_tied,
                closed_at=round_obj.closed_at or now,
            )

        await s.refresh(round_obj)

        fixed_before = await _count_fixed_top_in_pool(
            track=round_obj.track,
            age_category=round_obj.age_category,
            session=s,
        )
        remaining = TOP_N - fixed_before
        candidates = await _get_round_candidates(round_obj, session=s)
        counts = await _count_yes_per_app(round_obj.id, session=s)
        outcome = _compute_outcome_from_data(candidates, counts, top_n=remaining)

        await _upsert_round_aggregates(
            round_obj, counts, candidates, session=s
        )

        survivors_in_round = set(outcome.above_tie_ids) | set(outcome.tie_ids)
        await _fix_above_tie_in_top(
            track=round_obj.track,
            age_category=round_obj.age_category,
            above_tie_ids=list(outcome.above_tie_ids),
            sorted_app_ids=list(outcome.sorted_app_ids),
            round_no=round_obj.round_no,
            session=s,
        )
        losers_count = await _fix_losers_in_round(
            track=round_obj.track,
            age_category=round_obj.age_category,
            candidates=candidates,
            survivors=survivors_in_round,
            round_no=round_obj.round_no,
            session=s,
        )

        needs_next = outcome.is_tied
        will_apply_lot = (
            outcome.is_tied
            and auto_lot
            and round_obj.round_no >= max_round
        )
        pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)
        fixed_after = fixed_before + len(outcome.above_tie_ids)
        pool_completed = (
            not outcome.is_tied and fixed_after >= TOP_N
        ) or will_apply_lot

        close_report = await _build_close_report(
            round_obj,
            outcome=outcome,
            candidates=candidates,
            losers_count=losers_count,
            fixed_before=fixed_before,
            pool_completed=pool_completed,
            lot_applied=False,
            next_round_opened=needs_next and not will_apply_lot,
            session=s,
        )

        if session is None:
            await s.commit()

        logger.info(
            "Раунд закрыт",
            round_id=str(round_id),
            round_no=round_obj.round_no,
            track=round_obj.track.name,
            age_category=round_obj.age_category.name,
            is_tied=outcome.is_tied,
            above_tie_count=len(outcome.above_tie_ids),
            tie_count=len(outcome.tie_ids),
            losers_count=losers_count,
            remaining_before=remaining,
            max_round=max_round,
            auto_lot=auto_lot,
            will_apply_lot=will_apply_lot,
        )

        result_dto = RoundResult(
            pool=pool,
            round_no=round_obj.round_no,
            top_ids=tuple(outcome.above_tie_ids),
            tie_ids=tuple(outcome.tie_ids),
            decided_by_lot=(),
            needs_next_round=needs_next and not will_apply_lot,
            closed_at=now,
        )

    await _notify_round_closed(
        bot,
        pool=pool,
        round_obj=round_obj,
        report=close_report,
    )

    if will_apply_lot:
        await apply_lot_if_needed(round_id, session=session, bot=bot)
        await maybe_notify_shortlist_ready(bot, session=session)
        return result_dto

    if needs_next:
        await open_round(
            track=result_dto.pool.track,
            age_category=result_dto.pool.age_category,
            round_no=round_obj.round_no + 1,
            candidates=[],
            session=session,
            bot=bot,
        )

    await maybe_notify_shortlist_ready(bot, session=session)
    return result_dto


async def compute_top_n(
    round_id: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> list[Application]:
    """Сформировать топ-N для уже закрытого раунда.

    Используется в основном для совместимости с тестами / диагностики.
    В новой инкрементальной модели «топ» хранится прямо в
    ``Application.jury_status == V_TOP_10`` — там самый честный
    источник. Этот метод возвращает above_tie последнего расчёта.
    """
    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")
        outcome = await _compute_round_outcome(round_obj, session=s)
        ids = outcome.above_tie_ids if not outcome.is_tied else (
            list(outcome.above_tie_ids) + list(outcome.tie_ids)
        )
        if not ids:
            return []
        result = await s.execute(
            select(Application).where(Application.id.in_(ids))
        )
        apps_by_id = {a.id: a for a in result.scalars().all()}
        return [apps_by_id[i] for i in ids if i in apps_by_id]


async def apply_lot_if_needed(
    round_id: UUID,
    *,
    session: Optional[AsyncSession] = None,
    bot: "Bot | None" = None,
) -> list[Application]:
    """Автоматический жребий на оставшиеся вакансии пула.

    Срабатывает, если раунд закрыт с ничьёй на границе оставшихся
    вакансий (``remaining = TOP_N - count(V_TOP_10 в пуле)``).
    Случайно выбирает нужное число работ из tie-зоны текущего раунда,
    помечает их ``jury_status=V_TOP_10`` с ``jury_decided_by_lot=True``,
    переводит раунд в статус ``DRAWN_BY_LOT``. Остальные tie-апы
    помечаются ``NE_VOSHLO_V_TOP_10`` — пул финализируется.

    В инкрементальной модели above_tie прошлых раундов уже зафиксирован,
    поэтому ``remaining`` считается из БД, а не из ``outcome.above_tie``.

    Возвращает заявки, попавшие в топ по жребию. Если жребий не нужен —
    пустой список.
    """
    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")

        fixed = await _count_fixed_top_in_pool(
            track=round_obj.track,
            age_category=round_obj.age_category,
            session=s,
        )
        remaining = TOP_N - fixed
        if remaining <= 0:
            logger.info(
                "Жребий: вакансий не осталось — пропускаем",
                round_id=str(round_id),
            )
            return []

        outcome = await _compute_round_outcome(
            round_obj, session=s, top_n_override=remaining
        )
        if not outcome.is_tied:
            logger.info(
                "Жребий: ничьи нет — пропускаем",
                round_id=str(round_id),
            )
            return []
        if not outcome.tie_ids:
            return []

        if remaining >= len(outcome.tie_ids):
            chosen_ids = list(outcome.tie_ids)
        else:
            chosen_ids = random.sample(outcome.tie_ids, remaining)

        chosen_set = set(chosen_ids)
        await _fix_above_tie_in_top(
            track=round_obj.track,
            age_category=round_obj.age_category,
            above_tie_ids=chosen_ids,
            sorted_app_ids=list(outcome.sorted_app_ids),
            round_no=round_obj.round_no,
            decided_by_lot_ids=chosen_set,
            session=s,
        )

        lot_losers = [aid for aid in outcome.tie_ids if aid not in chosen_set]
        if lot_losers:
            await s.execute(
                update(Application)
                .where(Application.id.in_(lot_losers))
                .values(
                    jury_status=JuryStatus.NE_VOSHLO_V_TOP_10,
                    jury_final_round=round_obj.round_no,
                )
            )

        await s.execute(
            update(JuryRound)
            .where(JuryRound.id == round_id)
            .values(status=JuryRoundStatus.DRAWN_BY_LOT)
        )

        result_apps = await s.execute(
            select(Application).where(Application.id.in_(chosen_ids))
        )
        chosen = list(result_apps.scalars().all())

        if session is None:
            await s.commit()

        logger.info(
            "Применён автоматический жребий",
            round_id=str(round_id),
            chosen_count=len(chosen_ids),
            tie_zone_size=len(outcome.tie_ids),
            lot_losers=len(lot_losers),
        )
        pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)

    if bot is not None and chosen:
        from services import notifications

        await notifications.notify_moderation_chat_jury_event(
            bot,
            event_kind="lot_applied",
            pools=[(pool.track.value, pool.age_category.value)],
            round_no=round_obj.round_no,
        )
        await maybe_notify_shortlist_ready(bot, session=session)
    return chosen


# =====================================================================
# Шорт-лист
# =====================================================================


async def _finalize_pool(
    pool: PoolKey,
    *,
    session: AsyncSession,
) -> list[Application]:
    """Аварийная финализация пула (для ``/jury_finalize``).

    В обычном потоке пул финализируется инкрементально в ``close_round``:
    above_tie каждого раунда сразу попадает в ``V_TOP_10``. Эта функция
    нужна только когда модератор хочет принудительно остановить процесс:

    - если есть открытый раунд — закрываем его (``close_round`` сам
      сделает инкрементальную фиксацию и решит, нужен ли жребий);
    - если ``auto_lot=False`` и в последнем раунде осталась ничья —
      оставляем вакансии **пустыми**, помечая tie-зону как
      ``NE_VOSHLO_V_TOP_10``. Этот сценарий = «не дожали 10 работ,
      админ сознательно зафиксировал частичный шорт-лист».

    Возвращает текущие ``V_TOP_10``-заявки пула.
    """
    rounds = (
        await session.execute(
            select(JuryRound)
            .where(
                JuryRound.track == pool.track,
                JuryRound.age_category == pool.age_category,
            )
            .order_by(JuryRound.round_no.desc())
        )
    ).scalars().all()

    if rounds:
        last_round = rounds[0]
        if last_round.status == JuryRoundStatus.OPEN:
            # close_round внутри: инкрементальная фиксация + (возможно) жребий.
            await close_round(last_round.id, session=session)
            # перечитаем после закрытия
            await session.flush()

        auto_lot = await get_jury_auto_lot()
        if not auto_lot:
            # Если жребий запрещён и в последнем CLOSED раунде осталась
            # ничья — tie-зону отправляем в проигравшие, пул фиксируется
            # «частичным» (вакансии остаются пустыми).
            fresh_last = (
                await session.execute(
                    select(JuryRound).where(JuryRound.id == last_round.id)
                )
            ).scalar_one()
            if fresh_last.status == JuryRoundStatus.CLOSED:
                fixed = await _count_fixed_top_in_pool(
                    track=pool.track,
                    age_category=pool.age_category,
                    session=session,
                )
                if fixed < TOP_N:
                    outcome = await _compute_round_outcome(
                        fresh_last,
                        session=session,
                        top_n_override=TOP_N - fixed,
                    )
                    if outcome.is_tied and outcome.tie_ids:
                        await session.execute(
                            update(Application)
                            .where(Application.id.in_(list(outcome.tie_ids)))
                            .values(
                                jury_status=JuryStatus.NE_VOSHLO_V_TOP_10,
                                jury_final_round=fresh_last.round_no,
                            )
                        )
                        logger.info(
                            "Аварийная финализация без жребия — tie-зона помечена как «не вошло»",
                            pool=pool.as_label(),
                            tie_count=len(outcome.tie_ids),
                        )

    result_apps = await session.execute(
        select(Application)
        .where(
            Application.track == pool.track,
            Application.age_category == pool.age_category,
            Application.jury_status == JuryStatus.V_TOP_10,
        )
        .order_by(Application.pool_position.asc())
    )
    return list(result_apps.scalars().all())


async def build_shortlist(
    *,
    session: Optional[AsyncSession] = None,
    bot: "Bot | None" = None,
) -> list[Application]:
    """Собрать шорт-лист по всем пулам.

    В новой инкрементальной модели шорт-лист уже **накапливается** в
    ``Application.jury_status == V_TOP_10`` по мере закрытия раундов.
    Эта функция:

    1. Прогоняет ``_finalize_pool`` для каждого пула — для пулов с
       открытыми раундами или незавершёнными ничьями (актуально, если
       вызвана через ``/jury_finalize``).
    2. Возвращает плоский список ``V_TOP_10``-заявок всех пулов в
       порядке ``(track, age_category, pool_position)``.

    Уведомления участников и чата модерации о попадании в шорт-лист —
    задача ``services.notifications``.
    """
    async with _open_session_ctx(session) as s:
        all_shortlist: list[Application] = []
        for pool in all_pools():
            top_apps = await _finalize_pool(pool, session=s)
            all_shortlist.extend(top_apps)
            logger.info(
                "Пул финализирован",
                track=pool.track.name,
                age_category=pool.age_category.name,
                shortlist_size=len(top_apps),
            )

        if session is None:
            await s.commit()

        logger.info(
            "Шорт-лист сформирован",
            pools=len(all_pools()),
            total_works=len(all_shortlist),
        )
        await maybe_notify_shortlist_ready(bot, session=s)
        return all_shortlist


# =====================================================================
# Запросы для UX жюри (/jury_tasks)
# =====================================================================


async def get_open_tasks_for_jury(
    jury_huid: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> list[JuryTaskDTO]:
    """Список открытых задач судьи.

    Возвращает плоский список ``JuryTaskDTO`` — по одной DTO на
    каждую работу в карусели каждого открытого раунда, где судья:
    - назначен на пул (через ``JuryPoolAssignment`` или fallback);
    - ещё не отправил оценки в этом раунде.

    Порядок: сначала по пулу (``track`` → ``age_category``), затем по
    ``round_no``, затем внутри раунда — по ``(created_at ASC, id ASC)``
    единый для всех судей.

    ``draft_vote`` — текущее значение черновика, чтобы handler мог
    отрисовать эмодзи на кнопке ``Да``/``Нет``.

    ``cloud_link`` — публичная ссылка на папку (в режиме ``links``).
    """
    open_rounds_stmt = (
        select(JuryRound)
        .where(JuryRound.status == JuryRoundStatus.OPEN)
        .order_by(
            JuryRound.track,
            JuryRound.age_category,
            JuryRound.round_no,
        )
    )

    async with _open_session_ctx(session) as s:
        open_rounds = (await s.execute(open_rounds_stmt)).scalars().all()
        if not open_rounds:
            return []

        submitted_round_ids = set(
            (
                await s.execute(
                    select(JuryVote.round_id)
                    .where(
                        JuryVote.jury_huid == jury_huid,
                        JuryVote.state == JuryVoteState.SUBMITTED,
                    )
                    .group_by(JuryVote.round_id)
                )
            ).scalars().all()
        )

        all_drafts = (
            await s.execute(
                select(JuryVote).where(
                    JuryVote.jury_huid == jury_huid,
                    JuryVote.state == JuryVoteState.DRAFT,
                )
            )
        ).scalars().all()
        draft_by_round: dict[UUID, dict[UUID, JuryVoteValue]] = {}
        for v in all_drafts:
            draft_by_round.setdefault(v.round_id, {})[v.application_id] = v.vote

        relevant_rounds = [r for r in open_rounds if r.id not in submitted_round_ids]
        if not relevant_rounds:
            return []

        pool_assignments_cache: dict[PoolKey, set[UUID]] = {}

        async def _jury_in_pool(pool: PoolKey) -> bool:
            ids = pool_assignments_cache.get(pool)
            if ids is None:
                members = await get_jury_for_pool(pool, session=s)
                ids = {m.huid for m in members}
                pool_assignments_cache[pool] = ids
            return jury_huid in ids

        result: list[JuryTaskDTO] = []
        for round_obj in relevant_rounds:
            pool = PoolKey(
                track=round_obj.track,
                age_category=round_obj.age_category,
            )
            if not await _jury_in_pool(pool):
                continue
            candidates = await _get_round_candidates(round_obj, session=s)
            drafts_for_round = draft_by_round.get(round_obj.id, {})
            for local_no, app in enumerate(candidates, start=1):
                result.append(
                    JuryTaskDTO(
                        round_id=round_obj.id,
                        application_id=app.id,
                        pool=pool,
                        round_no=round_obj.round_no,
                        local_no=local_no,
                        title=app.title,
                        description=app.description,
                        cloud_link=app.cloud_link,
                        draft_vote=drafts_for_round.get(app.id),
                    )
                )
        return result


# =====================================================================
# Доп. публичные хелперы для хендлеров
# =====================================================================


async def get_jury_progress(
    jury_huid: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> dict[str, int]:
    """Прогресс судьи (для ``/jury_status``).

    Возвращает счётчики:
    - ``submitted_rounds`` — раунды, по которым отправлены оценки;
    - ``in_progress_rounds`` — раунды, по которым есть черновики;
    - ``not_started_rounds`` — открытые раунды без единого голоса.
    """
    async with _open_session_ctx(session) as s:
        open_rounds = (
            await s.execute(
                select(JuryRound).where(JuryRound.status == JuryRoundStatus.OPEN)
            )
        ).scalars().all()

        all_votes = (
            await s.execute(
                select(JuryVote.round_id, JuryVote.state)
                .where(JuryVote.jury_huid == jury_huid)
            )
        ).all()
        rounds_with_draft: set[UUID] = set()
        rounds_with_submitted: set[UUID] = set()
        for round_id, state in all_votes:
            if state == JuryVoteState.SUBMITTED:
                rounds_with_submitted.add(round_id)
            elif state == JuryVoteState.DRAFT:
                rounds_with_draft.add(round_id)

        pool_assignments_cache: dict[PoolKey, set[UUID]] = {}

        async def _is_assigned(pool: PoolKey) -> bool:
            ids = pool_assignments_cache.get(pool)
            if ids is None:
                members = await get_jury_for_pool(pool, session=s)
                ids = {m.huid for m in members}
                pool_assignments_cache[pool] = ids
            return jury_huid in ids

        submitted = 0
        in_progress = 0
        not_started = 0
        for r in open_rounds:
            pool = PoolKey(track=r.track, age_category=r.age_category)
            if not await _is_assigned(pool):
                continue
            if r.id in rounds_with_submitted:
                submitted += 1
            elif r.id in rounds_with_draft:
                in_progress += 1
            else:
                not_started += 1

        return {
            "submitted_rounds": submitted,
            "in_progress_rounds": in_progress,
            "not_started_rounds": not_started,
        }


async def get_round_candidates_with_drafts(
    round_id: UUID,
    jury_huid: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> tuple[JuryRound, list[Application], dict[UUID, JuryVoteValue]]:
    """Загрузить раунд + кандидатов + черновики судьи одной транзакцией.

    Хелпер для ``handlers/jury_tasks.py``: чтобы экран задачи мог
    отрисовать карусель и эмодзи на кнопках без повторных SQL.

    Применяется в режиме «одной сессии на запрос» (см.
    ``performance.mdc``) — handler передаёт сессию, хелпер делает
    три запроса батчем.
    """
    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")
        candidates = await _get_round_candidates(round_obj, session=s)
        drafts_rows = (
            await s.execute(
                select(JuryVote).where(
                    JuryVote.round_id == round_id,
                    JuryVote.jury_huid == jury_huid,
                )
            )
        ).scalars().all()
        drafts = {v.application_id: v.vote for v in drafts_rows}
        return round_obj, candidates, drafts


__all__ = [
    "open_round",
    "submit_votes",
    "upsert_draft_vote",
    "close_round",
    "compute_top_n",
    "apply_lot_if_needed",
    "build_shortlist",
    "get_open_tasks_for_jury",
    "get_jury_progress",
    "get_round_candidates_with_drafts",
    "purge_inactive_jury_votes_in_open_rounds",
    "try_auto_close_rounds_after_revoke",
    "auto_shortlist_undersized_pool",
    "is_global_shortlist_ready",
    "is_pool_done",
    "maybe_notify_shortlist_ready",
    "_count_dopushcheno_in_pool",
]
