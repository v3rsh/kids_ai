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
from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    Boolean,
    DateTime,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
