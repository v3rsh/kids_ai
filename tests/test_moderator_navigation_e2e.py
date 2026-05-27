"""Smoke-тесты хендлеров навигации модератора."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pybotx import BubbleMarkup

from handlers.moderator_actions import cmd_find, cmd_m_cancel_dialog
from states import ModeratorAction
from utils.moderator_nav import parse_origin, post_action_bubbles


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


def _message(*, data: dict | None = None, source_sync_id=None) -> MagicMock:
    message = MagicMock()
    message.data = data
    message.source_sync_id = source_sync_id
    message.body = ""
    message.sender.huid = uuid4()
    message.state.fsm = AsyncMock()
    message.state.fsm.get_state = AsyncMock(
        return_value=ModeratorAction.moderator_action_comment_input.value
    )
    message.state.fsm.set_state = AsyncMock()
    message.state.fsm.update_data = AsyncMock()
    message.state.fsm.get_data = AsyncMock(return_value={})
    message.state.fsm.clear = AsyncMock()
    message.state.transient_source_deleted = False
    message.bot.id = uuid4()
    return message


@pytest.mark.asyncio
class TestCmdFind:
    async def test_clears_moderator_action_state(self) -> None:
        message = _message(
            data={
                "br_id": "BR-2026-0001",
                "from": "queue",
            }
        )
        bot = MagicMock()
        app = MagicMock()
        app.br_id = "BR-2026-0001"
        mod_huid = {str(message.sender.huid)}

        with patch("services.access._moderator_huids", mod_huid), patch(
            "handlers.moderator_actions.find_by_br_id",
            new=AsyncMock(return_value=app),
        ), patch(
            "handlers.moderator_actions.card_bubbles_for_app",
            new=AsyncMock(
                return_value=post_action_bubbles(
                    parse_origin({"from": "queue"})
                )
            ),
        ), patch(
            "handlers.moderator_actions.render_application_card",
            new=AsyncMock(),
        ) as render_mock, patch(
            "handlers.moderator_actions.clear_dialog_state",
            new=AsyncMock(),
        ) as clear_mock:
            await cmd_find(message, bot)

        clear_mock.assert_awaited_once()
        render_mock.assert_awaited_once()
        _, kwargs = render_mock.call_args
        bubbles = kwargs["bubbles"]
        assert "/m_q_refresh" in _commands(bubbles)


@pytest.mark.asyncio
class TestPostActionOrigin:
    async def test_section_origin_back_to_m_list(self) -> None:
        origin = parse_origin(
            {
                "from": "section",
                "st": "DOPUSHCHENO",
                "tr": "TRADITIONAL",
                "ag": "AGE_7_12",
                "p": "2",
            }
        )
        data = _button_data(post_action_bubbles(origin), "/m_list")
        assert data == {
            "st": "DOPUSHCHENO",
            "tr": "TRADITIONAL",
            "ag": "AGE_7_12",
            "p": "2",
        }


@pytest.mark.asyncio
class TestCmdCancelDialog:
    async def test_clears_state_and_returns_back(self) -> None:
        message = _message()
        message.state.fsm.get_data = AsyncMock(
            return_value={
                "moderator_nav_origin_kind": "queue",
                "moderator_nav_origin_data": '{"kind": "queue"}',
                "moderator_nav_origin_br_id": "BR-2026-0001",
            }
        )
        bot = MagicMock()
        mod_huid = {str(message.sender.huid)}

        with patch("services.access._moderator_huids", mod_huid), patch(
            "handlers.moderator_actions.reply_to_user",
            new=AsyncMock(),
        ) as reply_mock, patch(
            "handlers.moderator_actions.clear_dialog_state",
            new=AsyncMock(),
        ) as clear_mock:
            await cmd_m_cancel_dialog(message, bot)

        clear_mock.assert_awaited_once()
        reply_mock.assert_awaited_once()
        bubbles = reply_mock.call_args.kwargs["bubbles"]
        assert "/m_q_refresh" in _commands(bubbles)


@pytest.mark.asyncio
class TestReplyToUserEdit:
    async def test_edit_when_source_sync_id_present(self) -> None:
        from utils.bot_utils import reply_to_user

        message = _message(source_sync_id=uuid4())
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        bot.answer_message = AsyncMock()
        bubbles = BubbleMarkup()
        bubbles.add_button(command="/m_q_refresh", label="back", new_row=True)

        await reply_to_user(message, bot, "text", bubbles=bubbles)

        bot.edit_message.assert_awaited_once()
        bot.answer_message.assert_not_called()
