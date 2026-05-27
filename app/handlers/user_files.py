"""
Приём файлов работы по треку.

Состояние FSM — ``UserIntake.user_intake_files_collect``. Поведение:

- **За одну загрузку — один файл** на всех треках. Второй файл подряд
  без нажатия «Добавить ещё файл» отвергается. Следующий файл на
  многофайловых треках — только после этой кнопки.
- Сообщения с несколькими вложениями (более одного attachment в raw-пейлоаде)
  полностью отвергаются на этом шаге; ни один файл не сохраняется.
  Пользователь получает transient-ошибку с понятной инструкцией.
- **Традиционное рисование**:
  - 1 файл — для 2D-работ (рисунок/открытка/коллаж/аппликация/комикс);
  - 2–4 файла — для поделки/3D-модели/фотоинсталляции.
  Бот не спрашивает заранее, какой подтип — после первого файла он
  показывает кнопки ``Добавить ещё файл`` / ``Завершить загрузку``.
  После 4-го файла шаг завершается автоматически.
- **ИИ-рисунок**: ровно 1 файл. После приёма — автопереход
  к согласиям.
- **От руки к ИИ**: ровно 1 файл (общий коллаж «до/после»).
  Второй файл бот отвергает и просит заменить.

Валидация:
- разрешённые расширения: ``.jpg .jpeg .png .heic .webp .pdf``;
- максимальный размер одного файла — ``MAX_FILE_SIZE_MB`` (по умолчанию 10 МБ).
- FSM-флаг ``file_upload_allowed`` — gate «можно ли принять файл сейчас».

Хранение между шагами:
- Файлы сохраняются во временный каталог
  ``Path(tempfile.gettempdir()) / "kids_ai_intake" / <huid>``;
- метаданные (путь, оригинальное имя, размер, MIME) — в FSM
  ``data["files"]`` как список dict;
- финальное переименование/перенос в ``ATTACHMENTS_DIR`` выполняет
  ``services.storage.rename_and_save_file`` уже в ``user_confirm.py``
  на submit (когда заявка имеет ``br_id``).

Этот модуль активен только в режиме ``intake_mode = FILES``. В режиме
``LINKS`` (§33.6 ТЗ) ``user_intake._handle_description`` пропускает
шаг файлов и сразу переводит в согласия — сбор URL делает
``handlers/user_links.py`` уже после ``submit``. См.
``docs/architecture.md`` → «Резервный сценарий приёма по ссылкам».
"""
import asyncio
import shutil
import tempfile
from pathlib import Path
from uuid import UUID

from loguru import logger
from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
)
from pybotx.models.attachments import OutgoingAttachment

from config import MAX_FILE_SIZE_MB
from database.models import Track
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import file_upload_bubbles
from states import UserIntake
from utils.bot_utils import (
    reply_to_user,
    safe_answer_transient,
    send_photo_persistent,
)


collector = HandlerCollector()


# =====================================================================
# Константы валидации
# =====================================================================

ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".webp", ".pdf"}
)
_MAX_FILE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
TRADITIONAL_MAX_FILES = 4  # лимит ракурсов для 3D-варианта трека «Традиционное рисование»
_TRADITIONAL_MAX_FILES = TRADITIONAL_MAX_FILES
_SINGLE_FILE_TRACKS: frozenset[Track] = frozenset({Track.AI, Track.HANDMADE_TO_AI})
FSM_KEY_FILE_UPLOAD_ALLOWED = "file_upload_allowed"

_ERR_BATCH_UPLOAD = (
    "За одну загрузку принимается один файл. Если нужно добавить ещё — "
    "нажмите «Добавить ещё файл». Если нужно заменить уже загруженный — "
    "начните подачу заново через «Подать работу»."
)
_ERR_SINGLE_FILE_TRACK = (
    "В этом треке принимается ровно один файл. Второй файл "
    "отвергнут — если нужно заменить первый, нажмите «Подать "
    "работу» в главном меню и подайте заявку заново."
)
_ERR_MULTIPLE_ATTACHMENTS_IN_ONE_MESSAGE = (
    "Вы отправили несколько файлов в одном сообщении. Бот принимает "
    "только один файл за одну отправку.\n\n"
    "Отправляйте файлы по одному. Для 2–4 файлов в треке "
    "«Традиционное рисование» после приёма первого нажмите "
    "«Добавить ещё файл» и пришлите следующий отдельным сообщением."
)

# Per-user lock: защита от двойного приёма при быстрой отправке двух файлов.
_file_upload_locks: dict[UUID, asyncio.Lock] = {}


def _file_upload_lock(huid: UUID) -> asyncio.Lock:
    """Вернуть asyncio.Lock для пользователя (lazy-создание)."""
    lock = _file_upload_locks.get(huid)
    if lock is None:
        lock = asyncio.Lock()
        _file_upload_locks[huid] = lock
    return lock


def check_file_upload(
    track: Track,
    files_count: int,
    *,
    upload_allowed: bool,
) -> str | None:
    """Проверить, можно ли принять очередной файл на шаге загрузки.

    Returns:
        Текст ошибки для пользователя или ``None``, если файл можно принять.
    """
    if not upload_allowed:
        return _ERR_BATCH_UPLOAD
    if track in _SINGLE_FILE_TRACKS and files_count >= 1:
        return _ERR_SINGLE_FILE_TRACK
    if track == Track.TRADITIONAL and files_count >= _TRADITIONAL_MAX_FILES:
        return (
            f"Достигнут лимит файлов для треку «Традиционное» "
            f"({_TRADITIONAL_MAX_FILES}). Завершите загрузку кнопкой "
            "«Завершить загрузку»."
        )
    return None


def validate_files_count_for_track(track: Track, count: int) -> str | None:
    """Submit-time проверка количества файлов по треку.

    Returns:
        Человекочитаемая причина отказа или ``None``.
    """
    if track in _SINGLE_FILE_TRACKS and count != 1:
        return "в этом треке допускается ровно один файл"
    if track == Track.TRADITIONAL and not (1 <= count <= _TRADITIONAL_MAX_FILES):
        return (
            f"для трека «Традиционное рисование» допускается от 1 "
            f"до {_TRADITIONAL_MAX_FILES} файлов"
        )
    return None


def count_raw_attachments(raw_command: dict | None) -> int:
    """Вернуть количество attachments в raw-пейлоаде входящего сообщения.

    Returns:
        Число элементов в ключе "attachments" (если список), иначе 0.
    """
    if not raw_command:
        return 0
    atts = raw_command.get("attachments") or []
    return len(atts) if isinstance(atts, list) else 0


def build_file_accepted_caption(
    track: Track,
    original_filename: str,
    files_count: int,
) -> str:
    """Подпись persistent-сообщения с эхо принятого файла (шаг 7 из 7)."""
    if track == Track.TRADITIONAL:
        if files_count >= _TRADITIONAL_MAX_FILES:
            return (
                f"**Шаг 7 из 7. Принят файл «{original_filename}» "
                f"({_TRADITIONAL_MAX_FILES}/{_TRADITIONAL_MAX_FILES}).**\n\n"
                "Лимит достигнут. Переходим к согласиям."
            )
        return (
            f"**Шаг 7 из 7. Принят файл «{original_filename}» "
            f"({files_count}/{_TRADITIONAL_MAX_FILES}).**\n\n"
            "Добавьте ещё файл или завершите загрузку."
        )
    if track == Track.HANDMADE_TO_AI:
        return (
            f"**Шаг 7 из 7. Принят коллаж «{original_filename}».**\n\n"
            "Переходим к согласиям."
        )
    return (
        f"**Шаг 7 из 7. Принят файл «{original_filename}».**\n\n"
        "Переходим к согласиям."
    )


# =====================================================================
# Временный каталог для файлов между шагами
# =====================================================================


def _intake_temp_dir(huid: UUID | str) -> Path:
    """Каталог временного хранения файлов одной сессии анкеты.

    Один каталог на пользователя — повторная подача после `/intake_restart`
    переиспользует тот же путь, но с предварительной очисткой
    (``_cleanup_intake_temp_dir``).
    """
    base = Path(tempfile.gettempdir()) / "kids_ai_intake" / str(huid)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _cleanup_intake_temp_dir(huid: UUID | str) -> None:
    """Удалить временный каталог сессии (best-effort, без исключений)."""
    base = Path(tempfile.gettempdir()) / "kids_ai_intake" / str(huid)
    if base.exists():
        shutil.rmtree(base, ignore_errors=True)
        logger.debug("Очищен временный каталог анкеты", path=str(base))


# =====================================================================
# Тексты-инструкции по трекам
# =====================================================================

_PROMPT_TRADITIONAL = (
    "**Шаг 7 из 7. Загрузите файл работы**\n\n"
    "Для рисунка/открытки/коллажа/аппликации/комикса — один файл. "
    "Для поделки/3D-модели/фотоинсталляции — от 2 до 4 файлов "
    "(после первого появятся кнопки «Добавить ещё файл» / "
    "«Завершить загрузку»).\n\n"
    "Допустимые форматы: JPG, JPEG, PNG, HEIC, WEBP, PDF. "
    f"Максимальный размер одного файла — {MAX_FILE_SIZE_MB} МБ.\n\n"
    "В одном сообщении отправляйте только один файл."
)
_PROMPT_AI = (
    "**Шаг 7 из 7. Загрузите итоговое изображение**\n\n"
    "Созданное с помощью ИИ (1 файл).\n\n"
    "Допустимые форматы: JPG, JPEG, PNG, HEIC, WEBP, PDF. "
    f"Максимальный размер — {MAX_FILE_SIZE_MB} МБ. Промпт прикладывать "
    "не обязательно.\n\n"
    "В одном сообщении отправляйте только один файл."
)
_PROMPT_HANDMADE_TO_AI = (
    "**Шаг 7 из 7. Загрузите общий коллаж «до / после»**\n\n"
    "Ручная работа + ИИ-версия в одном изображении (1 файл).\n\n"
    "Допустимые форматы: JPG, JPEG, PNG, HEIC, WEBP, PDF. "
    f"Максимальный размер — {MAX_FILE_SIZE_MB} МБ.\n\n"
    "В одном сообщении отправляйте только один файл. "
    "Если пришлёте второй файл — он будет отвергнут (нужен ровно "
    "один коллаж)."
)


# =====================================================================
# Публичная точка входа из user_intake (после описания)
# =====================================================================


async def prompt_for_files(
    message: IncomingMessage, bot: Bot, track: Track
) -> None:
    """Показать инструкцию по загрузке файлов для выбранного трека.

    Вызывается из ``user_intake._handle_description`` после установки
    состояния ``user_intake_files_collect``. Очищает временный каталог
    от прошлых сессий — на случай, если родитель перезапустил анкету.
    """
    huid = message.sender.huid
    _cleanup_intake_temp_dir(huid)
    # Каталог пересоздаётся пустым — для всех треков.
    _intake_temp_dir(huid)

    fsm = message.state.fsm
    await fsm.update_data(files=[], file_upload_allowed=True)

    text = {
        Track.TRADITIONAL: _PROMPT_TRADITIONAL,
        Track.AI: _PROMPT_AI,
        Track.HANDMADE_TO_AI: _PROMPT_HANDMADE_TO_AI,
    }[track]

    # Кнопки на начальном экране не показываем — пока нет ни одного
    # файла, нечего «завершать». Пустой BubbleMarkup() удалит старые
    # кнопки (см. .cursor/rules/pybotx-bubbles.mdc).
    await reply_to_user(message, bot, text, bubbles=BubbleMarkup())


# =====================================================================
# State-handler: приём свободного текста и файлов в режиме сбора
# =====================================================================


async def _handle_files_collect(
    message: IncomingMessage, bot: Bot
) -> None:
    """Обработка входящего сообщения в состоянии загрузки файлов.

    Маршрут:
    - есть прикреплённый файл → ``_process_incoming_file``;
    - иначе — мягкое напоминание «пришлите файл».
    """
    if message.file is None:
        # Если у нас уже есть хотя бы один файл — оставляем кнопки
        # навигации, иначе только текст.
        fsm = message.state.fsm
        data = await fsm.get_data()
        files = data.get("files") or []
        track_name = data.get("track")
        bubbles = BubbleMarkup()
        if files and track_name == Track.TRADITIONAL.name:
            can_add_more = len(files) < _TRADITIONAL_MAX_FILES
            bubbles = file_upload_bubbles(
                can_add_more=can_add_more, can_finish=True
            )
        await reply_to_user(
            message,
            bot,
            "Пришлите файл вложением. Текст в этом шаге не принимается.",
            bubbles=bubbles,
        )
        return

    if count_raw_attachments(message.raw_command) > 1:
        await safe_answer_transient(
            message, bot, _ERR_MULTIPLE_ATTACHMENTS_IN_ONE_MESSAGE
        )
        return

    await _process_incoming_file(message, bot)


async def _process_incoming_file(
    message: IncomingMessage, bot: Bot
) -> None:
    """Валидация и сохранение одного входящего файла."""
    huid = message.sender.huid
    async with _file_upload_lock(huid):
        await _process_incoming_file_locked(message, bot)


async def _process_incoming_file_locked(
    message: IncomingMessage, bot: Bot
) -> None:
    """Критическая секция приёма файла (под per-user lock).

    Алгоритм:
    1. Проверить gate ``file_upload_allowed`` и лимиты по треку.
    2. Валидировать расширение и размер.
    3. Сохранить во временный каталог сессии с уникальным префиксом.
    4. Пополнить FSM ``data["files"]``, закрыть gate.
    5. Для TRADITIONAL — кнопки (или автозавершение на 4-м);
       для AI / HANDMADE_TO_AI — сразу перейти к согласиям.
    """
    fsm = message.state.fsm
    data = await fsm.get_data()
    track_name = data.get("track")
    if not track_name:
        logger.warning(
            "files_collect: track отсутствует в FSM — сбрасываем",
            sender=str(message.sender.huid),
        )
        from keyboards import back_to_main_menu_bubbles

        await safe_answer_transient(
            message,
            bot,
            "Сессия анкеты потерялась. Начните заново — нажмите «Подать "
            "работу» в главном меню.",
            bubbles=back_to_main_menu_bubbles(),
        )
        return
    try:
        track = Track[track_name]
    except KeyError:
        logger.exception("files_collect: некорректный track в FSM")
        return

    files: list[dict] = list(data.get("files") or [])
    upload_allowed = bool(data.get(FSM_KEY_FILE_UPLOAD_ALLOWED, True))

    limit_error = check_file_upload(
        track, len(files), upload_allowed=upload_allowed
    )
    if limit_error:
        bubbles = BubbleMarkup()
        if (
            track == Track.TRADITIONAL
            and len(files) >= _TRADITIONAL_MAX_FILES
        ):
            bubbles = file_upload_bubbles(can_add_more=False, can_finish=True)
        await safe_answer_transient(message, bot, limit_error, bubbles=bubbles)
        return

    incoming = message.file
    original_filename = (incoming.filename or "").strip() or "file"
    extension = Path(original_filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        await safe_answer_transient(
            message,
            bot,
            (
                f"Заявка не может быть принята: неподдерживаемый формат "
                f"файла «{extension or '?'}».\n"
                "Допустимы только JPG, JPEG, PNG, HEIC, WEBP, PDF. "
                "Загрузите файл в подходящем формате."
            ),
        )
        return

    file_size = getattr(incoming, "size", None) or len(incoming.content or b"")
    if file_size > _MAX_FILE_BYTES:
        await safe_answer_transient(
            message,
            bot,
            (
                f"Файл слишком большой. Максимальный размер одного файла "
                f"— {MAX_FILE_SIZE_MB} МБ. Пожалуйста, уменьшите размер "
                "файла или загрузите другой файл."
            ),
        )
        return

    # Сохраняем во временный каталог; в имени файла используем индекс
    # 1..N + санитизированный оригинал, чтобы не зависеть от FS-кодировки.
    huid = message.sender.huid
    temp_dir = _intake_temp_dir(huid)
    index = len(files) + 1
    safe_original = _sanitize_filename(original_filename)
    saved_path = temp_dir / f"{index:02d}_{safe_original}"

    try:
        saved_path.write_bytes(incoming.content or b"")
    except OSError:
        logger.exception(
            "Не удалось сохранить файл во временный каталог",
            path=str(saved_path),
        )
        await safe_answer_transient(
            message,
            bot,
            (
                "Заявка не может быть принята: техническая ошибка при "
                "сохранении файла. Попробуйте ещё раз."
            ),
        )
        return

    files.append(
        {
            "temp_path": str(saved_path),
            "original_filename": original_filename,
            "size_bytes": file_size,
            "mime_type": _guess_mime_type(extension),
            "extension": extension,
        }
    )
    await fsm.update_data(files=files, file_upload_allowed=False)

    logger.info(
        "Файл анкеты принят",
        parent_huid=str(huid),
        index=index,
        original_filename=original_filename,
        size_bytes=file_size,
        track=track.name,
    )

    caption = build_file_accepted_caption(track, original_filename, len(files))
    bubbles: BubbleMarkup | None = None
    if track == Track.TRADITIONAL and len(files) < _TRADITIONAL_MAX_FILES:
        bubbles = file_upload_bubbles(can_add_more=True, can_finish=True)

    echo_photo = OutgoingAttachment(
        content=incoming.content or b"",
        filename=original_filename,
    )
    try:
        await send_photo_persistent(
            message, bot, caption, photo=echo_photo, bubbles=bubbles
        )
    except Exception:
        logger.exception(
            "Не удалось отправить echo-файл, fallback на текст",
            parent_huid=str(huid),
            original_filename=original_filename,
        )
        await reply_to_user(
            message,
            bot,
            caption,
            bubbles=bubbles if bubbles is not None else BubbleMarkup(),
        )

    if track == Track.TRADITIONAL and len(files) < _TRADITIONAL_MAX_FILES:
        return

    if track == Track.TRADITIONAL and len(files) >= _TRADITIONAL_MAX_FILES:
        logger.debug(
            "TRADITIONAL: достигнут лимит 4 файлов, автозавершение",
            parent_huid=str(huid),
        )

    await _proceed_to_consents(message, bot)


async def _proceed_to_consents(message: IncomingMessage, bot: Bot) -> None:
    """Переход в состояние согласий (LGPD/политика конкурса).

    Сами кнопки согласий рисует ``user_confirm.show_consents`` —
    импортируем локально, чтобы не было циклов на этапе загрузки модуля.
    """
    fsm = message.state.fsm
    await fsm.set_state(UserIntake.user_intake_consents)
    from handlers.user_confirm import show_consents

    await show_consents(message, bot)


# =====================================================================
# Кнопки шага сбора файлов: «Добавить ещё файл» / «Завершить загрузку»
# =====================================================================


@collector.command(
    "/intake_file_more",
    description="Добавить ещё файл",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_intake_file_more(
    message: IncomingMessage, bot: Bot
) -> None:
    """Просто подсказка «пришлите следующий файл», состояние не меняется."""
    fsm = message.state.fsm
    current = await fsm.get_state()
    if current != UserIntake.user_intake_files_collect.value:
        logger.debug(
            "intake_file_more вне ожидаемого состояния — игнорируем",
            current=current,
        )
        return

    data = await fsm.get_data()
    files = data.get("files") or []
    track_name = data.get("track")
    can_add_more = (
        track_name == Track.TRADITIONAL.name
        and len(files) < _TRADITIONAL_MAX_FILES
    )
    if can_add_more:
        await fsm.update_data(file_upload_allowed=True)
    bubbles = file_upload_bubbles(
        can_add_more=can_add_more, can_finish=bool(files)
    )
    await reply_to_user(
        message,
        bot,
        "Хорошо, пришлите следующий файл вложением.",
        bubbles=bubbles,
    )


@collector.command(
    "/intake_file_done",
    description="Завершить загрузку файлов",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_intake_file_done(
    message: IncomingMessage, bot: Bot
) -> None:
    """Кнопка «Завершить загрузку» — переход к согласиям."""
    fsm = message.state.fsm
    current = await fsm.get_state()
    if current != UserIntake.user_intake_files_collect.value:
        logger.debug(
            "intake_file_done вне ожидаемого состояния — игнорируем",
            current=current,
        )
        return

    data = await fsm.get_data()
    files = data.get("files") or []
    if not files:
        await safe_answer_transient(
            message,
            bot,
            (
                "Сначала пришлите хотя бы один файл. После приёма "
                "появятся кнопки навигации."
            ),
        )
        return

    await _proceed_to_consents(message, bot)


# =====================================================================
# Утилиты
# =====================================================================


def _sanitize_filename(filename: str) -> str:
    """Минимальная санитизация имени файла для временного хранения.

    Убираем разделители путей и nul-байт, чтобы случайно не выйти
    за пределы каталога. Остальной anti-traversal делает Path, а
    финальное переименование на сервере выполняет
    ``services.storage.rename_and_save_file`` (там используется шаблон
    «BR_ID-ParentName-ChildName-Track-AgeCategory.ext»; оригинальное
    имя в FS не сохраняется — только в meta.txt рядом с файлами).
    """
    cleaned = filename.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = cleaned.strip().lstrip(".")
    return cleaned or "file"


_MIME_BY_EXTENSION = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".heic": "image/heic",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def _guess_mime_type(extension: str) -> str:
    """Угадать MIME-тип по расширению (статическая таблица, без mimetypes)."""
    return _MIME_BY_EXTENSION.get(extension.lower(), "application/octet-stream")


# =====================================================================
# Регистрация state-handler'а в диспетчере common.default_message_handler
# =====================================================================

register_state_handler(
    UserIntake.user_intake_files_collect.value, _handle_files_collect
)
