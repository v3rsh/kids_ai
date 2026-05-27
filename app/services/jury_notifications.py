"""
Уведомления судьям о новой задаче жюри.

Единое правило ``maybe_announce_next_task_to_judge``: у судьи в любой
момент активна не более одной задачи с DM; остальные открытые раунды
видны в ``/jury_tasks``, но без проактивного сообщения.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from loguru import logger
from pybotx import BubbleMarkup

from services.pools import all_pools
from utils.bot_utils import resolve_bot_id
from utils.contracts import JuryTaskDTO, PoolKey

if TYPE_CHECKING:
    from pybotx import Bot
    from sqlalchemy.ext.asyncio import AsyncSession

JuryNotifyTrigger = Literal["open_round", "submit_votes", "judge_added"]

JURY_NEW_TASK_TEMPLATE = (
    "У вас новая задача в жюри: {track} / {age}, раунд {round_no}.\n"
    "Откройте «📋 К списку задач» и проголосуйте."
)


def _jury_task_dm_bubbles() -> BubbleMarkup:
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury_tasks", label="📋 К списку задач", new_row=True)
    bubbles.add_button(command="/jury", label="◀ Главное меню жюри", new_row=True)
    return bubbles


def _group_open_rounds(tasks: list[JuryTaskDTO]) -> list[JuryTaskDTO]:
    """Одна задача на раунд, порядок — ``all_pools()`` затем ``round_no``."""
    pool_index = {pool: idx for idx, pool in enumerate(all_pools())}
    first_by_round: dict[UUID, JuryTaskDTO] = {}
    for task in tasks:
        if task.round_id not in first_by_round:
            first_by_round[task.round_id] = task
    grouped = list(first_by_round.values())
    grouped.sort(
        key=lambda t: (
            pool_index.get(t.pool, 999),
            t.round_no,
            str(t.round_id),
        )
    )
    return grouped


async def maybe_announce_next_task_to_judge(
    bot: "Bot",
    *,
    jury_huid: UUID,
    trigger: JuryNotifyTrigger,
    session: "AsyncSession | None" = None,
) -> None:
    """Отправить DM про следующую задачу, если это уместно по правилу."""
    from services import jury as jury_service

    tasks = await jury_service.get_open_tasks_for_jury(jury_huid, session=session)
    grouped = _group_open_rounds(tasks)
    count_after = len(grouped)
    if count_after == 0:
        return

    if trigger == "open_round":
        if count_after - 1 != 0:
            return
    elif trigger in ("submit_votes", "judge_added"):
        pass
    else:  # pragma: no cover
        logger.warning("maybe_announce: неизвестный trigger", trigger=trigger)
        return

    first = grouped[0]
    chat_id = await _resolve_user_chat_id(jury_huid, session=session)
    if chat_id is None:
        logger.warning(
            "maybe_announce: нет chat_id судьи",
            jury_huid=str(jury_huid),
            trigger=trigger,
        )
        return

    bot_id = resolve_bot_id(bot)
    if bot_id is None:
        logger.error(
            "maybe_announce: bot_id не определяется",
            jury_huid=str(jury_huid),
        )
        return

    body = JURY_NEW_TASK_TEMPLATE.format(
        track=first.pool.track.value,
        age=first.pool.age_category.value,
        round_no=first.round_no,
    )
    try:
        await bot.send_message(
            bot_id=bot_id,
            chat_id=chat_id,
            body=body,
            bubbles=_jury_task_dm_bubbles(),
            wait_callback=False,
        )
    except Exception:
        logger.exception(
            "maybe_announce: не удалось отправить DM судье",
            jury_huid=str(jury_huid),
            trigger=trigger,
        )
        return

    logger.info(
        "maybe_announce: отправлен DM судье",
        jury_huid=str(jury_huid),
        trigger=trigger,
        pool=first.pool.as_label(),
        round_no=first.round_no,
    )


async def _resolve_user_chat_id(
    huid: UUID,
    *,
    session: "AsyncSession | None" = None,
) -> UUID | None:
    from sqlalchemy import select

    from database.db import get_session
    from database.models import User

    async def _do(s) -> UUID | None:
        result = await s.execute(select(User.chat_id).where(User.huid == huid))
        row = result.first()
        return row[0] if row else None

    if session is not None:
        return await _do(session)

    async with get_session()() as s:
        return await _do(s)


__all__ = [
    "JURY_NEW_TASK_TEMPLATE",
    "JuryNotifyTrigger",
    "maybe_announce_next_task_to_judge",
]
