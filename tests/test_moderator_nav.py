"""Тесты навигации модератора (origin + back-кнопки)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from database.models import ModerationStatus, Track
from utils.moderator_nav import (
    ModeratorNavOrigin,
    build_back_bubbles,
    card_action_buttons,
    find_button_data,
    parse_origin,
    post_action_bubbles,
)


def _commands(bubbles) -> list[str]:
    out: list[str] = []
    for row in bubbles:
        for button in row:
            out.append(button.command)
    return out


def _button_data(bubbles, command: str) -> dict | None:
    for row in bubbles:
        for button in row:
            if button.command == command:
                return dict(button.data) if button.data else None
    return None


def _app(*, status: ModerationStatus = ModerationStatus.NA_MODERATSII) -> MagicMock:
    app = MagicMock()
    app.br_id = "BR-2026-0042"
    app.moderation_status = status
    app.track = Track.TRADITIONAL
    return app


class TestParseOrigin:
    def test_empty_data_is_direct(self) -> None:
        assert parse_origin({}).kind == "direct"
        assert parse_origin(None).kind == "direct"

    def test_section_origin(self) -> None:
        origin = parse_origin(
            {
                "from": "section",
                "st": "DOPUSHCHENO",
                "tr": "TRADITIONAL",
                "ag": "AGE_7_12",
                "p": "2",
            }
        )
        assert origin.kind == "section"
        assert origin.section_status == "DOPUSHCHENO"
        assert origin.section_track == "TRADITIONAL"
        assert origin.section_age == "AGE_7_12"
        assert origin.section_page == 2

    def test_multi_subs_origin(self) -> None:
        origin = parse_origin({"from": "multi_subs", "p": "3"})
        assert origin.kind == "multi_subs"
        assert origin.multi_subs_page == 3


class TestFindButtonData:
    def test_queue_payload(self) -> None:
        data = find_button_data(
            "BR-2026-0001", ModeratorNavOrigin(kind="queue")
        )
        assert data["br_id"] == "BR-2026-0001"
        assert data["from"] == "queue"


class TestBuildBackBubbles:
    @pytest.mark.parametrize(
        "kind,expected_command",
        [
            ("section", "/m_list"),
            ("queue", "/m_q_refresh"),
            ("browse", "/m_b_refresh"),
            ("multi_subs", "/multi_subs"),
            ("admin_find", "/admin"),
        ],
    )
    def test_back_command_per_kind(self, kind: str, expected_command: str) -> None:
        origin = ModeratorNavOrigin(
            kind=kind,  # type: ignore[arg-type]
            section_status="DOPUSHCHENO",
            section_track="TRADITIONAL",
            section_age="AGE_7_12",
            section_page=1,
        )
        bubbles = build_back_bubbles(origin)
        assert expected_command in _commands(bubbles)
        assert "/moderator" in _commands(bubbles)

    def test_section_back_payload(self) -> None:
        origin = ModeratorNavOrigin(
            kind="section",
            section_status="DOPUSHCHENO",
            section_track="TRADITIONAL",
            section_age="AGE_7_12",
            section_page=2,
        )
        data = _button_data(build_back_bubbles(origin), "/m_list")
        assert data == {
            "st": "DOPUSHCHENO",
            "tr": "TRADITIONAL",
            "ag": "AGE_7_12",
            "p": "2",
        }

    def test_direct_only_menu(self) -> None:
        bubbles = build_back_bubbles(ModeratorNavOrigin(kind="direct"))
        assert _commands(bubbles) == ["/moderator"]


class TestCardActionButtons:
    def test_active_app_has_context_back(self) -> None:
        origin = ModeratorNavOrigin(kind="queue")
        bubbles = card_action_buttons(_app(), origin)
        assert "/m_q_refresh" in _commands(bubbles)
        assert "/moderator" in _commands(bubbles)
        assert "/files" in _commands(bubbles)

    def test_rejected_app_hides_actions(self) -> None:
        origin = ModeratorNavOrigin(kind="section", section_status="OTKLONENO")
        bubbles = card_action_buttons(
            _app(status=ModerationStatus.OTKLONENO), origin
        )
        commands = _commands(bubbles)
        assert "/comment" in commands
        assert "/files" not in commands
        assert "/m_list" in commands

    def test_action_buttons_carry_origin_in_data(self) -> None:
        origin = ModeratorNavOrigin(
            kind="section",
            section_status="DOPUSHCHENO",
            section_track="TRADITIONAL",
            section_age="AGE_7_12",
            section_page=1,
        )
        bubbles = card_action_buttons(_app(), origin)
        files_data = _button_data(bubbles, "/files")
        assert files_data is not None
        assert files_data["from"] == "section"
        assert files_data["br_id"] == "BR-2026-0042"


class TestPostActionBubbles:
    def test_contains_queue_next_and_context_back(self) -> None:
        origin = ModeratorNavOrigin(kind="queue")
        bubbles = post_action_bubbles(origin)
        commands = _commands(bubbles)
        assert "/queue_next" in commands
        assert "/m_q_refresh" in commands
        assert "/find" not in commands
