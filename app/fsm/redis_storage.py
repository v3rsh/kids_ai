"""
FSM Storage на Redis.

Единственное хранилище FSM проекта; AOF на named volume `redisdata`
(см. docker-compose.yml) обеспечивает сохранность состояний при
рестартах. Полностью асинхронный, использует redis.asyncio с пулом
соединений.
"""
import json
from typing import Any, Dict, Optional
from uuid import UUID

from loguru import logger

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None  # type: ignore[assignment]


class RedisFSMStorage:
    """
    Redis-хранилище для состояний FSM.

    Ключ хранения: fsm:{user_huid} (Redis hash)
    Поля: state, data
    TTL: автоматическая очистка через EXPIRE.
    """

    KEY_PREFIX = "fsm"

    def __init__(self, redis_url: str, ttl_days: int = 30):
        if aioredis is None:
            raise ImportError(
                "Пакет redis не установлен. "
                "Установите: pip install redis[hiredis]"
            )

        self._redis: aioredis.Redis = aioredis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            retry_on_timeout=True,
        )
        self._ttl_seconds = ttl_days * 86400  # дни → секунды

    def _key(self, user_huid: UUID) -> str:
        """Формирует Redis-ключ для пользователя."""
        return f"{self.KEY_PREFIX}:{user_huid}"

    async def _touch_ttl(self, key: str) -> None:
        """Обновляет TTL ключа."""
        await self._redis.expire(key, self._ttl_seconds)

    async def set_state(self, user_huid: UUID, state: Optional[str]) -> None:
        """
        Устанавливает состояние для пользователя.

        Args:
            user_huid: UUID пользователя
            state: Новое состояние (или None для сброса)
        """
        key = self._key(user_huid)

        if state is None:
            await self._redis.hdel(key, "state")
        else:
            await self._redis.hset(key, "state", state)

        await self._touch_ttl(key)
        logger.debug("Set state", key=key, state=state)

    async def get_state(self, user_huid: UUID) -> Optional[str]:
        """
        Получает текущее состояние пользователя.

        Args:
            user_huid: UUID пользователя

        Returns:
            Текущее состояние или None
        """
        key = self._key(user_huid)
        return await self._redis.hget(key, "state")

    async def set_data(self, user_huid: UUID, data: Dict[str, Any]) -> None:
        """
        Устанавливает данные состояния для пользователя.

        Args:
            user_huid: UUID пользователя
            data: Данные для сохранения
        """
        key = self._key(user_huid)
        data_json = json.dumps(data, ensure_ascii=False)
        await self._redis.hset(key, "data", data_json)
        await self._touch_ttl(key)

    async def get_data(self, user_huid: UUID) -> Dict[str, Any]:
        """
        Получает данные состояния пользователя.

        Args:
            user_huid: UUID пользователя

        Returns:
            Данные состояния (пустой dict если нет данных)
        """
        key = self._key(user_huid)
        raw = await self._redis.hget(key, "data")
        if raw:
            return json.loads(raw)
        return {}

    async def update_data(self, user_huid: UUID, **kwargs: Any) -> Dict[str, Any]:
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
        key = self._key(user_huid)
        await self._redis.delete(key)
        logger.debug("Cleared state", key=key)

    async def get_state_and_data(
        self, user_huid: UUID
    ) -> tuple[Optional[str], Dict[str, Any]]:
        """
        Получает состояние и данные пользователя одним запросом.

        Args:
            user_huid: UUID пользователя

        Returns:
            Tuple (state, data)
        """
        key = self._key(user_huid)
        values = await self._redis.hmget(key, "state", "data")
        state = values[0]  # str или None
        data_raw = values[1]
        data = json.loads(data_raw) if data_raw else {}
        return state, data

    async def cleanup_old_states(self, days: int = 30) -> int:
        """
        Очистка старых записей.

        В Redis TTL устанавливается автоматически при каждой записи,
        поэтому эта операция — no-op.

        Returns:
            Всегда 0 (очистка происходит автоматически через TTL)
        """
        logger.debug(
            "cleanup_old_states вызван, но Redis использует TTL — ручная очистка не требуется"
        )
        return 0

    async def ping(self) -> bool:
        """
        Проверяет доступность Redis.

        Returns:
            True если Redis доступен
        """
        try:
            return await self._redis.ping()
        except Exception:
            return False

    async def close(self) -> None:
        """Закрывает соединение с Redis."""
        await self._redis.aclose()
        logger.info("Redis FSM storage закрыт")
