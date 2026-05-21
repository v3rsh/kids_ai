"""Подключение к PostgreSQL через SQLAlchemy 2.0 async."""
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from database.models import Base
from config import DATABASE_URL

# Создаём асинхронный движок SQLAlchemy для PostgreSQL
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=20,
    max_overflow=30,
    pool_pre_ping=True,
    pool_recycle=3600,
    connect_args={
        "command_timeout": 10,
    },
)

# Фабрика сессий
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


def get_session():
    """Возвращает фабрику сессий.

    Использование:
        async with get_session()() as session:
            ...
    """
    return async_session_maker


async def init_db() -> None:
    """Инициализация БД: create_all.

    Используется в автономных скриптах. В основном приложении вызывается
    create_all + run_auto_migrations() из main.py.
    """
    logger.info("Инициализация базы данных...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("База данных инициализирована успешно")
