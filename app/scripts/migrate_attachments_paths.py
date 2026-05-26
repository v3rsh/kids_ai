#!/usr/bin/env python3
"""
Миграция хранилища заявок: «Безопасные рисунки» + русские имена треков
→ плоская латинская структура.

Что делает (идемпотентно):

1. На диске (root = ``config.ATTACHMENTS_DIR``):
   - Если есть подпапка ``Безопасные рисунки/`` — содержимое
     перемещается на уровень выше, сама папка удаляется.
   - Любая встретившаяся папка с русским именем переименовывается:
     ``01_Традиционное_рисование`` → ``01_traditional``
     ``02_ИИ_рисунок``             → ``02_ai``
     ``03_От_руки_к_ИИ``           → ``03_refine``
     ``99_Отклонено``              → ``99_rejected``
   - При конфликте (целевая папка уже существует) делается merge
     через ``shutil.copytree(dirs_exist_ok=True)`` + ``rmtree`` исходника.

2. В БД (``application_files.relative_path``) — один UPDATE через
   вложенные ``REPLACE(...)``. Идемпотентно: повторный запуск ничего
   не меняет.

3. Sanity-check: для каждой записи проверяется, что
   ``(ATTACHMENTS_DIR / relative_path).exists()``. Расхождения только
   логируются, скрипт не падает.

Запуск (внутри контейнера бота):

    docker exec -it kids_ai_bot python3 scripts/migrate_attachments_paths.py --dry-run
    docker exec -it kids_ai_bot python3 scripts/migrate_attachments_paths.py --apply

Без флагов — dry-run по умолчанию.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

# Делаем модули из /app импортируемыми, когда скрипт запускают как
# `python3 scripts/...` из /app внутри контейнера.
_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

from loguru import logger
from sqlalchemy import func, select, update

from config import ATTACHMENTS_DIR
from database.db import get_session
from database.models import ApplicationFile


# =====================================================================
# Карты переименования
# =====================================================================

OLD_ROOT_FOLDER = "Безопасные рисунки"

#: Старое имя → новое имя для одиночного сегмента пути.
SEGMENT_RENAMES: dict[str, str] = {
    "01_Традиционное_рисование": "01_traditional",
    "02_ИИ_рисунок": "02_ai",
    "03_От_руки_к_ИИ": "03_refine",
    "99_Отклонено": "99_rejected",
}


# =====================================================================
# Шаг 1. Файлы на диске
# =====================================================================


def _merge_directory(src: Path, dst: Path) -> None:
    """Слить содержимое ``src`` в существующий ``dst`` и удалить ``src``.

    Используется при конфликтах (целевая папка уже есть). После copytree
    с ``dirs_exist_ok=True`` исходник удаляется через ``rmtree``.
    """
    logger.warning(
        "Целевая папка уже существует — выполняется merge",
        src=str(src),
        dst=str(dst),
    )
    shutil.copytree(src, dst, dirs_exist_ok=True)
    shutil.rmtree(src)


def _flatten_root(root: Path, *, apply: bool) -> None:
    """Перенести содержимое ``root/Безопасные рисунки`` в ``root``."""
    nested = root / OLD_ROOT_FOLDER
    if not nested.exists():
        logger.info("Корневая папка «{}» не найдена — пропускаем", OLD_ROOT_FOLDER)
        return

    if not nested.is_dir():
        logger.warning("«{}» существует, но это не папка — пропускаем", OLD_ROOT_FOLDER)
        return

    entries = list(nested.iterdir())
    logger.info(
        "Найдена корневая папка «{}» с {} элементами — будет схлопнута",
        OLD_ROOT_FOLDER,
        len(entries),
    )

    if not apply:
        for entry in entries:
            logger.info("[dry-run] move {} → {}", entry, root / entry.name)
        logger.info("[dry-run] rmdir {}", nested)
        return

    for entry in entries:
        dst = root / entry.name
        if dst.exists() and dst.is_dir() and entry.is_dir():
            _merge_directory(entry, dst)
        elif dst.exists():
            logger.error(
                "Не могу перенести: целевой путь уже существует и не папка",
                src=str(entry),
                dst=str(dst),
            )
        else:
            shutil.move(str(entry), str(dst))
            logger.info("Перенесено: {} → {}", entry.name, dst)

    try:
        nested.rmdir()
        logger.info("Удалена пустая корневая папка: {}", nested)
    except OSError as exc:
        logger.warning("Не удалось удалить «{}»: {}", nested, exc)


def _rename_segments(root: Path, *, apply: bool) -> None:
    """Пройти по дереву и переименовать любые папки с русскими именами.

    Идём `bottom-up` (`os.walk(topdown=False)`-эквивалент), чтобы не
    переименовывать родителя до детей. В Python — рекурсия с проходом
    по `iterdir()`.
    """
    def _walk(path: Path) -> None:
        if not path.is_dir():
            return
        # Сначала рекурсивно в детей (после возможного rename — путь к
        # детям не меняется, потому что rename делается ПОСЛЕ).
        for child in list(path.iterdir()):
            if child.is_dir():
                _walk(child)
        # Теперь решаем — переименовывать ли саму папку.
        new_name = SEGMENT_RENAMES.get(path.name)
        if new_name is None:
            return
        new_path = path.parent / new_name
        if new_path.exists():
            if not apply:
                logger.info("[dry-run] merge {} → {}", path, new_path)
                return
            _merge_directory(path, new_path)
            return
        if not apply:
            logger.info("[dry-run] rename {} → {}", path, new_path)
            return
        path.rename(new_path)
        logger.info("Переименовано: {} → {}", path.name, new_name)

    _walk(root)


def migrate_filesystem(root: Path, *, apply: bool) -> None:
    """Полная миграция файловой структуры."""
    if not root.exists():
        logger.warning("ATTACHMENTS_DIR не существует: {}", root)
        return

    logger.info("Шаг 1: миграция файловой структуры в {}", root)
    _flatten_root(root, apply=apply)
    _rename_segments(root, apply=apply)


# =====================================================================
# Шаг 2. БД — application_files.relative_path
# =====================================================================


def _build_replace_expr(column):
    """Построить вложенный REPLACE() для всех переименований."""
    expr = func.replace(column, f"{OLD_ROOT_FOLDER}/", "")
    for old, new in SEGMENT_RENAMES.items():
        expr = func.replace(expr, old, new)
    return expr


async def migrate_db(*, apply: bool) -> None:
    """Обновить ``application_files.relative_path`` для всех записей."""
    logger.info("Шаг 2: обновление application_files.relative_path в БД")

    session_factory = get_session()
    async with session_factory() as session:
        # Считаем затронутые записи для отчёта.
        rows = (
            await session.execute(
                select(ApplicationFile.id, ApplicationFile.relative_path)
            )
        ).all()

        needs_update = 0
        for _id, rel in rows:
            if (
                f"{OLD_ROOT_FOLDER}/" in rel
                or any(seg in rel for seg in SEGMENT_RENAMES)
            ):
                needs_update += 1

        logger.info(
            "Записей в application_files: {}, требуют обновления: {}",
            len(rows),
            needs_update,
        )

        if needs_update == 0:
            logger.info("БД уже в актуальном состоянии — UPDATE не нужен")
            return

        if not apply:
            sample = [
                (rel, _preview_new(rel))
                for _id, rel in rows[:5]
                if f"{OLD_ROOT_FOLDER}/" in rel
                or any(seg in rel for seg in SEGMENT_RENAMES)
            ]
            for old, new in sample:
                logger.info("[dry-run] {} → {}", old, new)
            return

        new_expr = _build_replace_expr(ApplicationFile.relative_path)
        stmt = update(ApplicationFile).values(relative_path=new_expr)
        result = await session.execute(stmt)
        await session.commit()
        logger.info("UPDATE выполнен, затронуто строк: {}", result.rowcount)


def _preview_new(rel: str) -> str:
    """Локальная имитация REPLACE() для dry-run-вывода."""
    out = rel.replace(f"{OLD_ROOT_FOLDER}/", "")
    for old, new in SEGMENT_RENAMES.items():
        out = out.replace(old, new)
    return out


# =====================================================================
# Шаг 3. Sanity-check
# =====================================================================


async def sanity_check(root: Path) -> None:
    """Сверить relative_path с фактическими файлами на диске."""
    logger.info("Шаг 3: sanity-check (БД ↔ файловая система)")

    session_factory = get_session()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    ApplicationFile.id,
                    ApplicationFile.relative_path,
                    ApplicationFile.stored_filename,
                )
            )
        ).all()

    missing: list[tuple[str, Path]] = []
    for _id, rel, stored in rows:
        full = (root / rel).resolve()
        if not full.exists():
            missing.append((stored, full))

    if not missing:
        logger.info("OK: все {} записей соответствуют файлам на диске", len(rows))
        return

    logger.warning(
        "Не найдены файлы для {} записей из {}:", len(missing), len(rows)
    )
    for stored, path in missing[:20]:
        logger.warning("  {} → {}", stored, path)
    if len(missing) > 20:
        logger.warning("  … и ещё {} записей", len(missing) - 20)


# =====================================================================
# Entry point
# =====================================================================


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Миграция: убрать «Безопасные рисунки», "
            "перевести имена треков и 99_Отклонено на латиницу."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Выполнить миграцию. Без флага — dry-run (только показать).",
    )
    parser.add_argument(
        "--skip-sanity",
        action="store_true",
        help="Пропустить sanity-check (БД ↔ ФС).",
    )
    return parser.parse_args()


async def _async_main(apply: bool, skip_sanity: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    logger.info("=== migrate_attachments_paths.py [{}] ===", mode)
    logger.info("ATTACHMENTS_DIR = {}", ATTACHMENTS_DIR)

    migrate_filesystem(ATTACHMENTS_DIR, apply=apply)
    await migrate_db(apply=apply)
    if apply and not skip_sanity:
        await sanity_check(ATTACHMENTS_DIR)
    logger.info("=== Готово ===")


def main() -> int:
    args = _parse_args()
    try:
        asyncio.run(_async_main(apply=args.apply, skip_sanity=args.skip_sanity))
    except Exception:
        logger.exception("Миграция упала")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
