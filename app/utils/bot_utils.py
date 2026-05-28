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
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import aiofiles
from loguru import logger
from pybotx import Bot, BubbleMarkup, IncomingMessage
from pybotx.models.attachments import OutgoingAttachment

from utils.message_tracking import track_transient_message

if TYPE_CHECKING:
    from database.models import Application


# Лимит длины тела сообщения в eXpress/BotX (4096 символов).
# Превышение даёт `Message body length exceeds 4096 symbols` без
# возможности ретрая. Урезаем заранее с маркером, чтобы не падать.
MAX_MESSAGE_BODY = 4096
_TRUNCATE_MARKER = "\n…"

# Подстроки в str(exc), означающие невосстановимую ошибку CTS:
# ретраи только удлинят хендлер и снова получат 4xx. Распознаём
# заранее и сразу падаем в fallback / возвращаем False.
_NON_RETRYABLE_PATTERNS = (
    "message body length exceeds",
    "malformed_request",
    "code 400",
    "and payload",  # pybotx форматирует 4xx как "POST ... failed with code 400 and payload"
)


def _truncate_body(body: str) -> str:
    """Обрезает ``body`` до ``MAX_MESSAGE_BODY`` символов с маркером.

    eXpress принимает максимум 4096 символов в сообщении. Если уперлись
    в лимит — отдадим первые ``MAX_MESSAGE_BODY - len(marker)`` символов
    и допишем ``\\n…``, чтобы UI показал, что текст обрезан.

    Безопасна для не-str: вернёт аргумент как есть (pybotx сам отвалит
    с понятным типом-эксепшеном).
    """
    if not isinstance(body, str):
        return body
    if len(body) <= MAX_MESSAGE_BODY:
        return body
    cut = MAX_MESSAGE_BODY - len(_TRUNCATE_MARKER)
    return body[:cut] + _TRUNCATE_MARKER


def _is_non_retryable(exc: BaseException) -> bool:
    """True для 4xx/length-ошибок CTS, которые ретраить бесполезно."""
    text = str(exc).lower()
    if not text:
        return False
    return any(pat in text for pat in _NON_RETRYABLE_PATTERNS)


def resolve_bot_id(bot: Bot) -> UUID | None:
    """UUID первого bot account из ``Bot.bot_accounts``.

    В свежем pybotx ``Bot.bot_accounts`` — генератор (а не список),
    поэтому ``accounts[0]`` падает с ``TypeError``. Используем
    ``next(iter(...), None)``: работает и со списком, и с генератором,
    и с любым ``Iterable``.

    Возвращает ``None``, если bot accounts не настроены.
    """
    accounts = getattr(bot, "bot_accounts", None)
    if accounts is None:
        return None
    first = next(iter(accounts), None)
    if first is None:
        return None
    return getattr(first, "id", None)


def resolve_dm_chat_id(message: IncomingMessage) -> UUID | None:
    """UUID личного чата из ``IncomingMessage``.

    В pybotx ``UserSender`` не содержит ``chat_id`` — только ``message.chat.id``.
    Для проактивной отправки по чужому huid используйте
    ``services.notifications._resolve_user_chat_id``.
    """
    chat = getattr(message, "chat", None)
    return getattr(chat, "id", None) if chat else None


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
    """Вызывает async-функцию с retry. Возвращает True при успехе.

    Невосстановимые ошибки CTS (400 malformed, превышение лимита тела)
    распознаются ``_is_non_retryable`` и прекращают цикл сразу: повторять
    бесполезно, а каждый ретрай — лишняя задержка хендлера.
    """
    for attempt in range(retries):
        try:
            await coro_fn(**kwargs)
            return True
        except Exception as exc:
            if _is_non_retryable(exc):
                logger.warning(
                    "Невосстановимая ошибка {} (ретраи пропущены): {}",
                    label, exc,
                )
                return False
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
    body = _truncate_body(body)
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
    body = _truncate_body(body)
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
    body = _truncate_body(body)
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
    body = _truncate_body(body)
    send_kwargs = {"wait_callback": False, "file": photo, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles
    
    sync_id = await bot.answer_message(body, **send_kwargs)

    await track_transient_message(message.sender.huid, sync_id)

    return sync_id


async def send_photo_persistent(
    message: IncomingMessage,
    bot: Bot,
    body: str,
    photo: OutgoingAttachment,
    bubbles: Optional[BubbleMarkup] = None,
    **kwargs,
) -> UUID:
    """Persistent-сообщение с фото/файлом и подписью. Остаётся в истории чата.

    В отличие от ``send_photo_transient``, сообщение не трекается и не
    удаляется ``cleanup_middleware`` при следующей навигации.
    """
    body = _truncate_body(body)
    send_kwargs = {"wait_callback": False, "file": photo, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles

    return await bot.answer_message(body, **send_kwargs)


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
    body = _truncate_body(body)
    send_kwargs = {"wait_callback": False, **kwargs}
    if bubbles is not None:
        send_kwargs["bubbles"] = bubbles

    for attempt in range(retries):
        try:
            await bot.answer_message(body, **send_kwargs)
            return True
        except Exception as exc:
            if _is_non_retryable(exc):
                logger.error(
                    "Невосстановимая ошибка отправки (ретраи пропущены): {}", exc,
                )
                return False
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


def format_numbered_file_caption(
    br_id: str,
    index: int,
    total: int,
    filename: str,
) -> str:
    """Подпись к N-му файлу заявки (2..total) в цепочке вложений."""
    return f"📎 {br_id}: файл {index} из {total} — {filename}"


def format_anonymous_file_caption(index: int, total: int) -> str:
    """Подпись к N-му файлу без идентификаторов (экран жюри)."""
    return f"📎 Файл {index} из {total}"


def pagination_footer(current: int, total: int, *, title: str | None = None) -> str:
    """Хвост сообщения со счётчиком страниц.

    Пустая строка при ``total <= 1``, иначе ``\\n\\n[N из M]`` или
    ``\\n\\n**Title:** N из M``.
    """
    if total <= 1:
        return ""
    text = f"{current} из {total}"
    if title:
        text = f"**{title}:** {text}"
    return f"\n\n{text}"


# =====================================================================
# Хелперы карусели жюри (photo-якорь + edit caption на голосе)
# =====================================================================

# Подстроки в str(exc), при которых delete_message можно тихо
# проигнорировать (сообщение уже неактуально / удалено / чат недоступен).
# Совпадает с ``cleanup_middleware._SILENT_DELETE_PATTERNS``.
_JURY_ANCHOR_SILENT_DELETE_PATTERNS = (
    "not found",
    "already deleted",
    "event_not_found",
    "chat_not_found",
)


async def send_jury_carousel(
    message: IncomingMessage,
    bot: Bot,
    *,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    attachments: list[OutgoingAttachment] | None = None,
) -> UUID | None:
    """Отрисовать карточку текущей работы жюри: persistent photo-якорь + transient хвост.

    В отличие от ``send_application_files_with_card``:
    - 1-й файл уходит как **persistent** (`send_photo_persistent`) —
      ``cleanup_middleware`` его не удалит, можно редактировать caption
      через ``edit_jury_anchor_caption``;
    - 2..N — как обычно transient (``send_photo_transient``);
    - **не вызывает** ``delete_source_message`` — снятие старого якоря
      и source-сообщения отвечает хендлер (через ``delete_jury_anchor``).

    Если ``attachments`` пуст или ``None`` — отправляет текстовый
    persistent-якорь (через ``bot.answer_message``) с тем же body и bubbles.

    Args:
        message: входящее сообщение (нужен ``message.sender.huid`` для трекинга
            хвоста).
        bot: pybotx-инстанс.
        body: caption якоря (полный текст карточки).
        bubbles: ``BubbleMarkup`` якоря.
        attachments: файлы работы или ``None``.

    Returns:
        sync_id якоря (для записи в FSM) или ``None``, если отправка
        провалилась.
    """
    truncated = _truncate_body(body)

    if not attachments:
        send_kwargs = {"wait_callback": False}
        if bubbles is not None:
            send_kwargs["bubbles"] = bubbles
        try:
            return await bot.answer_message(truncated, **send_kwargs)
        except Exception:
            logger.exception("send_jury_carousel: не удалось отправить text-якорь")
            return None

    first, *rest = attachments
    try:
        anchor_sync_id = await send_photo_persistent(
            message,
            bot,
            body=truncated,
            photo=first,
            bubbles=bubbles,
        )
    except Exception:
        logger.exception(
            "send_jury_carousel: не удалось отправить photo-якорь",
            filename=first.filename,
        )
        return None

    total = len(attachments)
    for idx, attachment in enumerate(rest, start=2):
        caption = format_anonymous_file_caption(idx, total)
        try:
            await send_photo_transient(
                message,
                bot,
                body=caption,
                photo=attachment,
            )
        except Exception:
            logger.exception(
                "send_jury_carousel: не удалось отправить файл хвоста",
                filename=attachment.filename,
                idx=idx,
                total=total,
            )

    return anchor_sync_id


async def edit_jury_anchor_caption(
    bot: Bot,
    *,
    bot_id: UUID,
    anchor_sync_id: UUID,
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    retries: int = 2,
    delay: float = 1.0,
) -> bool:
    """Отредактировать caption и кнопки photo-якоря карусели жюри.

    Реализует «edit caption» из pybotx 0.76.3: ``body`` и ``bubbles``
    обновляются, ``file`` **НЕ передаётся** (``Undefined`` → ключа
    ``file`` нет в JSON → CTS не трогает вложение, фото остаётся на
    своём месте в истории).

    Эту функцию вызывает ``cmd_jt_vote`` (голос на текущей работе);
    ``cmd_jt_nav`` пересоздаёт якорь и edit caption не использует.

    Returns:
        True — edit прошёл за ``retries`` попыток; False — все попытки
        исчерпаны или ошибка невосстановимая (хендлер должен сделать
        fallback на полный рендер через ``send_jury_carousel``).
    """
    truncated = _truncate_body(body)
    kwargs: dict = {
        "bot_id": bot_id,
        "sync_id": anchor_sync_id,
        "body": truncated,
    }
    if bubbles is not None:
        kwargs["bubbles"] = bubbles

    return await _try_with_retry(
        bot.edit_message,
        kwargs,
        retries,
        delay,
        "edit_jury_anchor_caption",
    )


async def delete_jury_anchor(
    bot: Bot,
    *,
    bot_id: UUID,
    anchor_sync_id: UUID,
) -> None:
    """Безопасно удалить photo-якорь карусели жюри.

    Используется в ``cmd_jt_nav`` (перед отправкой нового якоря),
    ``cmd_jt_back``, ``cmd_jt_submit`` и всех ветках выхода из карусели,
    чтобы не оставлять «висящий» якорь с устаревшими кнопками.

    Тихо игнорирует «уже удалено / не найдено» (логирует на DEBUG);
    остальные ошибки — WARNING.
    """
    try:
        await bot.delete_message(bot_id=bot_id, sync_id=anchor_sync_id)
    except Exception as exc:
        text = str(exc).lower()
        if any(pat in text for pat in _JURY_ANCHOR_SILENT_DELETE_PATTERNS):
            logger.debug(
                "delete_jury_anchor: anchor уже удалён или не найден: {}",
                repr(exc),
                anchor_sync_id=str(anchor_sync_id),
            )
            return
        logger.warning(
            "delete_jury_anchor: не удалось удалить anchor: {}",
            repr(exc),
            anchor_sync_id=str(anchor_sync_id),
        )


async def send_application_files_with_card(
    message: IncomingMessage,
    bot: Bot,
    *,
    app: "Application",
    body: str,
    bubbles: Optional[BubbleMarkup] = None,
    anonymous_extra_captions: bool = False,
    attachments: list[OutgoingAttachment] | None = None,
) -> bool:
    """Отправить все файлы заявки: первый с карточкой, остальные — с подписью.

    Returns:
        True, если хотя бы один файл отправлен; False — fallback на текст.
    """
    if attachments is None:
        try:
            from services import storage as storage_service
        except ImportError:
            logger.exception(
                "Не удалось импортировать storage для отправки файлов заявки",
                br_id=app.br_id,
            )
            return False

        try:
            attachments = await storage_service.get_application_files_for_chat(app)
        except Exception:
            logger.exception(
                "Не удалось загрузить файлы заявки",
                br_id=app.br_id,
            )
            attachments = None

    if not attachments:
        return False

    await delete_source_message(message, bot)
    first, *rest = attachments
    total = len(attachments)
    await send_photo_transient(
        message,
        bot,
        body=body,
        photo=first,
        bubbles=bubbles,
    )
    for idx, attachment in enumerate(rest, start=2):
        if anonymous_extra_captions:
            caption = format_anonymous_file_caption(idx, total)
        else:
            caption = format_numbered_file_caption(
                app.br_id, idx, total, attachment.filename
            )
        await send_photo_transient(
            message,
            bot,
            body=caption,
            photo=attachment,
        )
    return True
