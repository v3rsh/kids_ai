"""
Бизнес-логика ветки модератора.

Сервис обслуживает команды модератора:

- ``list_queue`` — пагинация и фильтры;
- ``find_by_br_id`` — карточка заявки по ``BR-2026-XXXX``;
- ``change_status`` — смена статуса в одной из групп;
- ``add_comment`` — добавить комментарий модератора;
- ``count_stats`` — агрегированная статистика для ``/stats``.

Соглашения:
- Все запросы — через ``selectinload`` (правило `performance.mdc`).
- N+1 запросов нет: пагинация делается одним SELECT + один COUNT,
  агрегаты статистики — одной серией ``GROUP BY`` без циклов.
- Каждая публичная функция открывает свою сессию через
  ``database.db.get_session()`` (одна сессия на запрос).
- Возвращаемые типы — либо ORM-объекты ``Application`` (хендлеры
  читают их read-only и сразу формируют ответ), либо простые
  dataclass/dict для статистики.

Источник правды по полям статусов — ``database.models``:
``ModerationStatus``, ``JuryStatus``, ``VotingStatus``. Группа ``merch``
в БД отдельным enum не моделируется: «Потенциал для мерча» храним
как свободный текст в ``Application.merch_potential`` — туда же
смотрит Excel-витрина в одноимённой колонке.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Literal, Sequence
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.orm import selectinload

from database.db import get_session
from database.models import (
    AgeCategory,
    Application,
    JuryStatus,
    ModerationStatus,
    Track,
    VotingStatus,
)


# =====================================================================
# DTO/типы
# =====================================================================


StatusGroup = Literal["moderation", "jury", "voting", "merch"]
"""Группа статусов: ``moderation`` / ``jury`` / ``voting`` / ``merch``.

``jury`` — read-only для модератора: ручное редактирование запрещено,
бот перезапишет. Сервис ``change_status`` для этой группы вернёт
``ChangeStatusResult`` с ``ok=False`` и человекочитаемой ошибкой.
"""


@dataclass(frozen=True)
class QueueFilters:
    """Фильтры команды ``/queue``.

    ``date_from`` / ``date_to`` — границы по дате подачи (включительно,
    локальная дата подачи в TZ Europe/Moscow). ``None`` — без ограничения.

    ``moderation_statuses`` — пустое множество означает «без фильтра по
    статусу»; по умолчанию хендлер ``/queue`` ставит сюда
    ``{NA_MODERATSII, NUZHNO_ISPRAVIT}`` (это «активная» очередь).
    """

    tracks: tuple[Track, ...] = ()
    age_categories: tuple[AgeCategory, ...] = ()
    moderation_statuses: tuple[ModerationStatus, ...] = ()
    date_from: date | None = None
    date_to: date | None = None

    def is_empty(self) -> bool:
        """True, если фильтры не указаны вообще."""
        return (
            not self.tracks
            and not self.age_categories
            and not self.moderation_statuses
            and self.date_from is None
            and self.date_to is None
        )


@dataclass(frozen=True)
class QueuePage:
    """Страница результата ``list_queue``.

    ``items`` — заявки текущей страницы с уже подгруженными ``files``
    через ``selectinload``. ``total`` — общее число записей по фильтру
    (для счётчика «3 из 20»).
    """

    items: list[Application]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return max(1, (self.total + self.page_size - 1) // self.page_size)


@dataclass(frozen=True)
class ChangeStatusResult:
    """Итог ``change_status``.

    ``ok=False`` — статус не сменён (не нашли заявку, недопустимое
    значение, попытка ручного редактирования жюри). ``error`` — текст
    для модератора. ``application`` — ORM-объект (с подгруженными
    ``files``) — заполнено только при ``ok=True``.
    """

    ok: bool
    application: Application | None = None
    error: str | None = None
    previous_value: str | None = None
    new_value: str | None = None


@dataclass(frozen=True)
class StatsCounters:
    """Сводка статистики для ``/stats``.

    ``period_label`` — человекочитаемая подпись («сегодня» / «весь
    период»). Все распределения — ``dict[str, int]``, ключ — текстовое
    значение enum (``Track.value``, ``AgeCategory.value``,
    ``ModerationStatus.value``).
    """

    period_label: str
    period_from: datetime | None
    period_to: datetime | None
    total: int
    by_track: dict[str, int]
    by_age_category: dict[str, int]
    by_moderation_status: dict[str, int]
    needs_fix: int
    rejected: int


StatsPeriod = Literal["today", "all"]


# =====================================================================
# Внутренние утилиты
# =====================================================================

# Модератор работает с активной очередью —
# в дефолте это статусы «на модерации» и «нужно исправить».
DEFAULT_QUEUE_STATUSES: tuple[ModerationStatus, ...] = (
    ModerationStatus.NA_MODERATSII,
    ModerationStatus.NUZHNO_ISPRAVIT,
)


def _moderation_status_by_value(value: str) -> ModerationStatus | None:
    """Сопоставить пользовательский ввод значению enum статусов модерации."""
    needle = value.strip().casefold()
    for status in ModerationStatus:
        if status.value.casefold() == needle or status.name.casefold() == needle:
            return status
    return None


def _voting_status_by_value(value: str) -> VotingStatus | None:
    needle = value.strip().casefold()
    for status in VotingStatus:
        if status.value.casefold() == needle or status.name.casefold() == needle:
            return status
    return None


def _track_by_value(value: str) -> Track | None:
    needle = value.strip().casefold()
    for track in Track:
        if track.value.casefold() == needle or track.name.casefold() == needle:
            return track
    return None


def _age_category_by_value(value: str) -> AgeCategory | None:
    needle = value.strip().casefold()
    for cat in AgeCategory:
        if cat.value.casefold() == needle or cat.name.casefold() == needle:
            return cat
    return None


def parse_status_group(value: str) -> StatusGroup | None:
    """Преобразовать пользовательский ввод в ``StatusGroup``.

    Принимает русские синонимы (``модерация``, ``жюри``, ``голосование``,
    ``мерч``) и английские ключи. Возвращает ``None``, если не распознано.
    """
    if not value:
        return None
    needle = value.strip().casefold()
    aliases: dict[str, StatusGroup] = {
        "moderation": "moderation",
        "модерация": "moderation",
        "модерации": "moderation",
        "jury": "jury",
        "жюри": "jury",
        "voting": "voting",
        "голосование": "voting",
        "голосования": "voting",
        "merch": "merch",
        "мерч": "merch",
        "мерча": "merch",
    }
    return aliases.get(needle)


def _build_queue_where_clauses(filters: QueueFilters):
    """Собрать список ``where``-условий по ``QueueFilters``.

    Возвращается ``list[Any]`` для подстановки в ``select(...).where(*)``.
    """
    clauses = []
    if filters.tracks:
        clauses.append(Application.track.in_(list(filters.tracks)))
    if filters.age_categories:
        clauses.append(Application.age_category.in_(list(filters.age_categories)))
    if filters.moderation_statuses:
        clauses.append(
            Application.moderation_status.in_(list(filters.moderation_statuses))
        )
    if filters.date_from is not None:
        clauses.append(
            Application.created_at >= datetime.combine(filters.date_from, time.min)
        )
    if filters.date_to is not None:
        clauses.append(
            Application.created_at
            < datetime.combine(filters.date_to + timedelta(days=1), time.min)
        )
    return clauses


# =====================================================================
# Публичный API
# =====================================================================


async def list_queue(
    *,
    filters: QueueFilters | None = None,
    page: int = 1,
    page_size: int = 5,
) -> QueuePage:
    """Пагинация заявок для команды ``/queue``.

    Один SELECT с ``selectinload(Application.files)`` под список и один
    лёгкий COUNT под счётчик «N из M». N+1 не возникает: ``files``
    подтягиваются батчем по ``application_id IN (...)``.

    Args:
        filters: фильтры по треку/возрасту/статусу/дате.
            ``None`` ⇒ ``QueueFilters()`` (без фильтров, в выдаче будут
            заявки в любом статусе модерации).
        page: 1-индексированный номер страницы. Значения <1
            нормализуются к 1.
        page_size: размер страницы (по умолчанию 5).

    Returns:
        ``QueuePage`` с ``items`` (заявки страницы), ``total`` (всего по
        фильтру), ``page``, ``page_size``.
    """
    if filters is None:
        filters = QueueFilters()
    page = max(page, 1)
    page_size = max(page_size, 1)
    offset = (page - 1) * page_size

    where_clauses = _build_queue_where_clauses(filters)

    async with get_session()() as session:
        count_stmt = select(func.count()).select_from(Application)
        if where_clauses:
            count_stmt = count_stmt.where(and_(*where_clauses))
        total = (await session.execute(count_stmt)).scalar_one()

        list_stmt = (
            select(Application)
            .options(selectinload(Application.files))
            .order_by(Application.created_at.desc(), Application.id.desc())
            .offset(offset)
            .limit(page_size)
        )
        if where_clauses:
            list_stmt = list_stmt.where(and_(*where_clauses))

        items = list((await session.execute(list_stmt)).scalars().all())

    return QueuePage(items=items, total=int(total), page=page, page_size=page_size)


async def find_by_br_id(br_id: str) -> Application | None:
    """Карточка заявки по ``BR-2026-XXXX`` (команда ``/find``).

    ``br_id`` нормализуется (``upper`` + ``strip``). Возвращает ORM-объект
    с подгруженными ``files`` или ``None``, если заявка не найдена.
    """
    if not br_id:
        return None
    needle = br_id.strip().upper()
    async with get_session()() as session:
        stmt = (
            select(Application)
            .where(Application.br_id == needle)
            .options(selectinload(Application.files))
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def change_status(
    *,
    br_id: str,
    group: StatusGroup,
    new_value: str,
    by_huid: UUID,
) -> ChangeStatusResult:
    """Сменить статус заявки в одной из 4 групп (команда ``/status``).

    Поведение по группам:

    - ``moderation`` — ``ModerationStatus``. Принимаются как
      ``.value`` («допущено»), так и ``.name`` (``DOPUSHCHENO``).
    - ``voting`` — ``VotingStatus``. Аналогично.
    - ``merch`` — пишет произвольный текст в
      ``Application.merch_potential`` (отдельной enum-группы нет —
      произвольный текст «Потенциал для мерча»).
    - ``jury`` — **запрещено** ручное редактирование: сервис
      возвращает ``ok=False`` с пояснением.

    Args:
        br_id: ID заявки (``BR-2026-XXXX``).
        group: группа статусов.
        new_value: новое значение (для ``merch`` — произвольный текст).
        by_huid: HUID модератора (для логов; в БД пока не пишется —
          журнал смен статусов появится при необходимости).

    Returns:
        ``ChangeStatusResult`` — см. описание полей в классе.
    """
    needle = br_id.strip().upper() if br_id else ""
    if not needle:
        return ChangeStatusResult(ok=False, error="Пустой ID заявки")

    async with get_session()() as session:
        stmt = (
            select(Application)
            .where(Application.br_id == needle)
            .options(selectinload(Application.files))
        )
        app = (await session.execute(stmt)).scalar_one_or_none()
        if app is None:
            return ChangeStatusResult(
                ok=False, error=f"Заявка {needle} не найдена"
            )

        if group == "jury":
            return ChangeStatusResult(
                ok=False,
                error=(
                    "Группа «жюри» заполняется ботом автоматически "
                    "по итогам процесса по пулу. Ручное "
                    "редактирование запрещено."
                ),
            )

        previous: str
        new_repr: str

        if group == "moderation":
            status = _moderation_status_by_value(new_value)
            if status is None:
                allowed = ", ".join(s.value for s in ModerationStatus)
                return ChangeStatusResult(
                    ok=False,
                    error=(
                        f"Недопустимое значение «{new_value}» для группы "
                        f"«модерация». Допустимые: {allowed}"
                    ),
                )
            previous = app.moderation_status.value
            new_repr = status.value
            if app.moderation_status != status:
                app.moderation_status = status
        elif group == "voting":
            status_v = _voting_status_by_value(new_value)
            if status_v is None:
                allowed = ", ".join(s.value for s in VotingStatus)
                return ChangeStatusResult(
                    ok=False,
                    error=(
                        f"Недопустимое значение «{new_value}» для группы "
                        f"«голосование». Допустимые: {allowed}"
                    ),
                )
            previous = app.voting_status.value
            new_repr = status_v.value
            if app.voting_status != status_v:
                app.voting_status = status_v
        elif group == "merch":
            previous = app.merch_potential or ""
            text = (new_value or "").strip()
            app.merch_potential = text or None
            new_repr = text or "—"
        else:  # pragma: no cover — защитный fallback
            return ChangeStatusResult(
                ok=False, error=f"Неизвестная группа статусов: {group!r}"
            )

        await session.commit()
        await session.refresh(app, attribute_names=["files"])

        logger.info(
            "Модератор сменил статус заявки",
            br_id=app.br_id,
            group=group,
            previous=previous,
            new=new_repr,
            by_huid=str(by_huid),
        )
        return ChangeStatusResult(
            ok=True,
            application=app,
            previous_value=previous,
            new_value=new_repr,
        )


async def add_comment(
    *, br_id: str, text: str, by_huid: UUID
) -> Application | None:
    """Добавить/перезаписать комментарий модератора заявки.

    Текущая модель ``Application.moderator_comment`` — одно текстовое
    поле, поэтому предыдущий комментарий перезаписывается. Если позже
    появится журнал комментариев — этот сервис расширится без правок
    хендлеров (сигнатура остаётся прежней).

    Возвращает обновлённую заявку или ``None``, если не нашли.
    """
    needle = br_id.strip().upper() if br_id else ""
    if not needle:
        return None

    async with get_session()() as session:
        stmt = (
            select(Application)
            .where(Application.br_id == needle)
            .options(selectinload(Application.files))
        )
        app = (await session.execute(stmt)).scalar_one_or_none()
        if app is None:
            return None
        previous = app.moderator_comment
        app.moderator_comment = (text or "").strip() or None
        await session.commit()
        await session.refresh(app, attribute_names=["files"])
        logger.info(
            "Модератор обновил комментарий заявки",
            br_id=app.br_id,
            had_previous=bool(previous),
            new_length=len(app.moderator_comment or ""),
            by_huid=str(by_huid),
        )
        return app


async def count_stats(period: StatsPeriod = "all") -> StatsCounters:
    """Агрегированная статистика по заявкам для команды ``/stats``.

    Делается **тремя** SQL-запросами с ``GROUP BY`` (по треку,
    по возрасту, по статусу модерации) + один общий COUNT. Никаких
    ``SELECT`` в циклах: всё агрегируется на уровне БД.

    ``period``:

    - ``today`` — по дате подачи в текущем дне (локальное время сервера,
      00:00..24:00). При желании заказчик переопределит TZ.
    - ``all`` — за весь период приёма (без ограничения снизу).
    """
    if period == "today":
        today = datetime.utcnow().date()
        period_from = datetime.combine(today, time.min)
        period_to = datetime.combine(today + timedelta(days=1), time.min)
        period_label = "сегодня"
    elif period == "all":
        period_from = None
        period_to = None
        period_label = "весь период"
    else:  # pragma: no cover
        raise ValueError(f"Неизвестный период статистики: {period!r}")

    where_clauses = []
    if period_from is not None:
        where_clauses.append(Application.created_at >= period_from)
    if period_to is not None:
        where_clauses.append(Application.created_at < period_to)

    async with get_session()() as session:
        total_stmt = select(func.count()).select_from(Application)
        if where_clauses:
            total_stmt = total_stmt.where(and_(*where_clauses))
        total = int((await session.execute(total_stmt)).scalar_one())

        track_stmt = select(Application.track, func.count()).group_by(
            Application.track
        )
        if where_clauses:
            track_stmt = track_stmt.where(and_(*where_clauses))
        by_track = {
            track.value: int(cnt)
            for track, cnt in (await session.execute(track_stmt)).all()
        }

        age_stmt = select(Application.age_category, func.count()).group_by(
            Application.age_category
        )
        if where_clauses:
            age_stmt = age_stmt.where(and_(*where_clauses))
        by_age = {
            cat.value: int(cnt)
            for cat, cnt in (await session.execute(age_stmt)).all()
        }

        status_stmt = select(
            Application.moderation_status, func.count()
        ).group_by(Application.moderation_status)
        if where_clauses:
            status_stmt = status_stmt.where(and_(*where_clauses))
        by_status = {
            status.value: int(cnt)
            for status, cnt in (await session.execute(status_stmt)).all()
        }

    needs_fix = by_status.get(ModerationStatus.NUZHNO_ISPRAVIT.value, 0)
    rejected = by_status.get(ModerationStatus.OTKLONENO.value, 0)

    return StatsCounters(
        period_label=period_label,
        period_from=period_from,
        period_to=period_to,
        total=total,
        by_track=by_track,
        by_age_category=by_age,
        by_moderation_status=by_status,
        needs_fix=needs_fix,
        rejected=rejected,
    )


__all__ = [
    "DEFAULT_QUEUE_STATUSES",
    "QueueFilters",
    "QueuePage",
    "ChangeStatusResult",
    "StatsCounters",
    "StatsPeriod",
    "StatusGroup",
    "list_queue",
    "find_by_br_id",
    "change_status",
    "add_comment",
    "count_stats",
    "parse_status_group",
]
