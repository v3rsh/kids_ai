"""Покрытие подачи заявки (``services.applications``).

Тесты — pure-function/мок-уровень: на уровне юнит-функций без
PostgreSQL. Полная интеграция с advisory_lock'ом проверяется при
ручных smoke-сценариях (см. ``docs/testing.md`` → «Ручной чек-лист»).

Что покрывается:
1. ``normalize_child_name`` — детерминированное сравнение «Ёлка» ↔ «елка»
   и trim'ы пробелов.
2. Формат br_id ``BR-{YEAR}-{NNNN}`` через ``_select_next_br_number``
   на моке SQLAlchemy-сессии.
3. ``AgeCategory.from_age`` — границы 0–6 / 7–12 / 13–18.
4. ``find_possible_duplicate`` отдаёт ``None`` при пустом
   нормализованном имени.
5. ``IntakeMode("files")`` / ``IntakeMode("links")`` — happy-path
   валидации режима подачи.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.applications import (
    _select_next_br_number,
    find_possible_duplicate,
    normalize_child_name,
)
from database.models import AgeCategory, IntakeMode


class TestNormalizeChildName:
    """`normalize_child_name` — чистая функция, ключ алгоритма дубля."""

    def test_strip_lowercase(self):
        assert normalize_child_name("  Алиса  ") == "алиса"

    def test_yo_collapses_to_e(self):
        assert normalize_child_name("Алёна") == normalize_child_name("Алена")

    def test_yo_capital_too(self):
        assert normalize_child_name("Ёлка") == normalize_child_name("елка")

    def test_empty_input(self):
        assert normalize_child_name("") == ""
        assert normalize_child_name(None) == ""  # type: ignore[arg-type]

    def test_preserves_spaces_inside(self):
        assert normalize_child_name(" Анна-Мария ") == "анна-мария"


class TestAgeCategoryBounds:
    """`AgeCategory.from_age` — границы 0–6 / 7–12 / 13–18."""

    @pytest.mark.parametrize(
        "age,expected",
        [
            (0, AgeCategory.AGE_0_6),
            (6, AgeCategory.AGE_0_6),
            (7, AgeCategory.AGE_7_12),
            (12, AgeCategory.AGE_7_12),
            (13, AgeCategory.AGE_13_18),
            (18, AgeCategory.AGE_13_18),
        ],
    )
    def test_age_bucket(self, age: int, expected: AgeCategory):
        assert AgeCategory.from_age(age) is expected

    @pytest.mark.parametrize("age", [-1, 19, 99])
    def test_out_of_range_raises(self, age: int):
        with pytest.raises(ValueError):
            AgeCategory.from_age(age)


class TestBrIdGeneration:
    """`_select_next_br_number`: формат и инкремент BR-ID."""

    async def test_first_br_id_is_one_when_table_empty(self):
        """Пустая таблица → следующий номер = 1."""
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        n = await _select_next_br_number(session, "BR-2026-")
        assert n == 1

    async def test_increments_from_last_br_id(self):
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = "BR-2026-0042"
        session.execute = AsyncMock(return_value=result)
        n = await _select_next_br_number(session, "BR-2026-")
        assert n == 43

    async def test_fallback_to_one_on_parse_error(self):
        """Если БД отдала мусор, не падаем, начинаем с 1."""
        session = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = "BR-XXXX"
        session.execute = AsyncMock(return_value=result)
        n = await _select_next_br_number(session, "BR-2026-")
        assert n == 1


class TestFindPossibleDuplicate:
    """`find_possible_duplicate` — защита от пустого нормализованного имени."""

    async def test_empty_child_name_returns_none(self):
        result = await find_possible_duplicate(
            parent_huid=uuid.uuid4(),
            child_name="   ",
            track_name="TRADITIONAL",
        )
        assert result is None

    async def test_unknown_track_raises(self):
        with pytest.raises(ValueError):
            await find_possible_duplicate(
                parent_huid=uuid.uuid4(),
                child_name="Алиса",
                track_name="BOGUS_TRACK",
            )


class TestIntakeModeValidation:
    """Валидация ``intake_mode_value`` в ``create_application``."""

    def test_files_mode(self):
        assert IntakeMode("files") is IntakeMode.FILES

    def test_links_mode(self):
        assert IntakeMode("links") is IntakeMode.LINKS

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            IntakeMode("ftp")
