#!/usr/bin/env python3
"""
Дозаполнение голосования жюри: в незаполненных OPEN-раундах остаётся одна неоценённая работа.

Важно: новые голоса пишутся только как ``DRAFT`` (не ``SUBMITTED``), иначе бот
считает раунд отправленным и может закрыть пул без нажатия «Отправить оценки».

Полностью проголосованные раунды (голос по каждому кандидату) не трогаются.

--- Восстановление после ошибочного ``--apply`` со старой версией (SUBMITTED) ---

    docker exec kids_ai_bot python3 scripts/jury_almost_complete_votes.py --repair --dry-run \\
      --jury-huid d58a40a1-1aa8-55cf-8910-370a7ff5e274 \\
      --jury-huid 16bce2de-8ce7-5e40-ad71-2353a1fede07 \\
      --jury-huid 6ef7e757-de56-5f9c-995d-4255d7eb2ca0

    docker exec kids_ai_bot python3 scripts/jury_almost_complete_votes.py --repair --apply \\
      --reopen-closed-rounds \\
      --jury-huid d58a40a1-1aa8-55cf-8910-370a7ff5e274 \\
      --jury-huid 16bce2de-8ce7-5e40-ad71-2353a1fede07 \\
      --jury-huid 6ef7e757-de56-5f9c-995d-4255d7eb2ca0

``--repair``: неполные бюллетени (SUBMITTED не по всем работам) → DRAFT.
``--reopen-closed-rounds``: раунд 1 CLOSED → OPEN, сброс агрегатов/статусов пула,
удаление раундов 2+ (тестовый стенд).

--- Обычное дозаполнение (черновики) ---

    docker exec kids_ai_bot python3 scripts/jury_almost_complete_votes.py --dry-run \\
      --jury-huid ...

    docker exec kids_ai_bot python3 scripts/jury_almost_complete_votes.py --apply \\
      --jury-huid ...

Проверка:

    docker exec kids_ai_db psql -U postgres -d kids_ai -c "
    SELECT jr.track, jr.age_category, jr.round_no, jr.status, jv.jury_huid,
           count(*) AS vote_rows,
           count(*) FILTER (WHERE jv.state = 'SUBMITTED') AS submitted,
           count(*) FILTER (WHERE jv.state = 'DRAFT') AS drafts
    FROM jury_rounds jr
    LEFT JOIN jury_votes jv ON jv.round_id = jr.id
    WHERE jr.round_no = 1
    GROUP BY 1,2,3,4,5
    ORDER BY 1,2,3,5;"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, select, update

_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from database.db import get_session
from database.models import (
    Application,
    JuryMember,
    JuryRound,
    JuryRoundAggregate,
    JuryRoundStatus,
    JuryStatus,
    JuryVote,
    JuryVoteState,
    JuryVoteValue,
)
from services.jury import _get_round_candidates
from services.pools import all_pools, get_jury_for_pool
from utils.contracts import PoolKey

DEFAULT_JURY_HUIDS: tuple[str, ...] = (
    "d58a40a1-1aa8-55cf-8910-370a7ff5e274",
    "16bce2de-8ce7-5e40-ad71-2353a1fede07",
    "6ef7e757-de56-5f9c-995d-4255d7eb2ca0",
)


@dataclass
class FillStats:
    """Сводка дозаполнения."""

    skip_filled: int = 0
    skip_one_left: int = 0
    skip_no_candidates: int = 0
    filled: int = 0
    votes_inserted: int = 0
    details: list[str] = field(default_factory=list)


@dataclass
class RepairStats:
    """Сводка восстановления."""

    skip_complete_ballot: int = 0
    demoted_ballots: int = 0
    demoted_votes: int = 0
    reopened_rounds: int = 0
    deleted_later_rounds: int = 0
    reverted_applications: int = 0
    details: list[str] = field(default_factory=list)


def _vote_for_new_row(insert_index: int) -> JuryVoteValue:
    """YES/NO по очереди среди новых строк."""
    return JuryVoteValue.YES if insert_index % 2 == 0 else JuryVoteValue.NO


def pick_leave_unrated(
    candidates: list[Application],
    unrated: list[Application],
) -> Application:
    """Работа без голоса: последняя в пуле, если неоценена, иначе последняя среди unrated."""
    last_in_pool = candidates[-1]
    unrated_ids = {a.id for a in unrated}
    if last_in_pool.id in unrated_ids:
        return last_in_pool
    return unrated[-1]


def _is_complete_ballot(
    existing: list[JuryVote],
    candidate_ids: set[UUID],
) -> bool:
    """У судьи SUBMITTED по каждому кандидату раунда."""
    submitted_apps = {
        v.application_id
        for v in existing
        if v.state == JuryVoteState.SUBMITTED
    }
    return submitted_apps == candidate_ids and len(candidate_ids) > 0


async def process_jury_round_fill(
    *,
    round_obj: JuryRound,
    jury_huid: UUID,
    session,
    dry_run: bool,
    stats: FillStats,
) -> None:
    """Дозаполнить черновиками до одной неоценённой работы."""
    pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)
    pool_label = pool.as_label()
    candidates = await _get_round_candidates(round_obj, session=session)
    n = len(candidates)
    if n == 0:
        stats.skip_no_candidates += 1
        stats.details.append(
            f"skip no_candidates: {pool_label} r{round_obj.round_no} jury={jury_huid}"
        )
        return

    candidate_ids = {c.id for c in candidates}
    existing = list(
        (
            await session.execute(
                select(JuryVote).where(
                    JuryVote.round_id == round_obj.id,
                    JuryVote.jury_huid == jury_huid,
                )
            )
        ).scalars().all()
    )

    if _is_complete_ballot(existing, candidate_ids):
        stats.skip_filled += 1
        stats.details.append(
            f"skip filled: {pool_label} r{round_obj.round_no} jury={jury_huid} N={n}"
        )
        return

    voted_ids = {v.application_id for v in existing}
    unrated = [c for c in candidates if c.id not in voted_ids]

    if len(unrated) == 1:
        stats.skip_one_left += 1
        stats.details.append(
            f"skip one_left: {pool_label} r{round_obj.round_no} jury={jury_huid} "
            f"leave={unrated[0].br_id}"
        )
        return

    if len(unrated) < 2:
        stats.skip_filled += 1
        stats.details.append(
            f"skip unexpected: {pool_label} r{round_obj.round_no} jury={jury_huid} "
            f"unrated={len(unrated)} votes={len(existing)}"
        )
        return

    leave_unrated = pick_leave_unrated(candidates, unrated)
    to_insert = [
        c for c in candidates if c.id != leave_unrated.id and c.id not in voted_ids
    ]
    if not to_insert:
        stats.skip_one_left += 1
        stats.details.append(
            f"skip nothing_to_insert: {pool_label} r{round_obj.round_no} "
            f"jury={jury_huid} leave={leave_unrated.br_id}"
        )
        return

    insert_lines: list[str] = []
    for idx, app in enumerate(to_insert):
        value = _vote_for_new_row(idx)
        insert_lines.append(f"{app.br_id}={value.name}")
        if not dry_run:
            session.add(
                JuryVote(
                    round_id=round_obj.id,
                    application_id=app.id,
                    jury_huid=jury_huid,
                    vote=value,
                    state=JuryVoteState.DRAFT,
                )
            )

    stats.filled += 1
    stats.votes_inserted += len(to_insert)
    stats.details.append(
        f"{'would_fill' if dry_run else 'filled'}: {pool_label} r{round_obj.round_no} "
        f"jury={jury_huid} leave={leave_unrated.br_id} "
        f"+{len(to_insert)} DRAFT [{', '.join(insert_lines[:5])}"
        f"{'…' if len(insert_lines) > 5 else ''}]"
    )


async def repair_jury_ballot(
    *,
    round_obj: JuryRound,
    jury_huid: UUID,
    session,
    dry_run: bool,
    stats: RepairStats,
) -> None:
    """Откатить ошибочные SUBMITTED (неполный бюллетень) в DRAFT."""
    pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)
    pool_label = pool.as_label()
    candidates = await _get_round_candidates(round_obj, session=session)
    if not candidates:
        return

    candidate_ids = {c.id for c in candidates}
    existing = list(
        (
            await session.execute(
                select(JuryVote).where(
                    JuryVote.round_id == round_obj.id,
                    JuryVote.jury_huid == jury_huid,
                )
            )
        ).scalars().all()
    )
    if not existing:
        return

    if _is_complete_ballot(existing, candidate_ids):
        stats.skip_complete_ballot += 1
        stats.details.append(
            f"repair skip complete: {pool_label} r{round_obj.round_no} jury={jury_huid}"
        )
        return

    submitted_rows = [v for v in existing if v.state == JuryVoteState.SUBMITTED]
    if not submitted_rows:
        stats.details.append(
            f"repair skip no_submitted: {pool_label} r{round_obj.round_no} "
            f"jury={jury_huid}"
        )
        return

    if not dry_run:
        for row in existing:
            if row.state == JuryVoteState.SUBMITTED:
                row.state = JuryVoteState.DRAFT
                row.submitted_at = None

    stats.demoted_ballots += 1
    stats.demoted_votes += len(submitted_rows)
    stats.details.append(
        f"{'would_demote' if dry_run else 'demoted'}: {pool_label} r{round_obj.round_no} "
        f"jury={jury_huid} submitted_rows={len(submitted_rows)}/{len(candidate_ids)}"
    )


async def reopen_closed_round_one(
    pool: PoolKey,
    *,
    session,
    dry_run: bool,
    stats: RepairStats,
) -> None:
    """Вернуть преждевременно закрытый раунд 1 в OPEN (тестовый откат)."""
    pool_label = pool.as_label()
    round_r1 = (
        await session.execute(
            select(JuryRound).where(
                JuryRound.track == pool.track,
                JuryRound.age_category == pool.age_category,
                JuryRound.round_no == 1,
            )
        )
    ).scalar_one_or_none()

    if round_r1 is None:
        stats.details.append(f"reopen skip no_r1: {pool_label}")
        return

    if round_r1.status != JuryRoundStatus.CLOSED:
        stats.details.append(
            f"reopen skip status={round_r1.status.name}: {pool_label} r1"
        )
        return

    later_rounds = list(
        (
            await session.execute(
                select(JuryRound).where(
                    JuryRound.track == pool.track,
                    JuryRound.age_category == pool.age_category,
                    JuryRound.round_no > 1,
                )
            )
        ).scalars().all()
    )

    if not dry_run:
        if later_rounds:
            await session.execute(
                delete(JuryRound).where(
                    JuryRound.track == pool.track,
                    JuryRound.age_category == pool.age_category,
                    JuryRound.round_no > 1,
                )
            )
        await session.execute(
            delete(JuryRoundAggregate).where(
                JuryRoundAggregate.round_id == round_r1.id
            )
        )
        revert_result = await session.execute(
            update(Application)
            .where(
                Application.track == pool.track,
                Application.age_category == pool.age_category,
                Application.jury_final_round == 1,
                Application.jury_status.in_(
                    (JuryStatus.V_TOP_10, JuryStatus.NE_VOSHLO_V_TOP_10)
                ),
            )
            .values(
                jury_status=JuryStatus.NA_GOLOSOVANII,
                jury_final_round=None,
                pool_position=None,
                jury_decided_by_lot=False,
            )
        )
        stats.reverted_applications += int(revert_result.rowcount or 0)

        round_r1.status = JuryRoundStatus.OPEN
        round_r1.closed_at = None

    stats.reopened_rounds += 1
    stats.deleted_later_rounds += len(later_rounds)
    stats.details.append(
        f"{'would_reopen' if dry_run else 'reopened'}: {pool_label} r1 "
        f"deleted_later_rounds={len(later_rounds)}"
    )


async def _load_active_jury(target: set[UUID]) -> list[UUID]:
    async with get_session()() as session:
        active_jury = set(
            (
                await session.execute(
                    select(JuryMember.huid).where(JuryMember.is_active.is_(True))
                )
            ).scalars().all()
        )
    missing = target - active_jury
    if missing:
        logger.warning(
            "HUID не в активных jury_members — пропуск",
            huids=[str(h) for h in missing],
        )
    return [h for h in target if h in active_jury]


async def run_fill(jury_huids: list[UUID], *, dry_run: bool) -> FillStats:
    """Дозаполнение DRAFT в OPEN-раундах."""
    stats = FillStats()
    jury_huids_active = await _load_active_jury(set(jury_huids))

    async with get_session()() as session:
        rounds = list(
            (
                await session.execute(
                    select(JuryRound)
                    .where(JuryRound.status == JuryRoundStatus.OPEN)
                    .order_by(
                        JuryRound.track,
                        JuryRound.age_category,
                        JuryRound.round_no,
                    )
                )
            ).scalars().all()
        )
        if not rounds:
            logger.warning("Нет OPEN-раундов")
            return stats

        for round_obj in rounds:
            pool = PoolKey(
                track=round_obj.track,
                age_category=round_obj.age_category,
            )
            pool_jury = await get_jury_for_pool(pool, session=session)
            for member in pool_jury:
                if member.huid not in jury_huids_active:
                    continue
                await process_jury_round_fill(
                    round_obj=round_obj,
                    jury_huid=member.huid,
                    session=session,
                    dry_run=dry_run,
                    stats=stats,
                )

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    return stats


async def run_repair(
    jury_huids: list[UUID],
    *,
    dry_run: bool,
    reopen_closed_rounds: bool,
) -> RepairStats:
    """Откат SUBMITTED и опционально переоткрытие раунда 1."""
    stats = RepairStats()
    jury_huids_active = await _load_active_jury(set(jury_huids))

    async with get_session()() as session:
        all_rounds = list(
            (
                await session.execute(
                    select(JuryRound).order_by(
                        JuryRound.track,
                        JuryRound.age_category,
                        JuryRound.round_no,
                    )
                )
            ).scalars().all()
        )

        for round_obj in all_rounds:
            pool = PoolKey(
                track=round_obj.track,
                age_category=round_obj.age_category,
            )
            pool_jury = await get_jury_for_pool(pool, session=session)
            for member in pool_jury:
                if member.huid not in jury_huids_active:
                    continue
                await repair_jury_ballot(
                    round_obj=round_obj,
                    jury_huid=member.huid,
                    session=session,
                    dry_run=dry_run,
                    stats=stats,
                )

        if reopen_closed_rounds:
            for pool in all_pools():
                await reopen_closed_round_one(
                    pool,
                    session=session,
                    dry_run=dry_run,
                    stats=stats,
                )

        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Жюри: дозаполнение DRAFT до одной неоценённой работы "
            "или восстановление после ошибочного SUBMITTED."
        ),
    )
    parser.add_argument(
        "--jury-huid",
        action="append",
        dest="jury_huids",
        metavar="UUID",
        help="HUID судьи (можно указать несколько раз)",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Откатить неполные SUBMITTED → DRAFT (после ошибочного apply)",
    )
    parser.add_argument(
        "--reopen-closed-rounds",
        action="store_true",
        help="С --repair: вернуть CLOSED раунд 1 в OPEN, сбросить итоги пула",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Без записи в БД (по умолчанию)",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Записать в БД",
    )
    return parser


def resolve_jury_huids(raw: list[str] | None) -> list[UUID]:
    sources = raw if raw else list(DEFAULT_JURY_HUIDS)
    result: list[UUID] = []
    for item in sources:
        try:
            result.append(UUID(item))
        except ValueError as exc:
            raise SystemExit(f"Невалидный --jury-huid: {item!r}") from exc
    return result


async def main_async(args: argparse.Namespace) -> int:
    dry_run = not bool(args.apply)
    jury_huids = resolve_jury_huids(args.jury_huids)

    if args.repair:
        logger.info(
            "Старт repair",
            dry_run=dry_run,
            reopen_closed_rounds=bool(args.reopen_closed_rounds),
        )
        stats = await run_repair(
            jury_huids,
            dry_run=dry_run,
            reopen_closed_rounds=bool(args.reopen_closed_rounds),
        )
        for line in stats.details:
            logger.info(line)
        logger.info(
            "Repair готово",
            skip_complete_ballot=stats.skip_complete_ballot,
            demoted_ballots=stats.demoted_ballots,
            demoted_votes=stats.demoted_votes,
            reopened_rounds=stats.reopened_rounds,
            deleted_later_rounds=stats.deleted_later_rounds,
            reverted_applications=stats.reverted_applications,
            dry_run=dry_run,
        )
        return 0

    logger.info("Старт fill", dry_run=dry_run, jury_count=len(jury_huids))
    stats = await run_fill(jury_huids, dry_run=dry_run)
    for line in stats.details:
        logger.info(line)
    logger.info(
        "Fill готово",
        skip_filled=stats.skip_filled,
        skip_one_left=stats.skip_one_left,
        skip_no_candidates=stats.skip_no_candidates,
        filled=stats.filled,
        votes_inserted=stats.votes_inserted,
        dry_run=dry_run,
    )
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.apply and not args.dry_run:
        args.dry_run = True
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
