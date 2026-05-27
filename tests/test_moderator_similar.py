"""Тесты экрана «Похожие работы» модератора."""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from database.models import ModerationStatus, Track
from handlers.moderator_similar import _format_similar_body


def _entry(
    *,
    br_id: str,
    child_name: str,
    child_age: int,
    title: str,
    is_strict_match: bool,
) -> SimpleNamespace:
    return SimpleNamespace(
        br_id=br_id,
        child_name=child_name,
        child_age=child_age,
        title=title,
        moderation_status=ModerationStatus.NA_MODERATSII.value,
        is_actual_version=False,
        is_strict_match=is_strict_match,
        created_at=datetime(2026, 6, 1),
    )


def _anchor() -> MagicMock:
    app = MagicMock()
    app.br_id = "BR-2026-0100"
    app.parent_huid = uuid.uuid4()
    app.parent_full_name = "Иванов И.И."
    app.track = Track.TRADITIONAL
    return app


class TestFormatSimilarBody:
    def test_splits_strict_and_loose_sections(self) -> None:
        body = _format_similar_body(
            _anchor(),
            [
                _entry(
                    br_id="BR-2026-0101",
                    child_name="Маша",
                    child_age=7,
                    title="A",
                    is_strict_match=True,
                ),
                _entry(
                    br_id="BR-2026-0102",
                    child_name="Петя",
                    child_age=9,
                    title="B",
                    is_strict_match=False,
                ),
            ],
        )
        assert "🔴 **Тот же ребёнок**" in body
        assert "🟡 **Другие заявки родителя" in body
        assert "BR-2026-0101" in body
        assert "BR-2026-0102" in body

    def test_empty_related_shows_fallback(self) -> None:
        body = _format_similar_body(_anchor(), [])
        assert "Связанных заявок не найдено" in body
