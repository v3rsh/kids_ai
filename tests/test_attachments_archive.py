"""Юнит-тесты ``services.attachments_archive`` (Phase 1B архивации).

Покрытие:
- ``estimate_archive_budget``: арифметика с подменой
  ``get_disk_usage_bytes`` и мини-каталога ``ATTACHMENTS_DIR``;
- pre-flight ``ArchiveBudgetExceeded`` при превышении
  ``DISK_BLOCK_PCT`` — целевой каталог НЕ создаётся;
- happy path: копия мини-фикстуры в tmp ``ARCHIVE_DIR``, манифест
  и summary.txt;
- ``progress_cb`` вызывается минимум один раз на каждый BR-ID-каталог.

DB-вызов ``_load_intake_modes`` заглушается, чтобы тесты не зависели
от поднятого PostgreSQL.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services import attachments_archive as aa
from services.attachments_archive import (
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
        # Два файла суммарно ровно 30 байт.
        (src / "a.txt").write_bytes(b"x" * 10)
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"y" * 20)
        monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)

        # 50 ГБ занято из 100 ГБ всего.
        total = 100 * 1024 ** 3
        used = 50 * 1024 ** 3
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (used, total)
        )
        # Жёстко зафиксируем порог, чтобы pre-flight-блок не зависел
        # от глобального DISK_BLOCK_PCT окружения теста.
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        budget = await estimate_archive_budget()

        assert budget.attachments_bytes == 30
        assert budget.total_bytes == total
        assert budget.used_bytes == used
        assert budget.free_bytes == total - used
        # after_used = used + 30, after_pct практически 50%.
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
        (src / "big.bin").write_bytes(b"z" * (50 * 1024 ** 3 // (1024 ** 3) * 0 + 100))
        # ↑ 100 байт — символический объём, для арифметики после-блока.
        monkeypatch.setattr(aa, "ATTACHMENTS_DIR", src)

        total = 1000
        used = 900  # 90 %
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (used, total)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        budget = await estimate_archive_budget()
        # used + 100 = 1000 → 100 % ≥ 95 %.
        assert budget.after_pct >= budget.block_pct


# =====================================================================
# Pre-flight: ArchiveBudgetExceeded и НЕ-создание каталога
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
        # Подменяем budget так, чтобы after_pct гарантированно превысил порог.
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

        target = fake_archive_dir / "BR-2026_forced"

        with pytest.raises(ArchiveBudgetExceeded) as excinfo:
            await archive_attachments_to_disk(target_dir=target)

        # Бюджет несётся в исключении — UI покажет его в confirm.
        assert excinfo.value.budget is bad_budget
        assert excinfo.value.budget.after_pct >= excinfo.value.budget.block_pct
        # Целевой каталог НЕ создан.
        assert not target.exists()
        # В ARCHIVE_DIR нет случайных файлов.
        assert list(fake_archive_dir.iterdir()) == []


# =====================================================================
# Happy path: копия + manifest + summary + progress_cb
# =====================================================================


class TestHappyPath:
    async def test_copies_and_writes_manifest(
        self,
        monkeypatch,
        fake_attachments: Path,
        fake_archive_dir: Path,
        stub_intake_modes,
    ):
        # Безопасный бюджет: куча свободного места.
        monkeypatch.setattr(
            aa, "get_disk_usage_bytes", lambda: (0, 10 * 1024 ** 4)
        )
        monkeypatch.setattr(aa, "DISK_BLOCK_PCT", 95)

        calls: list[tuple[int, int, str, int]] = []

        async def _progress(index, total, br_id, size_bytes):
            calls.append((index, total, br_id, size_bytes))

        target = fake_archive_dir / "BR-2026_test"
        result = await archive_attachments_to_disk(
            target_dir=target,
            progress_cb=_progress,
        )

        assert result == target
        assert target.is_dir()

        # ----- Manifest -----
        manifest_path = target / "archive_manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text("utf-8"))
        br_ids = {entry["br_id"] for entry in manifest["entries"]}
        assert br_ids == {"BR-2026-0001", "BR-2026-0002", "BR-2026-0042"}
        assert manifest["br_folders_count"] == 3
        assert manifest["source"] == str(fake_attachments)
        assert manifest["destination"] == str(target)

        # intake_mode подтягивается из заглушки _load_intake_modes.
        for entry in manifest["entries"]:
            if entry["br_id"] == "BR-2026-0042":
                assert entry["intake_mode"] == "links"
            else:
                assert entry["intake_mode"] == "files"
            # relative_path — путь ВНУТРИ ATTACHMENTS_DIR.
            assert entry["relative_path"].startswith("2026-06-")
            assert entry["br_id"] in entry["relative_path"]
            assert entry["size_bytes"] > 0

        # ----- Summary -----
        summary_path = target / "summary.txt"
        assert summary_path.exists()
        summary = summary_path.read_text("utf-8")
        assert "BR-каталогов: 3" in summary
        assert str(fake_attachments) in summary

        # ----- Файлы реально скопированы по той же структуре -----
        copied = target / "2026-06-01" / "01_traditional" / "7-12"
        assert copied.exists()
        any_br = next(copied.iterdir())
        assert (any_br / "meta.txt").read_bytes() == b"meta"

        # ----- progress_cb: ≥ 1 раз на каждый BR-ID -----
        assert len(calls) >= 3
        called_br_ids = {br for (_idx, _total, br, _size) in calls}
        assert called_br_ids == br_ids
        # total в каждом вызове — общее число BR-ID, index ∈ [1..total].
        for idx, total, _br, _size in calls:
            assert total == 3
            assert 1 <= idx <= 3

    async def test_target_dir_exists_raises(
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

        target = fake_archive_dir / "exists"
        target.mkdir()

        with pytest.raises(RuntimeError, match="уже существует"):
            await archive_attachments_to_disk(target_dir=target)

    async def test_empty_source_writes_manifest(
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

        target = fake_archive_dir / "BR-2026_empty"
        result = await archive_attachments_to_disk(target_dir=target)
        assert result == target
        manifest = json.loads((target / "archive_manifest.json").read_text("utf-8"))
        assert manifest["br_folders_count"] == 0
        assert manifest["entries"] == []


# =====================================================================
# _find_br_id_folders — sanity
# =====================================================================


class TestFindBrIdFolders:
    def test_skips_non_br_subdirs(self, tmp_path: Path):
        # Не-BR-папки рядом — игнорируются.
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
