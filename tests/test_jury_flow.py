"""Покрытие алгоритма и инвариантов жюри (``services.jury``).

Базовая «коробка тестов» алгоритма уже в ``test_jury_algorithm.py``
(3 классических кейса). Здесь — расширение: проверяем инварианты
размера шорт-листа, детерминизм сортировки, граничные значения top_n
и сценарий «above_tie уже покрывает топ-N».
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from services.jury import _compute_outcome_from_data
from services.pools import all_pools


@dataclass
class _FakeApp:
    id: uuid.UUID
    created_at: datetime


def _make_apps(n: int) -> list[_FakeApp]:
    base = datetime(2026, 6, 15, 10, 0, 0)
    return [
        _FakeApp(id=uuid.UUID(int=i + 1), created_at=base + timedelta(minutes=i))
        for i in range(n)
    ]


class TestTopNInvariants:
    """Размерные инварианты ``_compute_outcome_from_data``."""

    def test_top_n_size_exact(self):
        """20 заявок, чёткая иерархия голосов → ровно 10 в топе."""
        apps = _make_apps(20)
        counts = {a.id: 50 - i for i, a in enumerate(apps)}
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        assert outcome.is_tied is False
        assert len(outcome.top_ids) == 10
        assert outcome.top_ids == [a.id for a in apps[:10]]

    def test_above_tie_covers_top_n_no_lottery(self):
        """Если above_tie уже == TOP_N — жребий не нужен."""
        apps = _make_apps(15)
        counts = {a.id: 0 for a in apps}
        for i in range(10):
            counts[apps[i].id] = 10
        for i in range(10, 15):
            counts[apps[i].id] = 5
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        assert outcome.is_tied is False
        assert len(outcome.top_ids) == 10

    def test_sort_is_deterministic_on_ties(self):
        """При равных голосах сортировка по (created_at ASC, id ASC) — стабильна."""
        apps = _make_apps(10)
        counts = {a.id: 1 for a in apps}
        outcome = _compute_outcome_from_data(apps, counts, top_n=10)
        for i in range(1, len(outcome.sorted_app_ids)):
            assert outcome.sorted_app_ids[i - 1].int < outcome.sorted_app_ids[i].int

    def test_smaller_top_n_works(self):
        """Алгоритм корректно работает с произвольным top_n."""
        apps = _make_apps(5)
        counts = {a.id: 0 for a in apps}
        counts[apps[0].id] = 3
        counts[apps[1].id] = 2
        for i in range(2, 5):
            counts[apps[i].id] = 1
        outcome = _compute_outcome_from_data(apps, counts, top_n=2)
        assert outcome.is_tied is False
        assert len(outcome.top_ids) == 2

    @pytest.mark.parametrize("total,top_n", [(5, 10), (10, 10), (0, 10)])
    def test_few_candidates_no_tie(self, total: int, top_n: int):
        """≤ top_n кандидатов → все в топе, ничьи быть не может."""
        apps = _make_apps(total)
        counts = {a.id: 5 for a in apps}
        outcome = _compute_outcome_from_data(apps, counts, top_n=top_n)
        assert outcome.is_tied is False
        assert len(outcome.top_ids) == total


class TestPoolStructure:
    """Структура пулов: 3 трека × 3 категории = 9 пулов."""

    def test_total_pools_count(self):
        pools = all_pools()
        assert len(pools) == 9

    def test_pool_keys_unique(self):
        pools = all_pools()
        keys = [(p.track, p.age_category) for p in pools]
        assert len(set(keys)) == len(pools)

    def test_pool_keys_have_three_tracks_and_three_ages(self):
        from database.models import AgeCategory, Track

        pools = all_pools()
        tracks = {p.track for p in pools}
        ages = {p.age_category for p in pools}
        assert tracks == set(Track)
        assert ages == set(AgeCategory)
