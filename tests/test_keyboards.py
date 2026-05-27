"""Smoke-тесты конструкторов клавиатур возврата в меню роли."""
from __future__ import annotations

from keyboards import (
    back_to_admin_menu_bubbles,
    back_to_jury_menu_bubbles,
    back_to_main_menu_bubbles,
    back_to_moderator_menu_bubbles,
    fix_needed_notification_bubbles,
    intake_cancel_bubble,
    track_selection_bubbles,
)
from pybotx import BubbleMarkup
from utils.moderator_nav import ModeratorNavOrigin, build_back_bubbles


def _commands(bubbles) -> list[str]:
    out: list[str] = []
    for row in bubbles:
        for button in row:
            out.append(button.command)
    return out


class TestBackMenuBubbles:
    def test_back_to_main_menu(self) -> None:
        assert _commands(back_to_main_menu_bubbles()) == ["/start"]

    def test_back_to_moderator_menu(self) -> None:
        assert _commands(back_to_moderator_menu_bubbles()) == ["/moderator"]

    def test_back_to_jury_menu(self) -> None:
        assert _commands(back_to_jury_menu_bubbles()) == ["/jury"]

    def test_back_to_admin_menu(self) -> None:
        assert _commands(back_to_admin_menu_bubbles()) == ["/admin"]

    def test_fix_needed_notification(self) -> None:
        assert _commands(fix_needed_notification_bubbles()) == [
            "/menu_contacts",
            "/start",
        ]

    def test_intake_cancel_on_track_selection(self) -> None:
        assert "/start" in _commands(track_selection_bubbles())

    def test_intake_cancel_bubble(self) -> None:
        bubbles = BubbleMarkup()
        intake_cancel_bubble(bubbles)
        assert _commands(bubbles) == ["/start"]

    def test_build_back_bubbles_queue(self) -> None:
        commands = _commands(build_back_bubbles(ModeratorNavOrigin(kind="queue")))
        assert "/m_q_refresh" in commands
        assert "/moderator" in commands
