"""
Архивная выгрузка папки ``data/attachments`` без записи на диск.

Назначение
----------
Запасной канал для случая «диск 95% занят, SSH к серверу нет».
Админ запускает выгрузку из бот-меню; сервис обходит заявки одну
за другой, для каждой собирает ZIP в памяти и yield-ит его в
вызывающий слой (``handlers.admin_export``), который шлёт архив
вложением в DM-чат админа.

Идея — никогда не писать ZIP на диск: между ``zipfile.ZipFile`` и
``OutgoingAttachment`` живёт только bytes-буфер. Если в кадре
ATTACHMENTS_DIR действительно почти не осталось места — единственное
ограничение по памяти, и оно регулируется `EXPORT_MAX_PART_BYTES`.

Селекторы
---------
- ``ALL`` — все заявки (``Application.is_actual_version`` не учитываем
  специально: модератор может счесть нужной любую исходную загрузку).
- ``SHORTLIST`` — только заявки в топ-10 (``JuryStatus.V_TOP_10``).
  Источник правды — ``services.registry.fetch_shortlist_applications``.

LINKS-режим
-----------
Хранилище для ``IntakeMode.LINKS`` пустое — файлы лежат в облаке у
родителя. Поэтому ZIP по такой заявке содержит только ``meta.txt``
(на лету собирается с указанием ``cloud_link``), а в манифесте
ставится статус ``links_only`` или ``pending_link`` (если ссылку
ещё не прислали). Параллельно собирается единый ``links.txt`` со
списком всех ``cloud_link`` — отдельным файлом в конце выгрузки.

Сервис не отвечает за отправку и rate-limit (этим занимается
caller); он просто поток ``ExportItem``-ов.
"""
from __future__ import annotations

import asyncio
import csv
import enum
import io
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Sequence

import aiofiles
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from config import EXPORT_MAX_PART_BYTES
from database.db import get_session
from database.models import (
    Application,
    ApplicationFile,
    IntakeMode,
)
from services.registry import fetch_shortlist_applications
from services.storage import (
    ATTACHMENTS_DIR,
    get_application_folder,
)


# =====================================================================
# Контракт выгрузки
# =====================================================================


class ExportSelector(str, enum.Enum):
    """Что именно выгружаем."""

    ALL = "all"
    SHORTLIST = "shortlist"


@dataclass(frozen=True)
class ExportItem:
    """Один элемент потока выгрузки.

    ``payload`` — готовые bytes для ``OutgoingAttachment``;
    ``filename`` — имя, под которым отправляем (без путей).
    ``kind`` — тип элемента, чтобы caller мог по-разному оформить caption.
    """

    kind: str  # "zip" | "manifest" | "links" | "summary"
    filename: str
    payload: bytes
    caption: str = ""
    # Сводные данные конкретного архива (для summary в конце выгрузки).
    meta: dict = field(default_factory=dict)


@dataclass
class ExportSummary:
    """Сводные счётчики на конец выгрузки."""

    selector: ExportSelector
    started_at: datetime
    finished_at: datetime | None = None
    apps_total: int = 0
    apps_with_files: int = 0
    apps_links_only: int = 0
    apps_pending_link: int = 0
    apps_oversize_meta_only: int = 0
    apps_failed: int = 0
    bytes_emitted: int = 0

    def as_text(self) -> str:
        finished = (self.finished_at or datetime.utcnow()).replace(microsecond=0)
        started = self.started_at.replace(microsecond=0)
        return (
            "📊 Итоги выгрузки\n"
            f"- селектор: **{self.selector.value.upper()}**\n"
            f"- всего заявок: **{self.apps_total}**\n"
            f"- с файлами: {self.apps_with_files}\n"
            f"- только-ссылки: {self.apps_links_only}\n"
            f"- ждут ссылку: {self.apps_pending_link}\n"
            f"- meta-only (over-size): {self.apps_oversize_meta_only}\n"
            f"- не удалось собрать: {self.apps_failed}\n"
            f"- объём: {_human_bytes(self.bytes_emitted)}\n"
            f"- начало: {started.isoformat(sep=' ')}Z\n"
            f"- окончание: {finished.isoformat(sep=' ')}Z"
        )


# =====================================================================
# Хелперы — выбор заявок и meta.txt в памяти
# =====================================================================


async def _load_selected_applications(
    selector: ExportSelector,
) -> list[Application]:
    """Загрузить заявки + их файлы по выбранному пресету.

    Eager-load файлов через ``selectinload`` — N+1 на ApplicationFile
    в цикле выгрузки нам совсем не нужен.
    """
    async with get_session()() as session:
        if selector is ExportSelector.SHORTLIST:
            apps = await fetch_shortlist_applications(session)
            if not apps:
                return []
            ids = [a.id for a in apps]
            stmt = (
                select(Application)
                .where(Application.id.in_(ids))
                .options(selectinload(Application.files))
                .order_by(Application.br_id.asc())
            )
        else:
            stmt = (
                select(Application)
                .options(selectinload(Application.files))
                .order_by(Application.br_id.asc())
            )
        return list((await session.scalars(stmt)).all())


def _build_meta_bytes(app: Application, files: Sequence[ApplicationFile]) -> bytes:
    """Собрать ``meta.txt`` в памяти (повторяет формат services.storage)."""
    moscow = timezone(timedelta(hours=3))
    dt = app.created_at or datetime.utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    submission = dt.astimezone(moscow).strftime("%Y-%m-%d %H:%M (Europe/Moscow)")
    contact = (
        app.parent_contact
        if app.parent_contact
        else (f"@{app.parent_ad_login}" if app.parent_ad_login else f"HUID: {app.parent_huid}")
    )
    lines = [
        f"ID заявки: {app.br_id}",
        f"Дата подачи: {submission}",
        f"ФИО родителя: {app.parent_full_name}",
        f"Подразделение: {app.parent_division}",
        f"Контакт: {contact}",
        f"Имя ребёнка: {app.child_name}",
        f"Возраст: {app.child_age}",
        f"Возрастная категория: {app.age_category.value}",
        f"Трек: {app.track.value}",
        f"Название работы: {app.title}",
        f"Описание: {(app.description or '').strip()}",
        f"Статус модерации: {app.moderation_status.value}",
        f"Статус жюри: {app.jury_status.value}",
        f"Режим приёма: {app.intake_mode.value}",
    ]
    if app.cloud_link:
        lines.append(f"Ссылка на папку (cloud): {app.cloud_link}")
    if files:
        lines.append("Исходные имена файлов:")
        for f in files:
            lines.append(f"  - {f.original_filename} → {f.stored_filename}")
    else:
        lines.append("Исходные имена файлов: (нет)")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _human_bytes(num: int) -> str:
    if num <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{num} B"
        num = num / 1024.0
    return f"{num:.1f} PB"


def _zip_filename_for(app: Application) -> str:
    """Имя ZIP — стабильное и понятное в любом проводнике."""
    return f"{app.br_id}.zip"


def _zip_inner_dir(app: Application) -> str:
    """Префикс пути внутри ZIP — повторяет структуру ATTACHMENTS_DIR.

    Чтобы при распаковке сразу собирался корректный каталог
    ``yyyy-mm-dd/track/age/parent_name/<files>``.
    """
    rel = get_application_folder(app).relative_to(ATTACHMENTS_DIR)
    return str(rel).replace("\\", "/")


# =====================================================================
# Сборка одного архива по заявке (в памяти)
# =====================================================================


def _sum_files_size(files: Sequence[ApplicationFile]) -> int:
    return sum(int(f.size_bytes or 0) for f in files)


async def _read_file_bytes(path: Path) -> bytes:
    async with aiofiles.open(path, "rb") as fp:
        return await fp.read()


async def _build_app_zip_bytes(
    app: Application,
    *,
    max_part_bytes: int,
) -> tuple[bytes, dict]:
    """Собрать ZIP по одной заявке. Возвращает (bytes, info-meta).

    ``info-meta`` — словарь для манифеста и summary: br_id, статус,
    кол-во файлов, итоговый размер, причины пропуска и т.п.
    """
    files = list(app.files)
    inner_prefix = _zip_inner_dir(app)
    folder = get_application_folder(app)

    info: dict = {
        "br_id": app.br_id,
        "intake_mode": app.intake_mode.value,
        "moderation_status": app.moderation_status.value,
        "jury_status": app.jury_status.value,
        "files_count": 0,
        "files_bytes": 0,
        "status": "files",
        "cloud_link": app.cloud_link or "",
        "inner_path": inner_prefix,
    }

    # ---- Сценарий LINKS ----
    if app.intake_mode is IntakeMode.LINKS:
        if not app.cloud_link:
            info["status"] = "pending_link"
            return b"", info

        info["status"] = "links_only"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr(
                f"{inner_prefix}/meta.txt",
                _build_meta_bytes(app, []),
            )
            zf.writestr(
                f"{inner_prefix}/cloud_link.txt",
                (app.cloud_link.strip() + "\n").encode("utf-8"),
            )
        return buf.getvalue(), info

    # ---- Сценарий FILES ----
    total_bytes = _sum_files_size(files)
    info["files_count"] = len(files)
    info["files_bytes"] = total_bytes

    over_size = total_bytes > max_part_bytes
    if over_size:
        logger.warning(
            "attachments_export: заявка превышает EXPORT_MAX_PART_BYTES — "
            "включаем только meta/description/reason",
            br_id=app.br_id,
            total_bytes=total_bytes,
            limit=max_part_bytes,
        )
        info["status"] = "oversize_meta_only"

    buf = io.BytesIO()
    # ZIP_STORED для уже сжатых медиа — нет смысла дважды грызть CPU
    # на jpg/png; для txt дефлейт всё равно даёт мизер. Берём STORED
    # чтобы не накручивать CPU и память.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        zf.writestr(f"{inner_prefix}/meta.txt", _build_meta_bytes(app, files))

        # description.txt и reason.txt (если есть на диске).
        for sidecar in ("description.txt", "reason.txt"):
            sidecar_path = folder / sidecar
            try:
                exists = await asyncio.to_thread(sidecar_path.exists)
            except Exception:
                exists = False
            if exists:
                try:
                    payload = await _read_file_bytes(sidecar_path)
                except Exception:
                    logger.exception(
                        "attachments_export: не удалось прочитать sidecar",
                        br_id=app.br_id,
                        sidecar=sidecar,
                    )
                    continue
                zf.writestr(f"{inner_prefix}/{sidecar}", payload)

        if not over_size:
            for af in files:
                src = ATTACHMENTS_DIR / af.relative_path
                try:
                    payload = await _read_file_bytes(src)
                except FileNotFoundError:
                    logger.warning(
                        "attachments_export: файл отсутствует на диске",
                        br_id=app.br_id,
                        path=str(src),
                    )
                    info["status"] = "partial_missing_files"
                    continue
                except Exception:
                    logger.exception(
                        "attachments_export: ошибка чтения файла",
                        br_id=app.br_id,
                        path=str(src),
                    )
                    info["status"] = "partial_missing_files"
                    continue
                zf.writestr(
                    f"{inner_prefix}/{af.stored_filename}", payload,
                )

    return buf.getvalue(), info


# =====================================================================
# Манифест
# =====================================================================


_MANIFEST_COLUMNS = [
    "br_id",
    "track",
    "age_category",
    "intake_mode",
    "moderation_status",
    "jury_status",
    "status",
    "files_count",
    "files_bytes",
    "cloud_link",
    "inner_path",
    "zip_filename",
]


def _manifest_csv_bytes(rows: list[dict]) -> bytes:
    """Собрать manifest.csv (UTF-8 + BOM, разделитель — ``;`` для Excel)."""
    out = io.StringIO()
    out.write("\ufeff")  # BOM, чтобы Excel сразу видел кодировку
    writer = csv.DictWriter(
        out,
        fieldnames=_MANIFEST_COLUMNS,
        delimiter=";",
        extrasaction="ignore",
    )
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return out.getvalue().encode("utf-8")


def _links_txt_bytes(rows: list[dict]) -> bytes:
    """links.txt — построчный список «BR-ID\\t<cloud_link>».

    Включаем только записи с непустым cloud_link.
    """
    out = io.StringIO()
    out.write("# Список облачных ссылок (only IntakeMode.LINKS, cloud_link != null)\n")
    out.write("# Формат: BR-ID<TAB>URL\n")
    for r in rows:
        if r.get("cloud_link"):
            out.write(f"{r['br_id']}\t{r['cloud_link']}\n")
    return out.getvalue().encode("utf-8")


# =====================================================================
# Публичный итератор
# =====================================================================


async def iter_attachments_export(
    selector: ExportSelector,
    *,
    max_part_bytes: int | None = None,
) -> AsyncIterator[ExportItem]:
    """Итератор архивной выгрузки: ZIP-ы, links.txt, manifest.csv, summary.

    Контракт:
    1. Сначала идут ZIP-ы по заявкам (можно ноль), для каждой — один
       элемент ``ExportItem(kind="zip")``. У LINKS-без-ссылки ZIP не
       генерируется (он бы содержал только meta — caller всё равно
       увидит её через manifest).
    2. После всех ZIP-ов — ``links.txt`` (kind="links") с агрегатом.
       Отдаётся всегда; если ссылок нет — файл с одной строкой
       заголовка.
    3. Затем — ``manifest.csv`` (kind="manifest") с одной строкой на
       каждую отобранную заявку.
    4. В конце — ``summary`` (kind="summary"), bytes пустой,
       caption — текст ``ExportSummary.as_text()``.

    Ошибки: если по конкретной заявке упало чтение файлов или сборка
    ZIP — она помечается ``status="failed"`` в манифесте, ZIP не
    отдаётся, но процесс продолжается.
    """
    limit = max_part_bytes or EXPORT_MAX_PART_BYTES
    summary = ExportSummary(selector=selector, started_at=datetime.utcnow())
    apps = await _load_selected_applications(selector)
    summary.apps_total = len(apps)

    if not apps:
        # Эмитим только summary — сообщить «пусто».
        summary.finished_at = datetime.utcnow()
        yield ExportItem(
            kind="summary",
            filename="export_summary.txt",
            payload=summary.as_text().encode("utf-8"),
            caption=summary.as_text(),
        )
        return

    manifest_rows: list[dict] = []

    for app in apps:
        try:
            zip_bytes, info = await _build_app_zip_bytes(app, max_part_bytes=limit)
        except Exception:
            logger.exception(
                "attachments_export: фатально не удалось собрать заявку",
                br_id=app.br_id,
            )
            manifest_rows.append(
                {
                    "br_id": app.br_id,
                    "track": app.track.value,
                    "age_category": app.age_category.value,
                    "intake_mode": app.intake_mode.value,
                    "moderation_status": app.moderation_status.value,
                    "jury_status": app.jury_status.value,
                    "status": "failed",
                    "files_count": 0,
                    "files_bytes": 0,
                    "cloud_link": app.cloud_link or "",
                    "inner_path": _zip_inner_dir(app),
                    "zip_filename": "",
                }
            )
            summary.apps_failed += 1
            continue

        row = {
            "br_id": info["br_id"],
            "track": app.track.value,
            "age_category": app.age_category.value,
            "intake_mode": info["intake_mode"],
            "moderation_status": info["moderation_status"],
            "jury_status": info["jury_status"],
            "status": info["status"],
            "files_count": info["files_count"],
            "files_bytes": info["files_bytes"],
            "cloud_link": info["cloud_link"],
            "inner_path": info["inner_path"],
            "zip_filename": _zip_filename_for(app) if zip_bytes else "",
        }
        manifest_rows.append(row)

        status = info["status"]
        if status == "pending_link":
            summary.apps_pending_link += 1
        elif status == "links_only":
            summary.apps_links_only += 1
        elif status == "oversize_meta_only":
            summary.apps_oversize_meta_only += 1
        else:
            summary.apps_with_files += 1

        if zip_bytes:
            summary.bytes_emitted += len(zip_bytes)
            yield ExportItem(
                kind="zip",
                filename=_zip_filename_for(app),
                payload=zip_bytes,
                caption=(
                    f"📦 {app.br_id} · {app.track.value} / "
                    f"{app.age_category.value} · "
                    f"{_human_bytes(len(zip_bytes))}"
                ),
                meta={"br_id": app.br_id, "status": status},
            )

    links_payload = _links_txt_bytes(manifest_rows)
    summary.bytes_emitted += len(links_payload)
    yield ExportItem(
        kind="links",
        filename="links.txt",
        payload=links_payload,
        caption="🔗 Сводный список облачных ссылок (по всем LINKS-заявкам)",
    )

    manifest_payload = _manifest_csv_bytes(manifest_rows)
    summary.bytes_emitted += len(manifest_payload)
    yield ExportItem(
        kind="manifest",
        filename="manifest.csv",
        payload=manifest_payload,
        caption=(
            "🧾 manifest.csv — карта выгрузки (br_id, статус, размер, "
            "расположение в каталоге)"
        ),
    )

    summary.finished_at = datetime.utcnow()
    text = summary.as_text()
    yield ExportItem(
        kind="summary",
        filename="export_summary.txt",
        payload=text.encode("utf-8"),
        caption=text,
    )


# =====================================================================
# Точечная переотправка (admin_export_app)
# =====================================================================


async def build_single_app_export(br_id: str) -> ExportItem | None:
    """Собрать ZIP по одной заявке по её BR-ID.

    None — если заявка не найдена. Используется командой
    ``/admin_export_app`` для точечной переотправки, без обхода всего
    каталога.
    """
    async with get_session()() as session:
        stmt = (
            select(Application)
            .where(Application.br_id == br_id)
            .options(selectinload(Application.files))
        )
        app = (await session.scalars(stmt)).first()
    if app is None:
        return None
    zip_bytes, info = await _build_app_zip_bytes(
        app, max_part_bytes=EXPORT_MAX_PART_BYTES
    )
    if not zip_bytes:
        return ExportItem(
            kind="summary",
            filename=f"{br_id}_status.txt",
            payload=(
                f"BR-ID {br_id}: статус {info['status']} — "
                f"архив не сформирован (нет файлов и нет cloud_link).\n"
            ).encode("utf-8"),
            caption=(
                f"ℹ️ {br_id}: статус **{info['status']}** "
                "(архив не сформирован)."
            ),
            meta=info,
        )
    return ExportItem(
        kind="zip",
        filename=_zip_filename_for(app),
        payload=zip_bytes,
        caption=(
            f"📦 {app.br_id} · {app.track.value} / "
            f"{app.age_category.value} · {_human_bytes(len(zip_bytes))} · "
            f"{info['status']}"
        ),
        meta=info,
    )


__all__ = [
    "ExportSelector",
    "ExportItem",
    "ExportSummary",
    "iter_attachments_export",
    "build_single_app_export",
]
