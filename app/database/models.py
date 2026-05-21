"""
SQLAlchemy-модели kids_ai.

Содержит:
- Базовую модель `User` (нужна для health-чека и проактивных сообщений).
- Перечисления (enum'ы) предметной области конкурса «Безопасные рисунки»
  по разделам §9, §10, §12, §22, §26, §33.6, §35 ТЗ.
- Доменные модели заявок, файлов, модераторов, жюри и runtime-настроек.

Соглашения (см. .cursor/rules/core-standards.mdc, performance.mdc):
- Использовать `DeclarativeBase`
- Для soft-delete добавлять `is_deleted` + `deleted_at`
- Индексировать поля, по которым строятся фильтры/joins
- Для timestamps использовать `datetime.utcnow` (UTC)
- Enum-имена UPPER_SNAKE_CASE, значения — строки по ТЗ
  (в БД сохраняется `.name`, см. `database/migrations.py → sync_enum_values`)
"""
import enum
import uuid
from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""

    pass


# =====================================================================
# Перечисления предметной области конкурса «Безопасные рисунки»
# =====================================================================
#
# В БД сохраняется ``.name`` (UPPER_SNAKE_CASE) — это удобно для миграций
# (`sync_enum_values`), а ``.value`` — текст для UI/реестра по ТЗ.


class Track(enum.Enum):
    """Конкурсный трек (§10)."""

    TRADITIONAL = "Традиционное рисование"
    AI = "ИИ-рисунок"
    HANDMADE_TO_AI = "От руки к ИИ"


class AgeCategory(enum.Enum):
    """Возрастная категория участника (§9).

    Категории без пересечений; вычисляются автоматически из возраста
    ребёнка (§11.2, §11.3) — ручного выбора нет (Wave 0, §8).
    """

    AGE_4_6 = "4–6"
    AGE_7_10 = "7–10"
    AGE_11_13 = "11–13"
    AGE_14_18 = "14–18"

    @classmethod
    def from_age(cls, age: int) -> "AgeCategory":
        """Возвращает категорию по возрасту в полных годах (§9, §11.2).

        Возраст вне допустимого диапазона (4–18) → ``ValueError``.
        Граничные значения 6/10/13 относятся к младшей категории.
        """
        if not isinstance(age, int) or age < 4 or age > 18:
            raise ValueError(
                f"Недопустимый возраст: {age}. Допустим диапазон 4–18 лет (§9)."
            )
        if age <= 6:
            return cls.AGE_4_6
        if age <= 10:
            return cls.AGE_7_10
        if age <= 13:
            return cls.AGE_11_13
        return cls.AGE_14_18


class IntakeMode(enum.Enum):
    """Режим приёма заявок (§33.6).

    ``FILES`` — основной (файлы загружаются на сервер бота).
    ``LINKS`` — резервный (родитель присылает ссылку на облако).
    Переключение — командой ``/intake_mode`` или автоматически при 95%.
    """

    FILES = "files"
    LINKS = "links"


class ModerationStatus(enum.Enum):
    """Статус модерации заявки (§26).

    После Wave 0 — финальный набор из 5 значений.
    """

    PRINYATO = "принято"
    NA_MODERATSII = "на модерации"
    DOPUSHCHENO = "допущено"
    NUZHNO_ISPRAVIT = "нужно исправить"
    OTKLONENO = "отклонено"


class JuryStatus(enum.Enum):
    """Статус жюри (§26 после Wave 0).

    Заполняется ботом автоматически по итогам процесса по пулу (§35.2).
    Ручное редактирование модератором запрещено: бот перезапишет
    при следующем обновлении реестра.
    """

    NE_PEREDANO_ZHYURI = "не передано жюри"
    NA_GOLOSOVANII = "на голосовании"
    V_TOP_10 = "в топ-10"
    NE_VOSHLO_V_TOP_10 = "не вошло в топ-10"


class VotingStatus(enum.Enum):
    """Статус народного голосования (§26).

    Бот сам процесс не реализует — поле меняет модератор/организатор.
    """

    NE_UCHASTVUET = "не участвует"
    PODGOTOVLENO_K_PUBLIKATSII = "подготовлено к публикации"
    OPUBLIKOVANO = "опубликовано"
    PRIZ_ZRITELSKIH_SIMPATIY = "приз зрительских симпатий"


class FileKind(enum.Enum):
    """Тип файла заявки (§22).

    ``ORIGINAL`` — обычная 2D-работа.
    ``ANGLE`` — ракурс 3D-работы/поделки/фотоинсталляции (с ``angle_no``).
    ``AI_IMAGE`` — итоговое изображение для трека «ИИ-рисунок».
    ``DIPTYCH`` — общий коллаж «до/после» для трека «От руки к ИИ».
    """

    ORIGINAL = "original"
    ANGLE = "angle"
    AI_IMAGE = "ai_image"
    DIPTYCH = "diptych"


class JuryRoundStatus(enum.Enum):
    """Статус раунда жюри по конкретному пулу (§35.2, §35.4).

    ``OPEN`` — раунд открыт, судьи голосуют.
    ``CLOSED`` — раунд закрыт (по полноте/дедлайну/команде модератора).
    ``DRAWN_BY_LOT`` — закрыт автоматическим жребием при сохранении ничьи.
    """

    OPEN = "open"
    CLOSED = "closed"
    DRAWN_BY_LOT = "drawn_by_lot"


class JuryVoteValue(enum.Enum):
    """Бинарная оценка жюри по работе (§35.1)."""

    YES = "yes"
    NO = "no"


class JuryVoteState(enum.Enum):
    """Статус голоса в БД (§35.3, §35.4).

    Черновики (``DRAFT``) пишутся в БД, чтобы пережить рестарт бота,
    но не учитываются при подсчёте до нажатия «Отправить оценки».
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"


class User(Base):
    """Пользователь бота.

    huid — идентификатор из eXpress (приходит в каждом IncomingMessage).
    chat_id — нужен для проактивных сообщений (push из планировщика).
    """

    __tablename__ = "users"

    huid: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    chat_id: Mapped[PyUUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    last_activity: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def __repr__(self) -> str:
        return f"<User huid={self.huid} full_name={self.full_name!r}>"


# =====================================================================
# Доменные модели конкурса «Безопасные рисунки»
# =====================================================================


class Application(Base):
    """Заявка на конкурс (§11, §15, §20, §25).

    Источник правды по всем полям реестра (§25.4). Excel-реестр собирается
    из этой таблицы по запросу `/export`, на диске не хранится.

    Полное ФИО родителя — в ``parent_full_name``; в имени папки используется
    только фамилия+имя (§21.2). ``ad_login`` ходит в ``meta.txt`` и Excel
    (§11.1), ``huid`` — всегда доступен модератору в карточке.

    Возрастная категория ``age_category`` вычисляется ботом
    автоматически из ``child_age`` через ``AgeCategory.from_age`` (§8, §11.3).
    """

    __tablename__ = "applications"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    br_id: Mapped[str] = mapped_column(
        String(20), unique=True, index=True, nullable=False
    )
    parent_huid: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), index=True, nullable=False
    )
    parent_full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_division: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_ad_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    child_name: Mapped[str] = mapped_column(String(255), nullable=False)
    child_age: Mapped[int] = mapped_column(Integer, nullable=False)
    age_category: Mapped[AgeCategory] = mapped_column(
        SAEnum(AgeCategory, name="age_category"), nullable=False
    )
    track: Mapped[Track] = mapped_column(
        SAEnum(Track, name="track"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    intake_mode: Mapped[IntakeMode] = mapped_column(
        SAEnum(IntakeMode, name="intake_mode"), nullable=False
    )
    cloud_link: Mapped[str | None] = mapped_column(Text, nullable=True)

    moderation_status: Mapped[ModerationStatus] = mapped_column(
        SAEnum(ModerationStatus, name="moderation_status"),
        nullable=False,
        default=ModerationStatus.NA_MODERATSII,
    )
    moderator_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    jury_status: Mapped[JuryStatus] = mapped_column(
        SAEnum(JuryStatus, name="jury_status"),
        nullable=False,
        default=JuryStatus.NE_PEREDANO_ZHYURI,
    )
    voting_status: Mapped[VotingStatus] = mapped_column(
        SAEnum(VotingStatus, name="voting_status"),
        nullable=False,
        default=VotingStatus.NE_UCHASTVUET,
    )
    merch_potential: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_possible_duplicate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    related_application_br_id: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    is_actual_version: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )

    # ===== Агрегированные поля жюри (поля №№ 23–29 реестра, §25.3.1) =====
    jury_round1_yes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jury_round2_yes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jury_round3_yes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jury_final_round: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jury_decided_by_lot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    pool_position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    files: Mapped[list["ApplicationFile"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Application br_id={self.br_id} track={self.track.name}>"


class ApplicationFile(Base):
    """Файл заявки (§12, §22, §23).

    Имена ``stored_filename`` формируются сервисом ``storage`` по шаблону
    §22 («BR-2026-XXXX_original.jpg» / «..._angle-N.<ext>» / ...).
    Для ``ANGLE`` обязательно поле ``angle_no`` (1..4, §12.1).
    ``relative_path`` — путь относительно ``ATTACHMENTS_DIR`` (см. config).
    """

    __tablename__ = "application_files"

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    application_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[FileKind] = mapped_column(
        SAEnum(FileKind, name="file_kind"), nullable=False
    )
    angle_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    application: Mapped[Application] = relationship(back_populates="files")

    def __repr__(self) -> str:
        return f"<ApplicationFile {self.stored_filename!r} kind={self.kind.name}>"


class Moderator(Base):
    """Справочник модераторов (§5.2, §27.2).

    Заполняется при старте бота из конфига ``MODERATOR_HUIDS``
    (см. ``services/access.py`` / lifespan).
    """

    __tablename__ = "moderators"

    huid: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    def __repr__(self) -> str:
        return f"<Moderator huid={self.huid} active={self.is_active}>"


class JuryMember(Base):
    """Справочник членов жюри (§5.4, §35.4).

    Заполняется при старте бота из конфига ``JURY_HUIDS``.
    Распределение по пулам — в ``JuryPoolAssignment`` (§35.6).
    """

    __tablename__ = "jury_members"

    huid: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )

    def __repr__(self) -> str:
        return f"<JuryMember huid={self.huid} active={self.is_active}>"


class JuryPoolAssignment(Base):
    """Назначение члена жюри на пул (§35.6).

    По умолчанию все судьи во всех 12 пулах; конфиг ``JURY_POOLS_CONFIG``
    позволяет сузить состав по конкретному пулу. Уникальность пары
    (huid, track, age_category) гарантирует, что один судья не будет
    назначен на пул дважды.
    """

    __tablename__ = "jury_pool_assignments"
    __table_args__ = (
        UniqueConstraint(
            "jury_huid",
            "track",
            "age_category",
            name="uq_jury_pool_assignment_huid_pool",
        ),
        Index(
            "ix_jury_pool_assignments_pool",
            "track",
            "age_category",
        ),
    )

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    jury_huid: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jury_members.huid", ondelete="CASCADE"),
        nullable=False,
    )
    track: Mapped[Track] = mapped_column(
        SAEnum(Track, name="track"), nullable=False
    )
    age_category: Mapped[AgeCategory] = mapped_column(
        SAEnum(AgeCategory, name="age_category"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )


class JuryRound(Base):
    """Раунд голосования по пулу (§35.2, §35.4, §35.6).

    Один раунд = одна пара (пул, номер раунда 1..3). Дедлайн —
    ``JURY_ROUND_DEADLINE_HOURS`` (по умолчанию 48 ч). Статус
    ``DRAWN_BY_LOT`` ставится, если на текущем раунде сработал
    автоматический жребий (§35.2).
    """

    __tablename__ = "jury_rounds"
    __table_args__ = (
        UniqueConstraint(
            "track",
            "age_category",
            "round_no",
            name="uq_jury_rounds_pool_round",
        ),
    )

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    track: Mapped[Track] = mapped_column(
        SAEnum(Track, name="track"), nullable=False
    )
    age_category: Mapped[AgeCategory] = mapped_column(
        SAEnum(AgeCategory, name="age_category"), nullable=False
    )
    round_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[JuryRoundStatus] = mapped_column(
        SAEnum(JuryRoundStatus, name="jury_round_status"),
        nullable=False,
        default=JuryRoundStatus.OPEN,
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    deadline_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<JuryRound {self.track.name}/{self.age_category.name}"
            f" r{self.round_no} {self.status.name}>"
        )


class JuryVote(Base):
    """Голос судьи по заявке в рамках раунда (§35.3, §35.4).

    Хранится в БД и переживает рестарт бота. Уникальность
    (round, application, jury) исключает дубли. До нажатия
    «Отправить оценки» — ``DRAFT``, не учитывается в подсчёте;
    после — ``SUBMITTED`` с проставленным ``submitted_at``.
    """

    __tablename__ = "jury_votes"
    __table_args__ = (
        UniqueConstraint(
            "round_id",
            "application_id",
            "jury_huid",
            name="uq_jury_votes_round_app_jury",
        ),
        Index("ix_jury_votes_round_jury", "round_id", "jury_huid"),
    )

    id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    round_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jury_rounds.id", ondelete="CASCADE"),
        nullable=False,
    )
    application_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    jury_huid: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("jury_members.huid", ondelete="CASCADE"),
        nullable=False,
    )
    vote: Mapped[JuryVoteValue] = mapped_column(
        SAEnum(JuryVoteValue, name="jury_vote_value"), nullable=False
    )
    state: Mapped[JuryVoteState] = mapped_column(
        SAEnum(JuryVoteState, name="jury_vote_state"),
        nullable=False,
        default=JuryVoteState.DRAFT,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<JuryVote round={self.round_id} app={self.application_id}"
            f" vote={self.vote.name} state={self.state.name}>"
        )
