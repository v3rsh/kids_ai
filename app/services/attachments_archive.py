"""
Архивация каталога ``ATTACHMENTS_DIR`` на локальный диск.

Запасной канал к выгрузке шорт-листа в DM
(``services/attachments_export.py``): пишет один tar.gz-файл
``ARCHIVE_DIR/bd-full.tar.gz`` со всеми BR-ID-каталогами под общим
префиксом ``attachments/``. Рядом с архивом и **внутри** него
сохраняются ``bd-full.manifest.json`` и ``bd-full.summary.txt``
(чтобы можно было прочитать сводку без распаковки).

Pre-flight: перед стартом сравниваем расчётный занятый объём диска
ПОСЛЕ создания tar.gz с ``ARCHIVE_DISK_CAP_PCT`` (потолок архива). Так
как tar.gz слабо сжимает уже сжатые медиа, в качестве верхней границы
берём сырой ``du(ATTACHMENTS_DIR)`` — это безопасно (будем чуть
консервативнее, чем в реальности). При превышении —
``ArchiveBudgetExceeded`` без побочных эффектов.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Union

from loguru import logger
from sqlalchemy import select

from config import (
    ARCHIVE_DIR,
    ARCHIVE_DISK_CAP_PCT,
    ATTACHMENTS_DIR,
    COMPETITION_YEAR,
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

#: Имя итогового tar.gz в ``ARCHIVE_DIR`` (без таймстампа: предыдущий
#: архив ротируется в ``bd-full.prev.tar.gz``).
ARCHIVE_FILENAME = "bd-full.tar.gz"
PREV_ARCHIVE_FILENAME = "bd-full.prev.tar.gz"
MANIFEST_FILENAME = "bd-full.manifest.json"
SUMMARY_FILENAME = "bd-full.summary.txt"

#: Префикс пути внутри tar.gz — все BR-ID-каталоги распаковываются в
#: ``attachments/<rel_path>``, чтобы при `tar -xzf` получался один
#: понятный корневой каталог рядом с manifest/summary.
_TAR_ATTACHMENTS_PREFIX = "attachments"


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
    """Pre-flight отказ: tar.gz не помещается под ``ARCHIVE_DISK_CAP_PCT``.

    Несёт исходную ``ArchiveBudget`` для UI — хендлер показывает её
    в confirm-приглашении и не выводит кнопку «Да, выполнить».
    """

    def __init__(self, budget: ArchiveBudget) -> None:
        super().__init__(
            f"После архивации диск будет заполнен на "
            f"{budget.after_pct:.1f}% (порог {budget.block_pct}%)."
        )
        self.budget = budget


# ``progress_cb(index, total, br_id, size_bytes)`` — callback по факту
# упаковки одного BR-ID-каталога в tar.gz. Может быть sync или async.
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
    """Оценить объём tar.gz и заполнение диска после архивации.

    Подсчёт ``attachments_bytes`` — ``du``-эквивалент по
    ``ATTACHMENTS_DIR`` (через ``rglob`` + ``stat``). Берётся как
    верхняя граница для tar.gz: медиа сжимаются плохо, поэтому
    реальный архив окажется примерно того же размера или чуть меньше.
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
        block_pct=ARCHIVE_DISK_CAP_PCT,
    )


def format_archive_budget_text(budget: ArchiveBudget) -> str:
    """Текст экрана-приглашения перед confirm."""
    lines = [
        "📦 Архивация data/attachments в `bd-full.tar.gz`.",
        "",
        f"Объём data/attachments: {budget.attachments_gb:.1f} ГБ.",
        (
            f"Свободно на разделе: {budget.free_gb:.1f} ГБ "
            f"({budget.free_pct:.0f}%)."
        ),
        (
            f"После архивации:    ≈ {budget.after_used_gb:.1f} ГБ занято "
            f"({budget.after_pct:.0f}%)."
        ),
        "",
        f"⚠️ Потолок для архива полной базы: ≥ {budget.block_pct}%.",
    ]
    if budget.after_pct >= budget.block_pct:
        lines.extend(
            [
                "",
                "После архивации диск превысит потолок. "
                "Освободите место (например, /admin_purge_rejected_images) "
                "или используйте выгрузку шорт-листа в DM — она не пишет "
                "на диск.",
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
# Сканирование исходного дерева
# =====================================================================


def _extract_br_id(folder_name: str) -> str | None:
    """``"BR-2026-0042_Иванов_…"`` → ``"BR-2026-0042"`` (или None)."""
    m = _BR_ID_RE.match(folder_name)
    return m.group(1) if m else None


def _find_br_id_folders(root: Path) -> list[Path]:
    """Все каталоги вида ``BR-YYYY-N…`` внутри ``root``.

    Отсортировано по полному пути — стабильность порядка для манифеста
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


def _folder_size_bytes(path: Path) -> int:
    """Сумма размеров всех файлов внутри ``path`` (для манифеста)."""
    total = 0
    for f in path.rglob("*"):
        if not f.is_file():
            continue
        try:
            total += f.stat().st_size
        except OSError:
            continue
    return total


# =====================================================================
# Сборка tar.gz
# =====================================================================


def _add_bytes_to_tar(
    tar: tarfile.TarFile,
    *,
    arcname: str,
    payload: bytes,
    mtime: int,
) -> None:
    """Положить bytes в открытый tar под именем ``arcname``."""
    ti = tarfile.TarInfo(name=arcname)
    ti.size = len(payload)
    ti.mtime = mtime
    ti.mode = 0o644
    tar.addfile(ti, io.BytesIO(payload))


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


def _rotate_existing(target: Path) -> Path | None:
    """Перенести существующий архив в ``*.prev.tar.gz``.

    Возвращает путь к ротированному файлу либо None, если ротировать
    было нечего.
    """
    if not target.exists():
        return None
    prev = target.parent / PREV_ARCHIVE_FILENAME
    if prev.exists():
        try:
            prev.unlink()
        except OSError:
            logger.exception(
                "archive: не удалось удалить предыдущий .prev",
                path=str(prev),
            )
            raise
    target.rename(prev)
    return prev


# =====================================================================
# Основной публичный entrypoint
# =====================================================================


async def archive_attachments_to_disk(
    *,
    target: Path | None = None,
    progress_cb: ProgressCb | None = None,
) -> Path:
    """Создать ``bd-full.tar.gz`` со всеми BR-ID-каталогами.

    Args:
        target: явный путь архива (``.tar.gz``). Если None — берётся
            ``ARCHIVE_DIR / "bd-full.tar.gz"``.
        progress_cb: вызывается по факту упаковки каждого
            BR-ID-каталога: ``cb(index, total, br_id, size_bytes)``.
            Может быть sync или async; исключения в callback логируются,
            но не прерывают упаковку.

    Returns:
        Путь созданного tar.gz.

    Raises:
        ArchiveBudgetExceeded: pre-flight отказ — после tar.gz диск был
            бы заполнен на ``≥ ARCHIVE_DISK_CAP_PCT``. Файл не создаётся.

    Notes:
        - Если в ``ARCHIVE_DIR`` уже лежит ``bd-full.tar.gz``, он
          ротируется в ``bd-full.prev.tar.gz`` (предыдущий ``.prev``
          удаляется). Новая запись идёт во временный файл и
          переименовывается атомарно (``rename``).
        - Манифест и summary кладутся одновременно ВНУТРЬ tar (под
          именами ``bd-full.manifest.json`` / ``bd-full.summary.txt``)
          и РЯДОМ с tar.gz — чтобы можно было прочитать сводку без
          распаковки.
        - Запись в БД не делается; чтение — одним запросом
          (``_load_intake_modes``), без N+1.
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

    if target is None:
        target = ARCHIVE_DIR / ARCHIVE_FILENAME
    target = Path(target)
    await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)

    tmp_target = target.parent / (target.name + ".tmp")
    if tmp_target.exists():
        await asyncio.to_thread(tmp_target.unlink)
    await asyncio.to_thread(_rotate_existing, target)

    logger.info(
        "Архивация на диск стартовала",
        source=str(ATTACHMENTS_DIR),
        target=str(target),
        attachments_bytes=budget.attachments_bytes,
        free_bytes=budget.free_bytes,
    )

    intake_modes = await _load_intake_modes()
    br_folders = await asyncio.to_thread(_find_br_id_folders, ATTACHMENTS_DIR)

    manifest_entries: list[dict] = []
    copied_bytes = 0
    total = len(br_folders)
    now_ts = int(datetime.now(timezone.utc).timestamp())

    tar = await asyncio.to_thread(tarfile.open, tmp_target, "w:gz")
    try:
        for index, src in enumerate(br_folders, start=1):
            rel = src.relative_to(ATTACHMENTS_DIR)
            arcname = f"{_TAR_ATTACHMENTS_PREFIX}/{rel.as_posix()}"
            try:
                size = await asyncio.to_thread(_folder_size_bytes, src)
                await asyncio.to_thread(
                    tar.add, str(src), arcname, True
                )
            except Exception:
                logger.exception(
                    "archive: не удалось упаковать BR-ID-каталог",
                    src=str(src),
                    arcname=arcname,
                )
                raise

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
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")
        summary_bytes = (
            f"Архив BR-{COMPETITION_YEAR}\n"
            f"Создан: {created_at_iso}\n"
            f"Источник: {ATTACHMENTS_DIR}\n"
            f"Назначение: {target}\n"
            f"BR-каталогов: {len(manifest_entries)}\n"
            f"Упаковано байт (raw): {copied_bytes}\n"
        ).encode("utf-8")

        await asyncio.to_thread(
            _add_bytes_to_tar,
            tar,
            arcname=MANIFEST_FILENAME,
            payload=manifest_bytes,
            mtime=now_ts,
        )
        await asyncio.to_thread(
            _add_bytes_to_tar,
            tar,
            arcname=SUMMARY_FILENAME,
            payload=summary_bytes,
            mtime=now_ts,
        )
    except Exception:
        await asyncio.to_thread(tar.close)
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except OSError:
                logger.exception(
                    "archive: не удалось удалить tmp после ошибки",
                    tmp=str(tmp_target),
                )
        raise
    else:
        await asyncio.to_thread(tar.close)

    manifest_beside = target.parent / MANIFEST_FILENAME
    summary_beside = target.parent / SUMMARY_FILENAME
    await asyncio.to_thread(manifest_beside.write_bytes, manifest_bytes)
    await asyncio.to_thread(summary_beside.write_bytes, summary_bytes)

    await asyncio.to_thread(tmp_target.rename, target)

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

    await _notify("🚀 Архивация в `bd-full.tar.gz` запущена…")
    try:
        dest = await archive_attachments_to_disk(progress_cb=progress_cb)
    except ArchiveBudgetExceeded as exc:
        await _notify(f"❌ Архивация не выполнена: {exc}")
        return
    except Exception as exc:
        logger.exception("archive: ошибка архивации")
        await _notify(f"❌ Архивация не выполнена: {exc}")
        return

    try:
        size_bytes = dest.stat().st_size
    except OSError:
        size_bytes = 0
    size_gb = size_bytes / _BYTES_IN_GB
    budget = await estimate_archive_budget()
    await _notify(
        "✅ Архивация завершена.\n\n"
        f"Файл: `{dest}`\n"
        f"Размер архива: {size_gb:.2f} ГБ\n"
        f"Свободно на разделе: {budget.free_gb:.1f} ГБ "
        f"({budget.free_pct:.0f}%)."
    )


__all__ = [
    "ARCHIVE_FILENAME",
    "MANIFEST_FILENAME",
    "PREV_ARCHIVE_FILENAME",
    "SUMMARY_FILENAME",
    "ArchiveBudget",
    "ArchiveBudgetExceeded",
    "archive_attachments_to_disk",
    "estimate_archive_budget",
    "format_archive_budget_text",
    "start_archive_task",
]
