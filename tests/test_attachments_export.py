"""Юнит-тесты ``services.attachments_export``.

Покрытие:
- ``_build_meta_bytes`` — детерминированный текст meta.txt;
- ``_build_app_zip_bytes`` — три ветки (FILES, LINKS+ссылка,
  LINKS-черновик без ссылки) + сценарий oversize;
- ``_manifest_csv_bytes`` / ``_links_txt_bytes`` — формат и BOM;
- ``iter_attachments_export`` на пустом селекторе — отдаёт только
  summary, без путаницы порядка элементов.

Намеренно НЕ трогаем настоящую БД и `bot.send_message` — все БД-вызовы
подменяются на in-memory заглушки.
"""
from __future__ import annotations

import io
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import attachments_export as ax_module
from services.attachments_export import (
    ExportSelector,
    _build_app_zip_bytes,
    _build_meta_bytes,
    _links_txt_bytes,
    _manifest_csv_bytes,
    iter_attachments_export,
)


# =====================================================================
# Suspended enums (минимально, чтобы DTO не тащил полный SQLAlchemy)
# =====================================================================


class _Track:
    def __init__(self, value: str) -> None:
        self.value = value


class _Age:
    def __init__(self, value: str) -> None:
        self.value = value


class _ModerationStatus:
    def __init__(self, value: str) -> None:
        self.value = value


class _JuryStatus:
    def __init__(self, value: str) -> None:
        self.value = value


from database.models import IntakeMode  # noqa: E402  (после _Track для читаемости)


def _make_app(
    *,
    br_id: str = "BR-2026-0042",
    intake_mode: IntakeMode = IntakeMode.FILES,
    cloud_link: str | None = None,
    files: list[SimpleNamespace] | None = None,
):
    return SimpleNamespace(
        id=uuid.uuid4(),
        br_id=br_id,
        parent_huid=uuid.uuid4(),
        parent_full_name="Иванов Сергей Петрович",
        parent_division="ИБ / Команда X",
        parent_ad_login="ivanov",
        parent_contact="ivanov@example.com",
        child_name="Анна",
        child_age=8,
        age_category=_Age("7–12"),
        track=_Track("Традиционное рисование"),
        title="Безопасный интернет",
        description="  Описание работы  ",
        intake_mode=intake_mode,
        cloud_link=cloud_link,
        moderation_status=_ModerationStatus("принято"),
        jury_status=_JuryStatus("в топ-10"),
        created_at=datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc),
        files=files or [],
    )


def _make_file(stored: str, original: str, size: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        kind=SimpleNamespace(name="ORIGINAL"),
        angle_no=None,
        original_filename=original,
        stored_filename=stored,
        relative_path=f"2026-06-01/test/{stored}",
        size_bytes=size,
        mime_type="image/jpeg",
        uploaded_at=datetime(2026, 6, 1, 9, 35, tzinfo=timezone.utc),
    )


# =====================================================================
# _build_meta_bytes
# =====================================================================


class TestBuildMetaBytes:
    def test_contains_all_required_fields(self):
        app = _make_app()
        text = _build_meta_bytes(app, []).decode("utf-8")
        assert "ID заявки: BR-2026-0042" in text
        assert "ФИО родителя: Иванов Сергей Петрович" in text
        assert "Имя ребёнка: Анна" in text
        assert "Возраст: 8" in text
        assert "Возрастная категория: 7–12" in text
        assert "Трек: Традиционное рисование" in text
        assert "Описание: Описание работы" in text  # strip пробелов
        assert "Статус модерации: принято" in text
        assert "Статус жюри: в топ-10" in text
        assert "Режим приёма: files" in text
        assert "Исходные имена файлов: (нет)" in text

    def test_links_mode_inserts_cloud_link(self):
        app = _make_app(
            intake_mode=IntakeMode.LINKS, cloud_link="https://disk.yandex.ru/d/abc"
        )
        text = _build_meta_bytes(app, []).decode("utf-8")
        assert "Режим приёма: links" in text
        assert "Ссылка на папку (cloud): https://disk.yandex.ru/d/abc" in text

    def test_files_listed(self):
        app = _make_app()
        files = [
            _make_file("BR-2026-0042_original.jpg", "Original.JPG", 1024),
        ]
        text = _build_meta_bytes(app, files).decode("utf-8")
        assert "Original.JPG → BR-2026-0042_original.jpg" in text


# =====================================================================
# _manifest_csv_bytes / _links_txt_bytes
# =====================================================================


class TestManifestAndLinks:
    def test_manifest_has_bom_and_semicolon(self):
        rows = [
            {
                "br_id": "BR-2026-0001",
                "track": "Традиционное рисование",
                "age_category": "7–12",
                "intake_mode": "files",
                "moderation_status": "принято",
                "jury_status": "не передано жюри",
                "status": "files",
                "files_count": 2,
                "files_bytes": 2048,
                "cloud_link": "",
                "inner_path": "2026-06-01/Традиционное/7-12/Иванов_Сергей_Анна_BR-2026-0001",
                "zip_filename": "BR-2026-0001.zip",
            }
        ]
        payload = _manifest_csv_bytes(rows)
        text = payload.decode("utf-8")
        assert text.startswith("\ufeff")
        first_line = text.splitlines()[0]
        assert ";" in first_line
        assert first_line.startswith("\ufeffbr_id;")

    def test_links_txt_skips_empty(self):
        rows = [
            {"br_id": "A", "cloud_link": "https://disk.yandex.ru/d/abc"},
            {"br_id": "B", "cloud_link": ""},
            {"br_id": "C", "cloud_link": None},
            {"br_id": "D", "cloud_link": "https://example.com/x"},
        ]
        text = _links_txt_bytes(rows).decode("utf-8")
        assert "A\thttps://disk.yandex.ru/d/abc" in text
        assert "D\thttps://example.com/x" in text
        assert "B\t" not in text
        assert "C\t" not in text


# =====================================================================
# _build_app_zip_bytes
# =====================================================================


@pytest.fixture(autouse=True)
def _patch_paths(monkeypatch, tmp_path: Path):
    """Перенаправляем ATTACHMENTS_DIR на tmp; build_meta использует
    storage.get_application_folder, поэтому подменяем и его."""
    monkeypatch.setattr(ax_module, "ATTACHMENTS_DIR", tmp_path)

    def _fake_folder(app):
        return tmp_path / "subdir" / app.br_id

    monkeypatch.setattr(ax_module, "get_application_folder", _fake_folder)
    return tmp_path


class TestBuildAppZipBytes:
    @pytest.mark.asyncio
    async def test_links_with_cloud_link(self, _patch_paths):
        app = _make_app(
            intake_mode=IntakeMode.LINKS,
            cloud_link="https://disk.yandex.ru/d/abc",
        )
        zip_bytes, info = await _build_app_zip_bytes(
            app, max_part_bytes=10 * 1024
        )
        assert info["status"] == "links_only"
        assert zip_bytes
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert any(n.endswith("/meta.txt") for n in names)
            assert any(n.endswith("/cloud_link.txt") for n in names)

    @pytest.mark.asyncio
    async def test_links_pending(self, _patch_paths):
        app = _make_app(intake_mode=IntakeMode.LINKS, cloud_link=None)
        zip_bytes, info = await _build_app_zip_bytes(
            app, max_part_bytes=10 * 1024
        )
        assert zip_bytes == b""
        assert info["status"] == "pending_link"

    @pytest.mark.asyncio
    async def test_files_zip_contains_meta_and_payload(self, _patch_paths):
        folder = _patch_paths / "subdir" / "BR-2026-0042"
        folder.mkdir(parents=True, exist_ok=True)
        # Файл из ApplicationFile.relative_path лежит в ATTACHMENTS_DIR
        file_rel = "2026-06-01/test/BR-2026-0042_original.jpg"
        (_patch_paths / "2026-06-01/test").mkdir(parents=True, exist_ok=True)
        (_patch_paths / file_rel).write_bytes(b"\x89PNG-fake-bytes")

        app = _make_app()
        af = _make_file("BR-2026-0042_original.jpg", "Original.JPG", 14)
        app.files = [af]

        zip_bytes, info = await _build_app_zip_bytes(
            app, max_part_bytes=10 * 1024 * 1024
        )
        assert info["status"] == "files"
        assert zip_bytes
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert any(n.endswith("/meta.txt") for n in names)
            assert any(n.endswith("/BR-2026-0042_original.jpg") for n in names)

    @pytest.mark.asyncio
    async def test_oversize_meta_only(self, _patch_paths):
        """Если суммарный размер файлов > лимита — оставляем только meta-блок."""
        app = _make_app()
        af = _make_file("big.bin", "big.bin", 10 * 1024 * 1024)
        app.files = [af]
        zip_bytes, info = await _build_app_zip_bytes(
            app, max_part_bytes=1 * 1024
        )
        assert info["status"] == "oversize_meta_only"
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert any(n.endswith("/meta.txt") for n in names)
            # Большой бинарь в архив не попал — иначе фишка теряется
            assert not any(n.endswith("/big.bin") for n in names)


# =====================================================================
# iter_attachments_export — empty path
# =====================================================================


class TestIterEmpty:
    @pytest.mark.asyncio
    async def test_empty_selector_yields_only_summary(self, monkeypatch):
        """Если БД не отдала ни одной заявки — отдаём один summary-item."""
        async def _fake_load(_selector):
            return []

        monkeypatch.setattr(
            ax_module, "_load_selected_applications", _fake_load
        )

        items = []
        async for item in iter_attachments_export(ExportSelector.SHORTLIST):
            items.append(item)

        assert len(items) == 1
        assert items[0].kind == "summary"
        assert "всего заявок: **0**" in items[0].caption
