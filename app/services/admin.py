"""
Сервис агрегатов для админ-меню.

Счётчики и сводки для ``/admin`` и раздела ``/admin_stats``.
Все запросы — без N+1: один SELECT / GROUP BY на метрику.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from database.db import get_session
from database.models import (
    Application,
    ApplicationFile,
    DiskAlert,
    JuryMember,
    JuryRound,
    JuryRoundStatus,
    JuryStatus,
    JuryVote,
    JuryVoteState,
    Moderator,
    User,
)
from services import access
from services.intake_mode import get_intake_mode
from services.storage import get_disk_usage_bytes, get_disk_usage_pct


@dataclass(frozen=True)
class AdminOverview:
    """Краткая сводка для бейджей главного меню админки."""

    moderators_count: int
    jury_count: int
    moderation_chat_configured: bool
    intake_mode: str
    disk_pct: float


@dataclass(frozen=True)
class UserStats:
    """Метрики пользователей бота."""

    total: int
    with_chat_id: int
    new_last_24h: int
    active_last_24h: int


@dataclass(frozen=True)
class JuryAggregate:
    """Сводка процесса жюри."""

    open_rounds: int
    closed_rounds: int
    drawn_by_lot: int
    top10_applications: int
    open_rounds_with_votes: int
    submitted_votes: int


@dataclass(frozen=True)
class DiskForecast:
    """Прогноз заполнения диска."""

    used_bytes: int
    total_bytes: int
    free_bytes: int
    pct: float
    apps_last_7d: int
    avg_bytes_per_app: float
    hours_left: float | None


@dataclass(frozen=True)
class AdminStatsReport:
    """Полный отчёт для ``/admin_stats``."""

    overview: AdminOverview
    users: UserStats
    apps_today: int
    apps_total: int
    jury: JuryAggregate
    disk: DiskForecast
    disk_alerts_7d: int
    by_track: dict[str, int] = field(default_factory=dict)
    by_age: dict[str, int] = field(default_factory=dict)
    by_moderation_status: dict[str, int] = field(default_factory=dict)


async def overview_counters() -> AdminOverview:
    """Счётчики для бейджей кнопок главного меню админки."""
    mode = await get_intake_mode()
    pct = get_disk_usage_pct()
    mod_chat = access.get_moderation_chat_id()

    async with get_session()() as session:
        mods = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Moderator)
                    .where(Moderator.is_active.is_(True))
                )
            ).scalar_one()
        )
        jury = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(JuryMember)
                    .where(JuryMember.is_active.is_(True))
                )
            ).scalar_one()
        )

    return AdminOverview(
        moderators_count=mods,
        jury_count=jury,
        moderation_chat_configured=mod_chat is not None,
        intake_mode=mode.value.upper(),
        disk_pct=pct,
    )


async def count_users_stats() -> UserStats:
    """Метрики таблицы ``users``."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with get_session()() as session:
        total = int(
            (await session.execute(select(func.count()).select_from(User))).scalar_one()
        )
        with_chat = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.chat_id.is_not(None))
                )
            ).scalar_one()
        )
        new_24h = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.created_at >= cutoff)
                )
            ).scalar_one()
        )
        active_24h = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.last_activity >= cutoff)
                )
            ).scalar_one()
        )
    return UserStats(
        total=total,
        with_chat_id=with_chat,
        new_last_24h=new_24h,
        active_last_24h=active_24h,
    )


async def jury_aggregate_state() -> JuryAggregate:
    """Агрегаты по раундам и голосам жюри."""
    async with get_session()() as session:
        round_rows = (
            await session.execute(
                select(JuryRound.status, func.count()).group_by(JuryRound.status)
            )
        ).all()
        counts = {status: int(cnt) for status, cnt in round_rows}

        top10 = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Application)
                    .where(Application.jury_status == JuryStatus.V_TOP_10)
                )
            ).scalar_one()
        )

        open_round_ids = [
            r.id
            for r in (
                await session.execute(
                    select(JuryRound.id).where(
                        JuryRound.status == JuryRoundStatus.OPEN
                    )
                )
            ).scalars().all()
        ]
        submitted = 0
        if open_round_ids:
            submitted = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(JuryVote)
                        .where(
                            JuryVote.round_id.in_(open_round_ids),
                            JuryVote.state == JuryVoteState.SUBMITTED,
                        )
                    )
                ).scalar_one()
            )

    return JuryAggregate(
        open_rounds=counts.get(JuryRoundStatus.OPEN, 0),
        closed_rounds=counts.get(JuryRoundStatus.CLOSED, 0),
        drawn_by_lot=counts.get(JuryRoundStatus.DRAWN_BY_LOT, 0),
        top10_applications=top10,
        open_rounds_with_votes=len(open_round_ids),
        submitted_votes=submitted,
    )


async def disk_forecast() -> DiskForecast:
    """Прогноз исчерпания диска по темпу заявок за 7 дней."""
    used, total = get_disk_usage_bytes()
    free = max(total - used, 0)
    pct = get_disk_usage_pct()
    cutoff = datetime.utcnow() - timedelta(days=7)

    async with get_session()() as session:
        apps_7d = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Application)
                    .where(Application.created_at >= cutoff)
                )
            ).scalar_one()
        )
        size_sum = (
            await session.execute(
                select(func.coalesce(func.sum(ApplicationFile.size_bytes), 0))
                .select_from(ApplicationFile)
                .join(Application, ApplicationFile.application_id == Application.id)
                .where(Application.created_at >= cutoff)
            )
        ).scalar_one()
        apps_7d = max(apps_7d, 1)
        avg_bytes = float(size_sum or 0) / apps_7d

    hours_left: float | None = None
    if avg_bytes > 0 and apps_7d > 0:
        per_day = apps_7d / 7.0
        per_day_bytes = per_day * avg_bytes
        if per_day_bytes > 0:
            hours_left = (free / per_day_bytes) * 24.0

    return DiskForecast(
        used_bytes=used,
        total_bytes=total,
        free_bytes=free,
        pct=pct,
        apps_last_7d=apps_7d,
        avg_bytes_per_app=avg_bytes,
        hours_left=hours_left,
    )


async def count_disk_alerts_since(days: int = 7) -> int:
    """Число disk_alerts за последние ``days`` дней."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with get_session()() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(DiskAlert)
                    .where(DiskAlert.created_at >= cutoff)
                )
            ).scalar_one()
        )


async def apps_by_parent_huid(parent_huid: UUID) -> list[Application]:
    """Все заявки родителя (с файлами), новые первыми."""
    async with get_session()() as session:
        result = await session.execute(
            select(Application)
            .where(Application.parent_huid == parent_huid)
            .options(selectinload(Application.files))
            .order_by(Application.created_at.desc())
        )
        return list(result.scalars().all())


async def build_admin_stats_report() -> AdminStatsReport:
    """Собрать полный отчёт ``/admin_stats``."""
    overview = await overview_counters()
    users = await count_users_stats()
    jury = await jury_aggregate_state()
    disk = await disk_forecast()
    alerts_7d = await count_disk_alerts_since(7)

    today = datetime.utcnow().date()
    today_start = datetime.combine(today, datetime.min.time())

    async with get_session()() as session:
        apps_today = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Application)
                    .where(Application.created_at >= today_start)
                )
            ).scalar_one()
        )
        apps_total = int(
            (await session.execute(select(func.count()).select_from(Application))).scalar_one()
        )
        by_track = {
            track.value: int(cnt)
            for track, cnt in (
                await session.execute(
                    select(Application.track, func.count()).group_by(Application.track)
                )
            ).all()
        }
        by_age = {
            cat.value: int(cnt)
            for cat, cnt in (
                await session.execute(
                    select(Application.age_category, func.count()).group_by(
                        Application.age_category
                    )
                )
            ).all()
        }
        by_status = {
            st.value: int(cnt)
            for st, cnt in (
                await session.execute(
                    select(Application.moderation_status, func.count()).group_by(
                        Application.moderation_status
                    )
                )
            ).all()
        }

    return AdminStatsReport(
        overview=overview,
        users=users,
        apps_today=apps_today,
        apps_total=apps_total,
        jury=jury,
        disk=disk,
        disk_alerts_7d=alerts_7d,
        by_track=by_track,
        by_age=by_age,
        by_moderation_status=by_status,
    )


__all__ = [
    "AdminOverview",
    "UserStats",
    "JuryAggregate",
    "DiskForecast",
    "AdminStatsReport",
    "overview_counters",
    "count_users_stats",
    "jury_aggregate_state",
    "disk_forecast",
    "count_disk_alerts_since",
    "apps_by_parent_huid",
    "build_admin_stats_report",
]
