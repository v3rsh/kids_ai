"""
Сервис локального файлового хранилища конкурса «Безопасные рисунки».

Отвечает за:
- создание структуры папок;
- переименование файлов по шаблону
  ``BR_ID-ParentName-ChildName-Track-AgeCategory[-Nx].ext``;
- генерацию текстовых метаданных (description.txt, meta.txt, reason.txt);
- физическое удаление файлов работы при отклонении;
- генерацию превью для жюри;
- мониторинг занятого места и автопредупреждения по порогам WARN/BLOCK;
- сбор файлов заявки для модератора (`/files`).

Все пути относительны ``config.ATTACHMENTS_DIR``. В контейнере — именованный
том ``attachments_volume`` (см. ``docker-compose.yml``). Файлы внутри тома
хранятся под русскими именами треков (рекомендуемая структура);
запасной вариант латинских имён имеется на случай ФС, где кириллица
в путях нестабильна — на ext4 он не нужен.

Async-стратегия:
- I/O-операции (`open`, `read`, `write`) выполняются через ``aiofiles``;
- mkdir/rename/unlink выполняются через ``asyncio.to_thread``, потому что
  стандартные ``os.*`` функции синхронные и блокируют event loop;
- ``shutil.disk_usage`` и Pillow.thumbnail — тоже через ``asyncio.to_thread``;
- работа с БД (``DiskAlert``) — через ``get_session()``-фабрику.
"""
from __future__ import annotations

import asyncio
import io
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import aiofiles
from loguru import logger

from config import (
    ATTACHMENTS_DIR,
    DISK_BLOCK_PCT,
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

#: Корень всех заявок (название папки видно администраторам через NextCloud).
ROOT_FOLDER_NAME = "Безопасные рисунки"

#: Папка для отклонённых заявок.
REJECTED_FOLDER_NAME = "99_Отклонено"

#: Имя файла превью для жюри.
PREVIEW_FILENAME = "preview.webp"
PREVIEW_MAX_SIDE_PX = 1280

#: Имена служебных txt-файлов внутри папки заявки.
DESCRIPTION_TXT = "description.txt"
META_TXT = "meta.txt"
REASON_TXT = "reason.txt"

#: "Служебные" txt — те, что НЕ удаляются при move_to_rejected.
META_FILENAMES: frozenset[str] = frozenset(
    {DESCRIPTION_TXT, META_TXT, REASON_TXT}
)

#: Префиксы треков для имён папок.
_TRACK_FOLDER_PREFIX: dict[Track, str] = {
    Track.TRADITIONAL: "01_Традиционное_рисование",
    Track.AI: "02_ИИ_рисунок",
    Track.HANDMADE_TO_AI: "03_От_руки_к_ИИ",
}

#: Латинский fallback для запасного варианта имён треков.
#: Используется только если ``ATTACHMENTS_USE_LATIN_TRACKS`` (env)
#: переключён в ``true`` — на ext4 не нужен, но оставлен для NextCloud-сценария.
_TRACK_FOLDER_PREFIX_LATIN: dict[Track, str] = {
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


def _format_track_folder(track: Track, *, use_latin: bool = False) -> str:
    """Имя папки трека (русский по умолчанию или латинский fallback)."""
    mapping = _TRACK_FOLDER_PREFIX_LATIN if use_latin else _TRACK_FOLDER_PREFIX
    return mapping[track]


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
    """Корень хранилища ``ATTACHMENTS_DIR/Безопасные рисунки``."""
    return ATTACHMENTS_DIR / ROOT_FOLDER_NAME


def get_rejected_root() -> Path:
    """Корень папки отклонённых ``ATTACHMENTS_DIR/Безопасные рисунки/99_Отклонено``."""
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
    """Контакт в meta.txt: ``@ad_login`` или ``HUID: <uuid>``."""
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


async def write_meta_txt(app: Application) -> Path:
    """Записать ``meta.txt`` рядом с файлами заявки."""
    folder = await create_application_folder(app)
    path = folder / META_TXT
    files_block = _format_files_block(list(app.files or []))

    body_lines = [
        f"ID заявки: {app.br_id}",
        f"Дата подачи: {_format_submission_dt(app)}",
        f"ФИО родителя: {app.parent_full_name}",
        f"Подразделение: {app.parent_division}",
        f"Контакт (eXpress): {_format_contact(app)}",
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
            путь в ``99_Отклонено/...``.
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


# =====================================================================
# Отклонение: физическое удаление + перенос метаданных
# =====================================================================


async def delete_application_files(app: Application) -> int:
    """Физически удалить все файлы работы заявки.

    Удаляются файлы вида ``BR-XXXX_original.*``, ``BR-XXXX_angle-N.*``,
    ``BR-XXXX_ai-image.*``, ``BR-XXXX_diptych.*`` **и** превью
    ``preview.webp``. Метаданные (``description.txt``, ``meta.txt``,
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
    """Переместить метаданные заявки в ``99_Отклонено/``.

    Порядок (атомарность относительно БД):
    1. Создаём папку назначения в ``99_Отклонено/<дата_модерации>/<имя>/``.
    2. Пишем туда ``reason.txt`` (с дословным текстом ``reason``, если
       передан; иначе шапка без причины — её должен поставить вызывающий).
    3. Переносим ``description.txt`` и ``meta.txt`` из активной папки
       в папку отклонённых.
    4. Физически удаляем все файлы работы из активной папки.
    5. Удаляем пустую активную папку (если в ней не осталось ничего).

    БД-операцию (смена ``moderation_status`` на ``OTKLONENO``) вызывающий
    код коммитит только **после** успешного завершения этого метода.

    Returns:
        Путь к папке заявки внутри ``99_Отклонено/...``.

    Notes:
        Метод толерантен к повторным вызовам: если папка отклонённых
        уже существует, метаданные перезаписываются; если активная
        папка уже удалена — log warning + продолжение.
    """
    src_folder = get_application_folder(app)
    dst_folder = get_rejected_application_folder(
        app, moderation_date=moderation_date
    )

    def _ensure_dst() -> None:
        dst_folder.mkdir(parents=True, exist_ok=True)

    await asyncio.to_thread(_ensure_dst)

    if reason is not None:
        await write_reason_txt(
            app,
            reason,
            moderator_full_name=moderator_full_name,
            moderation_date=moderation_date,
            base_folder=dst_folder,
        )

    def _move_metafiles() -> None:
        if not src_folder.exists():
            logger.warning(
                "move_to_rejected: исходная папка заявки отсутствует",
                br_id=app.br_id,
                folder=str(src_folder),
            )
            return
        for name in (DESCRIPTION_TXT, META_TXT):
            src = src_folder / name
            if not src.exists():
                continue
            dst = dst_folder / name
            try:
                shutil.move(str(src), str(dst))
            except OSError as exc:
                logger.error(
                    "Не удалось перенести метафайл",
                    br_id=app.br_id,
                    file=name,
                    error=str(exc),
                )

    await asyncio.to_thread(_move_metafiles)
    await delete_application_files(app)

    def _try_remove_empty_src() -> None:
        if src_folder.exists():
            try:
                # rmdir не удалит непустую папку — это безопасно.
                src_folder.rmdir()
            except OSError:
                # Не пусто (например, превью или непредвиденный файл) —
                # оставляем как есть, отдельный лог не нужен.
                pass

    await asyncio.to_thread(_try_remove_empty_src)

    logger.info(
        "Заявка перенесена в 99_Отклонено",
        br_id=app.br_id,
        rejected_folder=str(dst_folder.relative_to(ATTACHMENTS_DIR)),
    )
    return dst_folder


# =====================================================================
# Мониторинг диска и автопереход в LINKS
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


def should_block_intake() -> bool:
    """True ⇒ заполнение ≥ ``DISK_BLOCK_PCT`` (триггер авто-перехода в LINKS)."""
    return get_disk_usage_pct() >= DISK_BLOCK_PCT


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

    Действия:
    - если занято ≥ ``DISK_BLOCK_PCT`` и режим ещё ``FILES`` — переключает
      его в ``LINKS`` через ``intake_mode.maybe_auto_switch_to_links``
      и шлёт alert 95 % (если за 24 ч ещё не слали);
    - если занято ≥ ``DISK_WARN_PCT`` — шлёт alert 80 % (с дедупом 24 ч).

    Если ``bot`` не передан (вызов из smoke-теста или scheduler без
    инициализированного pybotx) — нотификации пропускаются, остаются
    только лог + автопереключение режима.
    """
    used, total = get_disk_usage_bytes()
    if total <= 0:
        return
    pct = (used / total) * 100.0
    free_bytes = total - used

    if pct >= DISK_BLOCK_PCT:
        try:
            from services.intake_mode import maybe_auto_switch_to_links

            await maybe_auto_switch_to_links()
        except Exception:
            logger.exception("Автопереключение в режим LINKS не удалось")

        if bot is not None and not await _was_alert_sent_recently(DISK_BLOCK_PCT):
            try:
                from services.notifications import (
                    notify_moderation_chat_disk_alert,
                )

                await notify_moderation_chat_disk_alert(
                    bot,
                    threshold_pct=DISK_BLOCK_PCT,
                    free_mb=int(free_bytes / (1024 * 1024)),
                    hours_left=0.0,
                )
                await _record_alert(DISK_BLOCK_PCT)
            except Exception:
                logger.exception("Не удалось отправить alert 95 %")
        return

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
            logger.exception("Не удалось отправить alert 80 %")


# =====================================================================
# Превью для жюри
# =====================================================================


def _find_source_image_for_preview(folder: Path) -> Path | None:
    """Выбрать исходник, из которого построить превью.

    Приоритет: ``*_original.*`` > ``*_diptych.*`` > ``*_ai-image.*`` >
    ``*_angle-1.*`` (затем 2, 3, 4). Если ничего из этих имён нет —
    None (жюри в этом случае получит текстовую ссылку при ``LINKS``
    или модератор увидит ошибку при ``FILES``).
    """
    if not folder.exists():
        return None

    candidates: list[tuple[int, Path]] = []
    for entry in folder.iterdir():
        if not entry.is_file():
            continue
        name = entry.name.lower()
        if "_original." in name:
            candidates.append((0, entry))
        elif "_diptych." in name:
            candidates.append((1, entry))
        elif "_ai-image." in name:
            candidates.append((2, entry))
        elif "_angle-" in name:
            # angle-1 → 10, angle-2 → 11 и т.д.
            try:
                angle = int(name.split("_angle-")[1].split(".")[0])
            except (IndexError, ValueError):
                angle = 99
            candidates.append((10 + angle, entry))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _generate_preview_sync(source: Path, dst: Path) -> Path | None:
    """Синхронная генерация превью (запускается через ``to_thread``).

    Pillow не умеет async; HEIC требует ``pillow-heif`` (не входит в
    зависимости MVP). При ошибке открытия — возвращает None.
    """
    try:
        from PIL import Image  # noqa: WPS433 — namespaced thirdparty
    except ImportError:
        logger.error("Pillow не установлен; превью не сгенерировано")
        return None

    try:
        with Image.open(source) as img:
            img = img.convert("RGB")
            img.thumbnail(
                (PREVIEW_MAX_SIDE_PX, PREVIEW_MAX_SIDE_PX),
                Image.Resampling.LANCZOS,
            )
            dst.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst, format="WEBP", quality=85, method=6)
    except Exception as exc:
        logger.warning(
            "Не удалось сгенерировать превью",
            source=str(source),
            error=str(exc),
        )
        return None
    return dst


async def get_preview_path(app: Application) -> Path | None:
    """Путь к ``preview.webp`` для жюри.

    Если файла нет — генерируется лениво из исходника. Возвращает
    None, если исходника для превью нет (например, в режиме ``LINKS``
    или папка пуста).
    """
    folder = get_application_folder(app)
    preview = folder / PREVIEW_FILENAME

    if preview.exists():
        return preview

    source = await asyncio.to_thread(_find_source_image_for_preview, folder)
    if source is None:
        return None

    return await asyncio.to_thread(_generate_preview_sync, source, preview)


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

    folder = get_application_folder(app)
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


__all__ = [
    "ROOT_FOLDER_NAME",
    "REJECTED_FOLDER_NAME",
    "PREVIEW_FILENAME",
    "PREVIEW_MAX_SIDE_PX",
    "DESCRIPTION_TXT",
    "META_TXT",
    "REASON_TXT",
    "META_FILENAMES",
    # Пути
    "get_root_dir",
    "get_rejected_root",
    "get_application_folder",
    "get_rejected_application_folder",
    # CRUD
    "create_application_folder",
    "rename_and_save_file",
    "write_description_txt",
    "write_meta_txt",
    "write_reason_txt",
    "move_to_rejected",
    "delete_application_files",
    # Disk
    "get_disk_usage_bytes",
    "get_disk_usage_pct",
    "should_block_intake",
    "estimate_hours_left",
    "check_and_alert_disk",
    "start_disk_monitor_task",
    # Preview / files
    "get_preview_path",
    "get_application_files_for_chat",
]
