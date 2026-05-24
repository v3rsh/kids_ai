"""
Трекинг transient-сообщений для автоматической очистки.

Transient-сообщения — это сообщения, которые должны быть удалены
при следующей навигации (клике по кнопке меню). Например:
- Ошибки валидации
- Информационные уведомления
- Сообщения с фото профиля
- Подтверждения действий

Redis ключ: bot_transient:{user_huid}
Тип: LIST строк sync_id
TTL: 24 часа (автоочистка).

Хранилище — Redis из docker-compose (контейнер `redis`, AOF на named
volume `redisdata`). Без Redis модуль работать не может: переменная
`REDIS_URL` обязательна и валидируется при старте в `config.py`.
"""
from typing import List
from uuid import UUID

from loguru import logger

from config import REDIS_URL


_redis_client = None

TRACKING_TTL_SECONDS = 86400

REDIS_KEY_PREFIX = "bot_transient"


async def _get_redis():
    """Возвращает singleton Redis-клиент для трекинга."""
    global _redis_client

    if _redis_client is None:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
        )
        await _redis_client.ping()
        logger.debug("Message tracking Redis connected")

    return _redis_client


def _redis_key(user_huid: UUID) -> str:
    """Формирует Redis-ключ для пользователя."""
    return f"{REDIS_KEY_PREFIX}:{user_huid}"


async def track_transient_message(user_huid: UUID, sync_id: UUID) -> bool:
    """
    Сохраняет sync_id transient-сообщения для последующего удаления.

    Args:
        user_huid: UUID пользователя
        sync_id: sync_id сообщения

    Returns:
        True если успешно сохранено
    """
    try:
        redis = await _get_redis()
        key = _redis_key(user_huid)
        await redis.rpush(key, str(sync_id))
        await redis.expire(key, TRACKING_TTL_SECONDS)
        logger.debug(
            "Tracked transient message",
            user_huid=str(user_huid),
            sync_id=str(sync_id),
        )
        return True
    except Exception as e:
        logger.error(
            "Error tracking transient message: {}",
            e,
            user_huid=str(user_huid),
            sync_id=str(sync_id),
        )
        return False


async def get_transient_messages(user_huid: UUID) -> List[UUID]:
    """
    Получает список sync_id всех transient-сообщений пользователя.

    Args:
        user_huid: UUID пользователя

    Returns:
        Список UUID sync_id
    """
    try:
        redis = await _get_redis()
        key = _redis_key(user_huid)
        sync_ids_str = await redis.lrange(key, 0, -1)

        result: List[UUID] = []
        for sid_str in sync_ids_str:
            try:
                result.append(UUID(sid_str))
            except (ValueError, TypeError):
                logger.warning("Invalid sync_id in tracking: {}", sid_str)

        return result
    except Exception as e:
        logger.error(
            "Error getting transient messages: {}",
            e,
            user_huid=str(user_huid),
        )
        return []


async def clear_transient_messages(user_huid: UUID) -> int:
    """
    Очищает список transient-сообщений пользователя.
    Вызывается после успешного удаления сообщений.

    Args:
        user_huid: UUID пользователя

    Returns:
        Количество очищенных записей
    """
    try:
        redis = await _get_redis()
        key = _redis_key(user_huid)
        count = await redis.llen(key)
        await redis.delete(key)
        logger.debug(
            "Cleared transient messages",
            user_huid=str(user_huid),
            count=count,
        )
        return count
    except Exception as e:
        logger.error(
            "Error clearing transient messages: {}",
            e,
            user_huid=str(user_huid),
        )
        return 0


async def remove_from_tracking(user_huid: UUID, sync_id: UUID) -> bool:
    """
    Удаляет конкретный sync_id из трекинга.
    Используется когда сообщение было удалено вручную.

    Args:
        user_huid: UUID пользователя
        sync_id: sync_id сообщения для удаления из трекинга

    Returns:
        True если sync_id был найден и удалён
    """
    try:
        redis = await _get_redis()
        key = _redis_key(user_huid)
        removed = await redis.lrem(key, 1, str(sync_id))
        if removed:
            logger.debug(
                "Removed from tracking",
                user_huid=str(user_huid),
                sync_id=str(sync_id),
            )
        return bool(removed)
    except Exception as e:
        logger.error(
            "Error removing from tracking: {}",
            e,
            user_huid=str(user_huid),
            sync_id=str(sync_id),
        )
        return False


async def close_redis() -> None:
    """Закрывает Redis-соединение при shutdown."""
    global _redis_client

    if _redis_client is not None:
        try:
            await _redis_client.aclose()
            logger.debug("Message tracking Redis closed")
        except Exception as e:
            logger.warning("Error closing message tracking Redis: {}", e)

    _redis_client = None
