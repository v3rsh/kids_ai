"""Тесты алгоритма голосования жюри.

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
    """Алгоритм формирования топ-N и жребия."""

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


class TestIncrementalShortlist(unittest.TestCase):
    """Инкрементальная фиксация шорт-листа с динамическим ``top_n``.

    В новой модели (см. ТЗ §35.5) above_tie каждого раунда фиксируется
    сразу в `V_TOP_10`, в следующий раунд уходит только tie-зона
    с уменьшенным ``top_n = TOP_N - already_fixed``.
    """

    def test_round_after_partial_fix_uses_smaller_top_n(self):
        """После R1 зафиксировано 8 above_tie → в R2 ``top_n=2`` (10-8)."""
        # Симулируем R2: на входе только tie_zone из 5 заявок, нужно выбрать 2.
        apps = _make_apps(5)
        counts = {a.id: 0 for a in apps}
        counts[apps[0].id] = 7
        counts[apps[1].id] = 6
        for i in range(2, 5):
            counts[apps[i].id] = 3
        outcome = _compute_outcome_from_data(apps, counts, top_n=2)
        self.assertFalse(outcome.is_tied)
        self.assertEqual(len(outcome.top_ids), 2)
        self.assertEqual(set(outcome.top_ids), {apps[0].id, apps[1].id})

    def test_round_after_partial_fix_with_tie_at_remaining(self):
        """Tie на оставшейся вакансии: above_tie=1, tie_ids=N."""
        apps = _make_apps(4)
        counts = {a.id: 0 for a in apps}
        counts[apps[0].id] = 7
        # 3 заявки делят одну оставшуюся вакансию (top_n=2)
        for i in range(1, 4):
            counts[apps[i].id] = 4
        outcome = _compute_outcome_from_data(apps, counts, top_n=2)
        self.assertTrue(outcome.is_tied)
        self.assertEqual(outcome.above_tie_ids, [apps[0].id])
        self.assertEqual(set(outcome.tie_ids), {apps[1].id, apps[2].id, apps[3].id})

    def test_top_n_zero_returns_empty(self):
        """``top_n=0`` (пул уже заполнен) → ни above_tie, ни tie_ids."""
        apps = _make_apps(3)
        counts = {a.id: 5 for a in apps}
        outcome = _compute_outcome_from_data(apps, counts, top_n=0)
        self.assertFalse(outcome.is_tied)
        self.assertEqual(outcome.top_ids, [])
        self.assertEqual(outcome.above_tie_ids, [])
        self.assertEqual(outcome.tie_ids, [])

    def test_unlimited_rounds_simulation(self):
        """5+ раундов: ничья в tie-зоне держится до выхода (auto_lot=off)."""
        # 4 заявки на 2 вакансии, каждый раунд: 2 побеждают, 2 в ничье.
        apps = _make_apps(4)
        # Раунд N: tie всё ещё, выходит 1 выше + 3 в tie.
        for round_no in range(1, 6):
            counts = {a.id: 4 for a in apps[:1]}
            counts.update({a.id: 3 for a in apps[1:]})
            outcome = _compute_outcome_from_data(apps, counts, top_n=2)
            self.assertTrue(outcome.is_tied)
            self.assertEqual(outcome.above_tie_ids, [apps[0].id])
            self.assertEqual(len(outcome.tie_ids), 3)


if __name__ == "__main__":
    unittest.main()
