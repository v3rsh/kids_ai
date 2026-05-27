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
    similar_apps_find_data,
    similar_apps_open_data,
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

    def test_similar_apps_origin(self) -> None:
        origin = parse_origin(
            {
                "from": "similar_apps",
                "src": "BR-2026-0001",
                "ret": "queue",
            }
        )
        assert origin.kind == "similar_apps"
        assert origin.similar_src_br_id == "BR-2026-0001"
        assert origin.anchor_return is not None
        assert origin.anchor_return.kind == "queue"


class TestSimilarAppsNav:
    def test_open_data_encodes_return_origin(self) -> None:
        data = similar_apps_open_data(
            "BR-2026-0042",
            ModeratorNavOrigin(kind="queue"),
        )
        assert data["src"] == "BR-2026-0042"
        assert data["ret"] == "queue"

    def test_find_from_similar_carries_anchor(self) -> None:
        data = similar_apps_find_data(
            "BR-2026-0002",
            anchor_src_br_id="BR-2026-0001",
            anchor_return=ModeratorNavOrigin(kind="queue"),
        )
        assert data["from"] == "similar_apps"
        assert data["src"] == "BR-2026-0001"
        assert data["br_id"] == "BR-2026-0002"
        assert data["ret"] == "queue"

    def test_similar_apps_back_command(self) -> None:
        origin = ModeratorNavOrigin(
            kind="similar_apps",
            similar_src_br_id="BR-2026-0001",
            anchor_return=ModeratorNavOrigin(kind="queue"),
        )
        bubbles = build_back_bubbles(origin)
        assert "/similar_apps" in _commands(bubbles)
        data = _button_data(bubbles, "/similar_apps")
        assert data is not None
        assert data["src"] == "BR-2026-0001"
        assert data["ret"] == "queue"


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

    @pytest.mark.parametrize(
        "status,hidden_commands",
        [
            (
                ModerationStatus.NA_MODERATSII,
                set(),
            ),
            (
                ModerationStatus.DOPUSHCHENO,
                {"/status"},
            ),
            (
                ModerationStatus.NUZHNO_ISPRAVIT,
                {"/notify_fix"},
            ),
        ],
    )
    def test_hides_button_for_current_status(
        self,
        status: ModerationStatus,
        hidden_commands: set[str],
    ) -> None:
        origin = ModeratorNavOrigin(kind="queue")
        commands = set(_commands(card_action_buttons(_app(status=status), origin)))
        for cmd in hidden_commands:
            assert cmd not in commands
        assert "/files" in commands
        assert "/comment" in commands
        assert "/notify_reject" in commands
        if status is ModerationStatus.NA_MODERATSII:
            assert {"/status", "/notify_fix"} <= commands

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

    def test_shows_similar_button_when_related(self) -> None:
        origin = ModeratorNavOrigin(kind="queue")
        bubbles = card_action_buttons(_app(), origin, related_count=2)
        assert "/similar_apps" in _commands(bubbles)
        similar_data = _button_data(bubbles, "/similar_apps")
        assert similar_data is not None
        assert similar_data["src"] == "BR-2026-0042"
        assert similar_data["ret"] == "queue"

    def test_hides_similar_button_without_related(self) -> None:
        bubbles = card_action_buttons(
            _app(), ModeratorNavOrigin(kind="queue"), related_count=0
        )
        assert "/similar_apps" not in _commands(bubbles)


class TestPostActionBubbles:
    def test_contains_queue_next_and_context_back(self) -> None:
        origin = ModeratorNavOrigin(kind="queue")
        bubbles = post_action_bubbles(origin)
        commands = _commands(bubbles)
        assert "/queue_next" in commands
        assert "/m_q_refresh" in commands
        assert "/find" not in commands
