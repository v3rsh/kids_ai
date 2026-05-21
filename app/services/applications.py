"""
Стаб сервиса заявок (Wave 1 → реализация в Wave 2 / ветка user).

Содержит сигнатуры публичных функций для работы с моделью
``Application`` и её жизненным циклом. Все реализации брошены как
``NotImplementedError`` — заполняются в Wave 2 без правок сигнатур,
чтобы не ломать импорты соседних веток.

Ссылки на ТЗ:
- §11 — состав полей анкеты;
- §15 — повторная отправка и алгоритм дубля (§15.3);
- §20 — формат BR-ID (``BR-2026-NNNN``).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Mapping
from uuid import UUID

if TYPE_CHECKING:
    from database.models import Application


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка user"


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
    """Создать новую заявку (§11, §14, §15).

    Поле ``br_id`` присваивается ботом сразу же — до запроса файлов,
    чтобы в режиме `links` родитель мог использовать его в имени папки
    (§33.6.2). Возрастная категория вычисляется автоматически из
    ``child_age`` через ``AgeCategory.from_age`` (§8, §11.3).

    Под капотом ожидается:
    1) вызов ``assign_br_id`` для генерации сквозного номера;
    2) проверка возможного дубля через ``find_possible_duplicate``
       и заполнение полей №20 / №21 реестра (§15.3);
    3) сохранение в БД с ``moderation_status = НА_МОДЕРАЦИИ``.
    """
    raise NotImplementedError(_STUB_MSG)


async def assign_br_id() -> str:
    """Сгенерировать следующий по порядку BR-ID (§20).

    Формат — ``BR-{COMPETITION_YEAR}-{NNNN}``, нумерация сквозная по
    всем заявкам года, без пропусков. Конкретный механизм
    (Postgres sequence vs SELECT MAX) — на усмотрение Wave 2.
    """
    raise NotImplementedError(_STUB_MSG)


async def find_possible_duplicate(
    *,
    parent_huid: UUID,
    child_name: str,
    track_name: str,
) -> "Application | None":
    """Алгоритм автопометки «возможный дубль» (§15.3).

    Возвращает последнюю ранее принятую заявку с тем же набором ключей:
    ``parent_huid`` + нормализованное имя ребёнка (lowercase, trim,
    замена ``ё``→``е``) + ``track``. Заявки в статусе ``отклонено``
    в проверке не участвуют. ``None`` — дубля нет.
    """
    raise NotImplementedError(_STUB_MSG)


async def mark_as_actual_version(
    *,
    br_id: str,
    actual: bool,
    by_moderator_huid: UUID,
) -> None:
    """Отметить заявку как актуальную версию (§15.2, поле №22 реестра).

    Поле проставляется только вручную модератором. При установке
    ``actual=True`` все остальные связанные заявки (тот же
    ``related_application_br_id`` цепочки) должны автоматически
    становиться ``is_actual_version=False`` — это требование §15.
    """
    raise NotImplementedError(_STUB_MSG)


def normalize_child_name(child_name: str) -> str:
    """Нормализация имени ребёнка для алгоритма дубля (§15.3).

    Утилита-помощник: ``lowercase`` → ``strip`` → замена ``ё``→``е``.
    Чистая функция без I/O, оставлена не-async для удобства вызова из
    тестов. Реализация — в Wave 2.
    """
    raise NotImplementedError(_STUB_MSG)


__all__ = [
    "create_application",
    "assign_br_id",
    "find_possible_duplicate",
    "mark_as_actual_version",
    "normalize_child_name",
]
