"""Юнит-тесты счётчика и очистки изображений ``99_rejected/``.

Покрытие:
- ``get_rejected_storage_stats`` — корректный подсчёт изображений /
  метаданных / прочих файлов и числа папок;
- ``purge_rejected_images`` — удаляет только изображения, txt и прочие
  не трогает; ``dry_run`` не удаляет; фильтр по ``br_id``; повторный
  вызов даёт 0 удалений.

Работаем на реальной ФС в ``tmp_path``; БД не используется.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from services import storage


def _make_rejected_folder(root: Path, br_id: str) -> Path:
    """Создать папку заявки под ``99_rejected/<date>/<BR-...>`` с файлами."""
    folder = root / storage.REJECTED_FOLDER_NAME / "2026-06-10" / f"{br_id}_Иванов_Анна"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{br_id}_original.jpg").write_bytes(b"x" * 100)
    (folder / f"{br_id}_angle-1.png").write_bytes(b"y" * 200)
    (folder / "preview.webp").write_bytes(b"z" * 30)
    (folder / storage.DESCRIPTION_TXT).write_text("desc", encoding="utf-8")
    (folder / storage.META_TXT).write_text("meta", encoding="utf-8")
    (folder / storage.REASON_TXT).write_text("reason", encoding="utf-8")
    return folder


@pytest.fixture()
def patched_attachments(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(storage, "ATTACHMENTS_DIR", root)
    return root


@pytest.mark.asyncio
async def test_storage_stats_counts(patched_attachments):
    _make_rejected_folder(patched_attachments, "BR-2026-0001")
    _make_rejected_folder(patched_attachments, "BR-2026-0002")

    stats = await storage.get_rejected_storage_stats()

    assert stats.folders_count == 2
    # По 3 изображения (jpg + png + webp) на папку.
    assert stats.image_files_count == 6
    assert stats.image_bytes == 2 * (100 + 200 + 30)
    # По 3 txt-метаданных на папку.
    assert stats.meta_files_count == 6
    assert stats.other_files_count == 0


@pytest.mark.asyncio
async def test_purge_removes_only_images(patched_attachments):
    folder = _make_rejected_folder(patched_attachments, "BR-2026-0001")

    result = await storage.purge_rejected_images()

    assert result.files_removed == 3  # jpg + png + webp
    assert result.bytes_freed == 100 + 200 + 30
    assert result.folders_touched == 1
    # Изображения удалены.
    assert not (folder / "BR-2026-0001_original.jpg").exists()
    assert not (folder / "BR-2026-0001_angle-1.png").exists()
    assert not (folder / "preview.webp").exists()
    # Метаданные сохранены.
    assert (folder / storage.DESCRIPTION_TXT).exists()
    assert (folder / storage.META_TXT).exists()
    assert (folder / storage.REASON_TXT).exists()


@pytest.mark.asyncio
async def test_purge_dry_run_keeps_files(patched_attachments):
    folder = _make_rejected_folder(patched_attachments, "BR-2026-0001")

    result = await storage.purge_rejected_images(dry_run=True)

    assert result.files_removed == 3
    assert result.bytes_freed == 100 + 200 + 30
    # Ничего не удалено.
    assert (folder / "BR-2026-0001_original.jpg").exists()
    assert (folder / "preview.webp").exists()


@pytest.mark.asyncio
async def test_purge_filter_by_br_id(patched_attachments):
    f1 = _make_rejected_folder(patched_attachments, "BR-2026-0001")
    f2 = _make_rejected_folder(patched_attachments, "BR-2026-0002")

    result = await storage.purge_rejected_images(br_id="BR-2026-0001")

    assert result.folders_touched == 1
    assert not (f1 / "BR-2026-0001_original.jpg").exists()
    # Вторая заявка не затронута.
    assert (f2 / "BR-2026-0002_original.jpg").exists()


@pytest.mark.asyncio
async def test_purge_idempotent(patched_attachments):
    _make_rejected_folder(patched_attachments, "BR-2026-0001")

    first = await storage.purge_rejected_images()
    second = await storage.purge_rejected_images()

    assert first.files_removed == 3
    assert second.files_removed == 0
    assert second.bytes_freed == 0
