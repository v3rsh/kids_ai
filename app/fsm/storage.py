"""
FSM Storage (pybotx)

Хранилище состояний FSM на базе SQLite.
Работает с UUID идентификаторами пользователей.
"""
import json
from typing import Dict, Any, Optional, List
from uuid import UUID

import aiosqlite
from loguru import logger

from config import STATES_DB_PATH, REDIS_URL, FSM_TTL_DAYS


class FSMStorage:
    """
    SQLite хранилище для состояний FSM.
    
    Ключ хранения: user_huid (UUID пользователя)
    """
    
    def __init__(self, db_path: str = STATES_DB_PATH):
        self.db_path = db_path
        self._initialized = False
    
    async def _init_db(self) -> None:
        """Инициализирует таблицу для хранения состояний"""
        if self._initialized:
            return
            
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS fsm_storage (
                    user_huid TEXT PRIMARY KEY,
                    state TEXT NULL,
                    data TEXT NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_fsm_updated_at 
                ON fsm_storage(updated_at)
            """)
            
            await db.commit()
            self._initialized = True
            logger.info("FSM storage initialized")
    
    async def set_state(self, user_huid: UUID, state: Optional[str]) -> None:
        """
        Устанавливает состояние для пользователя.
        
        Args:
            user_huid: UUID пользователя
            state: Новое состояние (или None для сброса)
        """
        await self._init_db()
        
        key = str(user_huid)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO fsm_storage (user_huid, state, data, updated_at)
                VALUES (?, ?, '{}', CURRENT_TIMESTAMP)
                ON CONFLICT(user_huid) DO UPDATE SET 
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, state))
            await db.commit()
            
        logger.debug("Set state", key=key, state=state)
    
    async def get_state(self, user_huid: UUID) -> Optional[str]:
        """
        Получает текущее состояние пользователя.
        
        Args:
            user_huid: UUID пользователя
        
        Returns:
            Текущее состояние или None
        """
        await self._init_db()
        
        key = str(user_huid)
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT state FROM fsm_storage WHERE user_huid = ?",
                (key,)
            ) as cursor:
                result = await cursor.fetchone()
                return result[0] if result else None
    
    async def set_data(self, user_huid: UUID, data: Dict[str, Any]) -> None:
        """
        Устанавливает данные состояния для пользователя.
        
        Args:
            user_huid: UUID пользователя
            data: Данные для сохранения
        """
        await self._init_db()
        
        key = str(user_huid)
        data_json = json.dumps(data, ensure_ascii=False)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO fsm_storage (user_huid, state, data, updated_at)
                VALUES (?, NULL, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_huid) DO UPDATE SET 
                    data = excluded.data,
                    updated_at = CURRENT_TIMESTAMP
            """, (key, data_json))
            await db.commit()
    
    async def get_data(self, user_huid: UUID) -> Dict[str, Any]:
        """
        Получает данные состояния пользователя.
        
        Args:
            user_huid: UUID пользователя
        
        Returns:
            Данные состояния (пустой dict если нет данных)
        """
        await self._init_db()
        
        key = str(user_huid)
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT data FROM fsm_storage WHERE user_huid = ?",
                (key,)
            ) as cursor:
                result = await cursor.fetchone()
                if result and result[0]:
                    return json.loads(result[0])
                return {}
    
    async def update_data(self, user_huid: UUID, **kwargs) -> Dict[str, Any]:
        """
        Обновляет данные состояния пользователя.
        
        Args:
            user_huid: UUID пользователя
            **kwargs: Данные для обновления
        
        Returns:
            Обновленные данные
        """
        current_data = await self.get_data(user_huid)
        current_data.update(kwargs)
        await self.set_data(user_huid, current_data)
        return current_data
    
    async def clear(self, user_huid: UUID) -> None:
        """
        Очищает состояние и данные пользователя.
        
        Args:
            user_huid: UUID пользователя
        """
        await self._init_db()
        
        key = str(user_huid)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM fsm_storage WHERE user_huid = ?",
                (key,)
            )
            await db.commit()
            
        logger.debug("Cleared state", key=key)
    
    async def get_state_and_data(self, user_huid: UUID) -> tuple[Optional[str], Dict[str, Any]]:
        """
        Получает состояние и данные пользователя одним запросом.
        
        Args:
            user_huid: UUID пользователя
        
        Returns:
            Tuple (state, data)
        """
        await self._init_db()
        
        key = str(user_huid)
        
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT state, data FROM fsm_storage WHERE user_huid = ?",
                (key,)
            ) as cursor:
                result = await cursor.fetchone()
                if result:
                    state = result[0]
                    data = json.loads(result[1]) if result[1] else {}
                    return state, data
                return None, {}
    
    async def cleanup_old_states(self, days: int = 30) -> int:
        """
        Удаляет старые записи состояний.
        
        Args:
            days: Количество дней для хранения
        
        Returns:
            Количество удаленных записей
        """
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                DELETE FROM fsm_storage 
                WHERE updated_at < datetime('now', ?)
            """, (f'-{days} days',))
            await db.commit()
            
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info("Cleaned up old FSM states", count=deleted)
            return deleted


# Глобальный экземпляр хранилища
_storage = None


def get_fsm_storage():
    """
    Получает глобальный экземпляр FSM storage.

    Если задан REDIS_URL — использует Redis, иначе SQLite.
    """
    global _storage
    if _storage is None:
        if REDIS_URL:
            from .redis_storage import RedisFSMStorage
            _storage = RedisFSMStorage(REDIS_URL, ttl_days=FSM_TTL_DAYS)
            logger.info("FSM storage: Redis ({})", REDIS_URL.split("@")[-1])
        else:
            _storage = FSMStorage()
            logger.info("FSM storage: SQLite ({})", STATES_DB_PATH)
    return _storage


async def init_fsm_storage() -> None:
    """Инициализирует FSM storage и проверяет доступность Redis."""
    storage = get_fsm_storage()

    if REDIS_URL:
        from .redis_storage import RedisFSMStorage
        if isinstance(storage, RedisFSMStorage):
            if await storage.ping():
                logger.info("Redis FSM storage подключён")
            else:
                raise RuntimeError(f"Не удалось подключиться к Redis: {REDIS_URL}")


async def close_fsm_storage() -> None:
    """Закрывает FSM storage (Redis)."""
    if not REDIS_URL:
        return

    from .redis_storage import RedisFSMStorage
    storage = get_fsm_storage()
    if isinstance(storage, RedisFSMStorage):
        await storage.close()
