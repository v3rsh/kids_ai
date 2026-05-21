"""
Очередь модератора и карусель просмотра (Wave 2 / B).

Реализует:

- ``/queue`` — постраничный список заявок (по 5, §34.2);
- ``/browse`` — карусель просмотра (по одной заявке);
- инлайн-кнопки навигации (``← Назад / Вперёд →``);
- управление фильтрами по треку / возрастной категории / статусу
  модерации / дате (§34.2);
- сброс фильтров.

Состояние пагинации/фильтров хранится в FSM-данных модератора (по его
HUID), поэтому переключение между ``/queue`` и ``/browse`` помнит
последний установленный фильтр (UX из §34).

Все DB-запросы делаются ``services.moderation.list_queue`` — один SELECT
+ один COUNT, без N+1 (правило `performance.mdc`). Здесь хендлеры лишь
сериализуют результат в текст и собирают клавиатуры.

``collector`` подключается в
``app/handlers/__init__.py → get_all_collectors()`` сразу за
``handlers/moderator.py`` (порядок внутри ветки B значения не имеет —
конфликтов команд нет).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from loguru import logger
from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
)

from database.models import AgeCategory, Application, ModerationStatus, Track
from fsm import cleanup_middleware, fsm_middleware
from services.access import moderator_only
from services.moderation import (
    DEFAULT_QUEUE_STATUSES,
    QueueFilters,
    QueuePage,
    list_queue,
)
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Хранение фильтров/состояния в FSM
# =====================================================================

# Все ключи в одном FSM-словаре под общим неймспейсом, чтобы не
# смешивать модераторскую навигацию с возможным состоянием в других
# ветках (модератор может одновременно быть и обычным пользователем
# для подачи заявок).
FSM_KEY_TRACKS = "moderator_queue_tracks"
FSM_KEY_AGES = "moderator_queue_ages"
FSM_KEY_STATUSES = "moderator_queue_statuses"
FSM_KEY_DATE_FROM = "moderator_queue_date_from"
FSM_KEY_DATE_TO = "moderator_queue_date_to"
FSM_KEY_QUEUE_PAGE = "moderator_queue_page"
FSM_KEY_BROWSE_INDEX = "moderator_browse_index"

QUEUE_PAGE_SIZE = 5  # §34.2


async def _load_filters(message: IncomingMessage) -> QueueFilters:
    """Прочитать сохранённые фильтры из FSM-данных модератора."""
    fsm = message.state.fsm
    data = await fsm.get_data()
    tracks = tuple(
        Track[name] for name in (data.get(FSM_KEY_TRACKS) or []) if name in Track.__members__
    )
    ages = tuple(
        AgeCategory[name]
        for name in (data.get(FSM_KEY_AGES) or [])
        if name in AgeCategory.__members__
    )
    statuses_raw = data.get(FSM_KEY_STATUSES)
    if statuses_raw is None:
        statuses: tuple[ModerationStatus, ...] = DEFAULT_QUEUE_STATUSES
    else:
        statuses = tuple(
            ModerationStatus[name]
            for name in statuses_raw
            if name in ModerationStatus.__members__
        )
    date_from = _parse_iso_date(data.get(FSM_KEY_DATE_FROM))
    date_to = _parse_iso_date(data.get(FSM_KEY_DATE_TO))
    return QueueFilters(
        tracks=tracks,
        age_categories=ages,
        moderation_statuses=statuses,
        date_from=date_from,
        date_to=date_to,
    )


def _parse_iso_date(raw: Any) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


async def _save_filters(message: IncomingMessage, filters: QueueFilters) -> None:
    fsm = message.state.fsm
    await fsm.update_data(
        **{
            FSM_KEY_TRACKS: [t.name for t in filters.tracks],
            FSM_KEY_AGES: [a.name for a in filters.age_categories],
            FSM_KEY_STATUSES: [s.name for s in filters.moderation_statuses],
            FSM_KEY_DATE_FROM: (
                filters.date_from.isoformat() if filters.date_from else None
            ),
            FSM_KEY_DATE_TO: (
                filters.date_to.isoformat() if filters.date_to else None
            ),
        }
    )


async def _save_queue_page(message: IncomingMessage, page: int) -> None:
    await message.state.fsm.update_data(**{FSM_KEY_QUEUE_PAGE: int(page)})


async def _load_queue_page(message: IncomingMessage) -> int:
    data = await message.state.fsm.get_data()
    return max(1, int(data.get(FSM_KEY_QUEUE_PAGE) or 1))


async def _save_browse_index(message: IncomingMessage, index: int) -> None:
    await message.state.fsm.update_data(**{FSM_KEY_BROWSE_INDEX: int(index)})


async def _load_browse_index(message: IncomingMessage) -> int:
    data = await message.state.fsm.get_data()
    return max(0, int(data.get(FSM_KEY_BROWSE_INDEX) or 0))


# =====================================================================
# Сериализация заявок в текст
# =====================================================================


def _short_card(app: Application) -> str:
    """Однострочное представление для списка ``/queue``."""
    files_count = len(app.files) if app.files is not None else 0
    return (
        f"• {app.br_id} · {app.track.value} · {app.age_category.value}\n"
        f"  «{app.title}» — {app.parent_full_name}, "
        f"ребёнок {app.child_name} ({app.child_age})\n"
        f"  Статус: {app.moderation_status.value} · файлов: {files_count}"
    )


def _full_card(app: Application) -> str:
    """Развёрнутая карточка для ``/browse`` и ``/find`` (§27.1)."""
    files_count = len(app.files) if app.files is not None else 0
    contact = (
        f"@{app.parent_ad_login}"
        if app.parent_ad_login
        else f"HUID: {app.parent_huid}"
    )
    duplicate_line = ""
    if app.is_possible_duplicate:
        related = app.related_application_br_id or "—"
        duplicate_line = f"\n⚠️ Возможный дубль (связанная: {related})"
    comment_line = ""
    if app.moderator_comment:
        comment_line = f"\n💬 Комментарий модератора: {app.moderator_comment}"
    intake_line = ""
    if app.cloud_link:
        intake_line = f"\n🔗 Ссылка на папку: {app.cloud_link}"
    return (
        f"📄 {app.br_id}\n"
        f"Подана: {_format_dt(app.created_at)}\n"
        f"Родитель: {app.parent_full_name} · {app.parent_division}\n"
        f"Контакт: {contact}\n"
        f"Ребёнок: {app.child_name}, {app.child_age} "
        f"({app.age_category.value})\n"
        f"Трек: {app.track.value}\n"
        f"Название: {app.title}\n"
        f"Описание: {app.description}\n"
        f"Файлов: {files_count} · Режим приёма: {app.intake_mode.value}\n"
        f"Статус модерации: {app.moderation_status.value}\n"
        f"Статус жюри: {app.jury_status.value} · "
        f"Статус голосования: {app.voting_status.value}"
        f"{duplicate_line}{comment_line}{intake_line}"
    )


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def _filters_summary(filters: QueueFilters) -> str:
    parts: list[str] = []
    if filters.tracks:
        parts.append("трек: " + ", ".join(t.value for t in filters.tracks))
    if filters.age_categories:
        parts.append(
            "возраст: " + ", ".join(a.value for a in filters.age_categories)
        )
    if filters.moderation_statuses != DEFAULT_QUEUE_STATUSES:
        if filters.moderation_statuses:
            parts.append(
                "статус: " + ", ".join(s.value for s in filters.moderation_statuses)
            )
        else:
            parts.append("статус: любой")
    if filters.date_from or filters.date_to:
        df = filters.date_from.isoformat() if filters.date_from else "—"
        dt_ = filters.date_to.isoformat() if filters.date_to else "—"
        parts.append(f"дата: {df}…{dt_}")
    return "; ".join(parts) if parts else "по умолчанию (на модерации + нужно исправить)"


# =====================================================================
# Клавиатуры
# =====================================================================


def _action_buttons_for_app(bubbles: BubbleMarkup, app: Application) -> None:
    """Инлайн-кнопки действий по карточке (§27.1)."""
    bubbles.add_button(
        command=f"/files {app.br_id}",
        label="📂 Файлы",
        new_row=True,
    )
    bubbles.add_button(
        command=f"/status {app.br_id} модерация допущено",
        label="✅ Допустить",
    )
    bubbles.add_button(
        command=f"/notify_fix {app.br_id}",
        label="✏️ На исправление",
    )
    bubbles.add_button(
        command=f"/notify_reject {app.br_id}",
        label="🚫 Отклонить",
        new_row=True,
    )
    bubbles.add_button(
        command=f"/comment {app.br_id}",
        label="💬 Комментарий",
    )


def _queue_filters_buttons(bubbles: BubbleMarkup) -> None:
    """Кнопки управления фильтрами в ``/queue``."""
    bubbles.add_button(
        command="/m_q_filter_track",
        label="🎨 Фильтр: трек",
        new_row=True,
    )
    bubbles.add_button(
        command="/m_q_filter_age",
        label="👶 Фильтр: возраст",
    )
    bubbles.add_button(
        command="/m_q_filter_status",
        label="📌 Фильтр: статус",
        new_row=True,
    )
    bubbles.add_button(
        command="/m_q_filter_clear",
        label="♻️ Сбросить фильтры",
    )


def _queue_pagination_buttons(bubbles: BubbleMarkup, page: QueuePage) -> None:
    """Кнопки навигации по страницам ``/queue``."""
    has_prev = page.page > 1
    has_next = page.page < page.total_pages

    if has_prev:
        bubbles.add_button(
            command="/m_q_page",
            label="← Назад",
            data={"to": str(page.page - 1)},
            new_row=True,
        )
    bubbles.add_button(
        command="/m_q_refresh",
        label=f"{page.page} из {page.total_pages}",
        new_row=not has_prev,
    )
    if has_next:
        bubbles.add_button(
            command="/m_q_page",
            label="Вперёд →",
            data={"to": str(page.page + 1)},
        )


def _browse_navigation_buttons(
    bubbles: BubbleMarkup,
    *,
    index: int,
    total: int,
) -> None:
    """Навигация в карусели ``/browse``."""
    if index > 0:
        bubbles.add_button(
            command="/m_b_nav",
            label="← Предыдущая",
            data={"to": str(index - 1)},
            new_row=True,
        )
    bubbles.add_button(
        command="/m_b_refresh",
        label=f"{index + 1} из {total}",
        new_row=index == 0,
    )
    if index + 1 < total:
        bubbles.add_button(
            command="/m_b_nav",
            label="Следующая →",
            data={"to": str(index + 1)},
        )
    bubbles.add_button(
        command="/queue",
        label="📋 К списку",
        new_row=True,
    )


# =====================================================================
# Команды /queue
# =====================================================================


@collector.command(
    "/queue",
    description="Очередь заявок на модерации",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_queue(message: IncomingMessage, bot: Bot) -> None:
    """Постраничный список заявок (§34.2).

    По умолчанию выводятся заявки в активных статусах модерации
    (``на модерации`` + ``нужно исправить``); фильтры/страница
    запоминаются в FSM модератора.
    """
    await _save_queue_page(message, 1)
    await _render_queue(message, bot, page=1)


@collector.command(
    "/m_q_page",
    description="Страница очереди модератора",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_queue_page(message: IncomingMessage, bot: Bot) -> None:
    """Перейти на конкретную страницу ``/queue``.

    Номер страницы передаётся через ``message.data["to"]`` (строка).
    """
    raw = (message.data or {}).get("to") if message.data else None
    try:
        page_no = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        page_no = 1
    page_no = max(page_no, 1)
    await _save_queue_page(message, page_no)
    await _render_queue(message, bot, page=page_no)


@collector.command(
    "/m_q_refresh",
    description="Обновить очередь модератора",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_queue_refresh(message: IncomingMessage, bot: Bot) -> None:
    """Перерисовать ``/queue`` с текущей страницей."""
    page_no = await _load_queue_page(message)
    await _render_queue(message, bot, page=page_no)


async def _render_queue(
    message: IncomingMessage,
    bot: Bot,
    *,
    page: int,
) -> None:
    filters = await _load_filters(message)
    result = await list_queue(filters=filters, page=page, page_size=QUEUE_PAGE_SIZE)

    if not result.items:
        body = (
            "Очередь пуста по текущим фильтрам.\n\n"
            f"Фильтры: {_filters_summary(filters)}"
        )
    else:
        lines = [
            f"📋 Очередь модерации ({result.total} всего):",
            f"Фильтры: {_filters_summary(filters)}",
            "",
        ]
        for app in result.items:
            lines.append(_short_card(app))
        body = "\n\n".join(lines)

    bubbles = BubbleMarkup()
    if result.items:
        for app in result.items:
            bubbles.add_button(
                command=f"/find {app.br_id}",
                label=f"📄 {app.br_id}",
                new_row=True,
            )
    _queue_filters_buttons(bubbles)
    _queue_pagination_buttons(bubbles, result)
    bubbles.add_button(
        command="/browse",
        label="🖼️ Карусель",
        new_row=True,
    )

    await reply_to_user(message, bot, body, bubbles=bubbles)


# =====================================================================
# Фильтры /queue
# =====================================================================


@collector.command(
    "/m_q_filter_track",
    description="Фильтр очереди по треку",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_filter_track(message: IncomingMessage, bot: Bot) -> None:
    """Меню выбора трека для фильтра.

    Выбор переключает один трек в множестве (повторное нажатие — снимает).
    """
    data = message.data or {}
    track_name = data.get("track")
    filters = await _load_filters(message)
    if track_name and track_name in Track.__members__:
        target = Track[track_name]
        new_set = set(filters.tracks)
        if target in new_set:
            new_set.discard(target)
        else:
            new_set.add(target)
        filters = QueueFilters(
            tracks=tuple(t for t in Track if t in new_set),
            age_categories=filters.age_categories,
            moderation_statuses=filters.moderation_statuses,
            date_from=filters.date_from,
            date_to=filters.date_to,
        )
        await _save_filters(message, filters)

    bubbles = BubbleMarkup()
    selected = set(filters.tracks)
    for track in Track:
        mark = "☑ " if track in selected else "☐ "
        bubbles.add_button(
            command="/m_q_filter_track",
            label=mark + track.value,
            data={"track": track.name},
            new_row=True,
        )
    bubbles.add_button(
        command="/m_q_refresh",
        label="◀ Назад к очереди",
        new_row=True,
    )
    await reply_to_user(
        message,
        bot,
        f"Выбор трека для фильтра.\nТекущие: {_filters_summary(filters)}",
        bubbles=bubbles,
    )


@collector.command(
    "/m_q_filter_age",
    description="Фильтр очереди по возрасту",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_filter_age(message: IncomingMessage, bot: Bot) -> None:
    """Меню выбора возрастной категории для фильтра."""
    data = message.data or {}
    age_name = data.get("age")
    filters = await _load_filters(message)
    if age_name and age_name in AgeCategory.__members__:
        target = AgeCategory[age_name]
        new_set = set(filters.age_categories)
        if target in new_set:
            new_set.discard(target)
        else:
            new_set.add(target)
        filters = QueueFilters(
            tracks=filters.tracks,
            age_categories=tuple(c for c in AgeCategory if c in new_set),
            moderation_statuses=filters.moderation_statuses,
            date_from=filters.date_from,
            date_to=filters.date_to,
        )
        await _save_filters(message, filters)

    bubbles = BubbleMarkup()
    selected = set(filters.age_categories)
    for cat in AgeCategory:
        mark = "☑ " if cat in selected else "☐ "
        bubbles.add_button(
            command="/m_q_filter_age",
            label=mark + cat.value,
            data={"age": cat.name},
            new_row=True,
        )
    bubbles.add_button(
        command="/m_q_refresh",
        label="◀ Назад к очереди",
        new_row=True,
    )
    await reply_to_user(
        message,
        bot,
        f"Выбор возрастной категории.\nТекущие: {_filters_summary(filters)}",
        bubbles=bubbles,
    )


@collector.command(
    "/m_q_filter_status",
    description="Фильтр очереди по статусу модерации",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_filter_status(message: IncomingMessage, bot: Bot) -> None:
    """Меню выбора статуса модерации для фильтра."""
    data = message.data or {}
    status_name = data.get("status")
    filters = await _load_filters(message)
    if status_name and status_name in ModerationStatus.__members__:
        target = ModerationStatus[status_name]
        # Здесь храним явно выбранные статусы (без дефолта).
        if filters.moderation_statuses == DEFAULT_QUEUE_STATUSES:
            base: set[ModerationStatus] = set()
        else:
            base = set(filters.moderation_statuses)
        if target in base:
            base.discard(target)
        else:
            base.add(target)
        new_statuses = (
            DEFAULT_QUEUE_STATUSES
            if not base
            else tuple(s for s in ModerationStatus if s in base)
        )
        filters = QueueFilters(
            tracks=filters.tracks,
            age_categories=filters.age_categories,
            moderation_statuses=new_statuses,
            date_from=filters.date_from,
            date_to=filters.date_to,
        )
        await _save_filters(message, filters)

    bubbles = BubbleMarkup()
    if filters.moderation_statuses == DEFAULT_QUEUE_STATUSES:
        selected: set[ModerationStatus] = set()
    else:
        selected = set(filters.moderation_statuses)
    for status in ModerationStatus:
        mark = "☑ " if status in selected else "☐ "
        bubbles.add_button(
            command="/m_q_filter_status",
            label=mark + status.value,
            data={"status": status.name},
            new_row=True,
        )
    bubbles.add_button(
        command="/m_q_refresh",
        label="◀ Назад к очереди",
        new_row=True,
    )
    body = (
        "Выбор статусов модерации.\n"
        "Если ничего не выбрано — действуют дефолтные «на модерации» + "
        "«нужно исправить» (§34.2).\n"
        f"Текущие: {_filters_summary(filters)}"
    )
    await reply_to_user(message, bot, body, bubbles=bubbles)


@collector.command(
    "/m_q_filter_clear",
    description="Сбросить фильтры очереди",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_filter_clear(message: IncomingMessage, bot: Bot) -> None:
    """Сбросить все фильтры (вернуть дефолт §34.2)."""
    await _save_filters(message, QueueFilters())
    await _save_queue_page(message, 1)
    logger.info(
        "Модератор сбросил фильтры очереди",
        huid=str(message.sender.huid),
    )
    await _render_queue(message, bot, page=1)


# =====================================================================
# Команда /browse — карусель просмотра
# =====================================================================


@collector.command(
    "/browse",
    description="Карусель просмотра заявок",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_browse(message: IncomingMessage, bot: Bot) -> None:
    """Запуск карусели просмотра (§34.2).

    Использует тот же набор фильтров, что и ``/queue``. Текущая позиция
    сбрасывается к 0 (первая заявка по фильтру).
    """
    await _save_browse_index(message, 0)
    await _render_browse(message, bot, index=0)


@collector.command(
    "/m_b_nav",
    description="Навигация в карусели модератора",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_browse_nav(message: IncomingMessage, bot: Bot) -> None:
    """Перейти к заявке номер ``message.data['to']`` в карусели."""
    raw = (message.data or {}).get("to")
    try:
        target = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        target = 0
    target = max(target, 0)
    await _save_browse_index(message, target)
    await _render_browse(message, bot, index=target)


@collector.command(
    "/m_b_refresh",
    description="Обновить карусель модератора",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_browse_refresh(message: IncomingMessage, bot: Bot) -> None:
    """Перерисовать карусель с текущей позицией."""
    index = await _load_browse_index(message)
    await _render_browse(message, bot, index=index)


async def _render_browse(
    message: IncomingMessage,
    bot: Bot,
    *,
    index: int,
) -> None:
    filters = await _load_filters(message)
    page_no = (index // QUEUE_PAGE_SIZE) + 1
    inner_offset = index % QUEUE_PAGE_SIZE

    result = await list_queue(
        filters=filters, page=page_no, page_size=QUEUE_PAGE_SIZE
    )
    total = result.total

    if total == 0 or inner_offset >= len(result.items):
        body = (
            "Заявок по текущим фильтрам нет.\n\n"
            f"Фильтры: {_filters_summary(filters)}"
        )
        bubbles = BubbleMarkup()
        bubbles.add_button(command="/queue", label="📋 К очереди", new_row=True)
        await reply_to_user(message, bot, body, bubbles=bubbles)
        return

    if index >= total:
        index = total - 1
        await _save_browse_index(message, index)
        page_no = (index // QUEUE_PAGE_SIZE) + 1
        inner_offset = index % QUEUE_PAGE_SIZE
        result = await list_queue(
            filters=filters, page=page_no, page_size=QUEUE_PAGE_SIZE
        )

    app = result.items[inner_offset]
    body = (
        f"Карусель: {index + 1} из {total}\n"
        f"Фильтры: {_filters_summary(filters)}\n\n"
        + _full_card(app)
    )

    bubbles = BubbleMarkup()
    _action_buttons_for_app(bubbles, app)
    _browse_navigation_buttons(bubbles, index=index, total=total)

    await reply_to_user(message, bot, body, bubbles=bubbles)


__all__ = ["collector", "_full_card", "_short_card"]
