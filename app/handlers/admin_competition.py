"""
Раздел админ-меню «Конкурс»: приём, выгрузки, архив, управление жюри.
"""
from __future__ import annotations

from datetime import datetime

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage
from pybotx.models.attachments import OutgoingAttachment
from sqlalchemy import func, select

from config import TOP_N
from database.db import get_session
from database.models import (
    AgeCategory,
    Application,
    JuryRound,
    JuryRoundStatus,
    JuryStatus,
    JuryVote,
    JuryVoteState,
    ModerationStatus,
    Track,
)
from fsm import cleanup_middleware, fsm_middleware
from handlers.moderator_jury_admin import ALL_POOLS, _format_pool, parse_pool_token
from keyboards import (
    admin_competition_archive_bubbles,
    admin_competition_data_bubbles,
    admin_competition_jury_bubbles,
    admin_competition_jury_start_bubbles,
    admin_competition_menu_bubbles,
    admin_competition_pool_bubbles,
    admin_confirm_bubbles,
    intake_open_toggle_bubbles,
)
from services import jury as jury_service
from services.access import admin_only
from services.attachments_archive import (
    estimate_archive_budget,
    format_archive_budget_text,
    start_archive_task,
)
from services.intake_mode import get_intake_mode
from services.intake_state import is_intake_open
from services.pools import all_pools, get_pool_applications
from utils.bot_utils import reply_to_user, resolve_bot_id
from utils.contracts import PoolKey


collector = HandlerCollector()


def _btn_data(message: IncomingMessage) -> dict:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


def _parse_pool_from_data(message: IncomingMessage) -> PoolKey | None:
    data = _btn_data(message)
    track_raw = (data.get("track") or "").strip()
    age_raw = (data.get("age") or "").strip()
    if not track_raw or not age_raw:
        return None
    try:
        track = Track[track_raw]
        age = AgeCategory[age_raw]
    except KeyError:
        parsed = parse_pool_token(f"{track_raw}/{age_raw}")
        if parsed is None:
            return None
        track, age = parsed
    return PoolKey(track=track, age_category=age)


async def _pool_card_lines(pool: PoolKey, *, session) -> tuple[str, bool, bool, bool]:
    """Текст карточки и флаги кнопок."""
    apps = await get_pool_applications(
        pool, session=session, status_filter=ModerationStatus.DOPUSHCHENO
    )
    dopushcheno = len(apps)
    fixed = await jury_service._count_fixed_top_in_pool(  # noqa: SLF001
        track=pool.track,
        age_category=pool.age_category,
        session=session,
    )
    open_round = (
        await session.execute(
            select(JuryRound).where(
                JuryRound.track == pool.track,
                JuryRound.age_category == pool.age_category,
                JuryRound.status == JuryRoundStatus.OPEN,
            )
        )
    ).scalar_one_or_none()

    lines = [
        f"**{pool.as_label()}**",
        f"ДОПУЩЕНО: **{dopushcheno}** · в шорт-листе: **{fixed}**",
    ]
    if open_round is not None:
        lines.append(
            f"Открыт раунд **{open_round.round_no}** (id `{open_round.id}`)."
        )
    elif fixed > 0 and dopushcheno <= TOP_N:
        lines.append(f"Пул закрыт без голосования: **{fixed}** работ в шорт-листе.")
    elif dopushcheno < TOP_N:
        if dopushcheno == 0:
            lines.append("Старт заблокирован: нет допущенных работ.")
        else:
            lines.append(
                f"Старт раунда 1 заблокирован: работ **{dopushcheno}** из "
                f"**{TOP_N}** — ждём допуска или закройте пул без голосования."
            )
    else:
        lines.append("Можно открыть раунд 1.")

    can_open = dopushcheno >= TOP_N and fixed == 0 and open_round is None
    can_auto_shortlist = 1 <= dopushcheno < TOP_N and fixed == 0
    has_open_round = open_round is not None
    return "\n".join(lines), can_open, can_auto_shortlist, has_open_round


@collector.command(
    "/admin_competition_intake",
    description="Приём заявок (раздел Конкурс)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_intake(
    message: IncomingMessage, bot: Bot
) -> None:
    currently_open = await is_intake_open()
    mode = await get_intake_mode()
    body = (
        "**Приём заявок**\n\n"
        f"Статус: **{'🔓 ОТКРЫТ' if currently_open else '🔒 ЗАКРЫТ'}**\n"
        f"Режим: **{mode.value.upper()}**"
    )
    bubbles = intake_open_toggle_bubbles(currently_open=currently_open)
    bubbles.add_button(
        command="/admin_section",
        label="◀ К разделу «Конкурс»",
        data={"section": "competition"},
        new_row=True,
    )
    await reply_to_user(message, bot, body, bubbles=bubbles)


@collector.command(
    "/admin_competition_data",
    description="Данные и выгрузки (раздел Конкурс)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_data(message: IncomingMessage, bot: Bot) -> None:
    await reply_to_user(
        message,
        bot,
        "**Данные и выгрузки**",
        bubbles=admin_competition_data_bubbles(),
    )


@collector.command(
    "/admin_competition_jury",
    description="Жюри (раздел Конкурс)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury(message: IncomingMessage, bot: Bot) -> None:
    await reply_to_user(
        message,
        bot,
        "**Управление жюри**",
        bubbles=admin_competition_jury_bubbles(),
    )


@collector.command(
    "/admin_competition_export_registry",
    description="Реестр XLSX в DM админа",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_export_registry(
    message: IncomingMessage, bot: Bot
) -> None:
    from services import registry

    try:
        payload = await registry.build_registry_xlsx()
    except Exception:
        logger.exception("admin_competition: ошибка registry.xlsx")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось сформировать registry.xlsx. См. логи.",
            bubbles=admin_competition_data_bubbles(),
        )
        return

    attachment = OutgoingAttachment(
        content=payload,
        filename=registry.registry_export_filename("registry"),
    )
    try:
        await bot.answer_message(
            "📤 Реестр заявок (актуально на момент запроса).",
            file=attachment,
            wait_callback=False,
            bubbles=admin_competition_data_bubbles(),
        )
    except Exception:
        logger.exception("admin_competition: не удалось отправить registry")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось отправить XLSX. См. логи.",
            bubbles=admin_competition_data_bubbles(),
        )


@collector.command(
    "/admin_competition_export_shortlist",
    description="Шорт-лист XLSX в DM админа",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_export_shortlist(
    message: IncomingMessage, bot: Bot
) -> None:
    from services import registry

    try:
        payload = await registry.build_shortlist_xlsx()
    except Exception:
        logger.exception("admin_competition: ошибка shortlist.xlsx")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось сформировать XLSX шорт-листа. См. логи.",
            bubbles=admin_competition_data_bubbles(),
        )
        return

    attachment = OutgoingAttachment(
        content=payload,
        filename=registry.registry_export_filename("shortlist"),
    )
    try:
        await bot.answer_message(
            "🏆 Шорт-лист (актуально на момент запроса).",
            file=attachment,
            wait_callback=False,
            bubbles=admin_competition_data_bubbles(),
        )
    except Exception:
        logger.exception("admin_competition: не удалось отправить shortlist")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось отправить XLSX. См. логи.",
            bubbles=admin_competition_data_bubbles(),
        )


@collector.command(
    "/admin_competition_archive",
    description="Архив attachments на диск",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_archive(message: IncomingMessage, bot: Bot) -> None:
    budget = await estimate_archive_budget()
    blocked = budget.after_pct >= budget.block_pct
    body = format_archive_budget_text(budget)
    if not blocked:
        body += "\n\nЗапустить копирование на диск?"
    await reply_to_user(
        message,
        bot,
        body,
        bubbles=admin_competition_archive_bubbles(blocked=blocked),
    )


@collector.command(
    "/admin_competition_jury_start",
    description="Старт голосования",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_start(
    message: IncomingMessage, bot: Bot
) -> None:
    await reply_to_user(
        message,
        bot,
        "**Старт голосования** — выберите режим:",
        bubbles=admin_competition_jury_start_bubbles(),
    )


@collector.command(
    "/admin_competition_jury_start_all",
    description="Confirm старт всех пулов",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_start_all(
    message: IncomingMessage, bot: Bot
) -> None:
    await reply_to_user(
        message,
        bot,
        (
            "⚠️ Открыть **раунд 1** во всех пулах с ≥ "
            f"**{TOP_N}** допущенными работами?\n\n"
            "Пулы с меньшим числом работ будут пропущены."
        ),
        bubbles=admin_confirm_bubbles(action="jury_start_all"),
    )


@collector.command(
    "/admin_competition_jury_pools",
    description="Список пулов жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_pools(
    message: IncomingMessage, bot: Bot
) -> None:
    bubbles = BubbleMarkup()
    for pool in all_pools():
        bubbles.add_button(
            command="/admin_competition_jury_pool",
            label=pool.as_label(),
            data={"track": pool.track.name, "age": pool.age_category.name},
            new_row=True,
        )
    bubbles.add_button(
        command="/admin_competition_jury",
        label="◀ К жюри",
        new_row=True,
    )
    await reply_to_user(
        message,
        bot,
        "**Пулы жюри** — выберите карточку:",
        bubbles=bubbles,
    )


@collector.command(
    "/admin_competition_jury_pool",
    description="Карточка пула",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_pool(
    message: IncomingMessage, bot: Bot
) -> None:
    pool = _parse_pool_from_data(message)
    if pool is None:
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось определить пул.",
            bubbles=admin_competition_jury_bubbles(),
        )
        return
    async with get_session()() as session:
        body, can_open, can_auto, has_open = await _pool_card_lines(
            pool, session=session
        )
    await reply_to_user(
        message,
        bot,
        body,
        bubbles=admin_competition_pool_bubbles(
            track=pool.track.name,
            age=pool.age_category.name,
            can_open=can_open,
            can_auto_shortlist=can_auto,
            has_open_round=has_open,
        ),
    )


@collector.command(
    "/admin_competition_jury_open_pool",
    description="Открыть раунд 1 в пуле",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_open_pool(
    message: IncomingMessage, bot: Bot
) -> None:
    pool = _parse_pool_from_data(message)
    if pool is None:
        return
    async with get_session()() as session:
        dopushcheno = await jury_service._count_dopushcheno_in_pool(  # noqa: SLF001
            pool, session=session
        )
        fixed = await jury_service._count_fixed_top_in_pool(  # noqa: SLF001
            track=pool.track,
            age_category=pool.age_category,
            session=session,
        )
        if fixed > 0:
            await reply_to_user(
                message,
                bot,
                f"Пул уже закрыт без голосования: **{fixed}** работ в шорт-листе.",
                bubbles=admin_competition_jury_bubbles(),
            )
            return
        if dopushcheno < TOP_N:
            await reply_to_user(
                message,
                bot,
                f"Нельзя открыть раунд: работ **{dopushcheno}** из **{TOP_N}**.",
                bubbles=admin_competition_pool_bubbles(
                    track=pool.track.name,
                    age=pool.age_category.name,
                    can_open=False,
                    can_auto_shortlist=1 <= dopushcheno < TOP_N,
                    has_open_round=False,
                ),
            )
            return
        existing = await jury_service.open_round(
            track=pool.track,
            age_category=pool.age_category,
            round_no=1,
            candidates=[],
            session=session,
            bot=bot,
        )
        await session.commit()
    await reply_to_user(
        message,
        bot,
        (
            f"✅ Пул **{pool.as_label()}**: "
            f"раунд **{existing.round_no}** "
            f"({'открыт' if existing.status == JuryRoundStatus.OPEN else existing.status.value})."
        ),
        bubbles=admin_competition_jury_bubbles(),
    )


@collector.command(
    "/admin_competition_jury_auto_shortlist",
    description="Закрыть пул без голосования",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_auto_shortlist(
    message: IncomingMessage, bot: Bot
) -> None:
    pool = _parse_pool_from_data(message)
    if pool is None:
        return
    await reply_to_user(
        message,
        bot,
        (
            f"⚠️ Закрыть пул **{pool.as_label()}** без голосования?\n"
            f"Все допущенные работы (< {TOP_N}) попадут в шорт-лист."
        ),
        bubbles=admin_confirm_bubbles(
            action="jury_auto_shortlist",
            payload={"track": pool.track.name, "age": pool.age_category.name},
        ),
    )


@collector.command(
    "/admin_competition_jury_close_pool",
    description="Закрыть открытый раунд пула",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_close_pool(
    message: IncomingMessage, bot: Bot
) -> None:
    pool = _parse_pool_from_data(message)
    if pool is None:
        return
    await reply_to_user(
        message,
        bot,
        f"⚠️ Закрыть текущий открытый раунд в пуле **{pool.as_label()}**?",
        bubbles=admin_confirm_bubbles(
            action="jury_close_pool",
            payload={"track": pool.track.name, "age": pool.age_category.name},
        ),
    )


@collector.command(
    "/admin_competition_jury_state",
    description="Статус пулов для админа",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_state(
    message: IncomingMessage, bot: Bot
) -> None:
    async with get_session()() as session:
        rounds_stmt = select(JuryRound)
        all_rounds = list((await session.execute(rounds_stmt)).scalars().all())
        votes_stmt = (
            select(
                JuryVote.round_id,
                func.count(func.distinct(JuryVote.jury_huid)),
            )
            .where(JuryVote.state == JuryVoteState.SUBMITTED)
            .group_by(JuryVote.round_id)
        )
        votes_per_round = {
            row[0]: int(row[1])
            for row in (await session.execute(votes_stmt)).all()
        }
        from database.models import JuryPoolAssignment

        assign_stmt = select(
            JuryPoolAssignment.track,
            JuryPoolAssignment.age_category,
            func.count(),
        ).group_by(JuryPoolAssignment.track, JuryPoolAssignment.age_category)
        assignments_count = {
            (track, age): int(cnt)
            for track, age, cnt in (await session.execute(assign_stmt)).all()
        }

    latest: dict[tuple[Track, AgeCategory], JuryRound] = {}
    for r in all_rounds:
        key = (r.track, r.age_category)
        cur = latest.get(key)
        if cur is None or r.round_no > cur.round_no:
            latest[key] = r

    lines = ["📈 **Статус пулов жюри**", ""]
    for track, cat in ALL_POOLS:
        label = _format_pool(track, cat)
        rnd = latest.get((track, cat))
        assigned = assignments_count.get((track, cat), 0)
        if rnd is None:
            lines.append(f"• {label}: раундов не было")
            continue
        submitted = votes_per_round.get(rnd.id, 0)
        lines.append(
            f"• {label}: р{rnd.round_no} · {rnd.status.value} · "
            f"{submitted}/{assigned} судей"
        )

    await reply_to_user(
        message,
        bot,
        "\n".join(lines),
        bubbles=admin_competition_jury_bubbles(),
    )


@collector.command(
    "/admin_competition_jury_finalize",
    description="Финализация жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_competition_jury_finalize(
    message: IncomingMessage, bot: Bot
) -> None:
    await reply_to_user(
        message,
        bot,
        "⚠️ Аварийная **финализация** жюри — зафиксировать шорт-лист?",
        bubbles=admin_confirm_bubbles(action="jury_finalize"),
    )


async def execute_jury_start_all(bot: Bot) -> str:
    """Последовательный старт раунда 1 во всех eligible пулах."""
    opened: list[str] = []
    skipped_small: list[str] = []
    skipped_zero: list[str] = []
    already: list[str] = []

    for pool in all_pools():
        async with get_session()() as session:
            dopushcheno = await jury_service._count_dopushcheno_in_pool(  # noqa: SLF001
                pool, session=session
            )
            if dopushcheno == 0:
                skipped_zero.append(pool.as_label())
                continue
            if dopushcheno < TOP_N:
                skipped_small.append(f"{pool.as_label()} ({dopushcheno})")
                continue
            fixed = await jury_service._count_fixed_top_in_pool(  # noqa: SLF001
                track=pool.track,
                age_category=pool.age_category,
                session=session,
            )
            if fixed > 0:
                already.append(pool.as_label())
                continue
            before = await session.execute(
                select(JuryRound.id).where(
                    JuryRound.track == pool.track,
                    JuryRound.age_category == pool.age_category,
                    JuryRound.round_no == 1,
                )
            )
            had_round = before.scalar_one_or_none() is not None
            round_obj = await jury_service.open_round(
                track=pool.track,
                age_category=pool.age_category,
                round_no=1,
                candidates=[],
                session=session,
                bot=bot,
            )
            await session.commit()
            if had_round:
                already.append(pool.as_label())
            else:
                opened.append(pool.as_label())

    lines = ["**Старт голосования — отчёт**", ""]
    if opened:
        lines.append(f"✅ Открыты ({len(opened)}):")
        lines.extend(f"  • {p}" for p in opened)
    if already:
        lines.append(f"ℹ️ Уже были открыты ({len(already)}):")
        lines.extend(f"  • {p}" for p in already)
    if skipped_small:
        lines.append(f"⏭ Пропущены (< {TOP_N}):")
        lines.extend(f"  • {p}" for p in skipped_small)
    if skipped_zero:
        lines.append("⛔ Пустые пулы:")
        lines.extend(f"  • {p}" for p in skipped_zero)
    return "\n".join(lines)


__all__ = ["collector", "execute_jury_start_all"]
