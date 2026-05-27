"""Тесты агрегатов /admin_stats — 9 пулов трек × возраст."""
from __future__ import annotations

from database.models import AgeCategory, Track
from services.admin import (
    build_by_pool_counts,
    format_track_age_stats_lines,
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

    def test_format_track_age_stats_lines_order_and_zeros(self) -> None:
        by_pool = build_by_pool_counts(
            {
                (Track.TRADITIONAL, AgeCategory.AGE_0_6): 3,
                (Track.AI, AgeCategory.AGE_7_12): 7,
            }
        )
        lines = format_track_age_stats_lines(by_pool)

        assert lines == [
            "Традиционное рисование:",
            "  • 0–6: 3",
            "  • 7–12: 0",
            "  • 13–18: 0",
            "",
            "ИИ-рисунок:",
            "  • 0–6: 0",
            "  • 7–12: 7",
            "  • 13–18: 0",
            "",
            "От руки к ИИ:",
            "  • 0–6: 0",
            "  • 7–12: 0",
            "  • 13–18: 0",
        ]
