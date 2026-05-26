"""Smoke-тесты конструкторов клавиатур возврата в меню роли."""
from __future__ import annotations

from keyboards import (
    back_to_admin_menu_bubbles,
    back_to_jury_menu_bubbles,
    back_to_main_menu_bubbles,
    back_to_moderator_menu_bubbles,
    fix_needed_notification_bubbles,
)


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
