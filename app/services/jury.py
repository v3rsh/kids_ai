"""
Сервис жюри-голосования (Wave 2 / ветка C).

Реализует:
- алгоритм §35.2 (раунды 1→2→3 + жребий на повторной ничье);
- формирование шорт-листа §35.5;
- синхронизацию полей реестра 23–29 и «Статуса жюри» (§25.3, §26);
- закрытие раунда по полноте / дедлайну / команде модератора (§35.4, §27.5);
- получение списка задач для конкретного судьи (§27.4 ``/jury_tasks``).

DTO ``JuryTaskDTO``, ``RoundResult`` и ``PoolKey`` живут в
``utils/contracts.py`` — реализация импортирует их оттуда, чтобы
смежные ветки Wave 2 (B, D) могли пользоваться теми же типами.

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
from typing import Mapping, Optional
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import JURY_ROUND_DEADLINE_HOURS, JURY_ROUNDS, TOP_N
from database.db import get_session
from database.models import (
    AgeCategory,
    Application,
    JuryRound,
    JuryRoundStatus,
    JuryStatus,
    JuryVote,
    JuryVoteState,
    JuryVoteValue,
    ModerationStatus,
    Track,
)
from services.pools import (
    all_pools,
    get_jury_for_pool,
    get_pool_applications,
)
from utils.contracts import JuryTaskDTO, PoolKey, RoundResult


# =====================================================================
# Внутренние структуры и хелперы
# =====================================================================


@dataclass
class _RoundOutcome:
    """Результат прогона алгоритма §35.2 для одного раунда.

    ``is_tied=False`` — топ-N сформирован (``top_ids`` финальные).
    ``is_tied=True`` — ничья на границе TOP_N: ``above_tie_ids``
    выше зоны ничьи, ``tie_ids`` — сама зона, кандидаты следующего
    раунда = их объединение.
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
    """Подсчёт SUBMITTED ``YES``-голосов по каждой работе раунда (§35.4).

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


async def _get_round_candidates(
    round_obj: JuryRound,
    *,
    session: AsyncSession,
) -> list[Application]:
    """Список заявок-кандидатов раунда (§35.2).

    - Раунд 1: все ``ДОПУЩЕНО``-заявки пула, созданные не позже
      ``round_obj.opened_at`` (фиксированный снапшот на момент открытия).
    - Раунды 2/3: above_tie ∪ tie_zone предыдущего раунда. Рекурсивно
      вычисляется детерминированно из ``SUBMITTED``-голосов прошлого
      раунда — никаких отдельных «таблиц кандидатов» не нужно.
    """
    pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)
    if round_obj.round_no == 1:
        apps = await get_pool_applications(
            pool,
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
    prior_outcome = await _compute_round_outcome(prior, session=session)
    next_ids = list(prior_outcome.above_tie_ids) + list(prior_outcome.tie_ids)
    if not next_ids:
        return []
    result = await session.execute(
        select(Application).where(Application.id.in_(next_ids))
    )
    apps_by_id = {a.id: a for a in result.scalars().all()}
    return [apps_by_id[i] for i in next_ids if i in apps_by_id]


async def _compute_round_outcome(
    round_obj: JuryRound,
    *,
    session: AsyncSession,
) -> _RoundOutcome:
    """Применить §35.2 к раунду: вернуть отсортированный результат + tie-зону.

    Сортировка: по голосам ``YES`` DESC, при равных — по
    ``(created_at ASC, id ASC)`` — единый детерминированный порядок,
    совпадающий с порядком карусели судьи.
    """
    candidates = await _get_round_candidates(round_obj, session=session)
    counts = await _count_yes_per_app(round_obj.id, session=session)

    sorted_apps = sorted(
        candidates,
        key=lambda a: (-counts.get(a.id, 0), a.created_at, a.id),
    )
    sorted_ids = [a.id for a in sorted_apps]

    if len(sorted_apps) <= TOP_N:
        return _RoundOutcome(
            counts=counts,
            sorted_app_ids=sorted_ids,
            top_ids=sorted_ids,
            above_tie_ids=sorted_ids,
            tie_ids=[],
            is_tied=False,
        )

    votes_at_n = counts.get(sorted_apps[TOP_N - 1].id, 0)
    votes_at_n_plus_1 = counts.get(sorted_apps[TOP_N].id, 0)
    if votes_at_n > votes_at_n_plus_1:
        top_ids = sorted_ids[:TOP_N]
        return _RoundOutcome(
            counts=counts,
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
        counts=counts,
        sorted_app_ids=sorted_ids,
        top_ids=[],
        above_tie_ids=above_tie,
        tie_ids=tie_zone,
        is_tied=True,
    )


# =====================================================================
# Открытие раунда (§35.2, §35.6)
# =====================================================================


async def open_round(
    *,
    track: Track,
    age_category: AgeCategory,
    round_no: int,
    candidates: list[Application],
    session: Optional[AsyncSession] = None,
) -> JuryRound:
    """Открыть новый раунд по пулу (§35.2, §35.6).

    Создаёт запись ``JuryRound`` со ``status=OPEN``, ``opened_at=now()``,
    ``deadline_at = now() + JURY_ROUND_DEADLINE_HOURS``. На раунде 1
    дополнительно переводит все заявки пула со статусом ``ДОПУЩЕНО`` в
    ``jury_status = НА_ГОЛОСОВАНИИ`` (синхронизация поля №16 реестра,
    §25.3.3).

    ``candidates`` для раунда 1 игнорируется (берётся всё из
    ``get_pool_applications`` на момент открытия — единый снапшот
    через ``opened_at``); для раундов 2/3 список нужен **только**
    в логе/sanity-check — кандидаты пересчитываются детерминированно
    из ``compute_round_outcome`` предыдущего раунда. Это сделано
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
        return round_obj


# =====================================================================
# Черновики голосов + отправка (§35.3, §35.4)
# =====================================================================


async def upsert_draft_vote(
    *,
    round_id: UUID,
    application_id: UUID,
    jury_huid: UUID,
    vote: JuryVoteValue,
    session: Optional[AsyncSession] = None,
) -> JuryVote:
    """Сохранить черновик голоса (§35.3).

    UPSERT по уникальному ключу ``(round_id, application_id, jury_huid)``:
    - если записи нет — создаёт новую с ``state=DRAFT``;
    - если есть — обновляет ``vote``, оставляя ``state`` как был.
      Если запись уже ``SUBMITTED`` — RuntimeError (после отправки
      повторная подача в раунде невозможна, §35.4).

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
                    " в этом раунде запрещена (§35.4)"
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
) -> None:
    """Зафиксировать оценки судьи в раунде (§35.3, §35.4).

    Принимает словарь ``app_id → JuryVoteValue`` со всеми работами пула
    в карусели. Алгоритм:

    1. Проверить, что ``votes`` покрывает **все** кандидаты раунда.
    2. Проверить правило разброса §35.1: есть и YES, и NO (если работ
       больше одной).
    3. UPSERT каждой записи ``(round_id, app_id, jury_huid)`` с
       ``state=SUBMITTED``, ``submitted_at=now()``. Это перезаписывает
       любые остаточные DRAFT.
    4. Если **все** назначенные на пул судьи отправили оценки —
       автоматически закрыть раунд (§35.4 пункт «а»).

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
                    "Правило разброса (§35.1): должна быть хотя бы одна"
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

    if all_submitted:
        logger.info(
            "Все назначенные судьи проголосовали — автоматическое закрытие раунда",
            round_id=str(round_id),
        )
        await close_round(round_id, session=session)


# =====================================================================
# Закрытие раунда, расчёт топ-N, жребий (§35.2, §35.4, §35.5)
# =====================================================================


async def close_round(
    round_id: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> RoundResult:
    """Закрыть раунд по триггеру (полнота / дедлайн / команда модератора).

    Алгоритм (§35.4):
    1. UPDATE ... SET status=CLOSED WHERE id=:id AND status=OPEN —
       идемпотентно: если кто-то уже закрыл, ничего не делаем.
    2. Подсчёт SUBMITTED-голосов, агрегация в
       ``Application.jury_round{N}_yes``.
    3. Прогон ``_compute_round_outcome`` — определение topN или зоны ничьи.
    4. Если ничья и ``round_no < JURY_ROUNDS`` — открываем следующий
       раунд (без жребия).
    5. Если ничья и это последний раунд — отметим, что нужен жребий
       (``apply_lot_if_needed`` вызывается отдельно: либо в этом же
       вызове, либо модератором/планировщиком).

    Возвращает ``RoundResult`` с агрегатами для логирования и для
    интеграции с уведомлениями.
    """
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
            outcome = await _compute_round_outcome(round_obj, session=s)
            return RoundResult(
                pool=PoolKey(track=round_obj.track, age_category=round_obj.age_category),
                round_no=round_obj.round_no,
                top_ids=tuple(outcome.top_ids),
                tie_ids=tuple(outcome.tie_ids),
                decided_by_lot=(),
                needs_next_round=outcome.is_tied
                and round_obj.round_no < JURY_ROUNDS,
                closed_at=round_obj.closed_at or now,
            )

        await s.refresh(round_obj)
        outcome = await _compute_round_outcome(round_obj, session=s)

        round_field = {
            1: Application.jury_round1_yes,
            2: Application.jury_round2_yes,
            3: Application.jury_round3_yes,
        }.get(round_obj.round_no)
        if round_field is not None:
            for app_id, yes_count in outcome.counts.items():
                await s.execute(
                    update(Application)
                    .where(Application.id == app_id)
                    .values({round_field: yes_count})
                )

        needs_next = outcome.is_tied and round_obj.round_no < JURY_ROUNDS

        if session is None:
            await s.commit()

        logger.info(
            "Раунд закрыт",
            round_id=str(round_id),
            round_no=round_obj.round_no,
            track=round_obj.track.name,
            age_category=round_obj.age_category.name,
            is_tied=outcome.is_tied,
            needs_next_round=needs_next,
            top_count=len(outcome.top_ids),
            tie_count=len(outcome.tie_ids),
            above_tie_count=len(outcome.above_tie_ids),
        )

        result_dto = RoundResult(
            pool=PoolKey(track=round_obj.track, age_category=round_obj.age_category),
            round_no=round_obj.round_no,
            top_ids=tuple(outcome.top_ids),
            tie_ids=tuple(outcome.tie_ids),
            decided_by_lot=(),
            needs_next_round=needs_next,
            closed_at=now,
        )

    if needs_next:
        next_candidate_ids = list(outcome.above_tie_ids) + list(outcome.tie_ids)
        async with _open_session_ctx(session) as s2:
            result_apps = await s2.execute(
                select(Application).where(Application.id.in_(next_candidate_ids))
            )
            next_candidates = list(result_apps.scalars().all())
        await open_round(
            track=result_dto.pool.track,
            age_category=result_dto.pool.age_category,
            round_no=round_obj.round_no + 1,
            candidates=next_candidates,
            session=session,
        )

    return result_dto


async def compute_top_n(
    round_id: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> list[Application]:
    """Сформировать топ-N для уже закрытого раунда (§35.2).

    Если ``is_tied=False`` — возвращает заявки топ-N.
    Если ``is_tied=True`` — возвращает кандидатов на следующий
    раунд (``above_tie ∪ tie_zone``), готовых для ``open_round(N+1)``.
    Caller отличает случаи по тому, что во втором случае размер
    больше TOP_N (или ровно столько, сколько кандидатов в зоне ничьи).
    """
    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")
        outcome = await _compute_round_outcome(round_obj, session=s)
        ids = outcome.top_ids if not outcome.is_tied else (
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
) -> list[Application]:
    """Автоматический жребий §35.2 finale.

    Срабатывает, если последний раунд закрыт с ничьёй на границе
    топ-N. Случайно выбирает нужное число работ из зоны ничьи,
    помечает им ``jury_decided_by_lot=True``, переводит раунд в
    статус ``DRAWN_BY_LOT``.

    Возвращает список заявок, попавших в топ-N **по жребию**
    (без already-confirmed-частью above_tie). Если жребий не
    нужен — возвращает пустой список.
    """
    async with _open_session_ctx(session) as s:
        round_obj = await _get_round(round_id, session=s)
        if round_obj is None:
            raise LookupError(f"Раунд {round_id} не найден")
        outcome = await _compute_round_outcome(round_obj, session=s)
        if not outcome.is_tied:
            return []
        remaining = TOP_N - len(outcome.above_tie_ids)
        if remaining <= 0:
            logger.info(
                "Жребий: above_tie уже покрывает топ-N — жребий не нужен",
                round_id=str(round_id),
            )
            return []
        if remaining >= len(outcome.tie_ids):
            chosen_ids = list(outcome.tie_ids)
        else:
            chosen_ids = random.sample(outcome.tie_ids, remaining)

        for app_id in chosen_ids:
            await s.execute(
                update(Application)
                .where(Application.id == app_id)
                .values(jury_decided_by_lot=True)
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
        )
        return chosen


# =====================================================================
# Шорт-лист (§35.5)
# =====================================================================


async def _finalize_pool(
    pool: PoolKey,
    *,
    session: AsyncSession,
) -> list[Application]:
    """Зафиксировать результаты пула: проставить ``jury_status``,
    ``jury_final_round``, ``pool_position``. Возвращает работы топ-N.
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
    if not rounds:
        return []
    final_round = rounds[0]
    if final_round.status == JuryRoundStatus.OPEN:
        return []

    outcome = await _compute_round_outcome(final_round, session=session)

    if outcome.is_tied:
        lot_apps = await apply_lot_if_needed(final_round.id, session=session)
        lot_ids = {a.id for a in lot_apps}
        top_ids = list(outcome.above_tie_ids) + list(lot_ids)
    else:
        top_ids = list(outcome.top_ids)
        lot_ids = set()

    top_set = set(top_ids)
    all_pool_apps = await get_pool_applications(
        pool,
        session=session,
        status_filter=None,
    )

    position_by_id = {
        app_id: idx + 1 for idx, app_id in enumerate(outcome.sorted_app_ids)
    }
    next_pos = len(position_by_id) + 1

    for app in all_pool_apps:
        if app.id in top_set:
            new_status = JuryStatus.V_TOP_10
            jury_final = final_round.round_no
            decided_by_lot = app.id in lot_ids
        elif app.jury_status in (
            JuryStatus.NE_PEREDANO_ZHYURI,
            JuryStatus.NA_GOLOSOVANII,
        ):
            if app.moderation_status != ModerationStatus.DOPUSHCHENO:
                continue
            new_status = JuryStatus.NE_VOSHLO_V_TOP_10
            jury_final = final_round.round_no
            decided_by_lot = False
        else:
            continue

        pos = position_by_id.get(app.id)
        if pos is None:
            pos = next_pos
            next_pos += 1

        await session.execute(
            update(Application)
            .where(Application.id == app.id)
            .values(
                jury_status=new_status,
                jury_final_round=jury_final,
                jury_decided_by_lot=decided_by_lot
                if app.id in top_set
                else app.jury_decided_by_lot,
                pool_position=pos,
            )
        )

    result_apps = await session.execute(
        select(Application).where(Application.id.in_(top_set))
    )
    return list(result_apps.scalars().all())


async def build_shortlist(
    *,
    session: Optional[AsyncSession] = None,
) -> list[Application]:
    """Сформировать шорт-лист по итогам всех 12 пулов (§35.5).

    Должна вызываться, когда все 12 пулов имеют закрытый финальный
    раунд (CLOSED или DRAWN_BY_LOT). Алгоритм:

    1. По каждому пулу — взять последний раунд, прогнать жребий
       (если нужно).
    2. Проставить ``Application.jury_status``, ``jury_final_round``,
       ``pool_position``, ``jury_decided_by_lot`` для каждой заявки
       пула (синхронизация полей реестра 23–29, см. §25.3.1).
    3. Вернуть плоский список заявок топ-N всех пулов (для дальнейшей
       Excel-выгрузки шорт-листа, §27.1 ``/export_shortlist``).

    Уведомления участников и чата модерации (§18.6, §35.5) —
    задача D / notifications. Здесь только обновление БД и логи.
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
        return all_shortlist


# =====================================================================
# Запросы для UX жюри (§27.4 /jury_tasks, §35.3)
# =====================================================================


async def get_open_tasks_for_jury(
    jury_huid: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> list[JuryTaskDTO]:
    """Список открытых задач судьи (§27.4 ``/jury_tasks``).

    Возвращает плоский список ``JuryTaskDTO`` — по одной DTO на
    каждую работу в карусели каждого открытого раунда, где судья:
    - назначен на пул (через ``JuryPoolAssignment`` или fallback);
    - ещё не отправил оценки в этом раунде.

    Порядок: сначала по пулу (``track`` → ``age_category``), затем по
    ``round_no``, затем внутри раунда — по ``(created_at ASC, id ASC)``
    единый для всех судей (§35.3, новая правка Wave 0).

    ``draft_vote`` — текущее значение черновика, чтобы handler мог
    отрисовать эмодзи на кнопке ``Да``/``Нет``.

    Превью изображения и ``cloud_link`` подставляются ленивым
    обращением к ``services.storage.get_preview_path`` (см. §35.3 +
    §33.6.4 для режима ``links``). Если функция не найдена —
    в DTO будет ``preview_path=None`` и ``cloud_link``=значение
    ``Application.cloud_link``.
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

        get_preview_path = _resolve_storage_preview_path()

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
                preview_path = None
                if get_preview_path is not None:
                    try:
                        preview_path = await _maybe_await(
                            get_preview_path(app.id)
                        )
                    except Exception:
                        logger.exception(
                            "Не удалось получить превью для заявки",
                            application_id=str(app.id),
                        )
                        preview_path = None
                result.append(
                    JuryTaskDTO(
                        round_id=round_obj.id,
                        application_id=app.id,
                        pool=pool,
                        round_no=round_obj.round_no,
                        local_no=local_no,
                        title=app.title,
                        description=app.description,
                        preview_path=preview_path,
                        cloud_link=app.cloud_link,
                        draft_vote=drafts_for_round.get(app.id),
                    )
                )
        return result


def _resolve_storage_preview_path():
    """Ленивая проверка наличия функции ``get_preview_path`` в services.storage.

    Контракт ``StorageService`` пока её не описывает — функция
    добавляется веткой D (Wave 2). Чтобы не блокировать сборку
    Wave 2 / C, мы импортируем её ленивые. Если функции нет — DTO
    отдаются без превью (``preview_path=None``), handler покажет
    «превью недоступно» в режиме files либо `cloud_link` в режиме
    links (см. §33.6.4).
    """
    try:
        from services import storage as _storage
    except ImportError:
        return None
    fn = getattr(_storage, "get_preview_path", None)
    return fn if callable(fn) else None


async def _maybe_await(value):
    """Поддержка как async, так и sync ``get_preview_path``."""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


# =====================================================================
# Доп. публичные хелперы для хендлеров
# =====================================================================


async def get_jury_progress(
    jury_huid: UUID,
    *,
    session: Optional[AsyncSession] = None,
) -> dict[str, int]:
    """Прогресс судьи (для ``/jury_status``, §27.4).

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
]
