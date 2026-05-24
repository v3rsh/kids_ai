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

from typing import TYPE_CHECKING
from uuid import UUID

from loguru import logger
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from config import COMPETITION_YEAR
from database.db import get_session
from database.models import (
    AgeCategory,
    Application,
    IntakeMode,
    ModerationStatus,
    Track,
)

if TYPE_CHECKING:  # pragma: no cover
    pass


# Advisory-lock key для сериализации генерации br_id внутри года.
# `pg_advisory_xact_lock(key)` снимается автоматически при коммите/откате —
# это безопаснее, чем sequence (нет «дыр» от откатов) и проще, чем INSERT
# с обработкой UniqueViolation + ретраи.
_BR_ID_LOCK_KEY = 0xBA8E_8001  # любое стабильное int — не пересекается с другими locks


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
    track_name: str,
) -> "Application | None":
    """Алгоритм автопометки «возможный дубль».

    Возвращает последнюю ранее принятую заявку с тем же набором ключей:
    ``parent_huid`` + нормализованное имя ребёнка + ``track``. Заявки в
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

    normalized_target = normalize_child_name(child_name)
    if not normalized_target:
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
            if normalize_child_name(candidate.child_name) == normalized_target:
                return candidate
    return None


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

    normalized_target = normalize_child_name(child_name)
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
                if normalize_child_name(cand.child_name) == normalized_target:
                    duplicate = cand
                    break

            app = Application(
                br_id=br_id,
                parent_huid=parent_huid,
                parent_full_name=parent_full_name,
                parent_division=parent_division,
                parent_ad_login=parent_ad_login,
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

        normalized = normalize_child_name(target.child_name)
        chain_query = select(Application).where(
            Application.parent_huid == target.parent_huid,
            Application.track == target.track,
            Application.id != target.id,
        )
        chain_result = await session.execute(chain_query)
        sibling_ids: list[UUID] = []
        for sibling in chain_result.scalars():
            if normalize_child_name(sibling.child_name) == normalized:
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


__all__ = [
    "create_application",
    "assign_br_id",
    "find_possible_duplicate",
    "mark_as_actual_version",
    "normalize_child_name",
]
