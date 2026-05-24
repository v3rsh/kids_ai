"""
Контракты между модулями (kids_ai / Безопасные рисунки).

Этот модуль — **единственная точка**, из которой соседние модули могут
импортировать друг у друга DTO и сигнатуры. Прямой импорт реализаций
из соседних ``app/services/*`` или ``app/handlers/*`` запрещён —
это создаёт циклические зависимости и мешает изоляции в тестах.

Содержимое:
- DTO предметной области (frozen dataclasses, без зависимостей на ORM
  и pybotx — можно безопасно собирать из любых слоёв);
- Protocol-классы под публичные функции из ``services/*``,
  чтобы вызывающая сторона могла подменять реализации в тестах
  и проверять совместимость через ``mypy`` / IDE.

Stylistic note: используем ``@dataclass(frozen=True)`` вместо pydantic
v1 (pybotx завязан на pydantic<1.11), чтобы DTO были предельно лёгкими
и хорошо хешировались (``PoolKey`` нужен ключом в словарях). Если
понадобится валидация — DTO можно обернуть в pydantic-модель локально,
не меняя контракт.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Awaitable,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    runtime_checkable,
)
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover — только для type-checker'а
    from sqlalchemy.ext.asyncio import AsyncSession

    from database.models import (
        AgeCategory,
        Application,
        ApplicationFile,
        FileKind,
        IntakeMode,
        JuryMember,
        JuryRound,
        JuryVote,
        JuryVoteValue,
        ModerationStatus,
        Track,
    )


# =====================================================================
# DTO предметной области
# =====================================================================


@dataclass(frozen=True)
class PoolKey:
    """Идентификатор пула жюри.

    Пул = пара ``(track, age_category)``. Используется как ключ в
    словарях (например, при агрегации уведомлений по моменту времени) —
    поэтому frozen + автохэш.
    """

    track: "Track"
    age_category: "AgeCategory"

    def as_label(self) -> str:
        """Человекочитаемое имя пула: ``Традиционное рисование / 7–12``."""
        return f"{self.track.value} / {self.age_category.value}"


@dataclass(frozen=True)
class ApplicationFileDTO:
    """DTO одного файла заявки."""

    id: UUID
    kind: "FileKind"
    angle_no: int | None
    original_filename: str
    stored_filename: str
    relative_path: str
    size_bytes: int
    mime_type: str
    uploaded_at: datetime


@dataclass(frozen=True)
class ApplicationDTO:
    """DTO заявки для передачи между модулями.

    Не дублирует все агрегаты жюри — только то, что нужно листинговым
    хендлерам (``/queue``, ``/find``, ``/files``). Полную модель можно
    запросить через ``services.applications`` по ``br_id``.
    """

    id: UUID
    br_id: str
    parent_huid: UUID
    parent_full_name: str
    parent_division: str
    parent_ad_login: str | None
    child_name: str
    child_age: int
    age_category: "AgeCategory"
    track: "Track"
    title: str
    description: str
    intake_mode: "IntakeMode"
    cloud_link: str | None
    moderation_status: "ModerationStatus"
    moderator_comment: str | None
    is_possible_duplicate: bool
    related_application_br_id: str | None
    is_actual_version: bool
    created_at: datetime
    updated_at: datetime
    files: tuple[ApplicationFileDTO, ...] = field(default_factory=tuple)

    @property
    def pool(self) -> PoolKey:
        """Пул заявки (для жюри)."""
        return PoolKey(track=self.track, age_category=self.age_category)


@dataclass(frozen=True)
class JuryTaskDTO:
    """DTO одной задачи жюри для команды ``/jury_tasks``.

    ``local_no`` — локальный номер работы в карусели пула (1..N),
    единый для всех судей; ID заявки судье **не показывается**
    ради анонимности.

    ``preview_path`` — путь к превью 1280 px (в режиме ``files``);
    ``cloud_link`` — публичная ссылка на папку (в режиме ``links``).
    Заполнено ровно одно из двух полей.

    ``draft_vote`` — текущее значение черновика (``YES``/``NO``/``None``),
    нужно для отрисовки эмодзи на кнопке.
    """

    round_id: UUID
    application_id: UUID
    pool: PoolKey
    round_no: int
    local_no: int
    title: str
    description: str
    preview_path: Path | None
    cloud_link: str | None
    draft_vote: "JuryVoteValue | None"


@dataclass(frozen=True)
class RoundResult:
    """Итог раунда жюри.

    ``top_ids`` — попавшие в топ-N на этом раунде (по строгому
    неравенству или по жребию).
    ``tie_ids`` — заявки в зоне ничьи на границе топ-N; если непуст
    и ``needs_next_round=True`` — для них открывается следующий раунд.
    ``decided_by_lot`` — список ID работ, попавших в топ-N жребием
    (для проставления флага в БД и реестре).
    """

    pool: PoolKey
    round_no: int
    top_ids: tuple[UUID, ...]
    tie_ids: tuple[UUID, ...]
    decided_by_lot: tuple[UUID, ...]
    needs_next_round: bool
    closed_at: datetime


# =====================================================================
# Protocol-классы для публичного API сервисов
# =====================================================================
#
# Используются для type-чекинга: вызывающая сторона может объявить
# зависимость на ``ApplicationsService`` (Protocol) и подменять её
# в тестах фейком. Не имеют связи с конкретными модулями: импортируется
# по ``isinstance(svc, ApplicationsService)`` за счёт runtime_checkable.


@runtime_checkable
class ApplicationsService(Protocol):
    """Контракт ``services.applications``."""

    async def create_application(self, /, **fields) -> "Application": ...
    async def assign_br_id(self) -> str: ...
    async def find_possible_duplicate(
        self,
        *,
        parent_huid: UUID,
        child_name: str,
        track_name: str,
    ) -> "Application | None": ...
    async def register_application_files(
        self, *, br_id: str, files: Iterable
    ) -> "Application": ...
    async def mark_as_actual_version(
        self, *, br_id: str, actual: bool, by_moderator_huid: UUID
    ) -> None: ...


@runtime_checkable
class StorageService(Protocol):
    """Контракт ``services.storage``."""

    async def create_application_folder(
        self, app: "Application"
    ) -> Path: ...
    async def rename_and_save_file(
        self,
        app: "Application",
        kind: "FileKind",
        angle_no: int | None,
        src_path: Path,
        original_filename: str,
    ) -> Path: ...
    async def write_meta_txt(self, app: "Application") -> Path: ...
    async def write_description_txt(self, app: "Application") -> Path: ...
    async def write_reason_txt(
        self, app: "Application", reason: str
    ) -> Path: ...
    async def move_to_rejected(self, app: "Application") -> Path: ...
    def get_disk_usage_bytes(self) -> tuple[int, int]: ...
    def should_block_intake(self) -> bool: ...


@runtime_checkable
class RegistryService(Protocol):
    """Контракт ``services.registry`` (bytes, не файл).

    ``registry_export_filename`` — единый источник правды по формату
    имени файла (см. ``docs/registry-spec.md``); используется
    хендлерами `/export_registry` и `/export_shortlist`.
    """

    async def build_registry_xlsx(self) -> bytes: ...
    async def build_shortlist_xlsx(self) -> bytes: ...
    def registry_export_filename(
        self,
        kind: str,
        now_msk: object | None = None,
    ) -> str: ...


@runtime_checkable
class NotificationsService(Protocol):
    """Контракт ``services.notifications`` (участники + чат модерации)."""

    async def notify_participant_accepted(
        self, bot, app: "Application"
    ) -> None: ...
    async def notify_participant_rejected(
        self, bot, app: "Application", reason: str
    ) -> None: ...
    async def notify_participant_fix_needed(
        self, bot, app: "Application", extra: str | None = None
    ) -> None: ...
    async def notify_participant_shortlist(
        self, bot, app: "Application"
    ) -> None: ...
    async def notify_participant_jury_result(
        self, bot, app: "Application", in_top_10: bool
    ) -> None: ...
    async def notify_moderation_chat_new_application(
        self, bot, app: "Application"
    ) -> None: ...
    async def notify_moderation_chat_jury_event(
        self,
        bot,
        *,
        event_kind: str,
        pools: list[tuple[str, str]],
        round_no: int | None,
        deadline_text: str | None = None,
        extra: str | None = None,
    ) -> None: ...
    async def notify_moderation_chat_disk_alert(
        self,
        bot,
        *,
        threshold_pct: int,
        free_mb: int,
        hours_left: float,
    ) -> None: ...


@runtime_checkable
class JuryService(Protocol):
    """Контракт ``services.jury``.

    Все async-методы поддерживают опциональный ``session: AsyncSession``
    (если не передан — сервис открывает свою short-living-сессию).
    """

    async def open_round(
        self,
        *,
        track: "Track",
        age_category: "AgeCategory",
        round_no: int,
        candidates: list["Application"],
        session: "Optional[AsyncSession]" = None,
    ) -> "JuryRound": ...
    async def submit_votes(
        self,
        *,
        round_id: UUID,
        jury_huid: UUID,
        votes: Mapping[UUID, "JuryVoteValue"],
        session: "Optional[AsyncSession]" = None,
    ) -> None: ...
    async def upsert_draft_vote(
        self,
        *,
        round_id: UUID,
        jury_huid: UUID,
        application_id: UUID,
        value: "JuryVoteValue",
        session: "Optional[AsyncSession]" = None,
    ) -> "JuryVote": ...
    async def close_round(
        self,
        round_id: UUID,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> RoundResult: ...
    async def compute_top_n(
        self,
        *,
        track: "Track",
        age_category: "AgeCategory",
        round_no: int,
        n: int = 10,
        session: "Optional[AsyncSession]" = None,
    ) -> list["Application"]: ...
    async def apply_lot_if_needed(
        self,
        *,
        track: "Track",
        age_category: "AgeCategory",
        round_no: int,
        n: int = 10,
        session: "Optional[AsyncSession]" = None,
    ) -> list["Application"]: ...
    async def build_shortlist(
        self,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> list["Application"]: ...
    async def get_open_tasks_for_jury(
        self,
        jury_huid: UUID,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> list[JuryTaskDTO]: ...
    async def get_round_candidates_with_drafts(
        self,
        *,
        round_id: UUID,
        jury_huid: UUID,
        session: "Optional[AsyncSession]" = None,
    ) -> list[dict]: ...
    async def get_jury_progress(
        self,
        jury_huid: UUID,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> dict: ...


@runtime_checkable
class PoolsService(Protocol):
    """Контракт ``services.pools``.

    ``sync_pool_assignments_from_config`` вызывается один раз на старте
    приложения (см. ``app/main.py``); распределение членов жюри по пулам
    хранится в БД (``pool_jury_assignments``) и переживает рестарт.
    """

    def all_pools(self) -> list[PoolKey]: ...
    async def get_pool_applications(
        self,
        pool: PoolKey,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> list["Application"]: ...
    async def get_jury_for_pool(
        self,
        pool: PoolKey,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> list["JuryMember"]: ...
    async def sync_pool_assignments_from_config(
        self,
        *,
        session: "Optional[AsyncSession]" = None,
    ) -> int: ...


@runtime_checkable
class IntakeModeService(Protocol):
    """Контракт ``services.intake_mode``."""

    async def get_intake_mode(self) -> "IntakeMode": ...
    async def set_intake_mode(
        self,
        mode: "IntakeMode",
        *,
        by_huid: UUID,
        reason: str | None = None,
    ) -> None: ...
    async def maybe_auto_switch_to_links(self) -> bool: ...


@runtime_checkable
class AccessService(Protocol):
    """Контракт ``services.access`` (проверка ролей и доступа к командам)."""

    def is_moderator(self, huid: UUID | str | None) -> bool: ...
    def is_jury(self, huid: UUID | str | None) -> bool: ...
    def is_admin(self, huid: UUID | str | None) -> bool: ...


__all__ = [
    # DTO
    "PoolKey",
    "ApplicationDTO",
    "ApplicationFileDTO",
    "JuryTaskDTO",
    "RoundResult",
    # Protocols
    "ApplicationsService",
    "StorageService",
    "RegistryService",
    "NotificationsService",
    "JuryService",
    "PoolsService",
    "IntakeModeService",
    "AccessService",
]
