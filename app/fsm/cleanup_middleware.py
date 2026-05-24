"""
Middleware для автоматической очистки transient-сообщений.

При клике на кнопку (source_sync_id присутствует) удаляет все
transient-сообщения, отправленные ранее, чтобы в чате оставалось
только актуальное меню.

Использование:
    @collector.command("/menu", middlewares=[fsm_middleware, cleanup_middleware])
    async def menu_handler(message: IncomingMessage, bot: Bot):
        ...
"""
from typing import Any, Callable

from loguru import logger
from pybotx import Bot, IncomingMessage

from utils.message_tracking import (
    get_transient_messages,
    clear_transient_messages,
)


# Тип для функции обработчика
IncomingMessageHandlerFunc = Callable[[IncomingMessage, Bot], Any]


async def cleanup_middleware(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    """
    Middleware для автоматической очистки transient-сообщений.
    
    Логика:
    - Если message.source_sync_id присутствует (клик по кнопке) —
      удалить все transient-сообщения ДО вызова handler
    - Если source_sync_id отсутствует (текстовый ввод) —
      пропустить очистку
    - Удаление безопасное: обёрнуто в try/except
    - После удаления очищается трекинг
    """
    user_huid = message.sender.huid

    # Очистка срабатывает только при клике по кнопке
    if message.source_sync_id:
        await _cleanup_transient_messages(message, bot, user_huid)

    await call_next(message, bot)


async def _cleanup_transient_messages(
    message: IncomingMessage,
    bot: Bot,
    user_huid,
) -> None:
    """
    Удаляет все transient-сообщения пользователя.

    Если source_sync_id — обычное menu-сообщение (НЕ в transient-списке),
    пропускает его, чтобы handler мог отредактировать через edit_message.

    Если source_sync_id — transient (фото-сообщение), удаляет его тоже,
    т.к. edit_message не может убрать вложение-фото. Устанавливает флаг
    message.state.transient_source_deleted = True, чтобы reply_to_user()
    пропустил edit и сразу отправил новое сообщение через answer_message.
    """
    try:
        transient_sync_ids = await get_transient_messages(user_huid)
        
        if not transient_sync_ids:
            logger.debug(
                "No transient messages to cleanup",
                user_huid=str(user_huid),
            )
            return
        
        source_sync_id = message.source_sync_id
        
        # Проверяем, является ли source transient-сообщением (фото и т.п.)
        # Menu-сообщения НЕ трекаются, поэтому для них source_in_transient = False
        transient_sync_ids_set = set(transient_sync_ids)
        source_is_transient = (
            source_sync_id is not None and source_sync_id in transient_sync_ids_set
        )
        
        if source_is_transient:
            logger.info(
                "Source message is transient (photo), will be deleted",
                user_huid=str(user_huid),
                source_sync_id=str(source_sync_id),
            )
        
        logger.info(
            "Cleaning up transient messages",
            user_huid=str(user_huid),
            count=len(transient_sync_ids),
        )
        
        deleted_count = 0
        
        for sync_id in transient_sync_ids:
            # Пропускаем source только если это НЕ transient (обычное menu-сообщение)
            # Transient source (фото) удаляем — edit_message не убирает вложения
            if source_sync_id and sync_id == source_sync_id and not source_is_transient:
                logger.debug(
                    "Skipping non-transient source message from cleanup",
                    sync_id=str(sync_id),
                )
                continue
            try:
                await bot.delete_message(
                    bot_id=message.bot.id,
                    sync_id=sync_id,
                )
                deleted_count += 1
                # Сигнализируем reply_to_user() что source удалён —
                # edit_message на удалённом сообщении молча не работает
                if sync_id == source_sync_id:
                    message.state.transient_source_deleted = True
            except Exception as e:
                # Сообщение могло быть уже удалено или недоступно
                error_str = str(e).lower()
                if "not found" in error_str or "already deleted" in error_str:
                    logger.debug(
                        "Transient message already deleted or not found",
                        sync_id=str(sync_id),
                    )
                else:
                    logger.warning(
                        "Failed to delete transient message: {}",
                        e,
                        sync_id=str(sync_id),
                    )
        
        await clear_transient_messages(user_huid)
        
        if deleted_count > 0:
            logger.info(
                "Transient messages deleted",
                user_huid=str(user_huid),
                deleted=deleted_count,
                total=len(transient_sync_ids),
            )
    
    except Exception as e:
        logger.error(
            "Error during transient messages cleanup: {}",
            e,
            user_huid=str(user_huid),
        )
