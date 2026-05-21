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
TTL: 24 часа (автоочистка)

При отсутствии Redis — fallback на FSM data (поле _transient_messages).
"""
from typing import List, Optional
from uuid import UUID

from loguru import logger

from config import REDIS_URL

# Redis клиент (lazy init)
_redis_client = None

# TTL для ключей трекинга (24 часа)
TRACKING_TTL_SECONDS = 86400

# Префикс для Redis ключей
REDIS_KEY_PREFIX = "bot_transient"

# Ключ в FSM data для fallback
FSM_DATA_KEY = "_transient_messages"


async def _get_redis():
    """Получает Redis-клиент (lazy initialization)."""
    global _redis_client
    
    if not REDIS_URL:
        return None
    
    if _redis_client is None:
        try:
            import redis.asyncio as aioredis
            _redis_client = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
            )
            # Проверяем соединение
            await _redis_client.ping()
            logger.debug("Message tracking Redis connected")
        except Exception as e:
            logger.warning("Message tracking: Redis недоступен, fallback на FSM: {}", e)
            _redis_client = False  # Маркер "не использовать Redis"
            return None
    
    if _redis_client is False:
        return None
    
    return _redis_client


def _redis_key(user_huid: UUID) -> str:
    """Формирует Redis-ключ для пользователя."""
    return f"{REDIS_KEY_PREFIX}:{user_huid}"


async def track_transient_message(
    user_huid: UUID,
    sync_id: UUID,
    fsm_context=None,
) -> bool:
    """
    Сохраняет sync_id transient-сообщения для последующего удаления.
    
    Args:
        user_huid: UUID пользователя
        sync_id: sync_id сообщения
        fsm_context: FSMContext для fallback (опционально)
    
    Returns:
        True если успешно сохранено
    """
    try:
        redis = await _get_redis()
        
        if redis:
            key = _redis_key(user_huid)
            await redis.rpush(key, str(sync_id))
            await redis.expire(key, TRACKING_TTL_SECONDS)
            logger.debug(
                "Tracked transient message",
                user_huid=str(user_huid),
                sync_id=str(sync_id),
            )
            return True
        
        # Fallback на FSM data
        if fsm_context:
            data = await fsm_context.get_data()
            messages = data.get(FSM_DATA_KEY, [])
            messages.append(str(sync_id))
            await fsm_context.update_data(**{FSM_DATA_KEY: messages})
            logger.debug(
                "Tracked transient message (FSM fallback)",
                user_huid=str(user_huid),
                sync_id=str(sync_id),
            )
            return True
        
        logger.warning(
            "Cannot track transient message: no Redis and no FSM context",
            user_huid=str(user_huid),
            sync_id=str(sync_id),
        )
        return False
        
    except Exception as e:
        logger.error(
            "Error tracking transient message: {}",
            e,
            user_huid=str(user_huid),
            sync_id=str(sync_id),
        )
        return False


async def get_transient_messages(
    user_huid: UUID,
    fsm_context=None,
) -> List[UUID]:
    """
    Получает список sync_id всех transient-сообщений пользователя.
    
    Args:
        user_huid: UUID пользователя
        fsm_context: FSMContext для fallback (опционально)
    
    Returns:
        Список UUID sync_id
    """
    try:
        redis = await _get_redis()
        
        if redis:
            key = _redis_key(user_huid)
            sync_ids_str = await redis.lrange(key, 0, -1)
            
            result = []
            for sid_str in sync_ids_str:
                try:
                    result.append(UUID(sid_str))
                except (ValueError, TypeError):
                    logger.warning("Invalid sync_id in tracking: {}", sid_str)
            
            return result
        
        # Fallback на FSM data
        if fsm_context:
            data = await fsm_context.get_data()
            messages = data.get(FSM_DATA_KEY, [])
            
            result = []
            for sid_str in messages:
                try:
                    result.append(UUID(sid_str))
                except (ValueError, TypeError):
                    logger.warning("Invalid sync_id in FSM tracking: {}", sid_str)
            
            return result
        
        return []
        
    except Exception as e:
        logger.error(
            "Error getting transient messages: {}",
            e,
            user_huid=str(user_huid),
        )
        return []


async def clear_transient_messages(
    user_huid: UUID,
    fsm_context=None,
) -> int:
    """
    Очищает список transient-сообщений пользователя.
    Вызывается после успешного удаления сообщений.
    
    Args:
        user_huid: UUID пользователя
        fsm_context: FSMContext для fallback (опционально)
    
    Returns:
        Количество очищенных записей
    """
    try:
        redis = await _get_redis()
        
        if redis:
            key = _redis_key(user_huid)
            count = await redis.llen(key)
            await redis.delete(key)
            logger.debug(
                "Cleared transient messages",
                user_huid=str(user_huid),
                count=count,
            )
            return count
        
        # Fallback на FSM data
        if fsm_context:
            data = await fsm_context.get_data()
            messages = data.get(FSM_DATA_KEY, [])
            count = len(messages)
            if count > 0:
                await fsm_context.update_data(**{FSM_DATA_KEY: []})
                logger.debug(
                    "Cleared transient messages (FSM fallback)",
                    user_huid=str(user_huid),
                    count=count,
                )
            return count
        
        return 0
        
    except Exception as e:
        logger.error(
            "Error clearing transient messages: {}",
            e,
            user_huid=str(user_huid),
        )
        return 0


async def remove_from_tracking(
    user_huid: UUID,
    sync_id: UUID,
    fsm_context=None,
) -> bool:
    """
    Удаляет конкретный sync_id из трекинга.
    Используется когда сообщение было удалено вручную.
    
    Args:
        user_huid: UUID пользователя
        sync_id: sync_id сообщения для удаления из трекинга
        fsm_context: FSMContext для fallback (опционально)
    
    Returns:
        True если sync_id был найден и удалён
    """
    try:
        redis = await _get_redis()
        
        if redis:
            key = _redis_key(user_huid)
            removed = await redis.lrem(key, 1, str(sync_id))
            if removed:
                logger.debug(
                    "Removed from tracking",
                    user_huid=str(user_huid),
                    sync_id=str(sync_id),
                )
            return bool(removed)
        
        # Fallback на FSM data
        if fsm_context:
            data = await fsm_context.get_data()
            messages = data.get(FSM_DATA_KEY, [])
            sync_id_str = str(sync_id)
            if sync_id_str in messages:
                messages.remove(sync_id_str)
                await fsm_context.update_data(**{FSM_DATA_KEY: messages})
                logger.debug(
                    "Removed from tracking (FSM fallback)",
                    user_huid=str(user_huid),
                    sync_id=str(sync_id),
                )
                return True
            return False
        
        return False
        
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
    
    if _redis_client and _redis_client is not False:
        try:
            await _redis_client.aclose()
            logger.debug("Message tracking Redis closed")
        except Exception as e:
            logger.warning("Error closing message tracking Redis: {}", e)
    
    _redis_client = None
