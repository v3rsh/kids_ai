"""
Сервис локального файлового хранилища конкурса «Безопасные рисунки».

Отвечает за:
- создание структуры папок;
- переименование файлов по шаблону
  ``BR_ID-ParentName-ChildName-Track-AgeCategory[-Nx].ext``;
- генерацию текстовых метаданных (description.txt, meta.txt, reason.txt);
- перенос всей папки работы в ``99_rejected/`` при отклонении
  (файлы сохраняются для возможных споров);
- мониторинг занятого места и автопредупреждения по порогам WARN/BLOCK;
- сбор файлов заявки для модератора (`/files`).

Все пути относительны ``config.ATTACHMENTS_DIR``. В контейнере путь
монтируется как bind-mount ``./data/attachments`` → ``/app/data/attachments``
(см. ``docker-compose.yml``). Все имена каталогов латинские
(``01_traditional``/``02_ai``/``03_refine``, ``99_rejected``); имена папок
заявок содержат фамилию/имя родителя и ребёнка — это нормально на ext4.

Структура (после миграции `scripts/migrate_attachments_paths.py`):
```
ATTACHMENTS_DIR/
  <YYYY-MM-DD>/                  # дата подачи
    01_traditional/              # трек
      7-12/                      # возрастная категория
        BR-2026-NNNN_Фамилия_Имя_Ребёнок/
          BR-2026-NNNN_original.jpg
          description.txt
          meta.txt
    02_ai/
    03_refine/
  99_rejected/                   # отклонённые (полная папка работы)
    <YYYY-MM-DD>/                # дата модерации
      BR-2026-NNNN_.../
        BR-2026-NNNN_original.jpg  # файлы работы сохраняются
        description.txt
        meta.txt
        reason.txt
```

Async-стратегия:
- I/O-операции (`open`, `read`, `write`) выполняются через ``aiofiles``;
- mkdir/rename/unlink выполняются через ``asyncio.to_thread``, потому что
  стандартные ``os.*`` функции синхронные и блокируют event loop;
- ``shutil.disk_usage`` — тоже через ``asyncio.to_thread``;
- работа с БД (``DiskAlert``) — через ``get_session()``-фабрику.
"""
from __future__ import annotations

import asyncio
import io
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

import aiofiles
from loguru import logger

from config import (
    ATTACHMENTS_DIR,
    DISK_WARN_PCT,
)
from database.models import (
    AgeCategory,
    Application,
    ApplicationFile,
    DiskAlert,
    FileKind,
    IntakeMode,
    Track,
)

if TYPE_CHECKING:
    from pybotx.models.attachments import OutgoingAttachment


# =====================================================================
# Константы структуры хранилища
# =====================================================================

#: Папка для отклонённых заявок.
REJECTED_FOLDER_NAME = "99_rejected"

#: Legacy-имя сгенерированного превью (исключается из выдачи файлов).
PREVIEW_FILENAME = "preview.webp"

#: Имена служебных txt-файлов внутри папки заявки.
DESCRIPTION_TXT = "description.txt"
META_TXT = "meta.txt"
REASON_TXT = "reason.txt"

#: "Служебные" txt — исключаются из выдачи файлов (`/files`) и из
#: очистки изображений отклонённых (``purge_rejected_images``).
META_FILENAMES: frozenset[str] = frozenset(
    {DESCRIPTION_TXT, META_TXT, REASON_TXT}
)

#: Расширения файлов-изображений (lower case, с точкой). Используются
#: в ``purge_rejected_images``/``get_rejected_storage_stats``: при очистке
#: места удаляются только изображения, метаданные (txt) сохраняются.
REJECTED_IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".gif",
        ".bmp",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
    }
)

#: Префиксы треков для имён папок (латиница — стабильно работает на любых ФС
#: и не требует кавычек в shell-командах). Человеко-читаемые названия треков
#: для UI/реестра берутся из ``Track.value``.
_TRACK_FOLDER_PREFIX: dict[Track, str] = {
    Track.TRADITIONAL: "01_traditional",
    Track.AI: "02_ai",
    Track.HANDMADE_TO_AI: "03_refine",
}


_MOSCOW_TZ = timezone(timedelta(hours=3))


# =====================================================================
# Утилиты формирования путей
# =====================================================================


_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_segment(value: str) -> str:
    """Очистить кусок пути от запрещённых ФС-символов и пробелов.

    Пробелы заменяются на ``_`` (чтобы команды/пути в логах были без
    кавычек), запрещённые символы (``\\ / : * ? " < > |``) — удаляются.
    Пустая строка возвращается как ``_``.
    """
    cleaned = _INVALID_FILENAME_CHARS.sub("", value or "").strip()
    cleaned = _WHITESPACE_RE.sub("_", cleaned)
    cleaned = cleaned.strip("._-") or "_"
    return cleaned


def _format_track_folder(track: Track) -> str:
    """Имя папки трека (латиница — `01_traditional`/`02_ai`/`03_refine`)."""
    return _TRACK_FOLDER_PREFIX[track]


def _format_age_folder(age_category: AgeCategory) -> str:
    """Имя папки возрастной группы.

    Использует обычный дефис ``-`` вместо ``–`` (en-dash), чтобы пути
    не зависели от Unicode-нормализации файловой системы. В Excel-реестре
    и в `meta.txt` остаётся значение enum (например, «0–6»/«7–12»/«13–18»
    с en-dash).

    Хардкода списка категорий нет — функция работает через
    ``AgeCategory.value``, поэтому любые изменения состава возрастных
    категорий не требуют правок в storage.
    """
    return age_category.value.replace("–", "-")


def _split_parent_name(parent_full_name: str) -> tuple[str, str]:
    """Разбить ФИО родителя на (Фамилия, Имя) — отчество отбрасывается.

    Логика: первые два токена.
    Если токен один — возвращаем (этот токен, пустую строку).
    Если три и больше — берём первые два (фамилия, имя; отчество в имя папки не идёт).
    """
    parts = [
        _WHITESPACE_RE.sub("", p)
        for p in (parent_full_name or "").strip().split()
        if p.strip()
    ]
    if not parts:
        return ("Родитель", "")
    if len(parts) == 1:
        return (parts[0], "")
    return (parts[0], parts[1])


def _format_application_folder_name(app: Application) -> str:
    """Имя папки конкретной заявки.

    Шаблон: ``BR-2026-XXXX_Фамилия_Имя_Имя-ребёнка``. ``-`` внутри
    имени ребёнка заменяется на ``_``, чтобы внешне «двойной разделитель»
    с дефисом из ``BR-2026-XXXX`` не сбивал глаз.
    """
    surname, name = _split_parent_name(app.parent_full_name)
    child = _sanitize_segment(app.child_name or "Ребёнок")
    surname = _sanitize_segment(surname)
    name = _sanitize_segment(name) if name else ""

    segments = [app.br_id, surname]
    if name:
        segments.append(name)
    segments.append(child)
    return "_".join(segments)


def _application_date_folder(app: Application) -> str:
    """``YYYY-MM-DD`` от даты подачи (Europe/Moscow)."""
    dt = app.created_at or datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    moscow = dt.astimezone(_MOSCOW_TZ)
    return moscow.strftime("%Y-%m-%d")


def get_root_dir() -> Path:
    """Корень хранилища — сам ``ATTACHMENTS_DIR`` (без вложенного раздела)."""
    return ATTACHMENTS_DIR


def get_rejected_root() -> Path:
    """Корень папки отклонённых ``ATTACHMENTS_DIR/99_rejected``."""
    return get_root_dir() / REJECTED_FOLDER_NAME


def get_application_folder(app: Application) -> Path:
    """Полный путь к папке заявки в активном дереве.

    Не создаёт её на диске — только формирует путь. Создание — в
    ``create_application_folder``.
    """
    return (
        get_root_dir()
        / _application_date_folder(app)
        / _format_track_folder(app.track)
        / _format_age_folder(app.age_category)
        / _format_application_folder_name(app)
    )


def get_rejected_application_folder(
    app: Application,
    *,
    moderation_date: datetime | None = None,
) -> Path:
    """Путь, в который ``move_to_rejected`` переносит метаданные заявки.

    ``moderation_date`` — дата **модерации**, а не дата подачи.
    По умолчанию — текущее московское время.
    """
    md = moderation_date or datetime.now(_MOSCOW_TZ)
    if md.tzinfo is None:
        md = md.replace(tzinfo=_MOSCOW_TZ)
    day = md.astimezone(_MOSCOW_TZ).strftime("%Y-%m-%d")
    return (
        get_rejected_root() / day / _format_application_folder_name(app)
    )


# =====================================================================
# Создание папки заявки
# =====================================================================


async def create_application_folder(app: Application) -> Path:
    """Создать папку заявки по канонической структуре хранилища.

    Идемпотентно: ``mkdir(parents=True, exist_ok=True)``. Возвращает
    созданный (или уже существующий) путь.
    """
    folder = get_application_folder(app)

    def _mkdir() -> None:
        folder.mkdir(parents=True, exist_ok=True)

    await asyncio.to_thread(_mkdir)
    logger.info(
        "Создана папка заявки",
        br_id=app.br_id,
        path=str(folder.relative_to(ATTACHMENTS_DIR)),
    )
    return folder


# =====================================================================
# Переименование и сохранение файла
# =====================================================================


def _build_stored_filename(
    app: Application,
    kind: FileKind,
    angle_no: int | None,
    src_path: Path,
) -> str:
    """Сформировать ``stored_filename`` по детерминированному шаблону.

    Расширение берётся из исходного файла, приводится к нижнему регистру.
    Для ``ANGLE`` обязателен ``angle_no`` (1..4).
    """
    ext = (src_path.suffix or "").lower().lstrip(".")
    if not ext:
        # Защита от файлов без расширения — берём «bin», чтобы хоть как-то
        # сохранить, модератор разберётся.
        ext = "bin"

    if kind is FileKind.ORIGINAL:
        return f"{app.br_id}_original.{ext}"
    if kind is FileKind.ANGLE:
        if angle_no is None or angle_no < 1 or angle_no > 4:
            raise ValueError(
                "Для FileKind.ANGLE требуется angle_no в диапазоне 1..4"
            )
        return f"{app.br_id}_angle-{angle_no}.{ext}"
    if kind is FileKind.AI_IMAGE:
        return f"{app.br_id}_ai-image.{ext}"
    if kind is FileKind.DIPTYCH:
        return f"{app.br_id}_diptych.{ext}"
    raise ValueError(f"Неизвестный FileKind: {kind!r}")


async def rename_and_save_file(
    app: Application,
    kind: FileKind,
    angle_no: int | None,
    src_path: Path,
    original_filename: str | None = None,
) -> Path:
    """Переместить файл в папку заявки под детерминированным именем.

    Returns:
        Финальный путь файла в папке заявки.

    Notes:
        - Папка заявки создаётся при необходимости.
        - Если файл с таким именем уже есть (повторная обработка) — он
          перезаписывается; это безопасно, потому что ``stored_filename``
          детерминирован.
        - Запись в БД (``ApplicationFile``) — **не** делается этим методом,
          её выполняет вызывающий код (ветка A / user_files) после
          получения возвращённого пути.
    """
    folder = await create_application_folder(app)
    stored_filename = _build_stored_filename(app, kind, angle_no, src_path)
    dst = folder / stored_filename

    def _move() -> None:
        # Move работает и через границу ФС (через копирование+удаление).
        shutil.move(str(src_path), str(dst))

    await asyncio.to_thread(_move)
    logger.info(
        "Файл заявки сохранён",
        br_id=app.br_id,
        kind=kind.name,
        angle_no=angle_no,
        original_filename=original_filename or src_path.name,
        stored_filename=stored_filename,
    )
    return dst


# =====================================================================
# Метаданные в txt-файлах
# =====================================================================


def _format_contact(app: Application) -> str:
    """Контакт для meta.txt и карточки модератора.

    Приоритет: ``parent_contact`` (что родитель явно ввёл на шаге
    «Контакт» в анкете) → ``@ad_login`` (если CTS-логин известен) →
    ``HUID: <uuid>`` (последний fallback).
    """
    if getattr(app, "parent_contact", None):
        return app.parent_contact
    if app.parent_ad_login:
        return f"@{app.parent_ad_login}"
    return f"HUID: {app.parent_huid}"


def _format_submission_dt(app: Application) -> str:
    """Дата подачи в Europe/Moscow для meta.txt."""
    dt = app.created_at or datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    moscow = dt.astimezone(_MOSCOW_TZ)
    return moscow.strftime("%Y-%m-%d %H:%M (Europe/Moscow)")


def _format_files_block(files: list[ApplicationFile]) -> str:
    """Блок «Исходные имена файлов» в meta.txt."""
    if not files:
        return "Исходные имена файлов: (нет)"
    sorted_files = sorted(
        files,
        key=lambda f: (
            0 if f.kind is FileKind.ORIGINAL else
            1 if f.kind is FileKind.ANGLE else
            2 if f.kind is FileKind.AI_IMAGE else
            3
        ),
    )
    lines = ["Исходные имена файлов:"]
    for f in sorted_files:
        lines.append(f"  - {f.original_filename} → {f.stored_filename}")
    return "\n".join(lines)


async def write_description_txt(app: Application) -> Path:
    """Записать ``description.txt``.

    Только пользовательское описание, без шапки. UTF-8.
    """
    folder = await create_application_folder(app)
    path = folder / DESCRIPTION_TXT
    body = (app.description or "").strip() + "\n"

    async with aiofiles.open(path, "w", encoding="utf-8") as fp:
        await fp.write(body)

    logger.info("Сохранён description.txt", br_id=app.br_id)
    return path


async def write_meta_txt(
    app: Application,
    *,
    files: Sequence[ApplicationFile] | None = None,
) -> Path:
    """Записать ``meta.txt`` рядом с файлами заявки.

    Аргумент ``files`` — явный список файлов заявки. Передаётся
    вызывающим кодом (``handlers.user_confirm._materialize_files``)
    после ``register_application_files``. Если ``files=None`` — функция
    пробует ``app.files`` (актуально только когда ``app`` всё ещё
    привязан к сессии). Параметр введён, чтобы избежать
    ``DetachedInstanceError`` при попытке lazy-load relationship на
    expunge-объекте после закрытия сессии.
    """
    folder = await create_application_folder(app)
    path = folder / META_TXT
    files_block = _format_files_block(
        list(files if files is not None else (app.files or []))
    )

    body_lines = [
        f"ID заявки: {app.br_id}",
        f"Дата подачи: {_format_submission_dt(app)}",
        f"ФИО родителя: {app.parent_full_name}",
        f"Подразделение: {app.parent_division}",
        f"Контакт: {_format_contact(app)}",
        f"Имя ребёнка: {app.child_name}",
        f"Возраст: {app.child_age}",
        f"Возрастная категория: {app.age_category.value}",
        f"Трек: {app.track.value}",
        f"Название работы: {app.title}",
        f"Описание: {(app.description or '').strip()}",
        f"Статус модерации: {app.moderation_status.value}",
        f"Режим приёма: {app.intake_mode.value}",
    ]
    if app.cloud_link:
        body_lines.append(f"Ссылка на папку (cloud): {app.cloud_link}")
    body_lines.append(files_block)

    async with aiofiles.open(path, "w", encoding="utf-8") as fp:
        await fp.write("\n".join(body_lines) + "\n")

    logger.info("Сохранён meta.txt", br_id=app.br_id)
    return path


async def write_reason_txt(
    app: Application,
    reason: str,
    *,
    moderator_full_name: str | None = None,
    moderation_date: datetime | None = None,
    base_folder: Path | None = None,
) -> Path:
    """Записать ``reason.txt`` при отклонении заявки.

    Шапка фиксируется ботом, тело ``reason`` пишется дословно
    (см. ``handlers.moderator_actions.cmd_notify_reject``).

    Args:
        moderator_full_name: ФИО модератора (если None — пишем «модератор»).
        moderation_date: дата модерации (по умолчанию — текущее
            московское время).
        base_folder: куда писать reason.txt. По умолчанию — папка
            заявки в активном дереве; при ``move_to_rejected`` передаётся
            путь в ``99_rejected/...``.
    """
    md = moderation_date or datetime.now(_MOSCOW_TZ)
    if md.tzinfo is None:
        md = md.replace(tzinfo=_MOSCOW_TZ)
    day = md.astimezone(_MOSCOW_TZ).strftime("%Y-%m-%d")

    folder = base_folder
    if folder is None:
        folder = await create_application_folder(app)
    else:
        def _ensure_folder() -> None:
            folder.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_ensure_folder)

    path = folder / REASON_TXT

    body_lines = [
        f"ID заявки: {app.br_id}",
        f"Дата модерации: {day}",
        f"Модератор: {moderator_full_name or 'модератор'}",
        f"Причина отклонения: {reason.strip()}",
    ]
    if app.moderator_comment:
        body_lines.append(f"Комментарий: {app.moderator_comment.strip()}")

    async with aiofiles.open(path, "w", encoding="utf-8") as fp:
        await fp.write("\n".join(body_lines) + "\n")

    logger.info("Сохранён reason.txt", br_id=app.br_id, folder=str(folder))
    return path


async def read_rejection_reason(app: Application) -> str | None:
    """Прочитать причину отклонения из ``reason.txt`` в ``99_rejected/``.

    Используется как fallback для заявок, отклонённых до сохранения
    причины в ``Application.moderator_comment``. Ищет файл по
    ``br_id`` рекурсивно под ``get_rejected_root()``.
    """
    root = get_rejected_root()
    br_id = app.br_id
    reason_prefix = "Причина отклонения: "

    def _find_and_parse() -> str | None:
        if not root.is_dir():
            return None
        for reason_path in root.rglob(REASON_TXT):
            try:
                text = reason_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if br_id not in text:
                continue
            for line in text.splitlines():
                if line.startswith(reason_prefix):
                    value = line[len(reason_prefix) :].strip()
                    return value or None
        return None

    return await asyncio.to_thread(_find_and_parse)


# =====================================================================
# Отклонение: физическое удаление + перенос метаданных
# =====================================================================


async def delete_application_files(app: Application) -> int:
    """Физически удалить все файлы работы заявки.

    Удаляются файлы вида ``BR-XXXX_original.*``, ``BR-XXXX_angle-N.*``,
    ``BR-XXXX_ai-image.*``, ``BR-XXXX_diptych.*``.
    Метаданные (``description.txt``, ``meta.txt``,
    ``reason.txt``) НЕ трогаются.

    Returns:
        Число удалённых файлов (для логов и диагностики).

    Notes:
        Не рейзит, если папка не существует — возвращает 0.
    """
    folder = get_application_folder(app)
    if not folder.exists():
        logger.warning(
            "delete_application_files: папка заявки не найдена",
            br_id=app.br_id,
            folder=str(folder),
        )
        return 0

    def _delete() -> int:
        removed = 0
        for entry in folder.iterdir():
            if not entry.is_file():
                continue
            if entry.name in META_FILENAMES:
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError as exc:
                logger.error(
                    "Не удалось удалить файл заявки",
                    br_id=app.br_id,
                    file=str(entry),
                    error=str(exc),
                )
        return removed

    removed = await asyncio.to_thread(_delete)
    logger.info(
        "Удалены файлы работы заявки", br_id=app.br_id, files_removed=removed
    )
    return removed


async def move_to_rejected(
    app: Application,
    *,
    reason: str | None = None,
    moderator_full_name: str | None = None,
    moderation_date: datetime | None = None,
) -> Path:
    """Перенести всю папку заявки в ``99_rejected/`` (файлы сохраняются).

    Порядок:
    1. Переносим **всю папку заявки** целиком из активного дерева в
       ``99_rejected/<дата_модерации>/<имя>/`` (work-файлы, превью,
       метаданные — всё сохраняется для возможных споров).
    2. Пишем/перезаписываем ``reason.txt`` в папке назначения.

    Файлы работы **не удаляются** — это сознательное решение ради
    сохранения материалов (см. план «Перенос отклонённых в 99»).

    БД-операцию (смена ``moderation_status`` на ``OTKLONENO``) вызывающий
    код коммитит только **после** успешного завершения этого метода.
    После переноса вызывающий обязан вызвать
    ``applications.remap_application_file_paths`` — чтобы ``relative_path``
    в БД указывал на новое расположение в ``99_rejected/``.

    Returns:
        Путь к папке заявки внутри ``99_rejected/...``.

    Notes:
        Идемпотентность: если папка назначения уже существует
        (повторный вызов) — содержимое исходной папки домерживается
        пофайлово с перезаписью, исходная папка удаляется. Если активная
        папка отсутствует — log warning + продолжение (только reason.txt).
    """
    src_folder = get_application_folder(app)
    dst_folder = get_rejected_application_folder(
        app, moderation_date=moderation_date
    )

    def _move_folder() -> None:
        if not src_folder.exists():
            logger.warning(
                "move_to_rejected: исходная папка заявки отсутствует",
                br_id=app.br_id,
                folder=str(src_folder),
            )
            dst_folder.mkdir(parents=True, exist_ok=True)
            return

        dst_folder.parent.mkdir(parents=True, exist_ok=True)

        if not dst_folder.exists():
            # Быстрый путь: переносим папку целиком одним вызовом.
            shutil.move(str(src_folder), str(dst_folder))
            return

        # Merge-фолбэк (повторный вызов / папка уже существует):
        # переносим содержимое пофайлово с перезаписью.
        for entry in src_folder.iterdir():
            target = dst_folder / entry.name
            try:
                if target.exists():
                    target.unlink()
                shutil.move(str(entry), str(target))
            except OSError as exc:
                logger.error(
                    "move_to_rejected: не удалось перенести файл",
                    br_id=app.br_id,
                    file=entry.name,
                    error=str(exc),
                )
        try:
            src_folder.rmdir()
        except OSError:
            # Не пусто (непредвиденный остаток) — оставляем как есть.
            pass

    await asyncio.to_thread(_move_folder)

    if reason is not None:
        await write_reason_txt(
            app,
            reason,
            moderator_full_name=moderator_full_name,
            moderation_date=moderation_date,
            base_folder=dst_folder,
        )

    logger.info(
        "Заявка перенесена в 99_rejected",
        br_id=app.br_id,
        rejected_folder=str(dst_folder.relative_to(ATTACHMENTS_DIR)),
    )
    return dst_folder


def resolve_application_folder(app: Application) -> Path:
    """Актуальная папка заявки: активное дерево или ``99_rejected/``.

    Для отклонённых заявок папка перенесена в ``99_rejected/`` —
    ``get_application_folder`` укажет на уже несуществующий путь. Этот
    хелпер возвращает фактическое расположение:

    1. ``get_application_folder(app)`` — если существует, вернуть её;
    2. иначе ищем папку с именем ``_format_application_folder_name(app)``
       под ``get_rejected_root()`` (рекурсивно по датам модерации);
    3. если ничего не найдено — возвращаем путь активного дерева
       (вызывающий сам обработает отсутствие).
    """
    active = get_application_folder(app)
    if active.exists():
        return active

    target_name = _format_application_folder_name(app)
    rejected_root = get_rejected_root()
    if rejected_root.is_dir():
        for entry in rejected_root.rglob(target_name):
            if entry.is_dir():
                return entry
    return active


# =====================================================================
# Очистка изображений отклонённых работ + счётчик хранилища
# =====================================================================


@dataclass(frozen=True)
class RejectedStorageStats:
    """Статистика по содержимому ``99_rejected/``.

    Используется для счётчика в админ-экранах (``/disk``, ``/admin_state``)
    и в confirm-приглашении перед очисткой изображений.
    """

    folders_count: int = 0
    image_files_count: int = 0
    image_bytes: int = 0
    meta_files_count: int = 0
    meta_bytes: int = 0
    other_files_count: int = 0
    other_bytes: int = 0

    @property
    def total_bytes(self) -> int:
        return self.image_bytes + self.meta_bytes + self.other_bytes


@dataclass(frozen=True)
class PurgeRejectedResult:
    """Итог очистки изображений отклонённых работ."""

    files_removed: int = 0
    bytes_freed: int = 0
    folders_touched: int = 0
    errors: int = 0


def _is_image_file(name: str) -> bool:
    """True ⇒ имя файла имеет расширение изображения (lower case)."""
    return Path(name).suffix.lower() in REJECTED_IMAGE_EXTENSIONS


def _iter_rejected_app_folders(br_id: str | None = None) -> list[Path]:
    """BR-папки под ``99_rejected/`` (все или фильтр по ``br_id``).

    ``br_id`` сопоставляется по префиксу имени папки
    (``BR-2026-NNNN_...``) без учёта регистра.
    """
    root = get_rejected_root()
    if not root.is_dir():
        return []

    needle = (br_id or "").strip().upper()
    found: list[Path] = []
    for entry in root.rglob("*"):
        if not entry.is_dir():
            continue
        if not entry.name.upper().startswith("BR-"):
            continue
        if needle and not entry.name.upper().startswith(needle):
            continue
        found.append(entry)
    found.sort()
    return found


async def get_rejected_storage_stats() -> RejectedStorageStats:
    """Подсчитать объём и состав ``99_rejected/`` (только ФС, без БД).

    Разделяет файлы на изображения / метаданные (txt) / прочие, чтобы
    показать, сколько места освободит ``purge_rejected_images``.
    """

    def _scan() -> RejectedStorageStats:
        folders = _iter_rejected_app_folders()
        image_count = image_bytes = 0
        meta_count = meta_bytes = 0
        other_count = other_bytes = 0
        for folder in folders:
            for entry in folder.iterdir():
                if not entry.is_file():
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                if entry.name in META_FILENAMES:
                    meta_count += 1
                    meta_bytes += size
                elif _is_image_file(entry.name):
                    image_count += 1
                    image_bytes += size
                else:
                    other_count += 1
                    other_bytes += size
        return RejectedStorageStats(
            folders_count=len(folders),
            image_files_count=image_count,
            image_bytes=image_bytes,
            meta_files_count=meta_count,
            meta_bytes=meta_bytes,
            other_files_count=other_count,
            other_bytes=other_bytes,
        )

    return await asyncio.to_thread(_scan)


async def purge_rejected_images(
    *,
    br_id: str | None = None,
    dry_run: bool = False,
) -> PurgeRejectedResult:
    """Удалить изображения из ``99_rejected/`` (метаданные сохраняются).

    Опциональный инструмент на случай нехватки места: физически удаляет
    только файлы-изображения (``REJECTED_IMAGE_EXTENSIONS``). Текстовые
    метаданные (``description.txt``/``meta.txt``/``reason.txt``) и прочие
    не-изображения **не трогаются**.

    Записи ``application_files`` в БД **не изменяются** — после очистки
    ``/files`` для таких заявок вернёт «файл отсутствует», но карточка,
    метаданные и причина отклонения остаются доступны.

    Args:
        br_id: если задан — очистить только эту заявку; иначе весь
            ``99_rejected/``.
        dry_run: True ⇒ только подсчёт, без удаления.

    Returns:
        ``PurgeRejectedResult`` с числом удалённых файлов, освобождённых
        байт, затронутых папок и ошибок.
    """

    def _purge() -> PurgeRejectedResult:
        folders = _iter_rejected_app_folders(br_id)
        files_removed = bytes_freed = folders_touched = errors = 0
        for folder in folders:
            touched = False
            for entry in folder.iterdir():
                if not entry.is_file():
                    continue
                if entry.name in META_FILENAMES:
                    continue
                if not _is_image_file(entry.name):
                    continue
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                if dry_run:
                    files_removed += 1
                    bytes_freed += size
                    touched = True
                    continue
                try:
                    entry.unlink()
                    files_removed += 1
                    bytes_freed += size
                    touched = True
                    logger.info(
                        "Удалено изображение отклонённой работы",
                        folder=folder.name,
                        file=entry.name,
                        bytes_freed=size,
                    )
                except OSError as exc:
                    errors += 1
                    logger.error(
                        "Не удалось удалить изображение отклонённой работы",
                        folder=folder.name,
                        file=entry.name,
                        error=str(exc),
                    )
            if touched:
                folders_touched += 1
        return PurgeRejectedResult(
            files_removed=files_removed,
            bytes_freed=bytes_freed,
            folders_touched=folders_touched,
            errors=errors,
        )

    result = await asyncio.to_thread(_purge)
    logger.info(
        "Очистка изображений отклонённых работ завершена",
        br_id=br_id or "ALL",
        dry_run=dry_run,
        files_removed=result.files_removed,
        bytes_freed=result.bytes_freed,
        folders_touched=result.folders_touched,
        errors=result.errors,
    )
    return result


# =====================================================================
# Мониторинг диска (предупреждение WARN, без авто-действий)
# =====================================================================


def get_disk_usage_bytes() -> tuple[int, int]:
    """``(used_bytes, total_bytes)`` для ``ATTACHMENTS_DIR``.

    Считается через ``shutil.disk_usage(ATTACHMENTS_DIR)``, который
    обращается к точке монтирования тома (а не подсчитывает размеры
    отдельных файлов рекурсивно — это было бы дорого).

    Returns:
        Кортеж ``(used, total)``. Если каталог не существует —
        ``(0, 0)``; вызывающий должен трактовать это как «не блокируем».
    """
    if not ATTACHMENTS_DIR.exists():
        return (0, 0)
    usage = shutil.disk_usage(str(ATTACHMENTS_DIR))
    return (usage.used, usage.total)


def get_disk_usage_pct() -> float:
    """Процент использования (0..100) для ``ATTACHMENTS_DIR``."""
    used, total = get_disk_usage_bytes()
    if total <= 0:
        return 0.0
    return round((used / total) * 100.0, 2)


def estimate_hours_left(
    *,
    free_bytes: int,
    consumed_bytes_last_hour: float,
) -> float:
    """Оценить, сколько часов осталось до 100 %.

    Если последний час не было поступлений — возвращает ``float('inf')``.
    """
    if consumed_bytes_last_hour <= 0:
        return float("inf")
    return round(free_bytes / consumed_bytes_last_hour, 1)


async def _was_alert_sent_recently(threshold_pct: int) -> bool:
    """Дедупликация alert'ов: True, если за последние 24 ч уже слали.

    Используется в ``check_and_alert_disk``, чтобы не спамить чат
    модерации. Запись о факте отправки делает вызывающий код
    после успешной нотификации.
    """
    try:
        from sqlalchemy import select

        from database.db import get_session
    except ImportError:  # pragma: no cover — safety net на этапе boot'а
        return False

    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with get_session()() as session:
        result = await session.execute(
            select(DiskAlert.id)
            .where(DiskAlert.threshold_pct == threshold_pct)
            .where(DiskAlert.created_at >= cutoff)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


async def _record_alert(threshold_pct: int) -> None:
    """Записать факт отправки alert'а в ``DiskAlert``."""
    try:
        from database.db import get_session
    except ImportError:  # pragma: no cover
        return

    async with get_session()() as session:
        session.add(DiskAlert(threshold_pct=threshold_pct))
        await session.commit()


async def _disk_monitor_loop(bot, interval_sec: int) -> None:
    """Бесконечный цикл периодического вызова ``check_and_alert_disk``.

    Запускается из ``app/main.py`` (lifespan). Sleep сначала — чтобы
    первый замер шёл уже после полной инициализации pybotx. Cancel'ом
    выходит из цикла без traceback'ов в логах.
    """
    import asyncio

    logger.info(
        "Запущен фоновый монитор диска",
        interval_sec=interval_sec,
    )
    try:
        while True:
            try:
                await asyncio.sleep(interval_sec)
                await check_and_alert_disk(bot=bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Ошибка в цикле мониторинга диска")
    except asyncio.CancelledError:
        logger.info("Фоновый монитор диска остановлен")
        raise


def start_disk_monitor_task(bot, interval_sec: int):
    """Запустить фоновую задачу мониторинга диска и вернуть Task.

    Использует ``asyncio.create_task`` — рассчитано на запуск из
    lifespan-блока, когда event loop уже активен.
    """
    import asyncio

    return asyncio.create_task(
        _disk_monitor_loop(bot, interval_sec),
        name="disk_monitor",
    )


async def check_and_alert_disk(bot=None) -> None:
    """Точка вызова после каждой загрузки файла.

    Действие: если занято ≥ ``DISK_WARN_PCT`` — шлёт предупреждение в чат
    модерации (с дедупом 24 ч). Никаких автоматических действий с
    режимом приёма не выполняется — переключение в ``LINKS`` делает
    только админ вручную (``/intake_mode`` / ``/admin_danger``).

    Если ``bot`` не передан (вызов из smoke-теста или scheduler без
    инициализированного pybotx) — нотификации пропускаются.
    """
    used, total = get_disk_usage_bytes()
    if total <= 0:
        return
    pct = (used / total) * 100.0
    free_bytes = total - used

    if pct >= DISK_WARN_PCT:
        if bot is None:
            return
        if await _was_alert_sent_recently(DISK_WARN_PCT):
            return
        try:
            from services.notifications import (
                notify_moderation_chat_disk_alert,
            )

            await notify_moderation_chat_disk_alert(
                bot,
                threshold_pct=DISK_WARN_PCT,
                free_mb=int(free_bytes / (1024 * 1024)),
                hours_left=-1.0,
            )
            await _record_alert(DISK_WARN_PCT)
        except Exception:
            logger.exception("Не удалось отправить disk alert")


# =====================================================================
# Команда /files модератора
# =====================================================================


async def get_application_files_for_chat(
    app: Application,
) -> list["OutgoingAttachment"] | None:
    """Вернуть OutgoingAttachment-список файлов для команды ``/files``.

    Поведение зависит от режима приёма заявки (``Application.intake_mode``):
    - ``FILES`` — собирает реальные файлы из папки заявки и возвращает
      список ``OutgoingAttachment`` (модераторский хендлер пересылает их в чат);
    - ``LINKS`` — возвращает ``None`` (модераторский хендлер сам отправит
      текст «Ссылка на папку: …» по ``app.cloud_link``).

    Returns:
        list[OutgoingAttachment] | None — ``None`` в режиме ``LINKS``.

    Notes:
        Метаданные (``description.txt`` / ``meta.txt`` / ``reason.txt``)
        в выгрузку НЕ включаются — это служебные файлы хранилища.
    """
    if app.intake_mode is IntakeMode.LINKS:
        return None

    try:
        from pybotx.models.attachments import OutgoingAttachment
    except ImportError:  # pragma: no cover
        logger.error("pybotx не доступен; /files отдать не сможем")
        return []

    folder = resolve_application_folder(app)
    if not folder.exists():
        logger.warning(
            "get_application_files_for_chat: папка заявки не найдена",
            br_id=app.br_id,
            folder=str(folder),
        )
        return []

    def _list_files() -> list[Path]:
        items: list[Path] = []
        for entry in sorted(folder.iterdir()):
            if not entry.is_file():
                continue
            if entry.name in META_FILENAMES:
                continue
            if entry.name == PREVIEW_FILENAME:
                continue
            items.append(entry)
        return items

    paths = await asyncio.to_thread(_list_files)

    attachments: list[OutgoingAttachment] = []
    for path in paths:
        try:
            async with aiofiles.open(path, "rb") as fp:
                content = await fp.read()
            attachments.append(
                OutgoingAttachment(content=content, filename=path.name)
            )
        except OSError as exc:
            logger.error(
                "Не удалось прочитать файл для /files",
                br_id=app.br_id,
                file=str(path),
                error=str(exc),
            )

    return attachments


async def cleanup_old_disk_alerts(*, days: int = 30) -> int:
    """Удалить записи ``disk_alerts`` старше ``days`` дней.

    Returns:
        Число удалённых строк.
    """
    from sqlalchemy import delete

    from database.db import get_session

    cutoff = datetime.utcnow() - timedelta(days=days)
    async with get_session()() as session:
        result = await session.execute(
            delete(DiskAlert).where(DiskAlert.created_at < cutoff)
        )
        await session.commit()
        return int(result.rowcount or 0)


__all__ = [
    "REJECTED_FOLDER_NAME",
    "PREVIEW_FILENAME",
    "DESCRIPTION_TXT",
    "META_TXT",
    "REASON_TXT",
    "META_FILENAMES",
    "REJECTED_IMAGE_EXTENSIONS",
    # Пути
    "get_root_dir",
    "get_rejected_root",
    "get_application_folder",
    "get_rejected_application_folder",
    "resolve_application_folder",
    # CRUD
    "create_application_folder",
    "rename_and_save_file",
    "write_description_txt",
    "write_meta_txt",
    "write_reason_txt",
    "read_rejection_reason",
    "move_to_rejected",
    "delete_application_files",
    # Очистка отклонённых
    "RejectedStorageStats",
    "PurgeRejectedResult",
    "get_rejected_storage_stats",
    "purge_rejected_images",
    # Disk
    "get_disk_usage_bytes",
    "get_disk_usage_pct",
    "estimate_hours_left",
    "check_and_alert_disk",
    "start_disk_monitor_task",
    "cleanup_old_disk_alerts",
    # Preview / files
    "get_application_files_for_chat",
]
