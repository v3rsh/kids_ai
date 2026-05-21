"""Тесты алгоритма голосования жюри §35.2.

Проверяем чистую функцию `_compute_outcome_from_data` на тривиальных
кейсах:

1. Нет ничьи → топ-N формируется за 1 раунд.
2. Ничья на границе → следующий раунд получает above_tie ∪ tie_zone.
3. Ничья на последнем раунде → жребий.
"""
import os
import sys
import unittest
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Минимальный env для импорта services.jury (он читает config)
os.environ.setdefault("BOT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("CTS_URL", "http://localhost")
os.environ.setdefault("BOT_SECRET_KEY", "test-secret")

# app/ в sys.path — как в test_validation.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from services.jury import _compute_outcome_from_data  # noqa: E402


@dataclass
class _FakeApp:
    """Минимальный «дабл» Application для теста: только нужные поля."""

    id: uuid.UUID
    created_at: datetime


def _make_apps(n: int) -> list[_FakeApp]:
    base = datetime(2026, 6, 1, 12, 0, 0)
    return [
        _FakeApp(id=uuid.UUID(int=i + 1), created_at=base + timedelta(minutes=i))
        for i in range(n)
    ]


class TestJuryAlgorithm(unittest.TestCase):
    """Алгоритм §35.2."""

    def test_no_tie_resolves_in_one_round(self):
        """Если у работы на позиции N строго больше YES, чем на N+1, —
        топ-N сформирован за раунд 1."""
        apps = _make_apps(15)
        counts = {a.id: 0 for a in apps}
        for i, a in enumerate(apps[:10]):
            counts[a.id] = 10 - i
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        self.assertFalse(outcome.is_tied)
        self.assertEqual(len(outcome.top_ids), 10)
        self.assertEqual(outcome.tie_ids, [])

    def test_tie_at_boundary_produces_next_round_candidates(self):
        """Ничья на границе → кандидаты следующего раунда = above_tie ∪ tie_zone."""
        apps = _make_apps(12)
        counts = {a.id: 0 for a in apps}
        for i in range(8):
            counts[apps[i].id] = 10
        for i in range(8, 12):
            counts[apps[i].id] = 5
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        self.assertTrue(outcome.is_tied)
        self.assertEqual(len(outcome.above_tie_ids), 8)
        self.assertEqual(len(outcome.tie_ids), 4)
        next_round_candidates = list(outcome.above_tie_ids) + list(outcome.tie_ids)
        self.assertEqual(len(next_round_candidates), 12)

    def test_persistent_tie_into_last_round(self):
        """Ничья в раунде 2 → раунд 3. Имитация многораундовой эскалации."""
        apps = _make_apps(12)
        counts_r1 = {a.id: (10 if i < 6 else 5) for i, a in enumerate(apps)}
        outcome1 = _compute_outcome_from_data(apps, counts_r1, top_n=10)
        self.assertTrue(outcome1.is_tied)
        r2_ids = list(outcome1.above_tie_ids) + list(outcome1.tie_ids)
        r2_candidates = [a for a in apps if a.id in r2_ids]

        counts_r2 = {a.id: 7 for a in r2_candidates}
        outcome2 = _compute_outcome_from_data(r2_candidates, counts_r2, top_n=10)
        self.assertTrue(outcome2.is_tied)
        self.assertEqual(set(outcome2.tie_ids), {a.id for a in r2_candidates})

    def test_strict_inequality_above_tie(self):
        """Случай: одна работа на позиции N, ничья ниже — топ-N формируется."""
        apps = _make_apps(15)
        counts = {a.id: 0 for a in apps}
        for i in range(10):
            counts[apps[i].id] = 20 - i
        for i in range(10, 15):
            counts[apps[i].id] = 3
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        self.assertFalse(outcome.is_tied)
        self.assertEqual(len(outcome.top_ids), 10)

    def test_fewer_candidates_than_top_n(self):
        """Если кандидатов меньше N — все в топ, ничьи быть не может."""
        apps = _make_apps(5)
        counts = {a.id: 1 for a in apps}
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        self.assertFalse(outcome.is_tied)
        self.assertEqual(len(outcome.top_ids), 5)

    def test_deterministic_sort_by_created_at(self):
        """При равных голосах порядок — по (created_at ASC, id ASC)."""
        apps = _make_apps(5)
        counts = {a.id: 1 for a in apps}
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        self.assertEqual(outcome.sorted_app_ids, [a.id for a in apps])


if __name__ == "__main__":
    unittest.main()
