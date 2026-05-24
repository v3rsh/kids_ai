"""Покрытие модераторских команд (``services.moderation``).

Pure-function-уровень. Полные интеграционные сценарии change_status
покрываются ручным smoke-чек-листом (см. ``docs/testing.md``).

Что покрывается:
1. ``parse_status_group`` — алиасы RU/EN и неизвестные значения.
2. ``_moderation_status_by_value`` / ``_voting_status_by_value`` —
   распознавание ``.value`` и ``.name``.
3. ``_build_queue_where_clauses`` — соответствие фильтров /queue
   набору SQL-условий.
4. ``QueueFilters.is_empty`` — корректность дефолта.
5. ``DEFAULT_QUEUE_STATUSES`` — соответствует «на_модерации +
   нужно_исправить».
"""
from __future__ import annotations

from datetime import date

import pytest

from database.models import (
    AgeCategory,
    ModerationStatus,
    Track,
    VotingStatus,
)
from services.moderation import (
    DEFAULT_QUEUE_STATUSES,
    QueueFilters,
    _age_category_by_value,
    _build_queue_where_clauses,
    _moderation_status_by_value,
    _track_by_value,
    _voting_status_by_value,
    parse_status_group,
)


class TestParseStatusGroup:
    """Поддержка алиасов /status."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("модерация", "moderation"),
            ("МОДЕРАЦИЯ", "moderation"),
            ("jury", "jury"),
            ("жюри", "jury"),
            ("voting", "voting"),
            ("голосование", "voting"),
            ("merch", "merch"),
            ("мерч", "merch"),
        ],
    )
    def test_aliases(self, raw: str, expected: str):
        assert parse_status_group(raw) == expected

    @pytest.mark.parametrize("raw", ["", "bogus", "fooba", "статус"])
    def test_unknown_returns_none(self, raw: str):
        assert parse_status_group(raw) is None


class TestEnumLookups:
    """Поиск enum по .value / .name — алгоритм /status."""

    def test_moderation_by_value(self):
        assert _moderation_status_by_value("допущено") is ModerationStatus.DOPUSHCHENO

    def test_moderation_by_name(self):
        assert (
            _moderation_status_by_value("DOPUSHCHENO") is ModerationStatus.DOPUSHCHENO
        )

    def test_moderation_unknown(self):
        assert _moderation_status_by_value("rofl") is None

    def test_voting_by_value(self):
        assert (
            _voting_status_by_value("опубликовано") is VotingStatus.OPUBLIKOVANO
        )

    def test_track_by_value(self):
        assert _track_by_value("Традиционное рисование") is Track.TRADITIONAL

    def test_age_category_by_value(self):
        assert _age_category_by_value("7–12") is AgeCategory.AGE_7_12


class TestQueueFilters:
    """Сборка SQL-where для /queue."""

    def test_empty_filters_produce_empty_clauses(self):
        clauses = _build_queue_where_clauses(QueueFilters())
        assert clauses == []

    def test_track_filter_only(self):
        f = QueueFilters(tracks={Track.TRADITIONAL})
        clauses = _build_queue_where_clauses(f)
        assert len(clauses) == 1

    def test_full_filter_count(self):
        """Полный фильтр → ровно 5 where-условий (track + age + status +
        date_from + date_to)."""
        f = QueueFilters(
            tracks={Track.TRADITIONAL, Track.AI},
            age_categories={AgeCategory.AGE_7_12},
            moderation_statuses={ModerationStatus.NA_MODERATSII},
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 21),
        )
        clauses = _build_queue_where_clauses(f)
        assert len(clauses) == 5

    def test_default_queue_statuses_are_moderation_pending(self):
        """Дефолт /queue — заявки на модерации + нужно исправить."""
        assert set(DEFAULT_QUEUE_STATUSES) == {
            ModerationStatus.NA_MODERATSII,
            ModerationStatus.NUZHNO_ISPRAVIT,
        }
