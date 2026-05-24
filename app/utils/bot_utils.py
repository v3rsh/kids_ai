"""
Утилиты для безопасной отправки сообщений через pybotx.

Все функции используют wait_callback=False, чтобы не зависеть
от получения callback-подтверждения от CTS.

Типы сообщений:
- Menu message — редактируется на месте, НЕ трекается
- Transient message — информационное, трекается и удаляется при навигации
"""
import asyncio
from pathlib import Path
from typing import Optional
from uuid import UUID

import aiofiles
from loguru import logger
from pybotx import Bot, BubbleMarkup, IncomingMessage
from pybotx.models.attachments import OutgoingAttachment

from utils.message_tracking import track_transient_message


async def load_user_photo(photo_path: str) -> OutgoingAttachment | None:
    """
    Загружает фото пользователя из файла.
    
    Args:
        photo_path: Путь к файлу фото
    
    Returns:
        OutgoingAttachment или None, если файл не найден
    """
    try:
        path = Path(photo_path)
        if not path.exists():
            logger.warning("Фото не найдено", path=photo_path)
            return None
        
        async with aiofiles.open(path, "rb") as f:
            content = await f.read()
        
        return OutgoingAttachment(
            content=content,
            filename=path.name,
        )
    except Exception:
        logger.exception("Ошибка загрузки фото", path=photo_path)
        return None


async def delete_source_message(message: IncomingMessage, bot: Bot) -> None:
    """
    Удаляет сообщение-источник (при клике по кнопке).
    
    Используется когда нужно заменить menu-сообщение на фото-сообщение,
    т.к. фото нельзя отправить через edit_message.
    Безопасно: если сообщение уже удалено или недоступно, ошибка логируется.
    """
    if message.source_sync_id:
        try:
            await bot.delete_message(
                bot_id=message.bot.id,
                sync_id=message.source_sync_id,
            )
        except Exception:
            logger.debug("Не удалось удалить исходное сообщение")


async def _try_with_retry(
    coro_fn,
    kwargs: dict,
    retries: int,
    delay: float,
    label: str,
) -> bool:
    """Вызывает async-функцию с retry. Возвращает True при успехе."""
    for attempt in range(retries):
        try:
            await coro_fn(**kwargs)
            return True
        except Exception as exc:
            if attempt < retries - 1:
                logger.warning(
                    "Попытка {} {}/{} не удалась: {}. Повтор через {}с...",
                    label, attempt + 1, retries, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.warning(
                    "Все {} попытки {} исчерпаны: {}",
                    retries, label, exc,
                )
    return False


async def reply_to_user(
    message: IncomingMessage,
    bot: Bot,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    retries: int = 2,
    delay: float = 1.0,
) -> None:
    """
    Ответ пользователю: edit при клике на кнопку, answer при текстовом вводе.

    При клике на кнопку (source_sync_id присутствует) — редактирует исходное сообщение.
    При текстовом вводе — отправляет новое сообщение с wait_callback=False.
    
    Если source был transient-сообщением (фото) и удалён cleanup_middleware,
    флаг message.state.transient_source_deleted = True пропускает edit
    (edit_message на удалённом сообщении молча не работает в CTS)
    и сразу отправляет новое сообщение.
    
    При сбое CTS (502 и т.п.) каждый вызов повторяется до retries раз
    с задержкой delay секунд. Если все попытки исчерпаны — логируется ошибка,
    исключение НЕ поднимается (чтобы не ронять хендлер).
    
    ВАЖНО: Это функция для MENU-сообщений (с кнопками навигации).
    Отправленные сообщения НЕ трекаются как transient, т.к. они редактируются на месте.
    
    Для transient-сообщений (информационных, без навигации) используй safe_answer_transient().
    """
    source_deleted = getattr(message.state, 'transient_source_deleted', False)

    if message.source_sync_id and not source_deleted:
        kwargs = {
            "bot_id": message.bot.id,
            "sync_id": message.source_sync_id,
            "body": body,
        }
        if bubbles is not None:
            kwargs["bubbles"] = bubbles

        if await _try_with_retry(bot.edit_message, kwargs, retries, delay, "edit_message"):
            return

        logger.warning("edit_message не удался после {} попыток, пробуем answer_message", retries)

    answer_kwargs = {"wait_callback": False}
    if bubbles is not None:
        answer_kwargs["bubbles"] = bubbles

    if not await _try_with_retry(
        lambda **kw: bot.answer_message(body, **kw),
        answer_kwargs, retries, delay, "answer_message",
    ):
        logger.error("Не удалось отправить сообщение после всех попыток (edit + answer)")


async def safe_answer(
    bot: Bot,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    **kwargs,
) -> UUID:
    """
    Обёртка над bot.answer_message с wait_callback=False.

    Используется когда нужно просто отправить новое сообщение
    без привязки к source_sync_id (например, валидационные ошибки).
    
    ВАЖНО: bubbles=None НЕ передаётся, чтобы pybotx не сериализовал
    null — CTS API возвращает 400 Bad Request.
    
    Returns:
        sync_id отправленного сообщения
    """
    send_kwargs = {"wait_callback": False, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles
    return await bot.answer_message(body, **send_kwargs)


async def safe_answer_transient(
    message: IncomingMessage,
    bot: Bot,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    **kwargs,
) -> UUID:
    """
    Отправка transient-сообщения с автоматическим трекингом.
    
    Transient-сообщения удаляются автоматически при следующей навигации
    (клике на кнопку меню). Используй для:
    - Ошибок валидации
    - Информационных уведомлений
    - Подтверждений действий
    - Любых сообщений, которые не должны оставаться в чате
    
    Args:
        message: Входящее сообщение (для получения user_huid и FSM context)
        bot: Экземпляр бота
        body: Текст сообщения
        bubbles: Клавиатура (опционально)
        **kwargs: Дополнительные аргументы для answer_message
    
    Returns:
        sync_id отправленного сообщения
    """
    send_kwargs = {"wait_callback": False, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles
    
    sync_id = await bot.answer_message(body, **send_kwargs)

    await track_transient_message(message.sender.huid, sync_id)

    return sync_id


async def send_photo_transient(
    message: IncomingMessage,
    bot: Bot,
    body: str,
    photo: OutgoingAttachment,
    bubbles: Optional[BubbleMarkup] = None,
    **kwargs,
) -> UUID:
    """
    Отправка сообщения с фото как transient.
    
    Фото-сообщения нельзя редактировать через edit_message,
    поэтому они всегда отправляются как новые и трекаются для удаления.
    
    Args:
        message: Входящее сообщение
        bot: Экземпляр бота
        body: Текст сообщения (caption)
        photo: Фото для отправки (OutgoingAttachment)
        bubbles: Клавиатура (опционально)
        **kwargs: Дополнительные аргументы для answer_message
    
    Returns:
        sync_id отправленного сообщения
    """
    send_kwargs = {"wait_callback": False, "file": photo, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles
    
    sync_id = await bot.answer_message(body, **send_kwargs)

    await track_transient_message(message.sender.huid, sync_id)

    return sync_id


async def send_with_retry(
    bot: Bot,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    retries: int = 2,
    delay: float = 1.0,
    **kwargs,
) -> bool:
    """
    Отправка сообщения с повторной попыткой при неудаче.
    
    Используется в критических точках регистрации, где сбой отправки
    приводит к застреванию пользователя в неправильном состоянии.
    
    ВАЖНО: bubbles=None НЕ передаётся в answer_message, чтобы pybotx
    использовал дефолт Undefined (а не сериализовал null в JSON,
    что вызывает 400 Bad Request от CTS).
    
    ВАЖНО: Эта функция НЕ трекает сообщения. Используется в регистрации,
    где cleanup middleware не активен.
    
    Args:
        bot: Экземпляр бота
        body: Текст сообщения
        bubbles: Клавиатура (опционально)
        retries: Количество попыток (по умолчанию 2)
        delay: Задержка между попытками в секундах
        **kwargs: Дополнительные аргументы для answer_message
    
    Returns:
        True если отправка успешна, False если все попытки исчерпаны
    """
    send_kwargs = {"wait_callback": False, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles
    
    for attempt in range(retries):
        try:
            await bot.answer_message(body, **send_kwargs)
            return True
        except Exception as exc:
            if attempt < retries - 1:
                logger.warning(
                    "Попытка отправки {}/{} не удалась: {}. Повтор через {} сек...",
                    attempt + 1, retries, exc, delay,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "Все {} попытки отправки сообщения исчерпаны. Последняя ошибка: {}",
                    retries, exc,
                )
    return False
