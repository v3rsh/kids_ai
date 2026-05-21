"""
Приём файлов работы по треку (§12, §16).

Состояние FSM — ``UserIntake.user_intake_files_collect``. Поведение:

- **Традиционное рисование (§12.1)**:
  - 1 файл — для 2D-работ (рисунок/открытка/коллаж/аппликация/комикс);
  - 2–4 файла — для поделки/3D-модели/фотоинсталляции.
  Бот не спрашивает заранее, какой подтип — после первого файла он
  показывает кнопки ``Добавить ещё файл`` / ``Завершить загрузку``.
  После 4-го файла шаг завершается автоматически (§12.1).
- **ИИ-рисунок (§12.2)**: ровно 1 файл. После приёма — автопереход
  к согласиям.
- **От руки к ИИ (§12.3)**: ровно 1 файл (общий коллаж «до/после»).
  Второй файл бот **отвергает** и просит заменить (§12.3).

Валидация (§16):
- разрешённые расширения: ``.jpg .jpeg .png .heic .webp .pdf``;
- максимальный размер одного файла — ``MAX_FILE_SIZE_MB`` (10 МБ по §11.4).

Хранение между шагами:
- Файлы сохраняются во временный каталог
  ``Path(tempfile.gettempdir()) / "kids_ai_intake" / <huid>``;
- метаданные (путь, оригинальное имя, размер, MIME) — в FSM
  ``data["files"]`` как список dict;
- финальное переименование/перенос в ``ATTACHMENTS_DIR`` выполняет
  ``services.storage.rename_and_save_file`` уже в ``user_confirm.py``
  на submit (когда заявка имеет ``br_id``).

WAVE4-TODO (LINKS-режим): интеграция с
``services.intake_mode.get_intake_mode()`` — если режим ``LINKS``,
вместо приёма файлов запрашивать ссылку на папку участника (§33.6).
На текущем этапе режим всегда ``FILES``; ссылочный UX в
``user_files`` / ``user_confirm`` будет добавлен отдельным
feature-коммитом (см. ``docs/testing.md`` → §6 п. 19).
"""
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

from config import MAX_FILE_SIZE_MB
from database.models import Track
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import file_upload_bubbles
from states import UserIntake
from utils.bot_utils import reply_to_user, safe_answer_transient


collector = HandlerCollector()


# =====================================================================
# Константы валидации (§16, §11.4)
# =====================================================================

ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".webp", ".pdf"}
)
_MAX_FILE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
_TRADITIONAL_MAX_FILES = 4  # §12.1 — лимит 4 файла для 3D-варианта


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
# Тексты-инструкции по трекам (§12)
# =====================================================================

_PROMPT_TRADITIONAL = (
    "Загрузите файл работы.\n\n"
    "Для рисунка/открытки/коллажа/аппликации/комикса — один файл. "
    "Для поделки/3D-модели/фотоинсталляции — от 2 до 4 файлов "
    "(после первого появятся кнопки «Добавить ещё файл» / "
    "«Завершить загрузку»).\n\n"
    "Допустимые форматы: JPG, JPEG, PNG, HEIC, WEBP, PDF. "
    f"Максимальный размер одного файла — {MAX_FILE_SIZE_MB} МБ."
)
_PROMPT_AI = (
    "Загрузите итоговое изображение, созданное с помощью ИИ "
    "(1 файл).\n\n"
    "Допустимые форматы: JPG, JPEG, PNG, HEIC, WEBP, PDF. "
    f"Максимальный размер — {MAX_FILE_SIZE_MB} МБ. Промпт прикладывать "
    "не обязательно."
)
_PROMPT_HANDMADE_TO_AI = (
    "Загрузите один файл — общий коллаж «до / после»: ручная работа "
    "+ ИИ-версия в одном изображении.\n\n"
    "Допустимые форматы: JPG, JPEG, PNG, HEIC, WEBP, PDF. "
    f"Максимальный размер — {MAX_FILE_SIZE_MB} МБ.\n\n"
    "Если пришлёте второй файл — он будет отвергнут (нужен ровно "
    "один коллаж)."
)


# =====================================================================
# Публичная точка входа из user_intake (после описания)
# =====================================================================


async def prompt_for_files(
    message: IncomingMessage, bot: Bot, track: Track
) -> None:
    """Показать инструкцию по загрузке файлов для выбранного трека (§12).

    Вызывается из ``user_intake._handle_description`` после установки
    состояния ``user_intake_files_collect``. Очищает временный каталог
    от прошлых сессий — на случай, если родитель перезапустил анкету.
    """
    huid = message.sender.huid
    _cleanup_intake_temp_dir(huid)
    # Каталог пересоздаётся пустым — для всех треков.
    _intake_temp_dir(huid)

    fsm = message.state.fsm
    await fsm.update_data(files=[])

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

    await _process_incoming_file(message, bot)


async def _process_incoming_file(
    message: IncomingMessage, bot: Bot
) -> None:
    """Валидация и сохранение одного входящего файла.

    Алгоритм:
    1. Достать ``message.file`` (``IncomingFileAttachment``).
    2. Валидировать расширение (§16) и размер (§11.4, §16).
    3. Если ИИ или Handmade-to-AI и уже есть 1 файл — отвергнуть
       (§12.2 / §12.3).
    4. Сохранить во временный каталог сессии с уникальным префиксом.
    5. Пополнить FSM ``data["files"]`` и:
       - для TRADITIONAL: показать кнопки (или автозавершить на 4-м);
       - для AI / HANDMADE_TO_AI: сразу перейти к согласиям.
    """
    fsm = message.state.fsm
    data = await fsm.get_data()
    track_name = data.get("track")
    if not track_name:
        logger.warning(
            "files_collect: track отсутствует в FSM — сбрасываем",
            sender=str(message.sender.huid),
        )
        await safe_answer_transient(
            message,
            bot,
            "Сессия анкеты потерялась. Начните заново — нажмите «Подать "
            "работу» в главном меню.",
        )
        return
    try:
        track = Track[track_name]
    except KeyError:
        logger.exception("files_collect: некорректный track в FSM")
        return

    files: list[dict] = list(data.get("files") or [])

    incoming = message.file
    original_filename = (incoming.filename or "").strip() or "file"
    extension = Path(original_filename).suffix.lower()

    if extension not in ALLOWED_EXTENSIONS:
        # §18.2 — «Заявка не может быть принята: неподдерживаемый формат»
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
        # §16 — фиксированный текст об ошибке размера.
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

    # Запрещаем второй файл в треках с лимитом 1 (§12.2 / §12.3).
    if track in (Track.AI, Track.HANDMADE_TO_AI) and len(files) >= 1:
        # §12.3 акцентирует слово «отвергает» — даём такую же
        # формулировку для трека «От руки к ИИ»; для ИИ-трека рамка
        # такая же.
        await safe_answer_transient(
            message,
            bot,
            (
                "В этом треке принимается ровно один файл. Второй файл "
                "отвергнут — если нужно заменить первый, нажмите «Подать "
                "работу» в главном меню и подайте заявку заново."
            ),
        )
        return

    # TRADITIONAL: жёсткий потолок 4 файла.
    if track == Track.TRADITIONAL and len(files) >= _TRADITIONAL_MAX_FILES:
        await safe_answer_transient(
            message,
            bot,
            (
                f"Достигнут лимит файлов для треку «Традиционное» "
                f"({_TRADITIONAL_MAX_FILES}). Завершите загрузку кнопкой "
                "«Завершить загрузку»."
            ),
            bubbles=file_upload_bubbles(can_add_more=False, can_finish=True),
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
    await fsm.update_data(files=files)

    logger.info(
        "Файл анкеты принят",
        parent_huid=str(huid),
        index=index,
        original_filename=original_filename,
        size_bytes=file_size,
        track=track.name,
    )

    # Дальше — траектория по треку.
    if track == Track.TRADITIONAL:
        if len(files) >= _TRADITIONAL_MAX_FILES:
            # §12.1 — после 4-го файла шаг завершается автоматически.
            logger.debug(
                "TRADITIONAL: достигнут лимит 4 файлов, автозавершение",
                parent_huid=str(huid),
            )
            await _proceed_to_consents(message, bot)
            return
        # Дать пользователю выбор: добавить ещё или завершить.
        await reply_to_user(
            message,
            bot,
            (
                f"Файл принят ({len(files)}/{_TRADITIONAL_MAX_FILES}). "
                "Добавьте ещё файл или завершите загрузку."
            ),
            bubbles=file_upload_bubbles(
                can_add_more=len(files) < _TRADITIONAL_MAX_FILES,
                can_finish=True,
            ),
        )
        return

    # AI / HANDMADE_TO_AI: ровно 1 файл — сразу к согласиям.
    await _proceed_to_consents(message, bot)


async def _proceed_to_consents(message: IncomingMessage, bot: Bot) -> None:
    """Переход в состояние согласий (§13).

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
    """Кнопка «Завершить загрузку» — переход к согласиям (§13)."""
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
        # §16 — без файла заявка не принимается.
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
    из §22, оригинальное имя в FS не сохраняется — только в meta.txt).
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
