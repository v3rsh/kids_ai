"""
Сервис заявок (родитель/участник).

Реализует жизненный цикл модели ``Application``:
- генерация сквозного ``br_id`` формата ``BR-{COMPETITION_YEAR}-NNNN``;
- алгоритм автопометки «возможный дубль»;
- создание заявки: атомарная транзакция с PostgreSQL advisory-lock'ом,
  чтобы две параллельные подачи не получили один br_id;
- маркировка актуальной версии заявки модератором.

Сессии БД открываются внутри функций (одна функция = одна сессия,
см. ``.cursor/rules/performance.mdc``). Под капотом — один SELECT для
вычисления next-id, один SELECT для поиска дубля, один INSERT для
самой заявки — итого ≤3 запроса на подачу. Внутри циклов запросов нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

from loguru import logger
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from config import COMPETITION_YEAR
from database.db import get_session
from database.models import (
    AgeCategory,
    Application,
    ApplicationFile,
    FileKind,
    IntakeMode,
    JuryStatus,
    ModerationStatus,
    Track,
    VotingStatus,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


@dataclass(frozen=True)
class ApplicationFileSpec:
    """Спецификация одного файла заявки для регистрации в БД.

    Используется в ``register_application_files`` — содержит ровно те
    поля, которые требуются для INSERT в ``application_files`` после
    того, как файл уже физически положен в папку заявки сервисом
    ``storage.rename_and_save_file``.
    """

    kind: FileKind
    angle_no: int | None
    original_filename: str
    stored_filename: str
    relative_path: str
    size_bytes: int
    mime_type: str


# Advisory-lock key для сериализации генерации br_id внутри года.
# `pg_advisory_xact_lock(key)` снимается автоматически при коммите/откате —
# это безопаснее, чем sequence (нет «дыр» от откатов) и проще, чем INSERT
# с обработкой UniqueViolation + ретраи.
_BR_ID_LOCK_KEY = 0xBA8E_8001  # любое стабильное int — не пересекается с другими locks


def child_submission_key(child_name: str, child_age: int) -> str:
    """Ключ «ребёнок» для дубля и лимита «1 работа в трек».

    ``normalize_child_name`` + возраст в полных годах (как в анкете).
    """
    return f"{normalize_child_name(child_name)}|{child_age}"


def submission_keys_match(
    *,
    child_name_a: str,
    child_age_a: int,
    child_name_b: str,
    child_age_b: int,
) -> bool:
    """True, если две заявки относятся к одному ребёнку по правилам конкурса."""
    return child_submission_key(child_name_a, child_age_a) == child_submission_key(
        child_name_b, child_age_b
    )


def normalize_child_name(child_name: str) -> str:
    """Нормализация имени ребёнка для алгоритма дубля.

    Чистая функция: ``strip`` → lowercase → замена ``ё``/``Ё`` → ``е``.
    Не делает unicode-NFC и не убирает пробелы внутри (двойное имя
    «Анна-Мария» с пробелами вокруг дефиса остаётся как есть).
    """
    if child_name is None:
        return ""
    return (
        child_name.strip()
        .lower()
        .replace("ё", "е")
        .replace("Ё", "е")
    )


async def assign_br_id() -> str:
    """Сгенерировать следующий по порядку BR-ID.

    Формат — ``BR-{COMPETITION_YEAR}-{NNNN}``, нумерация сквозная по году.
    Внутри функции открывается своя транзакция с PostgreSQL
    advisory-lock'ом, поэтому функция безопасна при конкурентных вызовах
    (например, две одновременные подачи).

    Используется в режиме ``links``, где br_id отдаётся участнику ДО
    запроса ссылки. В режиме ``files`` (основной) внутри
    ``create_application`` используется тот же алгоритм, но в общей с
    INSERT транзакции — это даёт строгую атомарность «выдан id ⇒
    запись существует».
    """
    prefix = f"BR-{COMPETITION_YEAR}-"
    async with get_session()() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _BR_ID_LOCK_KEY + COMPETITION_YEAR},
        )
        next_num = await _select_next_br_number(session, prefix)
        br_id = f"{prefix}{next_num:04d}"
        # commit — отпускаем advisory-lock; запись с этим br_id будет
        # создана отдельным вызовом ``create_application(br_id=...)``.
        await session.commit()
    logger.debug("assign_br_id выдан", br_id=br_id)
    return br_id


async def _select_next_br_number(session, prefix: str) -> int:
    """SELECT MAX(br_id) + 1 в рамках текущей транзакции/сессии."""
    result = await session.execute(
        select(func.max(Application.br_id)).where(
            Application.br_id.like(f"{prefix}%")
        )
    )
    last_br_id: str | None = result.scalar_one_or_none()
    if not last_br_id:
        return 1
    try:
        return int(last_br_id.removeprefix(prefix)) + 1
    except (ValueError, AttributeError):
        logger.warning(
            "Не удалось распарсить последний br_id, начинаем с 1",
            last_br_id=last_br_id,
        )
        return 1


async def find_possible_duplicate(
    *,
    parent_huid: UUID,
    child_name: str,
    child_age: int,
    track_name: str,
) -> "Application | None":
    """Алгоритм автопометки «возможный дубль».

    Возвращает последнюю ранее принятую заявку с тем же набором ключей:
    ``parent_huid`` + ``child_submission_key`` + ``track``. Заявки в
    статусе ``отклонено`` в проверке не участвуют.

    Реализация: один SELECT с фильтром по ``parent_huid + track``
    (узкая выборка), нормализация имени ребёнка — в Python (это
    избавляет от хрупкости ``LOWER + REPLACE`` в SQL). Внутри цикла
    запросов нет — соответствует правилу ``performance.mdc``.
    """
    try:
        track_enum = Track[track_name]
    except KeyError as exc:
        raise ValueError(
            f"Неизвестный track_name: {track_name!r}. "
            f"Допустимы: {[t.name for t in Track]}"
        ) from exc

    if not normalize_child_name(child_name):
        return None

    async with get_session()() as session:
        result = await session.execute(
            select(Application)
            .where(
                Application.parent_huid == parent_huid,
                Application.track == track_enum,
                Application.moderation_status != ModerationStatus.OTKLONENO,
            )
            .order_by(Application.created_at.desc())
        )
        for candidate in result.scalars():
            if submission_keys_match(
                child_name_a=child_name,
                child_age_a=child_age,
                child_name_b=candidate.child_name,
                child_age_b=candidate.child_age,
            ):
                return candidate
    return None


@dataclass(frozen=True)
class MultiSubmissionEntry:
    """Одна заявка внутри группы повторных подач."""

    br_id: str
    moderation_status: str
    created_at: datetime
    is_actual_version: bool


@dataclass(frozen=True)
class MultiSubmissionGroup:
    """Нарушение правила «1 работа в трек» для одного ребёнка."""

    parent_huid: UUID
    parent_full_name: str
    child_name: str
    child_age: int
    track: Track
    entries: tuple[MultiSubmissionEntry, ...]


@dataclass(frozen=True)
class ParentTrackEntry:
    """Заявка внутри группы «родитель + трек» для модератора."""

    br_id: str
    child_name: str
    child_age: int
    title: str
    moderation_status: str
    created_at: datetime
    is_actual_version: bool
    is_strict_match: bool = False


@dataclass(frozen=True)
class ParentTrackGroup:
    """Несколько активных заявок одного родителя в одном треке."""

    parent_huid: UUID
    parent_full_name: str
    track: Track
    entries: tuple[ParentTrackEntry, ...]


def _application_to_parent_track_entry(
    app: Application,
    *,
    anchor: Application | None = None,
) -> ParentTrackEntry:
    is_strict = False
    if anchor is not None:
        is_strict = submission_keys_match(
            child_name_a=anchor.child_name,
            child_age_a=anchor.child_age,
            child_name_b=app.child_name,
            child_age_b=app.child_age,
        )
    return ParentTrackEntry(
        br_id=app.br_id,
        child_name=app.child_name,
        child_age=app.child_age,
        title=app.title,
        moderation_status=app.moderation_status.value,
        created_at=app.created_at,
        is_actual_version=app.is_actual_version,
        is_strict_match=is_strict,
    )


def _group_by_parent_track(
    apps: Sequence[Application],
    *,
    only_active: bool,
) -> list[ParentTrackGroup]:
    """Сгруппировать заявки по (parent, track), оставить count>1."""
    buckets: dict[tuple[UUID, Track], list[Application]] = {}
    for app in apps:
        if only_active and app.moderation_status == ModerationStatus.OTKLONENO:
            continue
        key = (app.parent_huid, app.track)
        buckets.setdefault(key, []).append(app)

    groups: list[ParentTrackGroup] = []
    for (_parent, _track), members in buckets.items():
        if len(members) < 2:
            continue
        members_sorted = sorted(
            members, key=lambda a: (a.created_at, str(a.id)), reverse=True
        )
        sample = members_sorted[0]
        entries = tuple(
            _application_to_parent_track_entry(m) for m in members_sorted
        )
        groups.append(
            ParentTrackGroup(
                parent_huid=sample.parent_huid,
                parent_full_name=sample.parent_full_name,
                track=sample.track,
                entries=entries,
            )
        )
    groups.sort(
        key=lambda g: (g.parent_full_name.lower(), g.track.value),
    )
    return groups


def strict_duplicate_br_ids_in_group(
    group: ParentTrackGroup,
) -> set[str]:
    """BR-ID заявок, входящих в strict-цепочки (≥2 с одним child_key)."""
    by_child: dict[str, list[ParentTrackEntry]] = {}
    for entry in group.entries:
        key = child_submission_key(entry.child_name, entry.child_age)
        by_child.setdefault(key, []).append(entry)
    ids: set[str] = set()
    for members in by_child.values():
        if len(members) >= 2:
            for entry in members:
                ids.add(entry.br_id)
    return ids


def _group_applications(
    apps: Sequence[Application],
    *,
    only_active: bool,
) -> list[MultiSubmissionGroup]:
    """Сгруппировать заявки по (parent, child_key, track), оставить count>1."""
    buckets: dict[tuple[UUID, str, Track], list[Application]] = {}
    for app in apps:
        if only_active and app.moderation_status == ModerationStatus.OTKLONENO:
            continue
        key = (
            app.parent_huid,
            child_submission_key(app.child_name, app.child_age),
            app.track,
        )
        buckets.setdefault(key, []).append(app)

    groups: list[MultiSubmissionGroup] = []
    for (_parent, _child_key, _track), members in buckets.items():
        if len(members) < 2:
            continue
        members_sorted = sorted(
            members, key=lambda a: (a.created_at, str(a.id)), reverse=True
        )
        sample = members_sorted[0]
        entries = tuple(
            MultiSubmissionEntry(
                br_id=m.br_id,
                moderation_status=m.moderation_status.value,
                created_at=m.created_at,
                is_actual_version=m.is_actual_version,
            )
            for m in members_sorted
        )
        groups.append(
            MultiSubmissionGroup(
                parent_huid=sample.parent_huid,
                parent_full_name=sample.parent_full_name,
                child_name=sample.child_name,
                child_age=sample.child_age,
                track=sample.track,
                entries=entries,
            )
        )
    groups.sort(
        key=lambda g: (
            g.parent_full_name.lower(),
            g.child_name.lower(),
            g.track.value,
        )
    )
    return groups


async def find_multi_submission_groups(
    *,
    only_active: bool = True,
) -> list[MultiSubmissionGroup]:
    """Группы с более чем одной заявкой на (родитель, ребёнок, трек).

    Один SELECT по всем заявкам, группировка в Python (без N+1).
    """
    async with get_session()() as session:
        result = await session.execute(
            select(Application).order_by(Application.created_at.desc())
        )
        apps = list(result.scalars().all())
    return _group_applications(apps, only_active=only_active)


async def count_multi_submission_groups(*, only_active: bool = True) -> int:
    """Число групп «несколько заявок у родителя в одном треке»."""
    return len(await find_parent_track_groups(only_active=only_active))


async def find_parent_track_groups(
    *,
    only_active: bool = True,
) -> list[ParentTrackGroup]:
    """Группы с более чем одной заявкой на (родитель, трек).

    Один SELECT по всем заявкам, группировка в Python (без N+1).
    """
    async with get_session()() as session:
        result = await session.execute(
            select(Application).order_by(Application.created_at.desc())
        )
        apps = list(result.scalars().all())
    return _group_by_parent_track(apps, only_active=only_active)


async def find_related_for_moderator(
    br_id: str,
) -> tuple[Application | None, list[ParentTrackEntry]]:
    """Связанные «живые» заявки того же родителя в том же треке.

    Возвращает anchor-заявку и список других заявок (без текущей).
    Strict-совпадения (тот же ребёнок) — первыми.
    """
    needle = (br_id or "").strip().upper()
    if not needle:
        return None, []

    async with get_session()() as session:
        result = await session.execute(
            select(Application).where(Application.br_id == needle)
        )
        anchor: Application | None = result.scalar_one_or_none()
        if anchor is None:
            return None, []

        related_result = await session.execute(
            select(Application)
            .where(
                Application.parent_huid == anchor.parent_huid,
                Application.track == anchor.track,
                Application.moderation_status != ModerationStatus.OTKLONENO,
                Application.br_id != anchor.br_id,
            )
            .order_by(Application.created_at.desc())
        )
        related: list[ParentTrackEntry] = [
            _application_to_parent_track_entry(cand, anchor=anchor)
            for cand in related_result.scalars()
        ]

    related.sort(
        key=lambda e: (
            0 if e.is_strict_match else 1,
            -e.created_at.timestamp(),
        )
    )
    return anchor, related


async def find_related_for_application(
    app: Application,
) -> list[ParentTrackEntry]:
    """Связанные заявки для уже загруженной anchor-заявки."""
    _, related = await find_related_for_moderator(app.br_id)
    return related


async def find_active_siblings_for_application(
    app: Application,
) -> list[MultiSubmissionEntry]:
    """Другие «живые» заявки того же родителя, ребёнка и трека (без текущей)."""
    async with get_session()() as session:
        result = await session.execute(
            select(Application)
            .where(
                Application.parent_huid == app.parent_huid,
                Application.track == app.track,
                Application.moderation_status != ModerationStatus.OTKLONENO,
                Application.br_id != app.br_id,
            )
            .order_by(Application.created_at.desc())
        )
        siblings: list[MultiSubmissionEntry] = []
        for cand in result.scalars():
            if submission_keys_match(
                child_name_a=app.child_name,
                child_age_a=app.child_age,
                child_name_b=cand.child_name,
                child_age_b=cand.child_age,
            ):
                siblings.append(
                    MultiSubmissionEntry(
                        br_id=cand.br_id,
                        moderation_status=cand.moderation_status.value,
                        created_at=cand.created_at,
                        is_actual_version=cand.is_actual_version,
                    )
                )
    return siblings


def multi_submission_br_ids_from_applications(
    apps: Sequence[Application],
    *,
    only_active: bool = True,
) -> set[str]:
    """BR-ID заявок в группах «родитель + трек» (из уже загруженного списка)."""
    groups = _group_by_parent_track(apps, only_active=only_active)
    return application_ids_in_parent_track_groups(groups)


def application_ids_in_multi_submission_groups(
    groups: Sequence[MultiSubmissionGroup],
) -> set[str]:
    """Множество BR-ID всех заявок из strict-групп (ребёнок + трек)."""
    ids: set[str] = set()
    for group in groups:
        for entry in group.entries:
            ids.add(entry.br_id)
    return ids


def application_ids_in_parent_track_groups(
    groups: Sequence[ParentTrackGroup],
) -> set[str]:
    """Множество BR-ID всех заявок из групп «родитель + трек»."""
    ids: set[str] = set()
    for group in groups:
        for entry in group.entries:
            ids.add(entry.br_id)
    return ids


async def create_application(
    *,
    parent_huid: UUID,
    parent_full_name: str,
    parent_division: str,
    parent_ad_login: str | None,
    child_name: str,
    child_age: int,
    track_name: str,
    title: str,
    description: str,
    intake_mode_value: str,
    parent_contact: str | None = None,
    parent_contact_type: str | None = None,
    cloud_link: str | None = None,
) -> "Application":
    """Создать новую заявку.

    Алгоритм (всё в одной транзакции):
    1. ``pg_advisory_xact_lock`` — сериализуем выдачу br_id по году.
    2. ``SELECT MAX(br_id)`` + 1 → следующий номер по году.
    3. Поиск возможного дубля — отдельный SELECT по
       ``(parent_huid, track)`` с фильтрацией нормализованных имён
       в Python.
    4. INSERT в ``applications`` со статусом ``moderation_status =
       НА_МОДЕРАЦИИ`` и полями «возможный дубль» / «связанная заявка»
       при наличии дубля.
    5. ``commit()`` — атомарно отпускает lock и фиксирует запись.

    Возрастная категория вычисляется автоматически из ``child_age``
    через ``AgeCategory.from_age``. Невалидный возраст (вне 4..18) →
    ``ValueError``.
    """
    try:
        track_enum = Track[track_name]
    except KeyError as exc:
        raise ValueError(
            f"Неизвестный track_name: {track_name!r}. "
            f"Допустимы: {[t.name for t in Track]}"
        ) from exc

    age_category = AgeCategory.from_age(child_age)
    intake_mode_enum = IntakeMode(intake_mode_value)

    prefix = f"BR-{COMPETITION_YEAR}-"

    async with get_session()() as session:
        try:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _BR_ID_LOCK_KEY + COMPETITION_YEAR},
            )

            next_num = await _select_next_br_number(session, prefix)
            br_id = f"{prefix}{next_num:04d}"

            duplicate_query = (
                select(Application)
                .where(
                    Application.parent_huid == parent_huid,
                    Application.track == track_enum,
                    Application.moderation_status != ModerationStatus.OTKLONENO,
                )
                .order_by(Application.created_at.desc())
            )
            duplicate_result = await session.execute(duplicate_query)
            duplicate: Application | None = None
            for cand in duplicate_result.scalars():
                if submission_keys_match(
                    child_name_a=child_name,
                    child_age_a=child_age,
                    child_name_b=cand.child_name,
                    child_age_b=cand.child_age,
                ):
                    duplicate = cand
                    break

            app = Application(
                br_id=br_id,
                parent_huid=parent_huid,
                parent_full_name=parent_full_name,
                parent_division=parent_division,
                parent_ad_login=parent_ad_login,
                parent_contact=parent_contact,
                parent_contact_type=parent_contact_type,
                child_name=child_name,
                child_age=child_age,
                age_category=age_category,
                track=track_enum,
                title=title,
                description=description,
                intake_mode=intake_mode_enum,
                cloud_link=cloud_link,
                moderation_status=ModerationStatus.NA_MODERATSII,
                is_possible_duplicate=duplicate is not None,
                related_application_br_id=duplicate.br_id if duplicate else None,
                is_actual_version=True,
            )
            session.add(app)
            await session.commit()
            await session.refresh(app)
        except IntegrityError:
            await session.rollback()
            logger.exception(
                "IntegrityError при создании заявки",
                parent_huid=str(parent_huid),
            )
            raise

    logger.info(
        "Заявка создана",
        br_id=app.br_id,
        parent_huid=str(parent_huid),
        track=app.track.name,
        age_category=app.age_category.name,
        is_possible_duplicate=app.is_possible_duplicate,
        related_application_br_id=app.related_application_br_id,
    )
    return app


async def register_application_files(
    *,
    br_id: str,
    files: Sequence[ApplicationFileSpec],
) -> "Application":
    """Зарегистрировать файлы заявки в таблице ``application_files``.

    Вызывается из ``handlers.user_confirm._materialize_files`` после
    того, как все файлы успешно перенесены в папку заявки сервисом
    ``storage.rename_and_save_file``. Запись делается одним batch INSERT,
    после ``commit()`` объект ``Application`` перезагружается с
    ``selectinload(Application.files)`` и ``execution_options(populate_existing=True)``,
    а коллекция явно материализуется через ``list(reloaded.files)`` до
    ``session.expunge``. Это даёт detached-объект с уже заполненной
    коллекцией ``files`` — последующий ``write_meta_txt(app, files=...)``
    не упадёт ``DetachedInstanceError``.

    Args:
        br_id: ID заявки (``BR-2026-XXXX``).
        files: список спецификаций файлов; пустой допустим
            (например, в режиме ``LINKS``) — тогда возвращается
            заявка без вставок, но всё равно с подгруженной ``files``.

    Returns:
        ``Application`` с подгруженной коллекцией ``files``.

    Raises:
        ValueError: если заявка с указанным ``br_id`` не найдена.
    """
    async with get_session()() as session:
        stmt = select(Application).where(Application.br_id == br_id)
        app = (await session.execute(stmt)).scalar_one_or_none()
        if app is None:
            raise ValueError(f"Заявка не найдена: {br_id}")

        if files:
            session.add_all(
                ApplicationFile(
                    application_id=app.id,
                    kind=spec.kind,
                    angle_no=spec.angle_no,
                    original_filename=spec.original_filename,
                    stored_filename=spec.stored_filename,
                    relative_path=spec.relative_path,
                    size_bytes=spec.size_bytes,
                    mime_type=spec.mime_type,
                )
                for spec in files
            )
            await session.commit()

        reload_stmt = (
            select(Application)
            .where(Application.br_id == br_id)
            .options(selectinload(Application.files))
            .execution_options(populate_existing=True)
        )
        reloaded = (await session.execute(reload_stmt)).scalar_one()
        # Форсируем материализацию коллекции `files` до expunge.
        # populate_existing=True гарантирует, что selectinload отработает,
        # даже если Application уже в identity map; list(...) даёт прямую
        # уверенность, что .files лежит в __dict__ и не потребует lazy load
        # после отвязки от сессии (DetachedInstanceError в write_meta_txt).
        _ = list(reloaded.files)
        session.expunge(reloaded)

    logger.info(
        "Файлы заявки зарегистрированы в БД",
        br_id=br_id,
        files_count=len(files),
    )
    return reloaded


async def set_application_cloud_link(
    *,
    br_id: str,
    url: str,
) -> "Application":
    """Установить/обновить ``cloud_link`` заявки и вернуть свежий объект.

    Используется в режиме ``LINKS`` (§33.6 ТЗ): после ``cmd_submit``
    заявка лежит в БД с ``cloud_link=NULL``, и как только участник
    присылает URL — вызывается эта функция, после чего бот пишет
    ``meta.txt`` и шлёт уведомления.

    Reload идёт с ``selectinload(Application.files)`` — даже если
    коллекция пустая (LINKS-режим), это нужно, чтобы последующий
    ``write_meta_txt`` / ``notify_*`` могли спокойно обратиться к
    ``app.files`` на уже отвязанном от сессии объекте.

    Повторный вызов разрешён (например, участник прислал «более
    правильную» ссылку до того, как модератор успел открыть карточку) —
    логируем замену и пишем новый URL поверх.

    Args:
        br_id: ID заявки (``BR-2026-XXXX``).
        url: публичный URL на облачную папку участника.

    Returns:
        ``Application`` с подгруженной коллекцией ``files``.

    Raises:
        ValueError: если заявка с указанным ``br_id`` не найдена.
    """
    needle = (br_id or "").strip().upper()
    cleaned_url = (url or "").strip()
    if not needle:
        raise ValueError("br_id обязателен")
    if not cleaned_url:
        raise ValueError("url обязателен")

    async with get_session()() as session:
        result = await session.execute(
            select(Application).where(Application.br_id == needle)
        )
        app: "Application | None" = result.scalar_one_or_none()
        if app is None:
            raise ValueError(f"Заявка не найдена: {needle}")

        previous = app.cloud_link
        app.cloud_link = cleaned_url
        await session.commit()

        reload_stmt = (
            select(Application)
            .where(Application.br_id == needle)
            .options(selectinload(Application.files))
            .execution_options(populate_existing=True)
        )
        reloaded = (await session.execute(reload_stmt)).scalar_one()
        _ = list(reloaded.files)
        session.expunge(reloaded)

    if previous and previous != cleaned_url:
        logger.info(
            "cloud_link заявки перезаписан",
            br_id=needle,
            old=previous,
            new=cleaned_url,
        )
    else:
        logger.info("cloud_link заявки установлен", br_id=needle)
    return reloaded


async def mark_as_actual_version(
    *,
    br_id: str,
    actual: bool,
    by_moderator_huid: UUID,
) -> None:
    """Отметить заявку как актуальную версию (поле реестра
    «актуальная версия заявки»).

    Поле проставляется только вручную модератором. При установке
    ``actual=True`` все остальные связанные заявки цепочки (то есть
    те, чьи ключи дубля совпадают с этой) автоматически становятся
    ``is_actual_version=False``.

    Цепочка восстанавливается через ключи дубля: ``parent_huid``
    + нормализованное имя ребёнка + ``track``. Это надёжнее, чем
    идти по ``related_application_br_id``, потому что новая заявка
    ссылается на предыдущую, но обратной ссылки нет.
    """
    async with get_session()() as session:
        result = await session.execute(
            select(Application).where(Application.br_id == br_id)
        )
        target: Application | None = result.scalar_one_or_none()
        if target is None:
            raise ValueError(f"Заявка не найдена: {br_id}")

        if not actual:
            target.is_actual_version = False
            await session.commit()
            logger.info(
                "Заявка снята с актуальной версии",
                br_id=br_id,
                by=str(by_moderator_huid),
            )
            return

        chain_query = select(Application).where(
            Application.parent_huid == target.parent_huid,
            Application.track == target.track,
            Application.id != target.id,
        )
        chain_result = await session.execute(chain_query)
        sibling_ids: list[UUID] = []
        for sibling in chain_result.scalars():
            if submission_keys_match(
                child_name_a=target.child_name,
                child_age_a=target.child_age,
                child_name_b=sibling.child_name,
                child_age_b=sibling.child_age,
            ):
                sibling_ids.append(sibling.id)

        if sibling_ids:
            await session.execute(
                update(Application)
                .where(Application.id.in_(sibling_ids))
                .values(is_actual_version=False)
            )

        target.is_actual_version = True
        await session.commit()
    logger.info(
        "Заявка отмечена актуальной версией",
        br_id=br_id,
        siblings_unset=len(sibling_ids),
        by=str(by_moderator_huid),
    )


@dataclass(frozen=True)
class ParentApplicationsPage:
    """Страница списка заявок родителя для «Мои заявки».

    ``items`` — заявки текущей страницы без eager-load файлов
    (участнику файлы не показываем). ``total`` — общее число заявок
    родителя по ``parent_huid``.
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


async def list_by_parent_huid(
    parent_huid: UUID,
    *,
    page: int = 1,
    page_size: int = 6,
) -> ParentApplicationsPage:
    """Список заявок родителя для экрана «Мои заявки».

    Один COUNT + один SELECT с ``ORDER BY created_at DESC``. Файлы
    не подгружаются — участнику они не нужны.
    """
    page = max(page, 1)
    page_size = max(page_size, 1)
    offset = (page - 1) * page_size

    async with get_session()() as session:
        total = (
            await session.execute(
                select(func.count())
                .select_from(Application)
                .where(Application.parent_huid == parent_huid)
            )
        ).scalar_one()

        result = await session.execute(
            select(Application)
            .where(Application.parent_huid == parent_huid)
            .order_by(Application.created_at.desc(), Application.id.desc())
            .offset(offset)
            .limit(page_size)
        )
        items = list(result.scalars().all())

    return ParentApplicationsPage(
        items=items,
        total=int(total),
        page=page,
        page_size=page_size,
    )


async def update_application_for_fix(
    *,
    br_id: str,
    parent_huid: UUID,
    title: str,
    description: str,
    intake_mode_value: str,
    cloud_link: str | None = None,
) -> Application:
    """Обновить заявку «нужно исправить» без нового BR-ID.

    Сбрасывает модерацию и поля жюри, снимает флаг дубля. Файлы
    работы нужно заменить отдельно (``clear_application_work_files`` +
    ``register_application_files``).
    """
    needle = (br_id or "").strip().upper()
    intake_mode_enum = IntakeMode(intake_mode_value)

    async with get_session()() as session:
        app = (
            await session.execute(
                select(Application).where(Application.br_id == needle)
            )
        ).scalar_one_or_none()
        if app is None:
            raise ValueError(f"Заявка не найдена: {needle}")
        if app.parent_huid != parent_huid:
            raise ValueError("Заявка принадлежит другому участнику")
        if app.moderation_status != ModerationStatus.NUZHNO_ISPRAVIT:
            raise ValueError(
                "Исправление доступно только для статуса «нужно исправить»"
            )

        app.title = title.strip()
        app.description = description.strip()
        app.intake_mode = intake_mode_enum
        app.cloud_link = (cloud_link or "").strip() or None
        app.moderation_status = ModerationStatus.NA_MODERATSII
        app.moderator_comment = None
        app.is_possible_duplicate = False
        app.related_application_br_id = None
        app.is_actual_version = True
        app.jury_status = JuryStatus.NE_PEREDANO_ZHYURI
        app.jury_final_round = None
        app.jury_decided_by_lot = False
        app.pool_position = None
        app.voting_status = VotingStatus.NE_UCHASTVUET

        await session.commit()
        reload_stmt = (
            select(Application)
            .where(Application.br_id == needle)
            .options(selectinload(Application.files))
            .execution_options(populate_existing=True)
        )
        reloaded = (await session.execute(reload_stmt)).scalar_one()
        _ = list(reloaded.files)
        session.expunge(reloaded)

    logger.info(
        "Заявка обновлена (исправление)",
        br_id=needle,
        parent_huid=str(parent_huid),
    )
    return reloaded


async def clear_application_work_files(br_id: str) -> Application:
    """Удалить файлы работы заявки в БД и на диске (перед повторной загрузкой)."""
    needle = (br_id or "").strip().upper()
    async with get_session()() as session:
        app = (
            await session.execute(
                select(Application)
                .where(Application.br_id == needle)
                .options(selectinload(Application.files))
            )
        ).scalar_one_or_none()
        if app is None:
            raise ValueError(f"Заявка не найдена: {needle}")

        await session.execute(
            delete(ApplicationFile).where(
                ApplicationFile.application_id == app.id
            )
        )
        await session.commit()
        _ = list(app.files)

    from services import storage

    await storage.delete_application_files(app)

    async with get_session()() as session:
        reload_stmt = (
            select(Application)
            .where(Application.br_id == needle)
            .options(selectinload(Application.files))
            .execution_options(populate_existing=True)
        )
        reloaded = (await session.execute(reload_stmt)).scalar_one()
        _ = list(reloaded.files)
        session.expunge(reloaded)
    return reloaded


async def get_for_participant(
    br_id: str,
    parent_huid: UUID,
) -> Application | None:
    """Заявка по ``br_id``, доступная только её автору.

    Возвращает ``None``, если заявка не найдена или принадлежит другому
    родителю — без утечки факта существования чужой заявки.
    """
    needle = (br_id or "").strip().upper()
    if not needle:
        return None

    async with get_session()() as session:
        app = (
            await session.execute(
                select(Application).where(Application.br_id == needle)
            )
        ).scalar_one_or_none()
        if app is None or app.parent_huid != parent_huid:
            return None
        return app


__all__ = [
    "ApplicationFileSpec",
    "MultiSubmissionEntry",
    "MultiSubmissionGroup",
    "ParentApplicationsPage",
    "ParentTrackEntry",
    "ParentTrackGroup",
    "application_ids_in_multi_submission_groups",
    "application_ids_in_parent_track_groups",
    "assign_br_id",
    "child_submission_key",
    "clear_application_work_files",
    "count_multi_submission_groups",
    "create_application",
    "find_active_siblings_for_application",
    "find_multi_submission_groups",
    "find_parent_track_groups",
    "find_possible_duplicate",
    "find_related_for_application",
    "find_related_for_moderator",
    "get_for_participant",
    "list_by_parent_huid",
    "mark_as_actual_version",
    "multi_submission_br_ids_from_applications",
    "normalize_child_name",
    "register_application_files",
    "set_application_cloud_link",
    "strict_duplicate_br_ids_in_group",
    "submission_keys_match",
    "update_application_for_fix",
]
