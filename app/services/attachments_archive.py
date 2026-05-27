"""
Архивация каталога ``ATTACHMENTS_DIR`` на локальный диск.

Запасной канал к ZIP-выгрузке в DM (``services/attachments_export.py``):
вместо сборки ZIP в RAM и отправки в чат, копирует структуру
``ATTACHMENTS_DIR`` в ``ARCHIVE_DIR/BR-{YEAR}_{timestamp}/`` через
``shutil.copy2`` и пишет рядом ``archive_manifest.json`` + ``summary.txt``.

Pre-flight: перед стартом копирования сравниваем расчётный занятый
объём диска ПОСЛЕ копии с ``DISK_BLOCK_PCT``; при превышении —
``ArchiveBudgetExceeded`` без побочных эффектов.

Phase 1B: только сервис + unit-тесты. Хендлеры/кнопки/confirm-флоу
подключаются в Phase 2 (см. plan §2, §8.3).
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Union

from loguru import logger
from sqlalchemy import select

from config import (
    ARCHIVE_DIR,
    ATTACHMENTS_DIR,
    COMPETITION_YEAR,
    DISK_BLOCK_PCT,
)
from database.db import get_session
from database.models import Application
from services.storage import get_disk_usage_bytes


# =====================================================================
# Константы
# =====================================================================

_BYTES_IN_GB = 1024 ** 3

#: Любая папка вида ``BR-YYYY-N…`` считается каталогом одной заявки.
#: Сопоставляется по имени папки (без пути) — этого достаточно: имена
#: формируются в ``services.storage._format_application_folder_name``
#: по детерминированному шаблону ``BR-{YEAR}-NNNN_<Фамилия>…``.
_BR_ID_RE = re.compile(r"^(BR-\d{4}-\d+)(?:_|$)")


# =====================================================================
# DTO disk-budget
# =====================================================================


@dataclass(frozen=True)
class ArchiveBudget:
    """Расчёт disk-budget перед архивацией.

    Поля — в байтах; ``*_gb`` / ``*_pct`` — производные свойства для
    отображения в текстовом приглашении (см. ``format_archive_budget_text``).
    """

    attachments_bytes: int
    total_bytes: int
    used_bytes: int
    free_bytes: int
    after_used_bytes: int
    block_pct: int

    # ----- Производные значения (для UI) -----

    @property
    def used_gb(self) -> float:
        return self.used_bytes / _BYTES_IN_GB

    @property
    def attachments_gb(self) -> float:
        return self.attachments_bytes / _BYTES_IN_GB

    @property
    def free_gb(self) -> float:
        return self.free_bytes / _BYTES_IN_GB

    @property
    def after_used_gb(self) -> float:
        return self.after_used_bytes / _BYTES_IN_GB

    @property
    def used_pct(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.used_bytes / self.total_bytes * 100.0

    @property
    def free_pct(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.free_bytes / self.total_bytes * 100.0

    @property
    def after_pct(self) -> float:
        if self.total_bytes <= 0:
            return 100.0
        return self.after_used_bytes / self.total_bytes * 100.0


class ArchiveBudgetExceeded(RuntimeError):
    """Pre-flight отказ: копия превышает ``DISK_BLOCK_PCT``.

    Несёт исходную ``ArchiveBudget`` для UI — хендлер показывает её
    в confirm-приглашении и не выводит кнопку «Да, выполнить»
    (см. plan §8.3).
    """

    def __init__(self, budget: ArchiveBudget) -> None:
        super().__init__(
            f"После архивации диск будет заполнен на "
            f"{budget.after_pct:.1f}% (порог {budget.block_pct}%)."
        )
        self.budget = budget


# ``progress_cb(index, total, br_id, size_bytes)`` — callback по факту
# завершения копирования одного BR-ID-каталога. Может быть sync или async.
ProgressCb = Callable[
    [int, int, str, int],
    Union[Awaitable[None], None],
]


# =====================================================================
# Disk-budget
# =====================================================================


def _dir_size_bytes(path: Path) -> int:
    """Рекурсивный sum(stat.st_size) по всем файлам.

    Только файлы; симлинки на каталоги не рекурсим (rglob их и не
    обходит по умолчанию). Возвращает 0, если корня нет.
    """
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if not item.is_file():
            continue
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return total


async def estimate_archive_budget() -> ArchiveBudget:
    """Оценить объём копии и заполнение диска после архивации.

    Подсчёт ``attachments_bytes`` — ``du``-эквивалент по
    ``ATTACHMENTS_DIR`` (через ``rglob`` + ``stat``).
    Свободное место — через ``services.storage.get_disk_usage_bytes``
    (он отдаёт ``(used, total)`` от ``shutil.disk_usage``).
    """
    attachments_bytes = await asyncio.to_thread(
        _dir_size_bytes, ATTACHMENTS_DIR
    )
    used_bytes, total_bytes = get_disk_usage_bytes()
    free_bytes = max(total_bytes - used_bytes, 0)
    after_used = used_bytes + attachments_bytes
    return ArchiveBudget(
        attachments_bytes=attachments_bytes,
        total_bytes=total_bytes,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
        after_used_bytes=after_used,
        block_pct=DISK_BLOCK_PCT,
    )


def format_archive_budget_text(budget: ArchiveBudget) -> str:
    """Текст экрана-приглашения перед confirm (см. plan §8.3)."""
    lines = [
        "📦 Архивация data/attachments на диск.",
        "",
        f"Объём data/attachments: {budget.attachments_gb:.1f} ГБ.",
        (
            f"Свободно на разделе: {budget.free_gb:.1f} ГБ "
            f"({budget.free_pct:.0f}%)."
        ),
        (
            f"После копии:        ≈ {budget.after_used_gb:.1f} ГБ занято "
            f"({budget.after_pct:.0f}%)."
        ),
        "",
        f"⚠️ Бот блокирует приём файлов при ≥ {budget.block_pct}%.",
    ]
    if budget.after_pct >= budget.block_pct:
        lines.extend(
            [
                "",
                "После архивации диск превысит блокирующий порог. "
                "Освободите место или используйте ZIP в DM "
                "(`/admin_export_files`) — он не пишет на диск.",
            ]
        )
    return "\n".join(lines)


# =====================================================================
# DB lookup: per-BR-ID intake_mode (single query, без N+1)
# =====================================================================


async def _load_intake_modes() -> dict[str, str]:
    """Прочитать ``{br_id → intake_mode.value}`` одним запросом.

    Используется в манифесте архива. Заявки без записи в БД (например,
    осиротевшие папки на диске после ручных правок) попадают в манифест
    с ``intake_mode = None``.
    """
    async with get_session()() as session:
        stmt = select(Application.br_id, Application.intake_mode)
        rows = (await session.execute(stmt)).all()
    return {br_id: mode.value for br_id, mode in rows}


# =====================================================================
# Сканирование и копирование
# =====================================================================


def _extract_br_id(folder_name: str) -> str | None:
    """``"BR-2026-0042_Иванов_…"`` → ``"BR-2026-0042"`` (или None)."""
    m = _BR_ID_RE.match(folder_name)
    return m.group(1) if m else None


def _find_br_id_folders(root: Path) -> list[Path]:
    """Все каталоги вида ``BR-YYYY-N…`` внутри ``root``.

    Отсортированно по полному пути — стабильность порядка для манифеста
    и для прогресса в DM админа.
    """
    found: list[Path] = []
    if not root.exists():
        return found
    for entry in root.rglob("*"):
        if not entry.is_dir():
            continue
        if _extract_br_id(entry.name):
            found.append(entry)
    found.sort()
    return found


def _copy_tree_sync(src: Path, dst: Path) -> int:
    """Скопировать дерево ``src → dst`` через ``shutil.copy2``.

    Сохраняет mtime/permissions. Создаёт промежуточные подкаталоги.
    Возвращает суммарный размер скопированных файлов в байтах.
    """
    total = 0
    for srcfile in src.rglob("*"):
        if srcfile.is_dir():
            continue
        rel = srcfile.relative_to(src)
        dstfile = dst / rel
        dstfile.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(srcfile, dstfile)
        try:
            total += dstfile.stat().st_size
        except OSError:
            continue
    return total


async def _invoke_progress(
    cb: ProgressCb | None,
    index: int,
    total: int,
    br_id: str,
    size_bytes: int,
) -> None:
    """Унифицированный вызов sync/async callback."""
    if cb is None:
        return
    try:
        result = cb(index, total, br_id, size_bytes)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.exception(
            "archive: progress_cb упал",
            br_id=br_id,
            index=index,
        )


# =====================================================================
# Основной публичный entrypoint
# =====================================================================


async def archive_attachments_to_disk(
    *,
    target_dir: Path | None = None,
    progress_cb: ProgressCb | None = None,
) -> Path:
    """Скопировать ``ATTACHMENTS_DIR`` в архивный подкаталог.

    Args:
        target_dir: явный путь архива. Если None — формируется как
            ``ARCHIVE_DIR/BR-{COMPETITION_YEAR}_{UTC-timestamp}/``.
        progress_cb: вызывается по факту копирования каждого
            BR-ID-каталога: ``cb(index, total, br_id, size_bytes)``.
            Может быть sync или async; исключения в callback логируются,
            но не прерывают копирование.

    Returns:
        Путь созданного архивного каталога.

    Raises:
        ArchiveBudgetExceeded: pre-flight отказ — после копии диск был бы
            заполнен на ``≥ DISK_BLOCK_PCT``. Каталог не создаётся.
        RuntimeError: целевой путь уже существует.

    Notes:
        - Запись в БД не делается; чтение — одним запросом
          (``_load_intake_modes``), без N+1.
        - Манифест и summary пишутся даже при нулевом числе BR-ID.
    """
    budget = await estimate_archive_budget()
    if budget.after_pct >= budget.block_pct:
        logger.error(
            "Архивация отклонена pre-flight'ом",
            after_pct=round(budget.after_pct, 2),
            block_pct=budget.block_pct,
            attachments_bytes=budget.attachments_bytes,
        )
        raise ArchiveBudgetExceeded(budget)

    if target_dir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
        target = ARCHIVE_DIR / f"BR-{COMPETITION_YEAR}_{stamp}"
    else:
        target = target_dir

    if target.exists():
        raise RuntimeError(f"Каталог архива уже существует: {target}")

    logger.info(
        "Архивация на диск стартовала",
        source=str(ATTACHMENTS_DIR),
        target=str(target),
        attachments_bytes=budget.attachments_bytes,
        free_bytes=budget.free_bytes,
    )

    intake_modes = await _load_intake_modes()
    br_folders = await asyncio.to_thread(_find_br_id_folders, ATTACHMENTS_DIR)

    await asyncio.to_thread(target.mkdir, parents=True, exist_ok=False)

    manifest_entries: list[dict] = []
    copied_bytes = 0
    total = len(br_folders)

    try:
        for index, src in enumerate(br_folders, start=1):
            rel = src.relative_to(ATTACHMENTS_DIR)
            dst = target / rel
            await asyncio.to_thread(
                dst.parent.mkdir, parents=True, exist_ok=True
            )
            size = await asyncio.to_thread(_copy_tree_sync, src, dst)
            copied_bytes += size
            br_id = _extract_br_id(src.name) or src.name
            manifest_entries.append(
                {
                    "br_id": br_id,
                    "relative_path": str(rel),
                    "size_bytes": size,
                    "intake_mode": intake_modes.get(br_id),
                }
            )
            await _invoke_progress(progress_cb, index, total, br_id, size)
    except Exception:
        logger.exception(
            "Архивация: ошибка копирования",
            target=str(target),
            copied_so_far=len(manifest_entries),
        )
        raise

    created_at_iso = datetime.now(timezone.utc).isoformat()
    manifest = {
        "competition_year": COMPETITION_YEAR,
        "created_at": created_at_iso,
        "source": str(ATTACHMENTS_DIR),
        "destination": str(target),
        "br_folders_count": len(manifest_entries),
        "total_bytes": copied_bytes,
        "entries": manifest_entries,
    }
    manifest_path = target / "archive_manifest.json"
    summary_path = target / "summary.txt"

    await asyncio.to_thread(
        manifest_path.write_text,
        json.dumps(manifest, ensure_ascii=False, indent=2),
        "utf-8",
    )
    summary = (
        f"Архив BR-{COMPETITION_YEAR}\n"
        f"Создан: {created_at_iso}\n"
        f"Источник: {ATTACHMENTS_DIR}\n"
        f"Назначение: {target}\n"
        f"BR-каталогов: {len(manifest_entries)}\n"
        f"Скопировано байт: {copied_bytes}\n"
    )
    await asyncio.to_thread(summary_path.write_text, summary, "utf-8")

    logger.info(
        "Архивация на диск завершена",
        path=str(target),
        br_folders=len(manifest_entries),
        bytes=copied_bytes,
    )
    return target


async def start_archive_task(*, bot, bot_id, chat_id, huid) -> None:
    """Фоновая архивация с прогрессом в DM админа."""
    from utils.bot_utils import resolve_bot_id

    resolved = resolve_bot_id(bot) or bot_id
    last_reported = 0

    async def _notify(body: str) -> None:
        try:
            await bot.send_message(
                bot_id=resolved,
                chat_id=chat_id,
                body=body,
                wait_callback=False,
            )
        except Exception:
            logger.exception("archive: не удалось отправить прогресс", huid=str(huid))

    async def progress_cb(index: int, total: int, br_id: str, size_bytes: int) -> None:
        nonlocal last_reported
        if total and index - last_reported >= max(1, total // 10):
            last_reported = index
            mb = size_bytes / (1024 * 1024)
            await _notify(
                f"📦 Архивация: {index}/{total} ({br_id}, {mb:.0f} МБ)…"
            )

    await _notify("🚀 Архивация на диск запущена…")
    try:
        dest = await archive_attachments_to_disk(progress_cb=progress_cb)
    except ArchiveBudgetExceeded as exc:
        await _notify(f"❌ Архивация не выполнена: {exc}")
        return
    except Exception as exc:
        logger.exception("archive: ошибка архивации")
        await _notify(f"❌ Архивация не выполнена: {exc}")
        return

    budget = await estimate_archive_budget()
    await _notify(
        "✅ Архивация завершена.\n\n"
        f"Путь: `{dest}`\n"
        f"Свободно на разделе: {budget.free_gb:.1f} ГБ "
        f"({budget.free_pct:.0f}%)."
    )


__all__ = [
    "ArchiveBudget",
    "ArchiveBudgetExceeded",
    "archive_attachments_to_disk",
    "estimate_archive_budget",
    "format_archive_budget_text",
    "start_archive_task",
]
