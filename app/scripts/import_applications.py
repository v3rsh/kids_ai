#!/usr/bin/env python3
"""
Импорт заявок из CSV + JPG, упакованных в Docker-образ.

Данные по умолчанию: ``/app/seed_data/applications.csv`` и
``/app/seed_data/images/``.

Запуск (внутри контейнера бота после деплоя):

    docker exec kids_ai_bot python3 scripts/import_applications.py --dry-run
    docker exec kids_ai_bot python3 scripts/import_applications.py --apply

``parent_huid`` берётся из ``--parent-huid`` или env ``ADMIN_HUID``.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import mimetypes
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from loguru import logger
from sqlalchemy import select, update

from config import ADMIN_HUID, ATTACHMENTS_DIR
from database.db import get_session
from database.models import Application, ModerationStatus, Track, User
from services import applications as applications_service
from services import storage as storage_service
from database.models import FileKind

DEFAULT_CSV = _APP_ROOT / "seed_data" / "applications.csv"
DEFAULT_IMAGES_DIR = _APP_ROOT / "seed_data" / "images"

PARENT_CONTACT = "mvershkov@beeline.ru"
PARENT_CONTACT_TYPE = "email"
PARENT_AD_LOGIN = "mvershkov"
INTAKE_MODE = "files"

VALID_TRACKS = frozenset(t.name for t in Track)


@dataclass(frozen=True)
class SeedRow:
    """Одна строка CSV для импорта."""

    seq: str
    image_file: str
    child_name: str
    child_age: int
    track: str
    title: str
    description: str


def file_kind_for_track(track_name: str) -> FileKind:
    """FileKind по треку — как в ``user_confirm._file_kinds_for_track``."""
    if track_name == "TRADITIONAL":
        return FileKind.ORIGINAL
    if track_name == "AI":
        return FileKind.AI_IMAGE
    if track_name == "HANDMADE_TO_AI":
        return FileKind.DIPTYCH
    raise ValueError(f"Неизвестный track: {track_name!r}")


def parse_seed_row(raw: dict[str, str]) -> SeedRow:
    """Разобрать и провалидировать строку CSV."""
    seq = (raw.get("seq") or "").strip()
    image_file = (raw.get("image_file") or "").strip()
    child_name = (raw.get("child_name") or "").strip()
    track = (raw.get("track") or "").strip().upper()
    title = (raw.get("title") or "").strip()
    description = (raw.get("description") or "").strip()

    if not seq:
        raise ValueError("Пустой seq")
    if not image_file:
        raise ValueError(f"seq={seq}: пустой image_file")
    if not child_name:
        raise ValueError(f"seq={seq}: пустой child_name")
    if track not in VALID_TRACKS:
        raise ValueError(f"seq={seq}: неизвестный track {track!r}")
    if not title:
        raise ValueError(f"seq={seq}: пустой title")
    if not description:
        raise ValueError(f"seq={seq}: пустое description")

    try:
        child_age = int((raw.get("child_age") or "").strip())
    except ValueError as exc:
        raise ValueError(f"seq={seq}: невалидный child_age") from exc

    return SeedRow(
        seq=seq,
        image_file=image_file,
        child_name=child_name,
        child_age=child_age,
        track=track,
        title=title,
        description=description,
    )


def load_csv_rows(csv_path: Path) -> list[SeedRow]:
    """Прочитать CSV-манифест."""
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV не найден: {csv_path}")

    rows: list[SeedRow] = []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            rows.append(parse_seed_row(raw))
    return rows


def resolve_parent_huid(explicit: str | None) -> UUID:
    """HUID родителя из аргумента или env ADMIN_HUID."""
    raw = (explicit or ADMIN_HUID or "").strip()
    if not raw:
        raise ValueError(
            "parent_huid не задан: передайте --parent-huid или задайте ADMIN_HUID в .env"
        )
    return UUID(raw)


async def load_parent_profile(parent_huid: UUID) -> tuple[str, str]:
    """ФИО и подразделение родителя из users."""
    async with get_session()() as session:
        result = await session.execute(
            select(User).where(User.huid == parent_huid)
        )
        user = result.scalar_one_or_none()

    if user is None:
        logger.warning(
            "Пользователь не найден в users — используем заглушки",
            huid=str(parent_huid),
        )
        return "Вершков Михаил", "Beeline"

    full_name = (user.full_name or "").strip() or "Вершков Михаил"
    division = (user.department or "").strip() or "Beeline"
    return full_name, division


async def set_moderation_status(br_id: str, status: ModerationStatus) -> None:
    """Обновить moderation_status заявки."""
    async with get_session()() as session:
        await session.execute(
            update(Application)
            .where(Application.br_id == br_id)
            .values(moderation_status=status)
        )
        await session.commit()


async def import_one_row(
    row: SeedRow,
    *,
    parent_huid: UUID,
    parent_full_name: str,
    parent_division: str,
    images_dir: Path,
    moderation_status: ModerationStatus,
    dry_run: bool,
) -> str:
    """Импортировать одну строку. Возвращает статус: created/skipped/missing/error."""
    src = images_dir / row.image_file
    if not src.is_file():
        logger.error("Файл не найден", seq=row.seq, path=str(src))
        return "missing"

    duplicate = await applications_service.find_possible_duplicate(
        parent_huid=parent_huid,
        child_name=row.child_name,
        child_age=row.child_age,
        track_name=row.track,
    )
    if duplicate is not None:
        logger.info(
            "Пропуск — заявка уже есть",
            seq=row.seq,
            existing_br_id=duplicate.br_id,
            child_name=row.child_name,
        )
        return "skipped"

    if dry_run:
        logger.info(
            "dry-run OK",
            seq=row.seq,
            track=row.track,
            child=f"{row.child_name}/{row.child_age}",
            image=row.image_file,
        )
        return "dry_ok"

    application = await applications_service.create_application(
        parent_huid=parent_huid,
        parent_full_name=parent_full_name,
        parent_division=parent_division,
        parent_ad_login=PARENT_AD_LOGIN,
        parent_contact=PARENT_CONTACT,
        parent_contact_type=PARENT_CONTACT_TYPE,
        child_name=row.child_name,
        child_age=row.child_age,
        track_name=row.track,
        title=row.title,
        description=row.description,
        intake_mode_value=INTAKE_MODE,
    )

    if moderation_status != ModerationStatus.NA_MODERATSII:
        await set_moderation_status(application.br_id, moderation_status)
        application.moderation_status = moderation_status

    kind = file_kind_for_track(row.track)
    mime_type, _ = mimetypes.guess_type(src.name)
    if not mime_type:
        mime_type = "application/octet-stream"

    with tempfile.TemporaryDirectory(prefix="kids_ai_seed_") as tmp:
        tmp_src = Path(tmp) / src.name
        shutil.copy2(src, tmp_src)

        dst = await storage_service.rename_and_save_file(
            application,
            kind,
            None,
            tmp_src,
            original_filename=src.name,
        )

    relative_path = str(dst.relative_to(ATTACHMENTS_DIR))
    application = await applications_service.register_application_files(
        br_id=application.br_id,
        files=[
            applications_service.ApplicationFileSpec(
                kind=kind,
                angle_no=None,
                original_filename=src.name,
                stored_filename=dst.name,
                relative_path=relative_path,
                size_bytes=src.stat().st_size,
                mime_type=mime_type,
            )
        ],
    )
    await storage_service.write_description_txt(application)
    await storage_service.write_meta_txt(application, files=list(application.files))

    logger.info("Импортировано", seq=row.seq, br_id=application.br_id)
    return "created"


async def run_import(
    *,
    csv_path: Path,
    images_dir: Path,
    parent_huid: UUID,
    moderation_status: ModerationStatus,
    dry_run: bool,
) -> dict[str, int]:
    """Импорт всех строк CSV."""
    rows = load_csv_rows(csv_path)
    parent_full_name, parent_division = await load_parent_profile(parent_huid)

    stats: dict[str, int] = {
        "total": len(rows),
        "created": 0,
        "skipped": 0,
        "missing": 0,
        "dry_ok": 0,
        "error": 0,
    }

    logger.info(
        "Старт импорта",
        csv=str(csv_path),
        images_dir=str(images_dir),
        parent_huid=str(parent_huid),
        parent_full_name=parent_full_name,
        dry_run=dry_run,
        moderation_status=moderation_status.name,
    )

    for row in rows:
        try:
            result = await import_one_row(
                row,
                parent_huid=parent_huid,
                parent_full_name=parent_full_name,
                parent_division=parent_division,
                images_dir=images_dir,
                moderation_status=moderation_status,
                dry_run=dry_run,
            )
            stats[result] = stats.get(result, 0) + 1
        except Exception:
            stats["error"] += 1
            logger.exception("Ошибка импорта строки", seq=row.seq)

    return stats


def _parse_moderation_status(raw: str) -> ModerationStatus:
    needle = raw.strip().upper()
    for status in ModerationStatus:
        if status.name == needle or status.value.lower() == raw.strip().lower():
            return status
    allowed = ", ".join(s.name for s in ModerationStatus)
    raise ValueError(f"Неизвестный moderation_status: {raw!r}. Допустимы: {allowed}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Импорт заявок из seed_data (CSV + JPG в образе бота).",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_CSV,
        help=f"Путь к CSV (default: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=DEFAULT_IMAGES_DIR,
        help=f"Папка с JPG (default: {DEFAULT_IMAGES_DIR})",
    )
    parser.add_argument(
        "--parent-huid",
        default=None,
        help="HUID родителя (default: ADMIN_HUID из env)",
    )
    parser.add_argument(
        "--moderation-status",
        default="DOPUSHCHENO",
        help="Статус модерации после создания (default: DOPUSHCHENO)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реальный импорт в БД и на диск. Без флага — dry-run.",
    )
    return parser


async def main_async(args: argparse.Namespace) -> int:
    dry_run = not bool(args.apply)
    parent_huid = resolve_parent_huid(args.parent_huid)
    moderation_status = _parse_moderation_status(args.moderation_status)

    stats = await run_import(
        csv_path=args.csv,
        images_dir=args.images_dir,
        parent_huid=parent_huid,
        moderation_status=moderation_status,
        dry_run=dry_run,
    )

    logger.info("Импорт завершён", **stats)

    if stats.get("missing", 0) > 0:
        return 2
    if stats.get("error", 0) > 0:
        return 1
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
