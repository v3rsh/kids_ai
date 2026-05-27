"""Тесты агрегатов /admin_stats — 9 пулов трек × возраст."""
from __future__ import annotations

from database.models import AgeCategory, Track
from services.admin import (
    build_by_pool_counts,
    pool_label,
    pool_labels_in_order,
)


class TestAdminPoolStats:
    def test_pool_labels_in_order_returns_nine_pools(self) -> None:
        labels = pool_labels_in_order()
        assert len(labels) == 9
        assert len(set(labels)) == 9

    def test_pool_label_format(self) -> None:
        assert pool_label(Track.TRADITIONAL, AgeCategory.AGE_0_6) == (
            "Традиционное рисование / 0–6"
        )

    def test_build_by_pool_counts_fills_missing_with_zero(self) -> None:
        raw = {
            (Track.TRADITIONAL, AgeCategory.AGE_0_6): 5,
            (Track.AI, AgeCategory.AGE_13_18): 2,
        }
        result = build_by_pool_counts(raw)

        assert len(result) == 9
        assert result[pool_label(Track.TRADITIONAL, AgeCategory.AGE_0_6)] == 5
        assert result[pool_label(Track.AI, AgeCategory.AGE_13_18)] == 2
        assert result[pool_label(Track.TRADITIONAL, AgeCategory.AGE_7_12)] == 0
        assert result[pool_label(Track.HANDMADE_TO_AI, AgeCategory.AGE_0_6)] == 0

    def test_build_by_pool_counts_empty_raw(self) -> None:
        result = build_by_pool_counts({})
        assert len(result) == 9
        assert all(count == 0 for count in result.values())
