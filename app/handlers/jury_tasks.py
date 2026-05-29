"""
Handlers экрана задачи жюри.

Реализует UX оценки одного раунда судьёй:
- карусель работ одного пула в одном раунде (файлы работы + текст
  и кнопки на первом фото, анонимность через локальный номер 1..N);
- кнопки ``Да`` / ``Нет`` (черновик), помечаются эмодзи после выбора;
- навигация ``← Предыдущая`` / ``Следующая →`` (зацикленная при
  ``total > 1``), ``Следующая без оценки`` (поиск по кругу);
- при повторном входе — старт на первой неоценённой работе;
- ``📋 В меню задач`` — выход к списку задач (черновики сохраняются);
- ``✓ Отправить оценки`` — активна только когда (а) все работы оценены
  и (б) есть и YES, и NO (правило разброса в одном раунде).

Состояние карусели хранится в FSM (``JuryTaskFlow.jury_task_voting``):
``{"jury_task_round_id": uuid_str, "jury_task_index": int}``.
Черновики голосов хранятся в БД (``JuryVote.state=DRAFT``) и переживают
рестарт бота — FSM хранит только позицию курсора.
"""
from __future__ import annotations

from typing import Mapping, Optional, Sequence
from uuid import UUID

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.db import get_session
from database.models import Application, JuryVoteValue
from fsm import cleanup_middleware, fsm_middleware
from fsm.keys import (
    FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID,
    FSM_KEY_JURY_TASK_INDEX,
    FSM_KEY_JURY_TASK_ROUND_ID,
)
from keyboards import back_to_jury_menu_bubbles
from services import jury as jury_service
from services import storage as storage_service
from services.access import jury_only
from states import JuryTaskFlow
from utils.bot_utils import (
    delete_jury_anchor,
    delete_source_message,
    edit_jury_anchor_caption,
    reply_to_user,
    resolve_bot_id,
    safe_answer_transient,
    send_jury_carousel,
)
from utils.contracts import PoolKey
from utils.message_tracking import (
    clear_transient_messages,
    get_transient_messages,
)

collector = HandlerCollector()

_JURY_MULTI_FILES_NOTICE = (
    "\n\n**Внимание!** В этой работе {n} файла, "
    "они находятся под меню."
)


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


def _wrap_index(index: int, total: int) -> int:
    """Зацикленный индекс карусели: ``(index ± 1) % total``."""
    return index % total if total > 0 else 0


def _first_unrated_index(
    candidates: Sequence[Application],
    drafts: Mapping[UUID, JuryVoteValue],
) -> int:
    """Первая работа без черновика; если все оценены — 0."""
    for idx, app in enumerate(candidates):
        if app.id not in drafts:
            return idx
    return 0


def _find_next_unrated_index(
    candidates: Sequence[Application],
    drafts: Mapping[UUID, JuryVoteValue],
    current_index: int,
) -> Optional[int]:
    """Следующая неоценённая работа по кругу от ``(current_index + 1)``."""
    total = len(candidates)
    for offset in range(1, total):
        idx = (current_index + offset) % total
        if candidates[idx].id not in drafts:
            return idx
    return None


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
    candidates: Sequence[Application],
    drafts: Mapping[UUID, JuryVoteValue],
) -> BubbleMarkup:
    """Клавиатура экрана задачи: голос / навигация / выход."""
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
        bubbles.add_button(
            command="/jt_nav",
            label="← Предыдущая",
            data={"dir": "prev"},
            new_row=True,
        )
        bubbles.add_button(
            command="/jt_nav",
            label="Следующая →",
            data={"dir": "next"},
        )
        if _find_next_unrated_index(candidates, drafts, index) is not None:
            bubbles.add_button(
                command="/jt_nav",
                label="Следующая без оценки",
                data={"dir": "next_unrated"},
                new_row=True,
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
    attachment_count: int = 0,
) -> str:
    """Текст экрана задачи: анонимный заголовок + инструкция.

    Ничего идентифицирующего автора/родителя/BR-ID — только локальный
    номер работы, название, описание, возрастная категория и трек.
    """
    vote_line = "не оценено"
    if current_vote == JuryVoteValue.YES:
        vote_line = "✅ Да"
    elif current_vote == JuryVoteValue.NO:
        vote_line = "❌ Нет"

    lines = [
        f"**Работа {index + 1} из {total}**",
        "",
        f"**Название:** {app.title}",
        f"**Описание:** {app.description}",
        "",
        f"**Возрастная категория:** {pool.age_category.value}",
        f"**Трек:** {pool.track.value}",
        f"**Раунд:** {round_no}",
        "",
        f"**Твоя оценка:** {vote_line}",
    ]
    if cloud_link:
        lines.append(f"\n🔗 **Ссылка на работу:** {cloud_link}")
    lines.append(
        f"\n**Прогресс по раунду:** "
        f"✅ {progress_yes} · ❌ {progress_no} · "
        f"осталось {total - progress_yes - progress_no} из {total}"
    )
    if not can_submit:
        lines.append(
            "\n\nКнопка «Отправить оценки» появится, когда у всех работ "
            "будет оценка и среди них будут и «Да», и «Нет»."
        )
    else:
        lines.append("\n\nГотово! Нажмите «Отправить оценки» для финализации.")
    lines.append(
        "\n\n**Инструкция:**\n"
        "— Оцените, достойна ли работа финала.\n"
        "— Все работы должны быть с оценкой.\n"
        "— Как минимум одна работа должна иметь оценку, отличную "
        "от других."
    )
    if attachment_count >= 2:
        lines.append(_JURY_MULTI_FILES_NOTICE.format(n=attachment_count))
    return "\n".join(lines)


def _compute_submit_eligibility(
    drafts: Mapping[UUID, JuryVoteValue],
    candidates: list[Application],
) -> bool:
    """Условие активации кнопки «Отправить оценки».

    True, если: (а) каждый кандидат имеет голос, и (б) среди голосов
    есть и YES, и NO (правило разброса в одном раунде). При
    len(candidates) <= 1 правило разброса не требует разнообразия —
    достаточно одной оценки.
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


# =====================================================================
# Lifecycle photo-якоря карусели жюри
# =====================================================================


async def _read_anchor_sync_id(fsm) -> Optional[UUID]:
    """Прочитать sync_id текущего photo-якоря из FSM (или None)."""
    fsm_data = await fsm.get_data()
    return _safe_uuid(fsm_data.get(FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID))


async def _write_anchor_sync_id(fsm, sync_id: Optional[UUID]) -> None:
    """Записать (или очистить) sync_id photo-якоря в FSM."""
    await fsm.update_data(
        **{
            FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID: (
                str(sync_id) if sync_id is not None else None
            )
        }
    )


async def _drop_old_anchor(message: IncomingMessage, bot: Bot, fsm) -> Optional[UUID]:
    """Удалить старый photo-якорь, если он сохранён в FSM.

    Если ``source_sync_id == anchor_sync_id`` (типичный кейс при клике
    по кнопкам якоря — `/jt_nav`, `/jt_back`, выходные ветки),
    дополнительно выставляет ``message.state.transient_source_deleted``,
    чтобы ``reply_to_user`` не пытался ``edit_message`` на удалённом
    сообщении (CTS такой edit молча игнорирует).

    Возвращает sync_id удалённого якоря (для логов/тестов) или ``None``,
    если якорь не был установлен.
    """
    anchor_sync_id = await _read_anchor_sync_id(fsm)
    if anchor_sync_id is None:
        return None

    bot_id = resolve_bot_id(bot)
    if bot_id is None:
        logger.error("_drop_old_anchor: bot_id не определяется")
        return None

    await delete_jury_anchor(bot, bot_id=bot_id, anchor_sync_id=anchor_sync_id)

    if message.source_sync_id == anchor_sync_id:
        message.state.transient_source_deleted = True

    return anchor_sync_id


async def _force_cleanup_transient(message: IncomingMessage, bot: Bot) -> None:
    """Принудительно удалить трекаемые transient-сообщения судьи.

    Используется в fallback-ветке ``cmd_jt_vote``: если ``edit_message``
    провалился и мы вынужденно делаем полный рендер, нужно вычистить
    хвост вручную (cleanup_middleware на /jt_vote отключён).
    """
    huid = message.sender.huid
    bot_id = resolve_bot_id(bot)
    try:
        sync_ids = await get_transient_messages(huid)
    except Exception:
        logger.exception("_force_cleanup_transient: get_transient_messages")
        return
    if not sync_ids:
        return
    for sid in sync_ids:
        if bot_id is None:
            break
        try:
            await bot.delete_message(bot_id=bot_id, sync_id=sid)
        except Exception as exc:
            logger.debug(
                "_force_cleanup_transient: delete_message failed: {}",
                repr(exc),
                sync_id=str(sid),
            )
    try:
        await clear_transient_messages(huid)
    except Exception:
        logger.exception("_force_cleanup_transient: clear_transient_messages")


async def _exit_carousel_with_reply(
    message: IncomingMessage,
    bot: Bot,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
) -> None:
    """Выйти из карусели жюри: удалить якорь, очистить FSM, прислать сообщение.

    Используется во всех ветках выхода: «Раунд закрыт», «Раунд недоступен»,
    «Нет работ», «Состояние карусели потеряно», ошибки submit и т.п.
    """
    fsm = message.state.fsm
    await _drop_old_anchor(message, bot, fsm)
    await fsm.clear()
    await reply_to_user(message, bot, body, bubbles=bubbles)


async def _render_current_view(
    message: IncomingMessage,
    bot: Bot,
    round_id: UUID,
    *,
    requested_index: int | None = None,
    nav_direction: str | None = None,
    resume_unrated: bool = False,
) -> None:
    """Полностью перерисовать текущую позицию карусели (jt_open / jt_nav).

    1. Читает раунд + кандидатов + черновики (одна транзакция).
    2. Вычисляет индекс: wrap prev/next, ``next_unrated``, resume или clamp.
    3. Удаляет старый photo-якорь (если был в FSM) и source-сообщение.
    4. Шлёт новый persistent photo-якорь + transient хвост через
       ``send_jury_carousel``.
    5. Сохраняет sync_id нового якоря в FSM
       (``FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID``).
    """
    huid = message.sender.huid
    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    fsm_index = _safe_int(fsm_data.get(FSM_KEY_JURY_TASK_INDEX), 0)
    next_unrated_stale = False

    async with get_session()() as session:
        try:
            round_obj, candidates, drafts = await jury_service.get_round_candidates_with_drafts(
                round_id, huid, session=session
            )
        except LookupError:
            await _exit_carousel_with_reply(
                message,
                bot,
                "Этот раунд больше не доступен — возможно, он закрыт. "
                "Откройте список задач заново.",
                bubbles=_back_to_tasks_bubbles(),
            )
            return

        from database.models import JuryRoundStatus

        if round_obj.status != JuryRoundStatus.OPEN:
            await _exit_carousel_with_reply(
                message,
                bot,
                "Раунд закрыт — ваши оценки больше не принимаются. "
                "Список задач обновлён.",
                bubbles=_back_to_tasks_bubbles(),
            )
            return

        total = len(candidates)
        if total == 0:
            await _exit_carousel_with_reply(
                message,
                bot,
                "В этом раунде нет работ для оценки.",
                bubbles=_back_to_tasks_bubbles(),
            )
            return

        if nav_direction == "prev":
            index = _wrap_index(fsm_index - 1, total)
        elif nav_direction == "next":
            index = _wrap_index(fsm_index + 1, total)
        elif nav_direction == "next_unrated":
            skip_index = _find_next_unrated_index(candidates, drafts, fsm_index)
            if skip_index is None:
                index = _clamp_index(fsm_index, total)
                next_unrated_stale = True
            else:
                index = skip_index
        elif resume_unrated:
            index = _first_unrated_index(candidates, drafts)
        elif requested_index is not None:
            index = _clamp_index(requested_index, total)
        else:
            index = _clamp_index(fsm_index, total)

        current_app = candidates[index]
        current_vote = drafts.get(current_app.id)
        can_submit = _compute_submit_eligibility(drafts, candidates)
        progress_yes = sum(1 for v in drafts.values() if v == JuryVoteValue.YES)
        progress_no = sum(1 for v in drafts.values() if v == JuryVoteValue.NO)
        pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)

    await fsm.update_data(
        **{
            FSM_KEY_JURY_TASK_ROUND_ID: str(round_id),
            FSM_KEY_JURY_TASK_INDEX: index,
        }
    )

    try:
        attachments = await storage_service.get_application_files_for_chat(
            current_app
        )
    except Exception:
        logger.exception(
            "Не удалось загрузить файлы работы для экрана жюри",
            br_id=current_app.br_id,
        )
        attachments = None

    attachment_count = len(attachments) if attachments else 0

    bubbles = _build_carousel_bubbles(
        round_id=round_id,
        index=index,
        total=total,
        current_vote=current_vote,
        can_submit=can_submit,
        candidates=candidates,
        drafts=drafts,
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
        attachment_count=attachment_count,
    )

    # Снять старый якорь (если был в FSM) и source-сообщение.
    # source может быть либо старым якорем (повторный nav), либо menu-сообщением
    # «список задач» (первый jt_open) — оба удаляются безопасно.
    await _drop_old_anchor(message, bot, fsm)
    await delete_source_message(message, bot)

    new_anchor_sync_id = await send_jury_carousel(
        message,
        bot,
        body=text,
        bubbles=bubbles,
        attachments=attachments,
    )
    if new_anchor_sync_id is None:
        # Не удалось отправить ни photo-якорь, ни text-fallback — последний
        # шанс достучаться до судьи: persistent text через reply_to_user
        # (без edit, т.к. transient_source_deleted уже True).
        await reply_to_user(message, bot, text, bubbles=bubbles)
        await _write_anchor_sync_id(fsm, None)
        if next_unrated_stale:
            await safe_answer_transient(
                message,
                bot,
                "Все работы в этом раунде уже оценены.",
            )
        return

    await _write_anchor_sync_id(fsm, new_anchor_sync_id)

    if next_unrated_stale:
        await safe_answer_transient(
            message,
            bot,
            "Все работы в этом раунде уже оценены.",
        )


def _back_to_tasks_bubbles() -> BubbleMarkup:
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury_tasks", label="📋 К списку задач", new_row=True)
    bubbles.add_button(command="/jury", label="↩ В меню жюри", new_row=True)
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
    await _render_current_view(message, bot, round_id, resume_unrated=True)


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
    """Перейти на следующую/предыдущую работу карусели.

    ``cleanup_middleware`` удаляет хвост 1..N предыдущей работы.
    Старый photo-якорь снимается внутри ``_render_current_view``
    через ``_drop_old_anchor`` (он же выставляет
    ``transient_source_deleted``, чтобы reply_to_user в ошибочных
    ветках не пытался edit на удалённом сообщении).
    """
    data = message.data or {}
    direction = data.get("dir")
    if direction not in ("prev", "next", "next_unrated"):
        logger.warning("/jt_nav: невалидный dir", data=data)
        return
    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    round_id = _safe_uuid(fsm_data.get(FSM_KEY_JURY_TASK_ROUND_ID))
    if round_id is None:
        await _exit_carousel_with_reply(
            message,
            bot,
            "Состояние карусели потеряно. Откройте задачу заново.",
            bubbles=_back_to_tasks_bubbles(),
        )
        return
    await _render_current_view(message, bot, round_id, nav_direction=direction)


# =====================================================================
# /jt_vote — сохранение черновика голоса
# =====================================================================


@collector.command(
    "/jt_vote",
    description="Оценить работу (Да/Нет)",
    visible=False,
    middlewares=[fsm_middleware],
)
@jury_only
async def cmd_jt_vote(message: IncomingMessage, bot: Bot) -> None:
    """Сохранить черновик голоса для текущей работы карусели.

    Главная цель — **не двигать viewport** на самом частом клике судьи:
    после ``upsert_draft_vote`` обновляем только caption и кнопки
    photo-якоря через ``edit_jury_anchor_caption`` (`bot.edit_message`
    с body+bubbles без file). Фото и хвост из доп. файлов остаются
    на месте.

    ``cleanup_middleware`` намеренно снят с этой команды — иначе он
    удалил бы хвост ещё до хендлера, и файлы 2..N исчезали бы при
    каждом голосе.
    """
    data = message.data or {}
    vote_name = data.get("vote")
    if vote_name not in JuryVoteValue.__members__:
        logger.warning("/jt_vote: невалидное vote", data=data)
        return
    vote_value = JuryVoteValue[vote_name]

    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    round_id = _safe_uuid(fsm_data.get(FSM_KEY_JURY_TASK_ROUND_ID))
    index = _safe_int(fsm_data.get(FSM_KEY_JURY_TASK_INDEX), 0)
    if round_id is None:
        await _exit_carousel_with_reply(
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
            await _exit_carousel_with_reply(
                message,
                bot,
                "Этот раунд больше не доступен.",
                bubbles=_back_to_tasks_bubbles(),
            )
            return
        if not candidates:
            await _exit_carousel_with_reply(
                message,
                bot,
                "В этом раунде нет работ для оценки.",
                bubbles=_back_to_tasks_bubbles(),
            )
            return

        from database.models import JuryRoundStatus

        if round_obj.status != JuryRoundStatus.OPEN:
            await _exit_carousel_with_reply(
                message,
                bot,
                "Раунд закрыт — ваши оценки больше не принимаются. "
                "Список задач обновлён.",
                bubbles=_back_to_tasks_bubbles(),
            )
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
                bubbles=_back_to_tasks_bubbles(),
            )
            return

        # Перечитываем drafts с актуальным голосом для пересборки caption/bubbles.
        drafts = dict(drafts)
        drafts[target_app.id] = vote_value

        total = len(candidates)
        current_app = candidates[clamped]
        current_vote = drafts.get(current_app.id)
        can_submit = _compute_submit_eligibility(drafts, candidates)
        progress_yes = sum(1 for v in drafts.values() if v == JuryVoteValue.YES)
        progress_no = sum(1 for v in drafts.values() if v == JuryVoteValue.NO)
        pool = PoolKey(track=round_obj.track, age_category=round_obj.age_category)

    # Атрибут вне сессии — для caption нужно знать число файлов работы,
    # чтобы корректно показать notice о доп. файлах.
    try:
        attachment_count_list = await storage_service.get_application_files_for_chat(
            current_app
        )
    except Exception:
        logger.exception(
            "/jt_vote: не удалось получить число файлов",
            br_id=current_app.br_id,
        )
        attachment_count_list = None
    attachment_count = (
        len(attachment_count_list) if attachment_count_list else 0
    )

    bubbles = _build_carousel_bubbles(
        round_id=round_id,
        index=clamped,
        total=total,
        current_vote=current_vote,
        can_submit=can_submit,
        candidates=candidates,
        drafts=drafts,
    )
    text = _render_task_text(
        pool=pool,
        round_no=round_obj.round_no,
        index=clamped,
        total=total,
        app=current_app,
        current_vote=current_vote,
        progress_yes=progress_yes,
        progress_no=progress_no,
        cloud_link=current_app.cloud_link,
        can_submit=can_submit,
        attachment_count=attachment_count,
    )

    anchor_sync_id = await _read_anchor_sync_id(fsm)
    bot_id = resolve_bot_id(bot)

    if anchor_sync_id is not None and bot_id is not None:
        ok = await edit_jury_anchor_caption(
            bot,
            bot_id=bot_id,
            anchor_sync_id=anchor_sync_id,
            body=text,
            bubbles=bubbles,
        )
        if ok:
            return
        logger.warning(
            "/jt_vote: edit_jury_anchor_caption провалился — fallback на полный рендер",
            anchor_sync_id=str(anchor_sync_id),
        )

    # Fallback: якоря нет в FSM (рестарт / истёкший FSM) или edit не прошёл.
    # cleanup_middleware на /jt_vote отключён, поэтому хвост чистим вручную.
    await _force_cleanup_transient(message, bot)
    await _render_current_view(message, bot, round_id, requested_index=clamped)


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
    """Вернуться к списку задач («В меню задач»).

    Черновики НЕ сбрасываются — они в БД. FSM-данные карусели
    очищаются, чтобы при возврате стартовать с позиции 0. Photo-якорь
    удаляется явно, чтобы не оставлять «висящую» карточку с
    устаревшими кнопками.
    """
    fsm = message.state.fsm
    await _drop_old_anchor(message, bot, fsm)
    await fsm.clear()
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
        tasks, deadlines, opened_ats = await _fetch_open_rounds_meta(huid)
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
    text = _task_list_text(grouped, deadlines, opened_ats)
    bubbles = _task_list_bubbles(grouped, deadlines, opened_ats)
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
    """Финализация оценок судьи за раунд.

    Проверяет условия активации (все работы оценены, среди оценок есть
    и «Да», и «Нет»), переводит черновики в SUBMITTED через
    ``services.jury.submit_votes``, после успеха очищает FSM и
    возвращает судью в список задач.
    """
    fsm = message.state.fsm
    fsm_data = await fsm.get_data()
    round_id = _safe_uuid(fsm_data.get(FSM_KEY_JURY_TASK_ROUND_ID))
    if round_id is None:
        await _exit_carousel_with_reply(
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
            await _exit_carousel_with_reply(
                message,
                bot,
                "Раунд больше не доступен.",
                bubbles=_back_to_tasks_bubbles(),
            )
            return
        if not _compute_submit_eligibility(drafts, candidates):
            await safe_answer_transient(
                message,
                bot,
                "Сначала оцените все работы и убедитесь, что среди "
                "оценок есть и «Да», и «Нет» (правило разброса в одном раунде).",
                bubbles=_back_to_tasks_bubbles(),
            )
            return
        try:
            await jury_service.submit_votes(
                round_id=round_id,
                jury_huid=huid,
                votes=drafts,
                session=session,
                bot=bot,
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
                bubbles=_back_to_tasks_bubbles(),
            )
            return
        except PermissionError:
            await session.rollback()
            logger.warning(
                "/jt_submit: судья отозван во время отправки",
                round_id=str(round_id),
                jury_huid=str(huid),
            )
            await _exit_carousel_with_reply(
                message,
                bot,
                "Вы были отозваны из жюри, голоса не сохранены.",
                bubbles=back_to_jury_menu_bubbles(),
            )
            return

    logger.info(
        "Судья отправил оценки за раунд",
        round_id=str(round_id),
        jury_huid=str(huid),
    )
    await _drop_old_anchor(message, bot, fsm)
    await fsm.clear()
    await safe_answer_transient(
        message,
        bot,
        "✅ Оценки отправлены. Спасибо!",
        bubbles=back_to_jury_menu_bubbles(),
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
        bubbles=back_to_jury_menu_bubbles(),
    )


# Регистрируется в handlers.common диспетчере при импорте этого модуля.
from handlers.common import register_state_handler  # noqa: E402

register_state_handler(
    JuryTaskFlow.jury_task_voting.value, _voting_text_handler
)
register_state_handler(
    JuryTaskFlow.jury_task_confirm_submit.value, _voting_text_handler
)


__all__ = ["collector"]
