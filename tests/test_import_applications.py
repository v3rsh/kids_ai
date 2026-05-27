"""Unit-тесты для ``scripts/import_applications.py``."""
from __future__ import annotations

import csv
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from database.models import FileKind, ModerationStatus, Track
from scripts.import_applications import (
    file_kind_for_track,
    load_csv_rows,
    parse_seed_row,
    resolve_parent_huid,
)


class TestFileKindForTrack:
    def test_traditional(self):
        assert file_kind_for_track("TRADITIONAL") is FileKind.ORIGINAL

    def test_ai(self):
        assert file_kind_for_track("AI") is FileKind.AI_IMAGE

    def test_handmade(self):
        assert file_kind_for_track("HANDMADE_TO_AI") is FileKind.DIPTYCH

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Неизвестный track"):
            file_kind_for_track("BOGUS")


class TestParseSeedRow:
    def test_happy_path(self):
        row = parse_seed_row(
            {
                "seq": "001",
                "image_file": "001.jpg",
                "child_name": "Миша",
                "child_age": "5",
                "track": "TRADITIONAL",
                "title": "Заголовок",
                "description": "Описание работы.",
            }
        )
        assert row.seq == "001"
        assert row.child_age == 5
        assert row.track == "TRADITIONAL"

    def test_unknown_track_raises(self):
        with pytest.raises(ValueError, match="неизвестный track"):
            parse_seed_row(
                {
                    "seq": "001",
                    "image_file": "001.jpg",
                    "child_name": "Миша",
                    "child_age": "5",
                    "track": "WRONG",
                    "title": "T",
                    "description": "D",
                }
            )


class TestLoadCsvRows:
    def test_reads_manifest(self, tmp_path: Path):
        csv_path = tmp_path / "applications.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "seq",
                    "image_file",
                    "child_name",
                    "child_age",
                    "track",
                    "title",
                    "description",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "seq": "001",
                    "image_file": "001.jpg",
                    "child_name": "Миша",
                    "child_age": "5",
                    "track": "TRADITIONAL",
                    "title": "T",
                    "description": "D",
                }
            )

        rows = load_csv_rows(csv_path)
        assert len(rows) == 1
        assert rows[0].image_file == "001.jpg"


class TestResolveParentHuid:
    def test_from_explicit(self):
        uid = uuid.uuid4()
        assert resolve_parent_huid(str(uid)) == uid

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch):
        uid = uuid.uuid4()
        monkeypatch.setenv("ADMIN_HUID", str(uid))
        # перечитать config.ADMIN_HUID нельзя без reload — патчим модуль
        import scripts.import_applications as imp

        monkeypatch.setattr(imp, "ADMIN_HUID", str(uid))
        assert resolve_parent_huid(None) == uid

    def test_missing_raises(self, monkeypatch: pytest.MonkeyPatch):
        import scripts.import_applications as imp

        monkeypatch.setattr(imp, "ADMIN_HUID", None)
        with pytest.raises(ValueError, match="parent_huid не задан"):
            resolve_parent_huid(None)


class TestSeedCsvManifest:
    """Проверка сгенерированного манифеста в репозитории."""

    @pytest.fixture
    def manifest_path(self) -> Path:
        return Path(__file__).resolve().parents[1] / "app" / "seed_data" / "applications.csv"

    def test_has_100_rows(self, manifest_path: Path):
        rows = load_csv_rows(manifest_path)
        assert len(rows) == 100

    def test_track_totals(self, manifest_path: Path):
        rows = load_csv_rows(manifest_path)
        by_track = {}
        for row in rows:
            by_track[row.track] = by_track.get(row.track, 0) + 1
        assert by_track["TRADITIONAL"] == 26
        assert by_track["AI"] == 48
        assert by_track["HANDMADE_TO_AI"] == 26

    def test_ai_7_12_pool_has_32(self, manifest_path: Path):
        rows = load_csv_rows(manifest_path)
        count = sum(
            1 for r in rows if r.track == "AI" and 7 <= r.child_age <= 12
        )
        assert count == 32

    def test_unique_submission_keys(self, manifest_path: Path):
        rows = load_csv_rows(manifest_path)
        keys = {(r.child_name, r.child_age, r.track) for r in rows}
        assert len(keys) == 100


class TestImportOneRowSkipDuplicate:
    @pytest.mark.asyncio
    async def test_skips_existing(self, tmp_path: Path):
        from scripts.import_applications import import_one_row

        img = tmp_path / "001.jpg"
        img.write_bytes(b"fake jpeg")

        row = parse_seed_row(
            {
                "seq": "001",
                "image_file": "001.jpg",
                "child_name": "Миша",
                "child_age": "5",
                "track": "TRADITIONAL",
                "title": "T",
                "description": "D",
            }
        )

        existing = AsyncMock()
        existing.br_id = "BR-2026-0001"

        with patch(
            "scripts.import_applications.applications_service.find_possible_duplicate",
            new=AsyncMock(return_value=existing),
        ):
            result = await import_one_row(
                row,
                parent_huid=uuid.uuid4(),
                parent_full_name="Test",
                parent_division="Div",
                images_dir=tmp_path,
                moderation_status=ModerationStatus.DOPUSHCHENO,
                dry_run=False,
            )

        assert result == "skipped"
