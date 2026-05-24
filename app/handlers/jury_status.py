"""
Handler ``/jury_status``.

Показывает судье общий прогресс по его задачам: сколько раундов он
отправил, сколько в работе (есть черновики), сколько ещё не открывал.

Сама логика подсчёта живёт в ``services.jury.get_jury_progress``;
handler только форматирует ответ и предлагает кнопки возврата.
"""
from __future__ import annotations

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from fsm import cleanup_middleware, fsm_middleware
from services import jury as jury_service
from services.access import jury_only
from utils.bot_utils import reply_to_user

collector = HandlerCollector()


def _back_bubbles() -> BubbleMarkup:
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury_tasks", label="📋 К списку задач", new_row=True)
    bubbles.add_button(command="/jury_menu", label="↩ В меню жюри", new_row=True)
    return bubbles


def _format_progress(progress: dict[str, int]) -> str:
    submitted = progress.get("submitted_rounds", 0)
    in_progress = progress.get("in_progress_rounds", 0)
    not_started = progress.get("not_started_rounds", 0)
    total = submitted + in_progress + not_started
    if total == 0:
        return (
            "📊 У вас нет назначенных открытых раундов.\n\n"
            "Это значит, что либо ваши пулы ещё не запущены, "
            "либо все раунды уже закрыты."
        )
    return (
        "📊 Ваш прогресс по жюри:\n\n"
        f"• Отправлено оценок (раундов): {submitted}\n"
        f"• В работе (есть черновики): {in_progress}\n"
        f"• Не открыто (без черновиков): {not_started}\n\n"
        f"Всего открытых раундов вашего состава: {total}."
    )


@collector.command(
    "/jury_status",
    description="Общий прогресс по моим задачам жюри",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jury_status(message: IncomingMessage, bot: Bot) -> None:
    """Общий прогресс судьи: отправлено / в работе / не открывал.

    Под капотом — один вызов ``services.jury.get_jury_progress``.
    Открывает свою сессию (короткое чтение, не пересекается с
    карусельным потоком).
    """
    huid = message.sender.huid
    try:
        progress = await jury_service.get_jury_progress(huid)
    except Exception:
        logger.exception(
            "/jury_status: ошибка получения прогресса",
            jury_huid=str(huid),
        )
        await reply_to_user(
            message,
            bot,
            "Произошла ошибка при получении прогресса. Попробуйте позже.",
            bubbles=_back_bubbles(),
        )
        return
    text = _format_progress(progress)
    await reply_to_user(message, bot, text, bubbles=_back_bubbles())


__all__ = ["collector"]
