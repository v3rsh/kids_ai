"""Юнит-тесты переноса отклонённой заявки в ``99_rejected/``.

Покрытие:
- ``move_to_rejected`` переносит всю папку работы (включая
  изображения) в ``99_rejected/``, а не удаляет файлы;
- активная папка после переноса отсутствует;
- ``delete_application_files`` НЕ вызывается из ``move_to_rejected``;
- ``resolve_application_folder`` находит папку в ``99_rejected/``;
- идемпотентность: повторный вызов домерживает остаток.

Работаем на реальной ФС в ``tmp_path`` (storage-функции ходят только в
файловую систему), БД не используется.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from database.models import AgeCategory, Track
from services import storage


def _make_app(
    *,
    br_id: str = "BR-2026-0042",
    track: Track = Track.TRADITIONAL,
    age: AgeCategory = AgeCategory.AGE_7_12,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        br_id=br_id,
        parent_full_name="Иванов Сергей Петрович",
        child_name="Анна",
        track=track,
        age_category=age,
        moderator_comment=None,
        created_at=datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc),
    )


def _seed_active_folder(app: SimpleNamespace) -> Path:
    """Создать активную папку заявки с work-файлами и метаданными."""
    folder = storage.get_application_folder(app)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{app.br_id}_original.jpg").write_bytes(b"x" * 100)
    (folder / f"{app.br_id}_angle-1.png").write_bytes(b"y" * 50)
    (folder / storage.DESCRIPTION_TXT).write_text("desc", encoding="utf-8")
    (folder / storage.META_TXT).write_text("meta", encoding="utf-8")
    return folder


@pytest.fixture()
def patched_attachments(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "attachments"
    root.mkdir()
    monkeypatch.setattr(storage, "ATTACHMENTS_DIR", root)
    return root


@pytest.mark.asyncio
async def test_move_to_rejected_moves_all_files(patched_attachments, monkeypatch):
    app = _make_app()
    active = _seed_active_folder(app)

    # delete_application_files НЕ должна вызываться при отклонении.
    delete_mock = AsyncMock()
    monkeypatch.setattr(storage, "delete_application_files", delete_mock)

    dst = await storage.move_to_rejected(app, reason="не по теме")

    assert dst.exists()
    # Все файлы работы сохранены в 99_rejected.
    assert (dst / f"{app.br_id}_original.jpg").exists()
    assert (dst / f"{app.br_id}_angle-1.png").exists()
    assert (dst / storage.DESCRIPTION_TXT).exists()
    assert (dst / storage.META_TXT).exists()
    # reason.txt записан в папке назначения.
    reason_path = dst / storage.REASON_TXT
    assert reason_path.exists()
    assert "не по теме" in reason_path.read_text(encoding="utf-8")
    # Активная папка удалена (перенос целиком).
    assert not active.exists()
    # Удаление файлов не выполнялось.
    delete_mock.assert_not_awaited()
    # dst лежит под 99_rejected.
    assert storage.REJECTED_FOLDER_NAME in str(dst)


@pytest.mark.asyncio
async def test_resolve_application_folder_finds_rejected(patched_attachments):
    app = _make_app()
    _seed_active_folder(app)
    dst = await storage.move_to_rejected(app, reason="дубликат")

    resolved = storage.resolve_application_folder(app)
    assert resolved == dst
    assert resolved.exists()


@pytest.mark.asyncio
async def test_move_to_rejected_idempotent(patched_attachments):
    app = _make_app()
    _seed_active_folder(app)
    dst1 = await storage.move_to_rejected(app, reason="первый раз")

    # Повторный «приём» тех же файлов и повторное отклонение.
    _seed_active_folder(app)
    dst2 = await storage.move_to_rejected(app, reason="повтор")

    assert dst1 == dst2
    assert dst2.exists()
    assert (dst2 / f"{app.br_id}_original.jpg").exists()
    # Исходная активная папка снова удалена.
    assert not storage.get_application_folder(app).exists()
