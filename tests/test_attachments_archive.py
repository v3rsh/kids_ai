"""Юнит-тесты ``services.attachments_archive`` (tar.gz архивация).

Покрытие:
- ``estimate_archive_budget``: арифметика с подменой
  ``get_disk_usage_bytes`` и мини-каталога ``ATTACHMENTS_DIR``;
- pre-flight ``ArchiveBudgetExceeded`` при превышении
  ``DISK_BLOCK_PCT`` — ``bd-full.tar.gz`` НЕ создаётся;
- happy path: ``bd-full.tar.gz`` собирается, содержит дерево
  ``attachments/`` + ``bd-full.manifest.json`` + ``bd-full.summary.txt``,
  рядом с архивом лежат те же manifest/summary;
- ротация существующего ``bd-full.tar.gz`` в ``bd-full.prev.tar.gz``;
- ``progress_cb`` вызывается минимум один раз на каждый BR-ID-каталог.

DB-вызов ``_load_intake_modes`` заглушается, чтобы тесты не зависели
от поднятого PostgreSQL.
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from services import attachments_archive as aa
from services.attachments_archive import (
    ARCHIVE_FILENAME,
    MANIFEST_FILENAME,
    PREV_ARCHIVE_FILENAME,
    SUMMARY_FILENAME,
    ArchiveBudget,
    ArchiveBudgetExceeded,
    _extract_br_id,
    _find_br_id_folders,
    archive_attachments_to_disk,
    estimate_archive_budget,
)


# =====================================================================
# Хелперы создания фикстуры дерева ATTACHMENTS_DIR
# =====================================================================


def _make_br_folder(
    root: Path,
    *,
    date: str,
    track: str,
    age: str,
    br_id: str,
    name_tail: str = "Иванов_Сергей_Анна",
    files: dict[str, bytes] | None = None,
) -> Path:
    """Создать BR-ID-каталог по каноничной структуре storage."""
    folder = root / date / track / age / f"{br_id}_{name_tail}"
    folder.mkdir(parents=True, exist_ok=True)
    files = files or {
        "meta.txt": b"meta",
        f"{br_id}_original.jpg": b"\x89PNG-fake",
    }
    for name, payload in files.items():
        (folder / name).write_bytes(payload)
    return folder


@pytest.fixture
def fake_attachments(monkeypatch, tmp_path: Path) -> Path:
    """Минимальный ATTACHMENTS_DIR с тремя BR-ID-каталогами в разных треках."""
    src = tmp_path / "attachments"
    src.mkdir(parents=True, exist_ok=True)
    _make_br_folder(
        src,
        date="2026-06-01",
        track="01_traditional",
        age="7-12",
        br_id="BR-2026-0001",
    )
    _make_br_folder(
        src,
        date="2026-06-01",
        track="02_ai",
        age="13-18",
        br_id="BR-2026-0002",
        files={"meta.txt": b"meta", "BR-2026-0002_ai-image.png": b"AI-bytes"},
    )
    _make_br_folder(
        src,
        date="2026-06-02",
        track="03_refine",
        age="0-6",
        br_id="BR-2026-0042",
    )
    monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)
    return src


@pytest.fixture
def fake_archive_dir(monkeypatch, tmp_path: Path) -> Path:
    """tmp ARCHIVE_DIR — заменяет module-level константу."""
    arch = tmp_path / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(aa, "ARCHIVE_DIR", arch)
    return arch


@pytest.fixture
def stub_intake_modes(monkeypatch):
    """Заглушка для DB-чтения — без поднятия PostgreSQL."""

    async def _fake_load() -> dict[str, str]:
        return {
            "BR-2026-0001": "files",
            "BR-2026-0002": "files",
            "BR-2026-0042": "links",
        }

    monkeypatch.setattr(aa, "_load_intake_modes", _fake_load)


def _tar_namelist(tar_path: Path) -> list[str]:
    with tarfile.open(tar_path, "r:gz") as tar:
        return tar.getnames()


def _tar_read(tar_path: Path, member: str) -> bytes:
    with tarfile.open(tar_path, "r:gz") as tar:
        f = tar.extractfile(member)
        assert f is not None, f"Член {member!r} не найден в {tar_path}"
        return f.read()


# =====================================================================
# Маленькие unit-тесты на хелперы
# =====================================================================


class TestExtractBrId:
    def test_canonical_name(self):
        assert _extract_br_id("BR-2026-0042_Иванов_Сергей_Анна") == "BR-2026-0042"

    def test_short_number(self):
        assert _extract_br_id("BR-2026-7_X") == "BR-2026-7"

    def test_non_br(self):
        assert _extract_br_id("01_traditional") is None
        assert _extract_br_id("BR_2026_0001") is None


# =====================================================================
# estimate_archive_budget
# =====================================================================


class TestEstimateArchiveBudget:
    async def test_math_with_mocked_disk(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        src = tmp_path / "attachments"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x" * 10)
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"y" * 20)
        monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)

        total = 100 * 1024 ** 3
        used = 50 * 1024 ** 3
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (used, total)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        budget = await estimate_archive_budget()

        assert budget.attachments_bytes == 30
        assert budget.total_bytes == total
        assert budget.used_bytes == used
        assert budget.free_bytes == total - used
        assert budget.after_used_bytes == used + 30
        assert 49.0 < budget.after_pct < 51.0
        assert budget.free_pct == pytest.approx(50.0, rel=1e-3)
        assert budget.block_pct == 95

    async def test_empty_attachments_dir(self, monkeypatch, tmp_path: Path):
        src = tmp_path / "attachments_empty"
        src.mkdir()
        monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (0, 100)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        budget = await estimate_archive_budget()
        assert budget.attachments_bytes == 0
        assert budget.after_pct == 0.0

    async def test_after_pct_crosses_threshold(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        """attachments + used → ровно над порогом 95%."""
        src = tmp_path / "attachments"
        src.mkdir()
        (src / "big.bin").write_bytes(b"z" * 100)
        monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)

        total = 1000
        used = 900  # 90 %
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (used, total)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        budget = await estimate_archive_budget()
        assert budget.after_pct >= budget.block_pct


# =====================================================================
# Pre-flight: ArchiveBudgetExceeded и НЕ-создание архива
# =====================================================================


class TestPreflightRefusal:
    async def test_raises_and_does_not_create_target(
        self,
        monkeypatch,
        tmp_path: Path,
        fake_attachments: Path,
        fake_archive_dir: Path,
        stub_intake_modes,
    ):
        bad_budget = ArchiveBudget(
            attachments_bytes=10,
            total_bytes=100,
            used_bytes=95,
            free_bytes=5,
            after_used_bytes=105,
            block_pct=95,
        )

        async def _fake_estimate() -> ArchiveBudget:
            return bad_budget

        monkeypatch.setattr(aa, "estimate_archive_budget", _fake_estimate)

        target = fake_archive_dir / "bd-full.tar.gz"

        with pytest.raises(ArchiveBudgetExceeded) as excinfo:
            await archive_attachments_to_disk(target=target)

        assert excinfo.value.budget is bad_budget
        assert excinfo.value.budget.after_pct >= excinfo.value.budget.block_pct
        assert not target.exists()
        # В ARCHIVE_DIR нет случайных файлов.
        assert list(fake_archive_dir.iterdir()) == []


# =====================================================================
# Happy path: tar.gz + manifest + summary + progress_cb
# =====================================================================


class TestHappyPath:
    async def test_creates_targz_with_manifest_and_summary(
        self,
        monkeypatch,
        fake_attachments: Path,
        fake_archive_dir: Path,
        stub_intake_modes,
    ):
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (0, 10 * 1024 ** 4)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        calls: list[tuple[int, int, str, int]] = []

        async def _progress(index, total, br_id, size_bytes):
            calls.append((index, total, br_id, size_bytes))

        result = await archive_attachments_to_disk(progress_cb=_progress)

        target = fake_archive_dir / ARCHIVE_FILENAME
        assert result == target
        assert target.is_file()

        # ----- Содержимое tar.gz -----
        names = _tar_namelist(target)
        # Корневые служебные файлы.
        assert MANIFEST_FILENAME in names
        assert SUMMARY_FILENAME in names
        # Все BR-ID-каталоги под префиксом attachments/.
        joined = "\n".join(names)
        assert "BR-2026-0001" in joined
        assert "BR-2026-0002" in joined
        assert "BR-2026-0042" in joined
        # meta.txt одной из заявок реально прочитан.
        meta_member = next(
            n for n in names
            if n.endswith("BR-2026-0001_Иванов_Сергей_Анна/meta.txt")
        )
        assert meta_member.startswith("attachments/")
        assert _tar_read(target, meta_member) == b"meta"

        # ----- Manifest внутри tar.gz -----
        manifest_bytes = _tar_read(target, MANIFEST_FILENAME)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        br_ids = {entry["br_id"] for entry in manifest["entries"]}
        assert br_ids == {"BR-2026-0001", "BR-2026-0002", "BR-2026-0042"}
        assert manifest["br_folders_count"] == 3
        assert manifest["source"] == str(fake_attachments)
        assert manifest["destination"] == str(target)

        for entry in manifest["entries"]:
            if entry["br_id"] == "BR-2026-0042":
                assert entry["intake_mode"] == "links"
            else:
                assert entry["intake_mode"] == "files"
            assert entry["relative_path"].startswith("2026-06-")
            assert entry["br_id"] in entry["relative_path"]
            assert entry["size_bytes"] > 0

        # ----- Summary внутри tar.gz -----
        summary_bytes = _tar_read(target, SUMMARY_FILENAME)
        summary = summary_bytes.decode("utf-8")
        assert "BR-каталогов: 3" in summary
        assert str(fake_attachments) in summary

        # ----- Manifest + summary РЯДОМ с tar.gz -----
        manifest_beside = fake_archive_dir / MANIFEST_FILENAME
        summary_beside = fake_archive_dir / SUMMARY_FILENAME
        assert manifest_beside.read_bytes() == manifest_bytes
        assert summary_beside.read_bytes() == summary_bytes

        # ----- progress_cb: ≥ 1 раз на каждый BR-ID -----
        assert len(calls) >= 3
        called_br_ids = {br for (_idx, _total, br, _size) in calls}
        assert called_br_ids == br_ids
        for idx, total, _br, _size in calls:
            assert total == 3
            assert 1 <= idx <= 3

    async def test_existing_archive_rotated_to_prev(
        self,
        monkeypatch,
        fake_attachments: Path,
        fake_archive_dir: Path,
        stub_intake_modes,
    ):
        """Если bd-full.tar.gz уже есть — он уходит в bd-full.prev.tar.gz."""
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (0, 10 * 1024 ** 4)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        target = fake_archive_dir / ARCHIVE_FILENAME
        prev = fake_archive_dir / PREV_ARCHIVE_FILENAME

        # Симулируем «предыдущий» архив с известным содержимым.
        target.write_bytes(b"OLD_ARCHIVE_PAYLOAD")
        assert not prev.exists()

        await archive_attachments_to_disk()

        assert target.is_file()
        # Новый архив реально является tar.gz (начало — магия gzip 1f 8b).
        head = target.read_bytes()[:2]
        assert head == b"\x1f\x8b"
        # Старый ушёл в .prev.
        assert prev.read_bytes() == b"OLD_ARCHIVE_PAYLOAD"

    async def test_empty_source_writes_empty_targz(
        self,
        monkeypatch,
        tmp_path: Path,
        fake_archive_dir: Path,
        stub_intake_modes,
    ):
        """Пустой ATTACHMENTS_DIR → пустой манифест без падений."""
        src = tmp_path / "attachments_empty"
        src.mkdir()
        monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (0, 10 * 1024 ** 4)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        result = await archive_attachments_to_disk()
        target = fake_archive_dir / ARCHIVE_FILENAME
        assert result == target
        assert target.is_file()

        names = _tar_namelist(target)
        assert MANIFEST_FILENAME in names
        assert SUMMARY_FILENAME in names

        manifest = json.loads(
            _tar_read(target, MANIFEST_FILENAME).decode("utf-8")
        )
        assert manifest["br_folders_count"] == 0
        assert manifest["entries"] == []


# =====================================================================
# _find_br_id_folders — sanity
# =====================================================================


class TestFindBrIdFolders:
    def test_skips_non_br_subdirs(self, tmp_path: Path):
        (tmp_path / "01_traditional").mkdir()
        (tmp_path / "99_rejected").mkdir()
        br = tmp_path / "01_traditional" / "BR-2026-0001_X"
        br.mkdir()
        found = _find_br_id_folders(tmp_path)
        assert found == [br]

    def test_returns_sorted(self, tmp_path: Path):
        (tmp_path / "B").mkdir()
        (tmp_path / "A").mkdir()
        b2 = tmp_path / "B" / "BR-2026-0002_X"
        b2.mkdir()
        a1 = tmp_path / "A" / "BR-2026-0001_X"
        a1.mkdir()
        assert _find_br_id_folders(tmp_path) == [a1, b2]
