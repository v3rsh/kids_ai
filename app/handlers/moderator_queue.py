"""
Очередь модератора и карусель просмотра.

Реализует:

- ``/queue`` — постраничный список заявок (по 5);
- ``/browse`` — карусель просмотра (по одной заявке);
- инлайн-кнопки навигации (``← Назад / Вперёд →``);
- управление фильтрами по треку / возрастной категории / статусу
  модерации / дате;
- сброс фильтров.

Состояние пагинации/фильтров хранится в FSM-данных модератора (по его
HUID), поэтому переключение между ``/queue`` и ``/browse`` помнит
последний установленный фильтр.

Все DB-запросы делаются ``services.moderation.list_queue`` — один SELECT
+ один COUNT, без N+1 (правило `performance.mdc`). Здесь хендлеры лишь
сериализуют результат в текст и собирают клавиатуры.

``collector`` подключается в
``app/handlers/__init__.py → get_all_collectors()`` сразу за
``handlers/moderator.py`` (порядок внутри модераторских модулей значения
не имеет — конфликтов команд нет).
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
    MentionBuilder,
)

from database.models import AgeCategory, Application, IntakeMode, ModerationStatus, Track
from fsm import cleanup_middleware, fsm_middleware
from fsm.keys import (
    FSM_KEY_AGES,
    FSM_KEY_BROWSE_INDEX,
    FSM_KEY_DATE_FROM,
    FSM_KEY_DATE_TO,
    FSM_KEY_QUEUE_PAGE,
    FSM_KEY_STATUSES,
    FSM_KEY_TRACKS,
)
from keyboards import back_to_moderator_menu_bubbles
from services.access import moderator_only
from services.moderation import (
    DEFAULT_QUEUE_STATUSES,
    QueueFilters,
    QueuePage,
    list_queue,
)
from utils.bot_utils import (
    pagination_footer,
    reply_to_user,
    send_application_files_with_card,
)
from utils.moderator_nav import (
    ModeratorNavOrigin,
    append_moderator_menu_button,
    card_action_buttons,
    find_button_data,
)


collector = HandlerCollector()


# =====================================================================
# Хранение фильтров/состояния в FSM
# =====================================================================

QUEUE_PAGE_SIZE = 5


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
        f"• **{app.br_id}** · {app.track.value} · {app.age_category.value}\n"
        f"  «{app.title}» — {app.parent_full_name}, "
        f"ребёнок {app.child_name} ({app.child_age})\n"
        f"  **Статус:** {app.moderation_status.value} · файлов: {files_count}"
    )


def _format_related_warnings(
    app: Application,
    related: list,
) -> str:
    """Блок предупреждений о связанных заявках (родитель + трек)."""
    if not related and not app.is_possible_duplicate:
        return ""
    parts: list[str] = []
    if related:
        br_ids = ", ".join(f"**{entry.br_id}**" for entry in related)
        parts.append(
            f"\n\n⚠️ **Несколько заявок в треке** ({len(related)}): {br_ids}"
        )
        strict = [entry for entry in related if entry.is_strict_match]
        if strict:
            strict_ids = ", ".join(f"**{entry.br_id}**" for entry in strict)
            parts.append(f"\n   Тот же ребёнок: {strict_ids}")
    if app.is_possible_duplicate:
        related_id = app.related_application_br_id or "—"
        parts.append(f"\n   Возможный дубль → **{related_id}**")
    return "".join(parts)


def _full_card(
    app: Application,
    *,
    related: list | None = None,
) -> str:
    """Развёрнутая карточка для ``/browse`` и ``/find``.

    Поле «Родитель» рендерится через ``MentionBuilder.contact`` —
    клиент eXpress показывает кликабельный ``@@ФИО``, открывающий чат
    с родителем (см. ``.cursor/rules/mentions.mdc``).

    Контакт для связи (email/телефон, явно введённый родителем на шаге
    «Контакт» анкеты) показывается отдельной строкой; если поле пустое —
    fallback на ``@ad_login`` или ``HUID:``.
    """
    files_count = len(app.files) if app.files is not None else 0
    parent_mention = MentionBuilder.contact(
        entity_id=app.parent_huid,
        name=app.parent_full_name,
    )
    if getattr(app, "parent_contact", None):
        contact = app.parent_contact
    elif app.parent_ad_login:
        contact = f"@{app.parent_ad_login}"
    else:
        contact = f"HUID: {app.parent_huid}"
    comment_line = ""
    if app.moderator_comment:
        comment_line = (
            f"\n\n💬 **Комментарий модератора:** {app.moderator_comment}"
        )
    intake_line = ""
    if app.cloud_link:
        intake_line = f"\n\n🔗 **Ссылка на папку:** {app.cloud_link}"
    elif app.intake_mode is IntakeMode.LINKS:
        # Резервный режим (§33.6): заявка создана, но участник ещё
        # не прислал ссылку. Модератор не должен «принимать решение»
        # по такой заявке — материалы появятся, когда придёт URL.
        intake_line = (
            "\n\n⏳ **Ожидает ссылку от участника** "
            "(резервный режим, §33.6)"
        )
    return (
        f"📄 **{app.br_id}**\n\n"
        f"**Подана:** {_format_dt(app.created_at)}\n\n"
        f"**Родитель:** {parent_mention}\n"
        f"**Подразделение:** {app.parent_division}\n"
        f"**Контакт:** {contact}\n\n"
        f"**Ребёнок:** {app.child_name}, {app.child_age} "
        f"({app.age_category.value})\n\n"
        f"**Трек:** {app.track.value}\n"
        f"**Название:** {app.title}\n"
        f"**Описание:** {app.description}\n\n"
        f"**Файлов:** {files_count} · **Режим приёма:** "
        f"{app.intake_mode.value}\n"
        f"**Статус модерации:** {app.moderation_status.value}\n"
        f"**Статус жюри:** {app.jury_status.value}\n"
        f"**Статус голосования:** {app.voting_status.value}"
        f"{_format_related_warnings(app, related or [])}"
        f"{comment_line}{intake_line}"
    )


async def build_full_card(app: Application) -> str:
    """Карточка с подгруженным блоком связанных заявок."""
    from services import applications as applications_service

    related = await applications_service.find_related_for_application(app)
    return _full_card(app, related=related)


async def card_bubbles_for_app(
    app: Application,
    origin: ModeratorNavOrigin,
    *,
    with_navigation: bool = True,
) -> BubbleMarkup:
    """Кнопки карточки с учётом числа похожих работ."""
    from services import applications as applications_service

    related = await applications_service.find_related_for_application(app)
    return card_action_buttons(
        app,
        origin,
        with_navigation=with_navigation,
        related_count=len(related),
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
# Карточка заявки с файлами
# =====================================================================


async def render_application_card(
    message: IncomingMessage,
    bot: Bot,
    *,
    app: Application,
    bubbles: BubbleMarkup,
    prefix: str = "",
    suffix: str = "",
) -> None:
    """Отрисовать карточку заявки модератору с файлами работы.

    Поведение:
    - ``IntakeMode.FILES`` — все файлы заявки сразу: первый с полной
      карточкой (``_full_card`` + ``prefix`` + ``suffix``) и кнопками
      действий, остальные — с нумерованной подписью. Сообщения transient,
      cleanup-middleware удалит их при следующей навигации.
    - ``IntakeMode.LINKS`` или отсутствие файлов на диске — текстовая
      карточка через ``reply_to_user``.

    Args:
        prefix: дополнительный текст перед карточкой.
        suffix: дополнительный текст после карточки (например, счётчик
            карусели).
    """
    body = await build_full_card(app)
    if prefix:
        body = prefix + body
    if suffix:
        body = body + suffix
    if app.intake_mode is IntakeMode.LINKS:
        await reply_to_user(message, bot, body, bubbles=bubbles)
        return
    sent = await send_application_files_with_card(
        message, bot, app=app, body=body, bubbles=bubbles
    )
    if not sent:
        await reply_to_user(message, bot, body, bubbles=bubbles)


# =====================================================================
# Клавиатуры
# =====================================================================


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
    if has_next:
        bubbles.add_button(
            command="/m_q_page",
            label="Вперёд →",
            data={"to": str(page.page + 1)},
            new_row=not has_prev,
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
    if index + 1 < total:
        bubbles.add_button(
            command="/m_b_nav",
            label="Следующая →",
            data={"to": str(index + 1)},
            new_row=index == 0,
        )
    bubbles.add_button(
        command="/m_q_refresh",
        label="📋 К списку",
        new_row=True,
    )


# =====================================================================
# Команды /queue
# =====================================================================


@collector.command(
    "/queue_next",
    description="Перейти к следующей заявке очереди модерации",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_queue_next(message: IncomingMessage, bot: Bot) -> None:
    """Открыть первую (свежую) заявку из дефолтной очереди модерации.

    Сбрасывает фильтры/пагинацию модератора и ищет одну заявку в
    статусе ``DEFAULT_QUEUE_STATUSES`` (по умолчанию — только новые).
    Если очередь пуста — короткое сообщение с возвратом в меню.
    """
    default_filters = QueueFilters(moderation_statuses=DEFAULT_QUEUE_STATUSES)
    await _save_filters(message, default_filters)
    await _save_queue_page(message, 1)
    page = await list_queue(
        filters=default_filters,
        page=1,
        page_size=1,
    )
    if not page.items:
        bubbles = BubbleMarkup()
        bubbles.add_button(
            command="/m_q_refresh", label="📋 К очереди", new_row=True
        )
        append_moderator_menu_button(bubbles)
        await reply_to_user(
            message,
            bot,
            "🎉 **Очередь разобрана.** Новых заявок на модерацию нет.",
            bubbles=bubbles,
        )
        return

    app = page.items[0]
    origin = ModeratorNavOrigin(kind="queue")

    await render_application_card(
        message,
        bot,
        app=app,
        bubbles=await card_bubbles_for_app(app, origin),
        prefix=f"▶ Следующая заявка в очереди ({page.total} всего):\n\n",
    )


@collector.command(
    "/queue",
    description="Очередь заявок на модерации",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_queue(message: IncomingMessage, bot: Bot) -> None:
    """Постраничный список заявок.

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
            "**Очередь пуста** по текущим фильтрам.\n\n"
            f"**Фильтры:** {_filters_summary(filters)}"
        )
    else:
        lines = [
            f"**📋 Очередь модерации** ({result.total} всего):",
            f"**Фильтры:** {_filters_summary(filters)}",
            "",
        ]
        for app in result.items:
            lines.append(_short_card(app))
        body = "\n\n".join(lines) + pagination_footer(
            result.page, result.total_pages
        )

    bubbles = BubbleMarkup()
    queue_origin = ModeratorNavOrigin(kind="queue")
    if result.items:
        for app in result.items:
            bubbles.add_button(
                command="/find",
                label=f"📄 {app.br_id}",
                data=find_button_data(app.br_id, queue_origin),
                new_row=True,
            )
    _queue_filters_buttons(bubbles)
    _queue_pagination_buttons(bubbles, result)
    bubbles.add_button(
        command="/browse",
        label="🖼️ Карусель",
        new_row=True,
    )
    append_moderator_menu_button(bubbles)

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
        "«нужно исправить».\n"
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
    """Сбросить все фильтры (вернуть дефолт модерации)."""
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
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_browse(message: IncomingMessage, bot: Bot) -> None:
    """Запуск карусели просмотра заявок.

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
            "**Заявок по текущим фильтрам нет.**\n\n"
            f"**Фильтры:** {_filters_summary(filters)}"
        )
        bubbles = BubbleMarkup()
        bubbles.add_button(
            command="/m_q_refresh", label="📋 К очереди", new_row=True
        )
        append_moderator_menu_button(bubbles)
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
    prefix = f"**Фильтры:** {_filters_summary(filters)}\n\n"
    suffix = pagination_footer(index + 1, total, title="Карусель")

    browse_origin = ModeratorNavOrigin(kind="browse")
    bubbles = await card_bubbles_for_app(
        app, browse_origin, with_navigation=False
    )
    _browse_navigation_buttons(bubbles, index=index, total=total)
    append_moderator_menu_button(bubbles)

    await render_application_card(
        message, bot, app=app, bubbles=bubbles, prefix=prefix, suffix=suffix
    )


# =====================================================================
# Разделы по статусам модерации (Принятые / На рассмотрении / Отклонённые)
# =====================================================================
#
# Навигация: главное меню модератора → /m_accepted, /m_review, /m_rejected
# (роут открывает выбор трека) → выбор трека → выбор возрастной категории
# → постраничный список заявок выбранного среза.
#
# Все три статуса используют один универсальный хендлер /m_list,
# который по содержимому `data` определяет уровень навигации:
#   data={"st": <STATUS>}                       → выбор трека
#   data={"st": <STATUS>, "tr": <TRACK>}        → выбор возраста
#   data={"st": <STATUS>, "tr": ..., "ag": ...} → список заявок
#   + опциональный "p": номер страницы


_SECTION_LABELS: dict[str, tuple[str, str]] = {
    ModerationStatus.DOPUSHCHENO.name: ("✅ Принятые", "Принятые заявки"),
    ModerationStatus.NUZHNO_ISPRAVIT.name: ("✏️ На рассмотрении", "Заявки на рассмотрении"),
    ModerationStatus.OTKLONENO.name: ("🚫 Отклонённые", "Отклонённые заявки"),
}

_REJECTED_NOTE = (
    "_Файлы отклонённых заявок на сервере не хранятся — доступны только "
    "метаданные заявки._"
)


def _section_label(status_name: str) -> tuple[str, str]:
    """Пара (короткий, длинный) подпись раздела для данного статуса."""
    return _SECTION_LABELS.get(status_name, ("Раздел", "Раздел заявок"))


def _back_to_moderator_button(bubbles: BubbleMarkup) -> None:
    bubbles.add_button(
        command="/moderator",
        label="◀ В меню модератора",
        new_row=True,
    )


async def _render_section_track_picker(
    message: IncomingMessage,
    bot: Bot,
    *,
    status_name: str,
) -> None:
    """Уровень 1 раздела — выбор трека."""
    short, long_ = _section_label(status_name)
    bubbles = BubbleMarkup()
    for track in Track:
        bubbles.add_button(
            command="/m_list",
            label=f"🎨 {track.value}",
            data={"st": status_name, "tr": track.name},
            new_row=True,
        )
    _back_to_moderator_button(bubbles)
    body = f"**{short} · {long_}**\n\nВыберите трек:"
    if status_name == ModerationStatus.OTKLONENO.name:
        body = f"{body}\n\n{_REJECTED_NOTE}"
    await reply_to_user(message, bot, body, bubbles=bubbles)


async def _render_section_age_picker(
    message: IncomingMessage,
    bot: Bot,
    *,
    status_name: str,
    track: Track,
) -> None:
    """Уровень 2 раздела — выбор возрастной категории."""
    short, _ = _section_label(status_name)
    bubbles = BubbleMarkup()
    for cat in AgeCategory:
        bubbles.add_button(
            command="/m_list",
            label=f"👶 {cat.value}",
            data={"st": status_name, "tr": track.name, "ag": cat.name},
            new_row=True,
        )
    bubbles.add_button(
        command="/m_list",
        label="◀ К трекам",
        data={"st": status_name},
        new_row=True,
    )
    _back_to_moderator_button(bubbles)
    body = (
        f"**{short} · {track.value}**\n\nВыберите возрастную категорию:"
    )
    if status_name == ModerationStatus.OTKLONENO.name:
        body = f"{body}\n\n{_REJECTED_NOTE}"
    await reply_to_user(message, bot, body, bubbles=bubbles)


def _section_pagination_buttons(
    bubbles: BubbleMarkup,
    *,
    status_name: str,
    track: Track,
    age: AgeCategory,
    page: QueuePage,
) -> None:
    """Кнопки навигации по страницам раздела модератора."""
    has_prev = page.page > 1
    has_next = page.page < page.total_pages
    nav_data_base = {
        "st": status_name,
        "tr": track.name,
        "ag": age.name,
    }
    if has_prev:
        bubbles.add_button(
            command="/m_list",
            label="← Назад",
            data={**nav_data_base, "p": str(page.page - 1)},
            new_row=True,
        )
    if has_next:
        bubbles.add_button(
            command="/m_list",
            label="Вперёд →",
            data={**nav_data_base, "p": str(page.page + 1)},
            new_row=not has_prev,
        )


async def _render_section_list(
    message: IncomingMessage,
    bot: Bot,
    *,
    status_name: str,
    track: Track,
    age: AgeCategory,
    page: int,
) -> None:
    """Уровень 3 раздела — постраничный список заявок выбранного среза."""
    status = ModerationStatus[status_name]
    short, _ = _section_label(status_name)
    filters = QueueFilters(
        tracks=(track,),
        age_categories=(age,),
        moderation_statuses=(status,),
    )
    result = await list_queue(
        filters=filters, page=page, page_size=QUEUE_PAGE_SIZE
    )

    header_lines = [
        f"**{short} · {track.value} / {age.value}** ({result.total} всего)",
    ]
    if status_name == ModerationStatus.OTKLONENO.name:
        header_lines.append(_REJECTED_NOTE)

    if not result.items:
        body = "\n\n".join(header_lines + ["По этому срезу заявок нет."])
    else:
        body_lines = list(header_lines) + [""]
        for app in result.items:
            body_lines.append(_short_card(app))
        body = "\n\n".join(body_lines) + pagination_footer(
            result.page, result.total_pages
        )

    bubbles = BubbleMarkup()
    section_origin = ModeratorNavOrigin(
        kind="section",
        section_status=status_name,
        section_track=track.name,
        section_age=age.name,
        section_page=page,
    )
    for app in result.items:
        bubbles.add_button(
            command="/find",
            label=f"📄 {app.br_id}",
            data=find_button_data(app.br_id, section_origin),
            new_row=True,
        )

    _section_pagination_buttons(
        bubbles,
        status_name=status_name,
        track=track,
        age=age,
        page=result,
    )

    bubbles.add_button(
        command="/m_list",
        label="◀ К возрастам",
        data={"st": status_name, "tr": track.name},
        new_row=True,
    )
    bubbles.add_button(
        command="/m_list",
        label="🎨 К трекам",
        data={"st": status_name},
    )
    _back_to_moderator_button(bubbles)

    await reply_to_user(message, bot, body, bubbles=bubbles)


@collector.command(
    "/m_list",
    description="Раздел модератора: трек → возраст → список",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_m_list(message: IncomingMessage, bot: Bot) -> None:
    """Универсальная навигация по разделу: трек → возраст → список.

    Состояние навигации передаётся в ``data`` (``BubbleMarkup``-кнопкой);
    хендлер сам определяет, какой уровень рендерить.
    """
    data = message.data or {}
    status_name = data.get("st")
    if not status_name or status_name not in _SECTION_LABELS:
        await reply_to_user(
            message,
            bot,
            "Не удалось открыть раздел: некорректные параметры.",
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return

    track_name = data.get("tr")
    age_name = data.get("ag")
    raw_page = data.get("p")
    try:
        page = max(1, int(raw_page)) if raw_page is not None else 1
    except (TypeError, ValueError):
        page = 1

    if not track_name:
        await _render_section_track_picker(message, bot, status_name=status_name)
        return

    if track_name not in Track.__members__:
        await reply_to_user(
            message,
            bot,
            f"Неизвестный трек: {track_name!r}.",
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return
    track = Track[track_name]

    if not age_name:
        await _render_section_age_picker(
            message, bot, status_name=status_name, track=track
        )
        return

    if age_name not in AgeCategory.__members__:
        await reply_to_user(
            message,
            bot,
            f"Неизвестная возрастная категория: {age_name!r}.",
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return
    age = AgeCategory[age_name]

    await _render_section_list(
        message,
        bot,
        status_name=status_name,
        track=track,
        age=age,
        page=page,
    )


@collector.command(
    "/m_accepted",
    description="Принятые заявки (трек → возраст → список)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_m_accepted(message: IncomingMessage, bot: Bot) -> None:
    """Принятые заявки (статус «допущено») — выбор трека."""
    await _render_section_track_picker(
        message, bot, status_name=ModerationStatus.DOPUSHCHENO.name
    )


@collector.command(
    "/m_review",
    description="Заявки на рассмотрении (нужно исправить)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_m_review(message: IncomingMessage, bot: Bot) -> None:
    """Заявки, отправленные на исправление родителю — выбор трека."""
    await _render_section_track_picker(
        message, bot, status_name=ModerationStatus.NUZHNO_ISPRAVIT.name
    )


@collector.command(
    "/m_rejected",
    description="Отклонённые заявки (без файлов)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_m_rejected(message: IncomingMessage, bot: Bot) -> None:
    """Отклонённые заявки — выбор трека.

    Файлы отклонённых заявок физически удалены с сервера (см.
    ``services.storage.delete_application_files``), поэтому открытие
    карточки покажет только метаданные.
    """
    await _render_section_track_picker(
        message, bot, status_name=ModerationStatus.OTKLONENO.name
    )


__all__ = [
    "collector",
    "_full_card",
    "_short_card",
    "build_full_card",
    "card_bubbles_for_app",
    "render_application_card",
]
