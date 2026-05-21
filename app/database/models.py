"""
SQLAlchemy-модели kids_ai.

Каркасная модель `User` нужна как точка опоры для механизма автомиграции
и health-чека PostgreSQL. Расширяй по мере появления функционала.

Соглашения (см. .cursor/rules/core-standards.mdc, performance.mdc):
- Использовать `DeclarativeBase`
- Для soft-delete добавлять `is_deleted` + `deleted_at`
- Индексировать поля, по которым строятся фильтры/joins
- Для timestamps использовать `datetime.utcnow` (UTC)
"""
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
