"""
Handlers ветки жюри (Wave 2 / ветка C).

Точка входа в роль (§5.4):
- ``/jury_tasks`` — список открытых задач судьи (пул × раунд × прогресс
  × дедлайн), §27.4.
- ``/jury_menu`` — небольшое «главное меню жюри» (открыть задачи,
  посмотреть статус). По §27.4 отдельных команд для оценки нет — всё
  взаимодействие внутри задачи происходит через кнопки карусели,
  реализованной в ``app/handlers/jury_tasks.py``.

Защита: все команды обёрнуты в ``jury_only`` (§35.4) — не-судье бот
ответит «Команда доступна только членам жюри».
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.db import get_session
from fsm import cleanup_middleware, fsm_middleware
from services import jury as jury_service
from services.access import jury_only
from utils.bot_utils import reply_to_user
from utils.contracts import JuryTaskDTO, PoolKey

# WAVE3-TODO: подключить collector в `app/handlers/__init__.py` после
# того, как ветка C полностью протестирована (порядок:
# common → user → moderator → jury → admin).
collector = HandlerCollector()


# =====================================================================
# Меню жюри
# =====================================================================


def _jury_menu_bubbles() -> BubbleMarkup:
    """Главное меню жюри: открыть задачи + посмотреть статус."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury_tasks", label="📋 Мои задачи", new_row=True)
    bubbles.add_button(command="/jury_status", label="📊 Прогресс", new_row=True)
    return bubbles


_JURY_MENU_TEXT = (
    "🎨 Жюри конкурса «Безопасные рисунки».\n\n"
    "Используй кнопки ниже для работы со своими задачами."
)


@collector.command(
    "/jury_menu",
    description="Меню жюри",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jury_menu(message: IncomingMessage, bot: Bot) -> None:
    """Главное меню жюри. Доступно только членам жюри (§5.4)."""
    await reply_to_user(message, bot, _JURY_MENU_TEXT, bubbles=_jury_menu_bubbles())


# =====================================================================
# /jury_tasks — список открытых задач
# =====================================================================


def _format_deadline(deadline: Optional[datetime]) -> str:
    """Человекочитаемая разница до дедлайна."""
    if deadline is None:
        return "без дедлайна"
    now = datetime.utcnow()
    delta = deadline - now
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return "дедлайн прошёл"
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    if hours >= 24:
        days = hours // 24
        remain_hours = hours % 24
        return f"осталось {days} д {remain_hours} ч"
    return f"осталось {hours} ч {minutes:02d} мин"


def _group_tasks_by_round(
    tasks: list[JuryTaskDTO],
) -> dict[UUID, dict]:
    """Сгруппировать DTO по ``round_id``: pool, round_no, evaluated, total."""
    grouped: dict[UUID, dict] = {}
    for t in tasks:
        info = grouped.setdefault(
            t.round_id,
            {
                "pool": t.pool,
                "round_no": t.round_no,
                "total": 0,
                "evaluated": 0,
            },
        )
        info["total"] += 1
        if t.draft_vote is not None:
            info["evaluated"] += 1
    return grouped


async def _fetch_open_rounds_meta(
    jury_huid: UUID,
) -> tuple[list[JuryTaskDTO], dict[UUID, datetime]]:
    """Получить список JuryTaskDTO и дедлайны по их раундам.

    Делаем одной сессией, чтобы не плодить транзакции.
    """
    from sqlalchemy import select
    from database.models import JuryRound

    async with get_session()() as session:
        tasks = await jury_service.get_open_tasks_for_jury(
            jury_huid, session=session
        )
        if not tasks:
            return tasks, {}
        round_ids = list({t.round_id for t in tasks})
        rounds = (
            await session.execute(
                select(JuryRound).where(JuryRound.id.in_(round_ids))
            )
        ).scalars().all()
        deadlines = {r.id: r.deadline_at for r in rounds}
        return tasks, deadlines


def _task_list_bubbles(
    grouped: dict[UUID, dict],
    deadlines: dict[UUID, datetime],
) -> BubbleMarkup:
    """Кнопки списка задач: по одной на каждый (pool, round)."""
    bubbles = BubbleMarkup()
    sorted_round_ids = sorted(
        grouped.keys(),
        key=lambda rid: (
            grouped[rid]["pool"].track.name,
            grouped[rid]["pool"].age_category.name,
            grouped[rid]["round_no"],
        ),
    )
    for round_id in sorted_round_ids:
        info = grouped[round_id]
        pool: PoolKey = info["pool"]
        label = (
            f"{pool.as_label()} · раунд {info['round_no']} · "
            f"{info['evaluated']}/{info['total']}"
        )
        bubbles.add_button(
            command="/jt_open",
            label=label,
            data={"round_id": str(round_id)},
            new_row=True,
        )
    bubbles.add_button(
        command="/jury_status",
        label="📊 Общий прогресс",
        new_row=True,
    )
    bubbles.add_button(command="/jury_menu", label="↩ В меню жюри", new_row=True)
    return bubbles


def _task_list_text(
    grouped: dict[UUID, dict],
    deadlines: dict[UUID, datetime],
) -> str:
    """Текстовая часть экрана /jury_tasks."""
    if not grouped:
        return (
            "🎨 У вас нет открытых задач жюри.\n\n"
            "Это значит, что либо все раунды ваших пулов уже закрыты, "
            "либо вы уже отправили оценки во всех текущих раундах."
        )
    lines = ["🎨 Ваши открытые задачи (нажмите, чтобы открыть карусель):", ""]
    sorted_round_ids = sorted(
        grouped.keys(),
        key=lambda rid: (
            grouped[rid]["pool"].track.name,
            grouped[rid]["pool"].age_category.name,
            grouped[rid]["round_no"],
        ),
    )
    for round_id in sorted_round_ids:
        info = grouped[round_id]
        pool: PoolKey = info["pool"]
        deadline = deadlines.get(round_id)
        lines.append(
            f"• {pool.as_label()} — раунд {info['round_no']}\n"
            f"  Оценено: {info['evaluated']}/{info['total']} · "
            f"{_format_deadline(deadline)}"
        )
    return "\n".join(lines)


@collector.command(
    "/jury_tasks",
    description="Мой список открытых задач (жюри)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jury_tasks(message: IncomingMessage, bot: Bot) -> None:
    """Список открытых задач судьи (§27.4 ``/jury_tasks``).

    Сбрасывает любое предыдущее состояние карусели (если судья ушёл из
    задачи не нажав «В меню задач»), чтобы клик по новой задаче из
    списка стартовал с нулевой позиции карусели.
    """
    huid = message.sender.huid
    fsm = message.state.fsm
    current_state = await fsm.get_state()
    if current_state is not None:
        await fsm.clear()

    try:
        tasks, deadlines = await _fetch_open_rounds_meta(huid)
    except Exception:
        logger.exception("/jury_tasks: ошибка получения задач", jury_huid=str(huid))
        await reply_to_user(
            message,
            bot,
            "Произошла ошибка при получении ваших задач. Попробуйте позже.",
            bubbles=_jury_menu_bubbles(),
        )
        return

    grouped = _group_tasks_by_round(tasks)
    text = _task_list_text(grouped, deadlines)
    bubbles = _task_list_bubbles(grouped, deadlines)
    await reply_to_user(message, bot, text, bubbles=bubbles)


__all__ = ["collector"]
