"""
Handlers экрана задачи жюри (Wave 2 / ветка C).

Реализует §35.3:
- карусель работ одного пула в одном раунде (одно сообщение с превью,
  заголовком и описанием, анонимность через локальный номер 1..N);
- кнопки ``Да`` / ``Нет`` (черновик), помечаются эмодзи после выбора;
- навигация ``← Предыдущая`` / ``Следующая →``;
- ``📋 В меню задач`` — выход к списку задач (черновики сохраняются);
- ``✓ Отправить оценки`` — активна только когда (а) все работы оценены
  и (б) есть и YES, и NO (правило разброса §35.1).

Состояние карусели хранится в FSM (``JuryTaskFlow.jury_task_voting``):
``{"jury_task_round_id": uuid_str, "jury_task_index": int}``.
Черновики голосов хранятся в БД (``JuryVote.state=DRAFT``) и переживают
рестарт бота — FSM хранит только позицию курсора.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping, Optional
from uuid import UUID

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.db import get_session
from database.models import Application, JuryVoteValue
from fsm import cleanup_middleware, fsm_middleware
from services import jury as jury_service
from services.access import jury_only
from states import JuryTaskFlow
from utils.bot_utils import (
    delete_source_message,
    load_user_photo,
    reply_to_user,
    safe_answer_transient,
    send_photo_transient,
)
from utils.contracts import PoolKey

# WAVE3-TODO: подключить collector в `app/handlers/__init__.py` после
# того, как ветка C полностью протестирована.
collector = HandlerCollector()


# =====================================================================
# Утилиты карусели
# =====================================================================


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_uuid(value) -> Optional[UUID]:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _clamp_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(index, total - 1))


def _vote_label(current: Optional[JuryVoteValue], target: JuryVoteValue) -> str:
    """«Да» / «Нет» с эмодзи, если это текущий черновик."""
    text = "Да" if target == JuryVoteValue.YES else "Нет"
    if current == target:
        return f"✅ {text}" if target == JuryVoteValue.YES else f"❌ {text}"
    return text


def _build_carousel_bubbles(
    *,
    round_id: UUID,
    index: int,
    total: int,
    current_vote: Optional[JuryVoteValue],
    can_submit: bool,
) -> BubbleMarkup:
    """Клавиатура экрана задачи (§35.3)."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/jt_vote",
        label=_vote_label(current_vote, JuryVoteValue.YES),
        data={"vote": JuryVoteValue.YES.name},
    )
    bubbles.add_button(
        command="/jt_vote",
        label=_vote_label(current_vote, JuryVoteValue.NO),
        data={"vote": JuryVoteValue.NO.name},
    )
    if total > 1:
        if index > 0:
            bubbles.add_button(
                command="/jt_nav",
                label="← Предыдущая",
                data={"dir": "prev"},
                new_row=True,
            )
        if index < total - 1:
            bubbles.add_button(
                command="/jt_nav",
                label="Следующая →",
                data={"dir": "next"},
                new_row=(index == 0),
            )
    bubbles.add_button(
        command="/jt_back",
        label="📋 В меню задач",
        new_row=True,
    )
    if can_submit:
        bubbles.add_button(
            command="/jt_submit",
            label="✓ Отправить оценки",
            new_row=True,
        )
    return bubbles


def _render_task_text(
    *,
    pool: PoolKey,
    round_no: int,
    index: int,
    total: int,
    app: Application,
    current_vote: Optional[JuryVoteValue],
    progress_yes: int,
    progress_no: int,
    cloud_link: Optional[str],
    can_submit: bool,
) -> str:
    """Текст экрана задачи: анонимность + инструкция (§35.3, §35.4).

    Ничего идентифицирующего автора/родителя/BR-ID — только локальный
    номер работы, название, описание, возрастная категория и трек.
    """
    vote_line = "не оценено"
    if current_vote == JuryVoteValue.YES:
        vote_line = "✅ Да"
    elif current_vote == JuryVoteValue.NO:
        vote_line = "❌ Нет"

    lines = [
        f"Работа {index + 1} из {total}",
        "",
        f"Название: {app.title}",
        f"Описание: {app.description}",
        "",
        f"Возрастная категория: {pool.age_category.value}",
        f"Трек: {pool.track.value}",
        f"Раунд: {round_no}",
        "",
        f"Твоя оценка: {vote_line}",
    ]
    if cloud_link:
        lines.append(f"\n🔗 Ссылка на работу: {cloud_link}")
    lines.append(
        f"\nПрогресс по раунду: "
        f"✅ {progress_yes} · ❌ {progress_no} · "
        f"осталось {total - progress_yes - progress_no} из {total}"
    )
    if not can_submit:
        lines.append(
            "\nКнопка «Отправить оценки» появится, когда у всех работ "
            "будет оценка и среди них будут и «Да», и «Нет»."
        )
    else:
        lines.append("\nГотово! Нажмите «Отправить оценки» для финализации.")
    lines.append(
        "\nИнструкция:\n"
        "— Оцените, достойна ли работа финала.\n"
        "— Все работы должны быть с оценкой.\n"
        "— Как минимум одна работа должна иметь оценку, отличную "
        "от других."
    )
    return "\n".join(lines)


async def _resolve_preview_path(app_id: UUID) -> Optional[Path]:
    """Ленивый вызов ``services.storage.get_preview_path``.

    Контракт ``StorageService`` (utils/contracts.py) этой функции пока
    не описывает — она добавляется веткой D. Чтобы не блокировать
    Wave 2 / C, мы импортируем ленивыми и тихо игнорируем отсутствие.
    """
    try:
        from services import storage as _storage
    except ImportError:
        return None
    fn = getattr(_storage, "get_preview_path", None)
    if fn is None or not callable(fn):
        return None
    try:
        value = fn(app_id)
        if hasattr(value, "__await__"):
            value = await value
        if value is None:
            return None
        path = Path(value)
        return path if path.exists() else None
    except Exception:
        logger.exception(
            "get_preview_path: ошибка обращения к services.storage",
            application_id=str(app_id),
        )
        return None


def _compute_submit_eligibility(
    drafts: Mapping[UUID, JuryVoteValue],
    candidates: list[Application],
) -> bool:
    """Условие активации кнопки «Отправить оценки» (§35.3).

    True, если: (а) каждый кандидат имеет голос, и (б) есть и YES, и NO
    (правило разброса §35.1). При len(candidates) <= 1 правило
    разброса не требует разнообразия — достаточно одной оценки.
    """
    if not candidates:
        return False
    candidate_ids = [a.id for a in candidates]
    for cid in candidate_ids:
        if cid not in drafts:
            return False
    if len(candidate_ids) <= 1:
        return True
    values = {drafts[cid] for cid in candidate_ids}
    return JuryVoteValue.YES in values and JuryVoteValue.NO in values


async def _render_current_view(
    message: IncomingMessage,
    bot: Bot,
    round_id: UUID,
    requested_index: int = 0,
) -> None:
    """Отрисовать текущую позицию карусели.

    Делает три SQL-запроса (round + candidates + drafts) одной
    транзакцией через ``get_round_candidates_with_drafts``.
    """
    huid = message.sender.huid
    async with get_session()() as session:
        try:
            round_obj, candidates, drafts = await jury_service.get_round_candidates_with_drafts(
                round_id, huid, session=session
            )
        except LookupError:
            await reply_to_user(
                message,
                bot,
                "Этот раунд больше не доступен — возможно, он закрыт. "
                "Откройте список задач заново.",
                bubbles=_back_to_tasks_bubbles(),
            )
            await message.state.fsm.clear()
            return

        from database.models import JuryRoundStatus

        if round_obj.status != JuryRoundStatus.OPEN:
            await reply_to_user(
                message,
                bot,
                "Раунд закрыт — ваши оценки больше не принимаются. "
                "Список задач обновлён.",
                bubbles=_back_to_tasks_bubbles(),
            )
            await message.state.fsm.clear()
            return

        total = len(candidates)
        if total == 0:
            await reply_to_user(
                message,
                bot,
                "В этом раунде нет работ для оценки.",
                bubbles=_back_to_tasks_bubbles(),
            )
            await message.state.fsm.clear()
            return

        index = _clamp_index(requested_index, total)
        current_app = candidates[index]
        current_vote = drafts.get(current_app.id)
        can_submit = _compute_submit_eligibility(drafts, candidates)
        progress_yes = sum(1 for v in drafts.values() if v == JuryVoteValue.YES)
        progress_no = sum(1 for v in drafts.values() if v == JuryVoteValue.NO)
        pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)

    await message.state.fsm.update_data(
        jury_task_round_id=str(round_id),
        jury_task_index=index,
    )

    bubbles = _build_carousel_bubbles(
        round_id=round_id,
        index=index,
        total=total,
        current_vote=current_vote,
        can_submit=can_submit,
    )
    text = _render_task_text(
        pool=pool,
        round_no=round_obj.round_no,
        index=index,
        total=total,
        app=current_app,
        current_vote=current_vote,
        progress_yes=progress_yes,
        progress_no=progress_no,
        cloud_link=current_app.cloud_link,
        can_submit=can_submit,
    )

    preview_path = await _resolve_preview_path(current_app.id)
    if preview_path is not None:
        photo = await load_user_photo(str(preview_path))
        if photo is not None:
            await delete_source_message(message, bot)
            await send_photo_transient(
                message, bot, body=text, photo=photo, bubbles=bubbles
            )
            return
    await reply_to_user(message, bot, text, bubbles=bubbles)


def _back_to_tasks_bubbles() -> BubbleMarkup:
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury_tasks", label="📋 К списку задач", new_row=True)
    bubbles.add_button(command="/jury_menu", label="↩ В меню жюри", new_row=True)
    return bubbles


# =====================================================================
# /jt_open — открытие карусели
# =====================================================================


@collector.command(
    "/jt_open",
    description="Открыть задачу жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jt_open(message: IncomingMessage, bot: Bot) -> None:
    """Открыть карусель работ для указанного раунда."""
    data = message.data or {}
    round_id = _safe_uuid(data.get("round_id"))
    if round_id is None:
        logger.warning("/jt_open: невалидный round_id в data", data=data)
        await reply_to_user(
            message,
            bot,
            "Не удалось открыть задачу — обновите список.",
            bubbles=_back_to_tasks_bubbles(),
        )
        return
    await message.state.fsm.set_state(JuryTaskFlow.jury_task_voting)
    await _render_current_view(message, bot, round_id, requested_index=0)


# =====================================================================
# /jt_nav — навигация в карусели
# =====================================================================


@collector.command(
    "/jt_nav",
    description="Навигация по карусели жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jt_nav(message: IncomingMessage, bot: Bot) -> None:
    """Перейти на следующую/предыдущую работу карусели."""
    data = message.data or {}
    direction = data.get("dir")
    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    round_id = _safe_uuid(fsm_data.get("jury_task_round_id"))
    index = _safe_int(fsm_data.get("jury_task_index"), 0)
    if round_id is None:
        await reply_to_user(
            message,
            bot,
            "Состояние карусели потеряно. Откройте задачу заново.",
            bubbles=_back_to_tasks_bubbles(),
        )
        return
    delta = -1 if direction == "prev" else 1
    await _render_current_view(message, bot, round_id, requested_index=index + delta)


# =====================================================================
# /jt_vote — сохранение черновика голоса
# =====================================================================


@collector.command(
    "/jt_vote",
    description="Оценить работу (Да/Нет)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jt_vote(message: IncomingMessage, bot: Bot) -> None:
    """Сохранить черновик голоса для текущей работы (§35.3)."""
    data = message.data or {}
    vote_name = data.get("vote")
    if vote_name not in JuryVoteValue.__members__:
        logger.warning("/jt_vote: невалидное vote", data=data)
        return
    vote_value = JuryVoteValue[vote_name]

    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    round_id = _safe_uuid(fsm_data.get("jury_task_round_id"))
    index = _safe_int(fsm_data.get("jury_task_index"), 0)
    if round_id is None:
        await reply_to_user(
            message,
            bot,
            "Состояние карусели потеряно. Откройте задачу заново.",
            bubbles=_back_to_tasks_bubbles(),
        )
        return

    huid = message.sender.huid
    async with get_session()() as session:
        try:
            round_obj, candidates, _ = await jury_service.get_round_candidates_with_drafts(
                round_id, huid, session=session
            )
        except LookupError:
            await reply_to_user(
                message,
                bot,
                "Этот раунд больше не доступен.",
                bubbles=_back_to_tasks_bubbles(),
            )
            await fsm.clear()
            return
        if not candidates:
            await reply_to_user(
                message,
                bot,
                "В этом раунде нет работ для оценки.",
                bubbles=_back_to_tasks_bubbles(),
            )
            await fsm.clear()
            return

        clamped = _clamp_index(index, len(candidates))
        target_app = candidates[clamped]
        try:
            await jury_service.upsert_draft_vote(
                round_id=round_id,
                application_id=target_app.id,
                jury_huid=huid,
                vote=vote_value,
                session=session,
            )
            await session.commit()
        except RuntimeError as exc:
            await session.rollback()
            logger.warning("/jt_vote: голос уже отправлен", error=str(exc))
            await safe_answer_transient(
                message,
                bot,
                "Голос по этой работе уже отправлен — изменить нельзя.",
            )
            return

    await _render_current_view(message, bot, round_id, requested_index=index)


# =====================================================================
# /jt_back — вернуться в список задач
# =====================================================================


@collector.command(
    "/jt_back",
    description="Вернуться в меню задач жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jt_back(message: IncomingMessage, bot: Bot) -> None:
    """Вернуться к списку задач (§35.3 «В меню задач»).

    Черновики НЕ сбрасываются — они в БД. FSM-данные карусели
    очищаются, чтобы при возврате стартовать с позиции 0.
    """
    await message.state.fsm.clear()
    await cmd_jury_tasks_internal(message, bot)


async def cmd_jury_tasks_internal(message: IncomingMessage, bot: Bot) -> None:
    """Делегирование в /jury_tasks через прямой вызов сервиса.

    Не используем ``bot.answer_message(command="/jury_tasks")`` чтобы
    не плодить событий — просто перерисовываем экран в текущем
    сообщении (правило message-navigation.mdc).
    """
    from handlers.jury import (  # late import: избегаем цикл
        _fetch_open_rounds_meta,
        _group_tasks_by_round,
        _task_list_bubbles,
        _task_list_text,
    )

    huid = message.sender.huid
    try:
        tasks, deadlines = await _fetch_open_rounds_meta(huid)
    except Exception:
        logger.exception("/jt_back: ошибка получения задач", jury_huid=str(huid))
        await reply_to_user(
            message,
            bot,
            "Произошла ошибка. Попробуйте позже.",
            bubbles=_back_to_tasks_bubbles(),
        )
        return
    grouped = _group_tasks_by_round(tasks)
    text = _task_list_text(grouped, deadlines)
    bubbles = _task_list_bubbles(grouped, deadlines)
    await reply_to_user(message, bot, text, bubbles=bubbles)


# =====================================================================
# /jt_submit — отправка оценок
# =====================================================================


@collector.command(
    "/jt_submit",
    description="Отправить оценки за раунд",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@jury_only
async def cmd_jt_submit(message: IncomingMessage, bot: Bot) -> None:
    """Финализация оценок судьи (§35.3, §35.4).

    Проверяет условия активации (§35.1), переводит черновики в
    SUBMITTED через ``services.jury.submit_votes``, после успеха
    очищает FSM и возвращает судью в список задач.
    """
    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    round_id = _safe_uuid(fsm_data.get("jury_task_round_id"))
    if round_id is None:
        await reply_to_user(
            message,
            bot,
            "Состояние карусели потеряно. Откройте задачу заново.",
            bubbles=_back_to_tasks_bubbles(),
        )
        return

    huid = message.sender.huid
    async with get_session()() as session:
        try:
            round_obj, candidates, drafts = await jury_service.get_round_candidates_with_drafts(
                round_id, huid, session=session
            )
        except LookupError:
            await reply_to_user(
                message,
                bot,
                "Раунд больше не доступен.",
                bubbles=_back_to_tasks_bubbles(),
            )
            await fsm.clear()
            return
        if not _compute_submit_eligibility(drafts, candidates):
            await safe_answer_transient(
                message,
                bot,
                "Сначала оцените все работы и убедитесь, что среди "
                "оценок есть и «Да», и «Нет» (правило разброса §35.1).",
            )
            return
        try:
            await jury_service.submit_votes(
                round_id=round_id,
                jury_huid=huid,
                votes=drafts,
                session=session,
            )
            await session.commit()
        except (ValueError, LookupError) as exc:
            await session.rollback()
            logger.warning(
                "/jt_submit: ошибка отправки оценок",
                round_id=str(round_id),
                jury_huid=str(huid),
                error=str(exc),
            )
            await safe_answer_transient(
                message,
                bot,
                f"Не удалось отправить оценки: {exc}",
            )
            return

    logger.info(
        "Судья отправил оценки за раунд",
        round_id=str(round_id),
        jury_huid=str(huid),
    )
    await fsm.clear()
    await safe_answer_transient(
        message,
        bot,
        "✅ Оценки отправлены. Спасибо!",
    )
    await cmd_jury_tasks_internal(message, bot)


# =====================================================================
# Регистрация state-handler'ов
# =====================================================================


async def _voting_text_handler(message: IncomingMessage, bot: Bot) -> None:
    """Free-text внутри состояния карусели — мягко напоминаем про кнопки."""
    await safe_answer_transient(
        message,
        bot,
        "Для оценки используйте кнопки «Да» / «Нет», навигацию и "
        "«Отправить оценки». Свободный текст здесь не обрабатывается.",
    )


# Регистрируется в handlers.common диспетчере при импорте этого модуля
# (Wave 3 импортирует collector в `handlers/__init__.py`).
from handlers.common import register_state_handler  # noqa: E402

register_state_handler(
    JuryTaskFlow.jury_task_voting.value, _voting_text_handler
)
register_state_handler(
    JuryTaskFlow.jury_task_confirm_submit.value, _voting_text_handler
)


__all__ = ["collector"]
