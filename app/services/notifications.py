"""
Сервис автосообщений конкурса «Безопасные рисунки».

Содержит:
- module-level **шаблоны** сообщений (участнику, в чат модерации,
  alert'ы о диске). Шаблоны выделены, чтобы заказчик мог
  переопределить тексты через конфиг без правки кода;
- функции отправки участникам (через ``bot.send_message`` с
  ``wait_callback=False``);
- функции отправки в чат «Безопасные рисунки — модерация» (UUID в
  ``MODERATION_CHAT_ID``);
- агрегатор событий жюри: одно сообщение со списком пулов на
  одновременные открытия/закрытия раундов (debounce 5 секунд).

Безопасность доставки:
- если ``MODERATION_CHAT_ID`` не задан — функции в чат модерации
  ничего не делают, пишут ``WARNING`` (бот пригоден к запуску без
  чата модерации, нужно для smoke / dev);
- если у пользователя нет ``chat_id`` (не открывал бота с момента
  релиза) — нотификации участнику пишут ``WARNING`` и no-op.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from loguru import logger

from services.access import get_moderation_chat_id

if TYPE_CHECKING:
    from pybotx import Bot

    from database.models import Application


# =====================================================================
# Текстовые шаблоны
# =====================================================================
# Шаблоны вынесены в module-level, чтобы:
# 1. Заказчик мог переопределить тексты через конфиг.
# 2. Тесты могли импортировать константы и проверить, что функции
#    реально отправляют ровно такой текст.
# 3. ``.format(**ctx)`` параметров — единственное место, где шаблон
#    превращается в финальный текст.

ACCEPTED_TEMPLATE = (
    "Спасибо! Заявка принята и передана на модерацию.\n"
    "Если нам понадобится уточнение или более качественное изображение, "
    "мы свяжемся с вами по указанному контакту."
)
"""Участнику: заявка принята и передана на модерацию."""

REJECTED_TEMPLATE = (
    "Работа не прошла модерацию, потому что не соответствует условиям "
    "конкурса: {reason}.\n"
    "Спасибо за интерес к проекту."
)
"""Участнику: работа не прошла модерацию."""

FIX_NEEDED_TEMPLATE = (
    "Работа прошла предварительную проверку, но нам нужен файл лучшего "
    "качества / дополнительный ракурс / корректный формат.\n"
    "Пожалуйста, отправьте исправленные материалы до 21 июня "
    "(последний день приёма заявок)."
)
"""Участнику: требуется исправление."""

FIX_NEEDED_EXTRA_TEMPLATE = "\n\nУточнение модератора: {extra}"
"""Опциональное уточнение модератора, добавляется к ``FIX_NEEDED_TEMPLATE``."""

SHORTLIST_TEMPLATE = (
    "Поздравляем! Работа прошла в шорт-лист конкурса.\n"
    "Она может быть опубликована в подборке для голосования за "
    "приз зрительских симпатий."
)
"""Участнику: работа попала в шорт-лист."""

JURY_RESULT_IN_TOP10_TEMPLATE = (
    "Работа вашего ребёнка вошла в шорт-лист конкурса "
    "«Безопасные рисунки» (топ-10 в своей категории). Итоги — 30 июня."
)
"""Участнику: финал жюри — вошла в топ-10."""

JURY_RESULT_NOT_IN_TOP10_TEMPLATE = (
    "Спасибо за участие в конкурсе «Безопасные рисунки»! По итогам "
    "работы жюри ваша работа не вошла в шорт-лист. Это не оценка "
    "таланта — выбор делался по конкретным критериям конкурса. "
    "Рады, что вы участвовали."
)
"""Участнику: финал жюри — НЕ вошла в топ-10."""

NEW_APPLICATION_MODERATION_TEMPLATE = (
    "Новая заявка на конкурс «Безопасные рисунки».\n\n"
    "ID: {br_id}\n"
    "Родитель: {parent_full_name}\n"
    "Ребёнок: {child_name}, {child_age}\n"
    "Возрастная категория: {age_category}\n"
    "Трек: {track}\n"
    "Название работы: {title}\n"
    "Ссылка на папку: {files_pointer}"
)
"""Новая заявка в чат модерации.

``files_pointer`` подставляет либо команду ``/files BR-XXXX`` (режим
``FILES``), либо публичный URL папки (режим ``LINKS``).
"""

JURY_ROUND_OPENED_TEMPLATE = (
    "Раунд {round_no} открыт в пулах:\n"
    "{pool_lines}\n"
    "Дедлайн — {deadline}."
)
"""Чат модерации: открытие раунда (агрегируется по моменту времени)."""

JURY_ROUND_OPENED_SINGLE_TEMPLATE = (
    "Пул `{pool}`: раунд {round_no} открыт, претендентов — {candidates_n}, "
    "дедлайн — {deadline}."
)
"""Чат модерации: открытие раунда в одном пуле (когда агрегация не сработала)."""

JURY_ROUND_CLOSED_TEMPLATE = (
    "Раунд {round_no} закрыт в пулах:\n"
    "{pool_lines}"
)
"""Чат модерации: закрытие раунда (агрегируется)."""

JURY_ROUND_CLOSED_SINGLE_TEMPLATE = (
    "Пул `{pool}`: раунд {round_no} закрыт."
)
"""Чат модерации: закрытие раунда в одном пуле."""

JURY_LOT_TEMPLATE = (
    "Пул `{pool}`: после раунда {round_no} применён автоматический жребий. "
    "Решение зафиксировано в реестре (флаг «определено жребием»)."
)
"""Чат модерации: срабатывание жребия (НЕ агрегируется, индивидуально)."""

JURY_SHORTLIST_READY_TEMPLATE = (
    "Шорт-лист сформирован, доступен по команде `/export_shortlist`."
)
"""Чат модерации: готовность шорт-листа (НЕ агрегируется)."""

DISK_ALERT_80_TEMPLATE = (
    "⚠️ Хранилище конкурса заполнено на 80 %. Свободно: {free_mb} МБ. "
    "При текущей скорости поступления заявок место закончится через "
    "{hours_left} ч. Рекомендуется: ужесточить отбор отклонения, "
    "рассмотреть переключение на резервный сценарий приёма по ссылкам "
    "(раздел 33.6)."
)
"""Alert 80 % заполнения диска."""

DISK_ALERT_95_TEMPLATE = (
    "🚨 Хранилище конкурса заполнено на 95 %. Свободно: {free_mb} МБ. "
    "Приём файлов автоматически переключён в режим LINKS (раздел 33.6). "
    "Уведомите участников и проверьте свободное место."
)
"""Alert 95 % заполнения диска (триггер автопереключения intake_mode)."""

INTAKE_BLOCKED_PARTICIPANT_TEMPLATE = (
    "К сожалению, приём файлов временно приостановлен — сервер конкурса "
    "заполнен. Мы уже работаем над этим. Сохраните данные заявки и "
    "попробуйте отправить файлы позже, либо следите за объявлениями "
    "организаторов о переключении на приём работ по ссылкам."
)
"""Сообщение участнику при попытке загрузить файл на 95 % заполнения."""

INTAKE_MODE_LINKS_NOTICE_TEMPLATE = (
    "Из-за технических ограничений мы временно переходим на приём "
    "работ по ссылкам. Заявки, уже принятые сервером, не теряются. "
    "Новые заявки оформляйте по инструкции бота."
)
"""Общее уведомление при переключении в режим LINKS."""


# =====================================================================
# Утилиты доставки
# =====================================================================


def _format_pool_lines(pools: list[tuple[str, str]]) -> str:
    """Сформировать список «- Трек / Категория» для агрегированных сообщений."""
    return "\n".join(f"- {track} / {age}" for track, age in pools)


def _format_deadline(dt: datetime | None) -> str:
    """Дедлайн раунда: ``26 июня 18:00``. При None — «не задан»."""
    if dt is None:
        return "не задан"
    months = [
        "января", "февраля", "марта", "апреля", "мая", "июня",
        "июля", "августа", "сентября", "октября", "ноября", "декабря",
    ]
    return f"{dt.day} {months[dt.month - 1]} {dt.strftime('%H:%M')}"


async def _send_to_user(
    bot: "Bot",
    *,
    huid: UUID,
    chat_id: UUID | None,
    body: str,
    purpose: str,
) -> None:
    """Отправить сообщение участнику.

    ``chat_id`` обязателен (в pybotx нет «личного» канала по huid).
    Если бот не знает chat_id (пользователь не приходил после
    последнего рестарта) — пишем WARNING.
    """
    if chat_id is None:
        logger.warning(
            "Не отправили нотификацию участнику: нет chat_id",
            purpose=purpose,
            huid=str(huid),
        )
        return
    try:
        await bot.send_message(
            bot_id=bot.id if hasattr(bot, "id") else None,
            chat_id=chat_id,
            body=body,
            wait_callback=False,
        )
    except TypeError:
        # ``bot.id`` может быть недоступен в pybotx — у Bot нет глобального ID.
        # Тогда отправляем без bot_id и логируем; в pybotx это допустимо,
        # если контекст вызова определён (например, из хендлера).
        await bot.send_message(
            chat_id=chat_id,
            body=body,
            wait_callback=False,
        )
    logger.info(
        "Отправлено сообщение участнику",
        purpose=purpose,
        huid=str(huid),
        chat_id=str(chat_id),
    )


async def _resolve_user_chat_id(huid: UUID) -> UUID | None:
    """Найти chat_id пользователя по huid в таблице ``users``."""
    try:
        from sqlalchemy import select

        from database.db import get_session
        from database.models import User
    except ImportError:  # pragma: no cover
        return None

    async with get_session()() as session:
        result = await session.execute(
            select(User.chat_id).where(User.huid == huid)
        )
        row = result.first()
        return row[0] if row else None


async def _send_to_moderation_chat(
    bot: "Bot",
    body: str,
    *,
    purpose: str,
    bubbles=None,
) -> None:
    """Отправить сообщение в чат «Безопасные рисунки — модерация».

    Источник ``chat_id`` — кэш ``services.access`` (актуальное значение
    из БД, обновляется при ``/admin_chat_approve``). Если чат ещё не
    настроен — пишем WARNING и no-op (бот остаётся работоспособным
    без чата модерации).
    """
    chat_uuid = get_moderation_chat_id()
    if chat_uuid is None:
        logger.warning(
            "Не отправили нотификацию в чат модерации: moderation_chat_id не настроен",
            purpose=purpose,
        )
        return

    bot_id = getattr(bot, "id", None) or getattr(
        getattr(bot, "bot_accounts", [None])[0], "id", None
    )

    kwargs = {
        "chat_id": chat_uuid,
        "body": body,
        "wait_callback": False,
    }
    if bot_id is not None:
        kwargs["bot_id"] = bot_id
    if bubbles is not None:
        kwargs["bubbles"] = bubbles

    try:
        await bot.send_message(**kwargs)
        logger.info(
            "Отправлено сообщение в чат модерации",
            purpose=purpose,
            chat_id=str(chat_uuid),
        )
    except Exception:
        logger.exception(
            "Не удалось отправить сообщение в чат модерации",
            purpose=purpose,
        )


# =====================================================================
# Сообщения участнику
# =====================================================================


async def notify_participant_accepted(bot: "Bot", app: "Application") -> None:
    """Заявка принята и передана на модерацию."""
    chat_id = await _resolve_user_chat_id(app.parent_huid)
    await _send_to_user(
        bot,
        huid=app.parent_huid,
        chat_id=chat_id,
        body=ACCEPTED_TEMPLATE,
        purpose="participant_accepted",
    )


async def notify_participant_rejected(
    bot: "Bot", app: "Application", reason: str
) -> None:
    """Работа не прошла модерацию.

    ``reason`` берётся из ``/notify_reject`` дословно.
    """
    chat_id = await _resolve_user_chat_id(app.parent_huid)
    await _send_to_user(
        bot,
        huid=app.parent_huid,
        chat_id=chat_id,
        body=REJECTED_TEMPLATE.format(reason=(reason or "").strip()),
        purpose="participant_rejected",
    )


async def notify_participant_fix_needed(
    bot: "Bot",
    app: "Application",
    extra: str | None = None,
) -> None:
    """Требуется исправление; ``extra`` добавляется отдельным абзацем.

    Команда ``/notify_fix`` может передать ``текст_уточнения``; если
    передан, он добавляется к базовому шаблону через
    ``FIX_NEEDED_EXTRA_TEMPLATE``.
    """
    body = FIX_NEEDED_TEMPLATE
    if extra and extra.strip():
        body += FIX_NEEDED_EXTRA_TEMPLATE.format(extra=extra.strip())
    chat_id = await _resolve_user_chat_id(app.parent_huid)
    await _send_to_user(
        bot,
        huid=app.parent_huid,
        chat_id=chat_id,
        body=body,
        purpose="participant_fix_needed",
    )


async def notify_participant_shortlist(
    bot: "Bot", app: "Application"
) -> None:
    """Работа попала в шорт-лист."""
    chat_id = await _resolve_user_chat_id(app.parent_huid)
    await _send_to_user(
        bot,
        huid=app.parent_huid,
        chat_id=chat_id,
        body=SHORTLIST_TEMPLATE,
        purpose="participant_shortlist",
    )


async def notify_participant_jury_result(
    bot: "Bot", app: "Application", in_top_10: bool
) -> None:
    """Итоговое сообщение участнику по результатам жюри."""
    body = (
        JURY_RESULT_IN_TOP10_TEMPLATE
        if in_top_10
        else JURY_RESULT_NOT_IN_TOP10_TEMPLATE
    )
    chat_id = await _resolve_user_chat_id(app.parent_huid)
    await _send_to_user(
        bot,
        huid=app.parent_huid,
        chat_id=chat_id,
        body=body,
        purpose=f"participant_jury_result_{'top10' if in_top_10 else 'out'}",
    )


# =====================================================================
# Сообщения в чат модерации
# =====================================================================


def _format_files_pointer(app: "Application") -> str:
    """Поле «команда/ссылка просмотра файлов» для чата модерации."""
    from database.models import IntakeMode

    if app.intake_mode is IntakeMode.LINKS and app.cloud_link:
        return app.cloud_link
    return f"/files {app.br_id}"


async def notify_moderation_chat_new_application(
    bot: "Bot", app: "Application"
) -> None:
    """Служебное сообщение о новой заявке в чат модерации.

    Если настроен ``EXPRESS_DEEPLINK_TEMPLATE``, добавляем кнопку
    «🔎 Открыть в боте» с URL-ссылкой на DM с ботом. Команды
    (``/find BR-XXXX``, ``/files BR-XXXX``) остаются в теле сообщения —
    deeplink только открывает чат, ввод команды по-прежнему за модератором
    (у eXpress нет аналога ``?start=payload``).
    """
    body = NEW_APPLICATION_MODERATION_TEMPLATE.format(
        br_id=app.br_id,
        parent_full_name=app.parent_full_name,
        child_name=app.child_name,
        child_age=app.child_age,
        age_category=app.age_category.value,
        track=app.track.value,
        title=app.title,
        files_pointer=_format_files_pointer(app),
    )
    bubbles = _moderation_chat_open_in_bot_bubbles(bot)
    await _send_to_moderation_chat(
        bot,
        body,
        purpose="moderation_new_application",
        bubbles=bubbles,
    )


def _moderation_chat_open_in_bot_bubbles(bot: "Bot"):
    """``BubbleMarkup`` с одной кнопкой-ссылкой «Открыть в боте».

    Возвращает None, если deeplink не настроен — `_send_to_moderation_chat`
    тогда просто не добавит поле ``bubbles`` в JSON (см. правило
    `.cursor/rules/pybotx-bubbles.mdc`).
    """
    from pybotx import BubbleMarkup

    from utils.deeplink import build_bot_deeplink

    bot_id = getattr(bot, "id", None) or getattr(
        getattr(bot, "bot_accounts", [None])[0], "id", None
    )
    link = build_bot_deeplink(bot_id)
    if not link:
        return None
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/open_in_bot",
        label="🔎 Открыть в боте",
        link=link,
        new_row=True,
    )
    return bubbles


async def notify_moderation_chat_disk_alert(
    bot: "Bot",
    *,
    threshold_pct: int,
    free_mb: int,
    hours_left: float,
) -> None:
    """Alert о заполнении диска (80 % / 95 %).

    Дедупликация (раз в 24 ч на порог) делается в
    ``services.storage.check_and_alert_disk`` через таблицу
    ``disk_alerts`` — здесь только сама отправка.
    """
    if threshold_pct >= 95:
        body = DISK_ALERT_95_TEMPLATE.format(free_mb=free_mb)
    else:
        hours_text = (
            f"{hours_left:.1f}" if hours_left and hours_left > 0
            else "—"
        )
        body = DISK_ALERT_80_TEMPLATE.format(
            free_mb=free_mb, hours_left=hours_text
        )
    await _send_to_moderation_chat(
        bot,
        body,
        purpose=f"moderation_disk_alert_{threshold_pct}",
        bubbles=_moderation_chat_open_in_bot_bubbles(bot),
    )


# =====================================================================
# Уведомления о событиях жюри — с агрегацией
# =====================================================================
#
# Правило агрегации: открытие и закрытие раундов **агрегируются по
# моменту времени**. Если бот одновременно открывает или закрывает
# раунды сразу в нескольких пулах — отправляем одно сообщение
# со списком пулов.
#
# Реализация: события не отправляются сразу, а кладутся в asyncio.Queue;
# background-task периодически (раз в 5 секунд, в момент idle) собирает
# из очереди все события одного типа + одного round_no и шлёт одним
# сообщением. Жребий и шорт-лист — индивидуальные, обходят очередь
# и шлются сразу.

JuryEventKind = Literal[
    "round_opened",
    "round_closed",
    "lot_applied",
    "shortlist_ready",
]


@dataclass
class _JuryEvent:
    """Один pending-евент жюри (для агрегации в окне дебаунса)."""

    kind: JuryEventKind
    pool: tuple[str, str]  # (track_label, age_label)
    round_no: int | None
    deadline_text: str | None = None
    extra: str | None = None


@dataclass
class _AggregatorState:
    """Состояние in-memory агрегатора (один на процесс)."""

    queue: asyncio.Queue[_JuryEvent] = field(default_factory=asyncio.Queue)
    flush_task: asyncio.Task | None = None
    bot: "Bot | None" = None


_AGGREGATOR_DEBOUNCE_SECONDS = 5.0
_AGGREGATOR: _AggregatorState | None = None


def _get_aggregator() -> _AggregatorState:
    global _AGGREGATOR
    if _AGGREGATOR is None:
        _AGGREGATOR = _AggregatorState()
    return _AGGREGATOR


async def _flush_aggregator() -> None:
    """Собрать накопленные события и отправить агрегированные сообщения."""
    agg = _get_aggregator()
    bot = agg.bot
    pending: list[_JuryEvent] = []
    while not agg.queue.empty():
        try:
            pending.append(agg.queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    if not pending or bot is None:
        return

    # Группируем round_opened и round_closed по (kind, round_no).
    grouped: dict[tuple[str, int | None], list[_JuryEvent]] = {}
    for ev in pending:
        if ev.kind in ("lot_applied", "shortlist_ready"):
            # Эти типы не агрегируем — шлём как есть, по одному.
            await _send_jury_event_single(bot, ev)
            continue
        grouped.setdefault((ev.kind, ev.round_no), []).append(ev)

    for (kind, round_no), events in grouped.items():
        pools = [ev.pool for ev in events]
        deadline_text = next(
            (ev.deadline_text for ev in events if ev.deadline_text),
            None,
        )
        if kind == "round_opened":
            if len(pools) == 1:
                pool_label = f"{pools[0][0]} / {pools[0][1]}"
                body = JURY_ROUND_OPENED_SINGLE_TEMPLATE.format(
                    pool=pool_label,
                    round_no=round_no or 1,
                    candidates_n=events[0].extra or "—",
                    deadline=deadline_text or "не задан",
                )
            else:
                body = JURY_ROUND_OPENED_TEMPLATE.format(
                    round_no=round_no or 1,
                    pool_lines=_format_pool_lines(pools),
                    deadline=deadline_text or "не задан",
                )
        elif kind == "round_closed":
            if len(pools) == 1:
                pool_label = f"{pools[0][0]} / {pools[0][1]}"
                body = JURY_ROUND_CLOSED_SINGLE_TEMPLATE.format(
                    pool=pool_label,
                    round_no=round_no or 1,
                )
            else:
                body = JURY_ROUND_CLOSED_TEMPLATE.format(
                    round_no=round_no or 1,
                    pool_lines=_format_pool_lines(pools),
                )
        else:
            continue  # pragma: no cover — типов больше нет
        await _send_to_moderation_chat(
            bot,
            body,
            purpose=f"moderation_jury_{kind}_aggregated",
            bubbles=_moderation_chat_open_in_bot_bubbles(bot),
        )


async def _send_jury_event_single(bot: "Bot", ev: _JuryEvent) -> None:
    """Не-агрегируемые события (жребий, шорт-лист)."""
    pool_label = f"{ev.pool[0]} / {ev.pool[1]}"
    if ev.kind == "lot_applied":
        body = JURY_LOT_TEMPLATE.format(
            pool=pool_label, round_no=ev.round_no or 1
        )
        await _send_to_moderation_chat(
            bot,
            body,
            purpose="moderation_jury_lot",
            bubbles=_moderation_chat_open_in_bot_bubbles(bot),
        )
    elif ev.kind == "shortlist_ready":
        await _send_to_moderation_chat(
            bot,
            JURY_SHORTLIST_READY_TEMPLATE,
            purpose="moderation_jury_shortlist_ready",
            bubbles=_moderation_chat_open_in_bot_bubbles(bot),
        )


async def _aggregator_worker() -> None:
    """Background-таск: ждёт ``_AGGREGATOR_DEBOUNCE_SECONDS`` после
    каждого события и сбрасывает очередь."""
    agg = _get_aggregator()
    try:
        while True:
            await asyncio.sleep(_AGGREGATOR_DEBOUNCE_SECONDS)
            if agg.queue.empty():
                # Очередь пуста — выходим, чтобы не висел вечный таск.
                agg.flush_task = None
                return
            await _flush_aggregator()
    except asyncio.CancelledError:
        await _flush_aggregator()
        raise


async def _enqueue_jury_event(bot: "Bot", event: _JuryEvent) -> None:
    """Добавить событие в очередь агрегатора и запустить worker при необходимости."""
    agg = _get_aggregator()
    agg.bot = bot
    await agg.queue.put(event)
    if agg.flush_task is None or agg.flush_task.done():
        agg.flush_task = asyncio.create_task(_aggregator_worker())


async def notify_moderation_chat_jury_event(
    bot: "Bot",
    *,
    event_kind: str,
    pools: list[tuple[str, str]],
    round_no: int | None,
    deadline_text: str | None = None,
    extra: str | None = None,
) -> None:
    """Событие жюри для чата модерации.

    Поведение по типу события:
    - ``round_opened`` / ``round_closed`` — кладём в очередь агрегатора;
      одно сообщение со списком пулов уйдёт через ``_AGGREGATOR_DEBOUNCE_SECONDS``
      секунд (если за это время прилетят ещё события того же типа и
      номера раунда — они склеятся в одно сообщение).
    - ``lot_applied`` — индивидуально, без агрегации.
    - ``shortlist_ready`` — индивидуально, без агрегации; ``pools``
      игнорируется.

    Args:
        event_kind: ``round_opened`` / ``round_closed`` / ``lot_applied`` /
            ``shortlist_ready``.
        pools: ``[(track_label, age_label), ...]`` — для жребия достаточно
            одного элемента; для шорт-листа можно передать пустой список.
        round_no: номер раунда (1..3) или None для shortlist_ready.
        deadline_text: человекочитаемый дедлайн раунда — для round_opened.
        extra: произвольная строка для шаблона (например, число претендентов
            при одиночном round_opened).
    """
    if event_kind not in (
        "round_opened",
        "round_closed",
        "lot_applied",
        "shortlist_ready",
    ):
        logger.warning(
            "Неизвестный тип события жюри для нотификации",
            event_kind=event_kind,
        )
        return

    if event_kind == "shortlist_ready":
        await _send_jury_event_single(
            bot,
            _JuryEvent(
                kind="shortlist_ready",
                pool=("", ""),
                round_no=None,
            ),
        )
        return

    if event_kind == "lot_applied":
        if not pools:
            logger.warning("lot_applied без указания пула; пропускаем")
            return
        await _send_jury_event_single(
            bot,
            _JuryEvent(
                kind="lot_applied",
                pool=pools[0],
                round_no=round_no,
            ),
        )
        return

    # round_opened / round_closed — через агрегатор.
    for pool in pools:
        await _enqueue_jury_event(
            bot,
            _JuryEvent(
                kind=event_kind,  # type: ignore[arg-type]
                pool=pool,
                round_no=round_no,
                deadline_text=deadline_text,
                extra=extra,
            ),
        )


async def flush_jury_event_aggregator() -> None:
    """Принудительно сбросить очередь агрегатора (для тестов / shutdown)."""
    agg = _get_aggregator()
    if agg.flush_task and not agg.flush_task.done():
        agg.flush_task.cancel()
        try:
            await agg.flush_task
        except asyncio.CancelledError:
            pass
    else:
        await _flush_aggregator()


__all__ = [
    # Шаблоны участнику
    "ACCEPTED_TEMPLATE",
    "REJECTED_TEMPLATE",
    "FIX_NEEDED_TEMPLATE",
    "FIX_NEEDED_EXTRA_TEMPLATE",
    "SHORTLIST_TEMPLATE",
    "JURY_RESULT_IN_TOP10_TEMPLATE",
    "JURY_RESULT_NOT_IN_TOP10_TEMPLATE",
    "INTAKE_BLOCKED_PARTICIPANT_TEMPLATE",
    "INTAKE_MODE_LINKS_NOTICE_TEMPLATE",
    # Шаблоны в чат модерации
    "NEW_APPLICATION_MODERATION_TEMPLATE",
    "JURY_ROUND_OPENED_TEMPLATE",
    "JURY_ROUND_OPENED_SINGLE_TEMPLATE",
    "JURY_ROUND_CLOSED_TEMPLATE",
    "JURY_ROUND_CLOSED_SINGLE_TEMPLATE",
    "JURY_LOT_TEMPLATE",
    "JURY_SHORTLIST_READY_TEMPLATE",
    "DISK_ALERT_80_TEMPLATE",
    "DISK_ALERT_95_TEMPLATE",
    # Функции участнику
    "notify_participant_accepted",
    "notify_participant_rejected",
    "notify_participant_fix_needed",
    "notify_participant_shortlist",
    "notify_participant_jury_result",
    # Функции в чат модерации
    "notify_moderation_chat_new_application",
    "notify_moderation_chat_jury_event",
    "notify_moderation_chat_disk_alert",
    # Утилиты
    "flush_jury_event_aggregator",
]
