"""
Очередь повторных попыток ``bot.delete_message`` при сбое CTS.

Зачем:
    eXpress-CTS периодически проседает на endpoint ``delete_event``
    (502 / 500 / таймауты), при этом ``/send`` продолжает работать.
    Без retry в чате у пользователя накапливаются «висящие» transient-
    сообщения (фото заявок, уведомления), хотя новая навигация уже
    отрисована. См. ``app/fsm/cleanup_middleware.py``.

Как:
    1. В cleanup_middleware при «транзитной» ошибке CTS sync_id
       попадает в один глобальный Redis-ZSET ``bot_delete_retry``.
       Member — ``"{huid}:{sync_id}"``, score — Unix-таймстамп
       следующей попытки. Один ZSET даёт O(log N) ZRANGEBYSCORE
       без сканирования всех ключей.
    2. Фоновая задача (``start_delete_retry_task``) раз в
       ``DELETE_RETRY_INTERVAL_SEC`` секунд берёт все due-записи
       и пробует удалить через ``bot.delete_message``.
       - успех → удаляем из ZSET, чистим запись в meta-hash;
       - снова transient-ошибка → переставляем score
         ``now + backoff`` (экспоненциально: 30s → 60s → 120s → 240s);
       - после ``DELETE_RETRY_MAX_ATTEMPTS`` попыток или
         ``DELETE_RETRY_MAX_AGE_SEC`` секунд возраста — сдаёмся.
    3. Воркер запускается в ``app/main.py`` под
       ``if ENABLE_SCHEDULER:`` — в multi-worker деплое не
       дублируется.

Гарантии:
    - Без CTS-сбоя очередь пустая, ZRANGEBYSCORE отрабатывает за миллисекунды.
    - Воркер устойчив к любому исключению: внешний loop ловит всё и
      продолжает следующую итерацию.
"""
from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from uuid import UUID

from loguru import logger

from config import (
    DELETE_RETRY_INTERVAL_SEC,
    DELETE_RETRY_MAX_AGE_SEC,
    DELETE_RETRY_MAX_ATTEMPTS,
    REDIS_URL,
)

if TYPE_CHECKING:
    from pybotx import Bot


# Глобальный ZSET с due-таймстампами повторных попыток.
# Member-формат: ``{bot_id}:{huid}:{sync_id}``. bot_id нужен, чтобы
# при multi-bot-деплое случайно не дёрнуть delete_message не от того
# аккаунта.
_RETRY_ZSET_KEY = "bot_delete_retry"
# HASH с метаданными попыток. Ключ: ``bot_delete_retry:meta``,
# field: тот же member, value: ``"{attempts}:{first_failed_at}"``.
_RETRY_META_KEY = "bot_delete_retry:meta"

# Backoff между попытками (секунды). Длина <= MAX_ATTEMPTS;
# последнее значение используется для всех «хвостовых» попыток.
_BACKOFF_SECONDS = (30, 60, 120, 240, 240)

# Лимит на одну итерацию воркера — чтобы при массовом сбое CTS не
# уйти в долгую транзакцию.
_BATCH_LIMIT = 200


_redis_client = None


async def _get_redis():
    """Singleton Redis-клиент для очереди retry-delete."""
    global _redis_client

    if _redis_client is None:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(
            REDIS_URL,
            decode_responses=True,
        )
        await _redis_client.ping()
        logger.debug("delete_retry Redis connected")

    return _redis_client


def _member_key(bot_id: UUID, user_huid: UUID, sync_id: UUID) -> str:
    return f"{bot_id}:{user_huid}:{sync_id}"


def _parse_member(member: str) -> tuple[UUID, UUID, UUID] | None:
    """Парсит member ZSET-а обратно в (bot_id, huid, sync_id).

    Возвращает None для битых записей (мусор после ручного вмешательства
    в Redis или старого формата).
    """
    parts = member.split(":")
    if len(parts) != 3:
        return None
    try:
        return UUID(parts[0]), UUID(parts[1]), UUID(parts[2])
    except (ValueError, TypeError):
        return None


async def enqueue_delete_retry(
    bot_id: UUID,
    user_huid: UUID,
    sync_id: UUID,
) -> bool:
    """Поставить ``sync_id`` в очередь повторного удаления.

    Идемпотентно: повторный вызов для того же ``sync_id`` обновит score
    (next attempt) и атрибуты в meta-hash.

    Returns:
        True, если запись добавлена/обновлена в Redis.
    """
    try:
        redis = await _get_redis()
        member = _member_key(bot_id, user_huid, sync_id)
        now = int(time.time())
        next_attempt = now + _BACKOFF_SECONDS[0]

        # Атомарно: проверить, есть ли уже запись (сохранить first_failed_at),
        # если нет — создать; обновить score на next_attempt.
        existing = await redis.hget(_RETRY_META_KEY, member)
        if existing is None:
            attempts = 0
            first_failed_at = now
        else:
            try:
                attempts_str, first_str = existing.split(":")
                attempts = int(attempts_str)
                first_failed_at = int(first_str)
            except (ValueError, AttributeError):
                attempts = 0
                first_failed_at = now

        await redis.zadd(_RETRY_ZSET_KEY, {member: next_attempt})
        await redis.hset(
            _RETRY_META_KEY,
            member,
            f"{attempts}:{first_failed_at}",
        )
        logger.debug(
            "Enqueued delete_message retry",
            sync_id=str(sync_id),
            user_huid=str(user_huid),
            next_attempt_at=next_attempt,
            attempts=attempts,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Failed to enqueue delete retry: {}",
            repr(exc),
            sync_id=str(sync_id),
        )
        return False


async def _drop_member(redis, member: str) -> None:
    """Удалить запись и её метаданные из обеих структур."""
    await redis.zrem(_RETRY_ZSET_KEY, member)
    await redis.hdel(_RETRY_META_KEY, member)


def _next_backoff(attempts: int) -> int:
    """Backoff (сек) для следующей попытки после ``attempts`` неудач."""
    idx = min(attempts, len(_BACKOFF_SECONDS) - 1)
    return _BACKOFF_SECONDS[idx]


async def _process_due_retries(bot: "Bot") -> None:
    """Один проход воркера: попробовать удалить все due-записи."""
    redis = await _get_redis()
    now = int(time.time())

    # ZRANGEBYSCORE -inf..now → все, у кого пришёл срок повтора.
    members = await redis.zrangebyscore(
        _RETRY_ZSET_KEY,
        min=0,
        max=now,
        start=0,
        num=_BATCH_LIMIT,
    )
    if not members:
        return

    logger.debug(
        "delete_retry: due batch",
        count=len(members),
    )

    for member in members:
        parsed = _parse_member(member)
        if parsed is None:
            logger.warning(
                "delete_retry: битый member, удаляю из очереди",
                member=member,
            )
            await _drop_member(redis, member)
            continue
        bot_id, user_huid, sync_id = parsed

        meta = await redis.hget(_RETRY_META_KEY, member)
        try:
            attempts_str, first_str = (meta or "0:0").split(":")
            attempts = int(attempts_str)
            first_failed_at = int(first_str) or now
        except (ValueError, AttributeError):
            attempts = 0
            first_failed_at = now

        age = now - first_failed_at
        if age > DELETE_RETRY_MAX_AGE_SEC:
            logger.warning(
                "delete_retry: отказываемся от удаления (возраст > {} c)",
                DELETE_RETRY_MAX_AGE_SEC,
                sync_id=str(sync_id),
                attempts=attempts,
                age_sec=age,
            )
            await _drop_member(redis, member)
            continue

        try:
            await bot.delete_message(bot_id=bot_id, sync_id=sync_id)
        except Exception as exc:
            attempts += 1
            if attempts >= DELETE_RETRY_MAX_ATTEMPTS:
                logger.warning(
                    "delete_retry: отказываемся после {} попыток: {}",
                    attempts, repr(exc),
                    sync_id=str(sync_id),
                )
                await _drop_member(redis, member)
                continue

            backoff = _next_backoff(attempts)
            next_attempt = now + backoff
            await redis.zadd(_RETRY_ZSET_KEY, {member: next_attempt})
            await redis.hset(
                _RETRY_META_KEY,
                member,
                f"{attempts}:{first_failed_at}",
            )
            logger.debug(
                "delete_retry: повтор не удался, отложен на {} c",
                backoff,
                sync_id=str(sync_id),
                attempts=attempts,
                error=repr(exc),
            )
            continue

        await _drop_member(redis, member)
        logger.info(
            "delete_retry: сообщение успешно удалено после {} попыток",
            attempts + 1,
            sync_id=str(sync_id),
        )


async def _retry_loop(bot: "Bot", interval_sec: int) -> None:
    """Внешний цикл: каждые ``interval_sec`` обрабатывает due-записи."""
    logger.info(
        "delete_retry: воркер запущен (interval={}s, max_attempts={}, max_age={}s)",
        interval_sec,
        DELETE_RETRY_MAX_ATTEMPTS,
        DELETE_RETRY_MAX_AGE_SEC,
    )
    while True:
        try:
            await _process_due_retries(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "delete_retry: ошибка в воркере (продолжаем): {}",
                repr(exc),
            )
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            raise


def start_delete_retry_task(
    bot: "Bot",
    interval_sec: int = DELETE_RETRY_INTERVAL_SEC,
) -> asyncio.Task:
    """Запускает фоновый воркер повторных попыток удаления.

    Подключается в ``app/main.py`` под ``if ENABLE_SCHEDULER:`` —
    аналогично ``services.storage.start_disk_monitor_task``.
    """
    return asyncio.create_task(
        _retry_loop(bot, interval_sec),
        name="delete_retry_worker",
    )


async def close_redis() -> None:
    """Закрывает Redis-соединение очереди при shutdown."""
    global _redis_client

    if _redis_client is not None:
        try:
            await _redis_client.aclose()
            logger.debug("delete_retry Redis closed")
        except Exception as exc:
            logger.warning("Error closing delete_retry Redis: {}", repr(exc))

    _redis_client = None
