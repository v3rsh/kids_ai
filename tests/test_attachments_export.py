"""Юнит-тесты ``services.attachments_export``.

Покрытие:
- ``_build_meta_bytes`` — детерминированный текст meta.txt;
- ``_collect_app_entries`` — три ветки (FILES, LINKS+ссылка,
  LINKS-черновик без ссылки) + сценарий oversize;
- ``_manifest_csv_bytes`` / ``_links_txt_bytes`` — формат и BOM;
- ``_build_pool_targz`` — группировка пула в один tar.gz и split
  на части при превышении ``EXPORT_MAX_PART_BYTES``;
- ``iter_attachments_export`` на пустом селекторе — отдаёт только
  summary; на нескольких пулах — по одному tar.gz на пул с
  ожидаемыми короткими именами.

Намеренно НЕ трогаем настоящую БД и `bot.send_message` — все БД-вызовы
подменяются на in-memory заглушки.
"""
from __future__ import annotations

import io
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.models import AgeCategory, IntakeMode, Track
from services import attachments_export as ax_module
from services.attachments_export import (
    ExportSelector,
    _build_meta_bytes,
    _build_pool_targz,
    _collect_app_entries,
    _links_txt_bytes,
    _manifest_csv_bytes,
    iter_attachments_export,
)


# =====================================================================
# Suspended enums (минимально, чтобы DTO не тащил полный SQLAlchemy)
# =====================================================================


class _ModerationStatus:
    def __init__(self, value: str) -> None:
        self.value = value


class _JuryStatus:
    def __init__(self, value: str) -> None:
        self.value = value


def _make_app(
    *,
    br_id: str = "BR-2026-0042",
    intake_mode: IntakeMode = IntakeMode.FILES,
    track: Track = Track.TRADITIONAL,
    age: AgeCategory = AgeCategory.AGE_7_12,
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
        age_category=age,
        track=track,
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


def _tar_names(payload: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        return tar.getnames()


def _tar_read(payload: bytes, member: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        f = tar.extractfile(member)
        assert f is not None, f"Член {member!r} не найден"
        return f.read()


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
    def test_manifest_has_bom_and_semicolon_and_archive_filename(self):
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
                "archive_filename": "trad-7-12.tar.gz",
            }
        ]
        payload = _manifest_csv_bytes(rows)
        text = payload.decode("utf-8")
        assert text.startswith("\ufeff")
        first_line = text.splitlines()[0]
        assert ";" in first_line
        assert first_line.startswith("\ufeffbr_id;")
        assert "archive_filename" in first_line
        assert "zip_filename" not in first_line
        assert "trad-7-12.tar.gz" in text

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
# _collect_app_entries
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


class TestCollectAppEntries:
    @pytest.mark.asyncio
    async def test_links_with_cloud_link(self, _patch_paths):
        app = _make_app(
            intake_mode=IntakeMode.LINKS,
            cloud_link="https://disk.yandex.ru/d/abc",
        )
        entries, info = await _collect_app_entries(
            app, max_part_bytes=10 * 1024
        )
        assert info["status"] == "links_only"
        names = [n for n, _ in entries]
        assert any(n.endswith("/meta.txt") for n in names)
        assert any(n.endswith("/cloud_link.txt") for n in names)

    @pytest.mark.asyncio
    async def test_links_pending(self, _patch_paths):
        app = _make_app(intake_mode=IntakeMode.LINKS, cloud_link=None)
        entries, info = await _collect_app_entries(
            app, max_part_bytes=10 * 1024
        )
        assert entries == []
        assert info["status"] == "pending_link"

    @pytest.mark.asyncio
    async def test_files_entries_contain_meta_and_payload(self, _patch_paths):
        folder = _patch_paths / "subdir" / "BR-2026-0042"
        folder.mkdir(parents=True, exist_ok=True)
        file_rel = "2026-06-01/test/BR-2026-0042_original.jpg"
        (_patch_paths / "2026-06-01/test").mkdir(parents=True, exist_ok=True)
        (_patch_paths / file_rel).write_bytes(b"\x89PNG-fake-bytes")

        app = _make_app()
        af = _make_file("BR-2026-0042_original.jpg", "Original.JPG", 14)
        app.files = [af]

        entries, info = await _collect_app_entries(
            app, max_part_bytes=10 * 1024 * 1024
        )
        assert info["status"] == "files"
        names = [n for n, _ in entries]
        assert any(n.endswith("/meta.txt") for n in names)
        assert any(n.endswith("/BR-2026-0042_original.jpg") for n in names)

    @pytest.mark.asyncio
    async def test_oversize_meta_only(self, _patch_paths):
        """Если суммарный размер файлов > лимита — оставляем только meta-блок."""
        app = _make_app()
        af = _make_file("big.bin", "big.bin", 10 * 1024 * 1024)
        app.files = [af]
        entries, info = await _collect_app_entries(
            app, max_part_bytes=1 * 1024
        )
        assert info["status"] == "oversize_meta_only"
        names = [n for n, _ in entries]
        assert any(n.endswith("/meta.txt") for n in names)
        assert not any(n.endswith("/big.bin") for n in names)


# =====================================================================
# _build_pool_targz — группировка и split на части
# =====================================================================


class TestBuildPoolTarGz:
    @pytest.mark.asyncio
    async def test_single_part_when_pool_fits(self, _patch_paths):
        """Маленький пул → один tar.gz с короткой схемой имени."""
        apps = [
            _make_app(
                br_id="BR-2026-0001",
                intake_mode=IntakeMode.LINKS,
                cloud_link="https://example.com/a",
            ),
            _make_app(
                br_id="BR-2026-0002",
                intake_mode=IntakeMode.LINKS,
                cloud_link="https://example.com/b",
            ),
        ]
        parts, infos = await _build_pool_targz(
            apps,
            base_name="trad-7-12",
            max_part_bytes=10 * 1024 * 1024,
        )
        assert len(parts) == 1
        filename, payload, count = parts[0]
        assert filename == "trad-7-12.tar.gz"
        assert count == 2

        names = _tar_names(payload)
        joined = "\n".join(names)
        assert "BR-2026-0001" in joined
        assert "BR-2026-0002" in joined
        # У каждого info прокинули archive_filename.
        for info in infos:
            assert info["archive_filename"] == "trad-7-12.tar.gz"

    @pytest.mark.asyncio
    async def test_pending_link_skipped_but_in_infos(self, _patch_paths):
        """LINKS без ссылки не попадает в архив, но остаётся в infos."""
        apps = [
            _make_app(
                br_id="BR-2026-0001",
                intake_mode=IntakeMode.LINKS,
                cloud_link="https://example.com/a",
            ),
            _make_app(
                br_id="BR-2026-0009",
                intake_mode=IntakeMode.LINKS,
                cloud_link=None,
            ),
        ]
        parts, infos = await _build_pool_targz(
            apps,
            base_name="trad-7-12",
            max_part_bytes=10 * 1024 * 1024,
        )
        assert len(parts) == 1
        names = _tar_names(parts[0][1])
        joined = "\n".join(names)
        assert "BR-2026-0001" in joined
        assert "BR-2026-0009" not in joined

        by_br = {info["br_id"]: info for info in infos}
        assert by_br["BR-2026-0001"]["archive_filename"] == "trad-7-12.tar.gz"
        assert by_br["BR-2026-0009"]["archive_filename"] == ""
        assert by_br["BR-2026-0009"]["status"] == "pending_link"

    @pytest.mark.asyncio
    async def test_pool_split_on_oversize(self, _patch_paths):
        """Крошечный лимит → пул режется на partNN-части."""
        # Подкладываем реальные файлы, чтобы entries имели заметный
        # сырой размер.
        for br_id in ("BR-2026-0001", "BR-2026-0002", "BR-2026-0003"):
            folder = _patch_paths / "subdir" / br_id
            folder.mkdir(parents=True, exist_ok=True)
        (_patch_paths / "2026-06-01" / "test").mkdir(parents=True, exist_ok=True)

        apps = []
        for i, br_id in enumerate(
            ("BR-2026-0001", "BR-2026-0002", "BR-2026-0003"), start=1
        ):
            rel = f"2026-06-01/test/{br_id}.bin"
            (_patch_paths / rel).write_bytes(b"X" * 2048)
            app = _make_app(br_id=br_id)
            app.files = [_make_file(f"{br_id}.bin", f"{br_id}.bin", 2048)]
            apps.append(app)

        # Лимит ~1.5 КБ → каждая заявка идёт в свою часть (2 КБ payload).
        parts, infos = await _build_pool_targz(
            apps,
            base_name="trad-7-12",
            max_part_bytes=1500,
        )
        # Хотя бы две части, имена пронумерованы partNN.
        assert len(parts) >= 2
        filenames = [filename for filename, _, _ in parts]
        for i, fn in enumerate(filenames, start=1):
            assert fn == f"trad-7-12.part{i:02d}.tar.gz"

        # Все три BR-ID встречаются хотя бы в одной части.
        seen: set[str] = set()
        for _, payload, _ in parts:
            for n in _tar_names(payload):
                for br in ("BR-2026-0001", "BR-2026-0002", "BR-2026-0003"):
                    if br in n:
                        seen.add(br)
        assert seen == {"BR-2026-0001", "BR-2026-0002", "BR-2026-0003"}

        # У каждого info archive_filename совпадает с одной из реальных
        # частей.
        part_names = set(filenames)
        for info in infos:
            assert info["archive_filename"] in part_names

    @pytest.mark.asyncio
    async def test_empty_pool_returns_no_parts(self, _patch_paths):
        parts, infos = await _build_pool_targz(
            [],
            base_name="trad-7-12",
            max_part_bytes=10 * 1024,
        )
        assert parts == []
        assert infos == []


# =====================================================================
# iter_attachments_export
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


class TestIterPoolGrouping:
    @pytest.mark.asyncio
    async def test_pools_yield_short_named_tar_gz(
        self, monkeypatch, _patch_paths
    ):
        """Две заявки в двух разных пулах → два tar.gz с короткими именами."""
        app_trad = _make_app(
            br_id="BR-2026-0001",
            intake_mode=IntakeMode.LINKS,
            cloud_link="https://example.com/a",
            track=Track.TRADITIONAL,
            age=AgeCategory.AGE_7_12,
        )
        app_ai = _make_app(
            br_id="BR-2026-0002",
            intake_mode=IntakeMode.LINKS,
            cloud_link="https://example.com/b",
            track=Track.AI,
            age=AgeCategory.AGE_0_6,
        )

        async def _fake_load(_selector):
            return [app_trad, app_ai]

        monkeypatch.setattr(
            ax_module, "_load_selected_applications", _fake_load
        )

        items = []
        async for item in iter_attachments_export(ExportSelector.SHORTLIST):
            items.append(item)

        tar_items = [it for it in items if it.kind == "tar"]
        names = {it.filename for it in tar_items}
        assert names == {"ai-0-6.tar.gz", "trad-7-12.tar.gz"}

        # Финальные элементы: links + manifest + summary (по одному).
        kinds = [it.kind for it in items]
        assert kinds.count("links") == 1
        assert kinds.count("manifest") == 1
        assert kinds[-1] == "summary"

        # manifest содержит обе заявки и обе колонки archive_filename.
        manifest_item = next(it for it in items if it.kind == "manifest")
        manifest_text = manifest_item.payload.decode("utf-8")
        assert "BR-2026-0001" in manifest_text
        assert "BR-2026-0002" in manifest_text
        assert "trad-7-12.tar.gz" in manifest_text
        assert "ai-0-6.tar.gz" in manifest_text
