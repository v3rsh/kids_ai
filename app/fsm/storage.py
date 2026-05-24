"""
FSM Storage (pybotx).

Единственное хранилище FSM — Redis (`RedisFSMStorage`). Контейнер Redis
из `docker-compose.yml` хранит AOF-файл на named volume `redisdata`,
поэтому состояния анкет переживают рестарты бота, Redis и хоста.
"""
from loguru import logger

from config import REDIS_URL, FSM_TTL_DAYS

from .redis_storage import RedisFSMStorage


_storage: RedisFSMStorage | None = None


def get_fsm_storage() -> RedisFSMStorage:
    """Возвращает глобальный экземпляр Redis FSM storage."""
    global _storage
    if _storage is None:
        if not REDIS_URL:
            raise RuntimeError(
                "REDIS_URL не задан. FSM работает только с Redis "
                "(см. docker-compose.yml: контейнер redis на 172.20.0.4). "
                "Заполни REDIS_URL в .env."
            )
        _storage = RedisFSMStorage(REDIS_URL, ttl_days=FSM_TTL_DAYS)
        logger.info("FSM storage: Redis ({})", REDIS_URL.split("@")[-1])
    return _storage


async def init_fsm_storage() -> None:
    """Инициализирует FSM storage и проверяет доступность Redis."""
    storage = get_fsm_storage()
    if await storage.ping():
        logger.info("Redis FSM storage подключён")
    else:
        raise RuntimeError(f"Не удалось подключиться к Redis: {REDIS_URL}")


async def close_fsm_storage() -> None:
    """Закрывает соединение с Redis (вызывается на shutdown)."""
    global _storage
    if _storage is None:
        return
    await _storage.close()
    _storage = None
