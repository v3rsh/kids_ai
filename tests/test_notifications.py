"""Проактивные DM участнику: в каждом уведомлении есть клавиатура."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import notifications


def _extract_button_commands(bubbles) -> list[str]:
    commands: list[str] = []
    for row in bubbles:
        for button in row:
            commands.append(button.command)
    return commands


@pytest.fixture
def fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def fake_app() -> MagicMock:
    app = MagicMock()
    app.parent_huid = uuid.uuid4()
    app.br_id = "BR-2026-0001"
    return app


@pytest.fixture
def chat_id() -> uuid.UUID:
    return uuid.uuid4()


class TestParticipantNotificationsBubbles:
    @pytest.mark.parametrize(
        "notify_fn,expected_commands",
        [
            (notifications.notify_participant_accepted, ["/start"]),
            (notifications.notify_participant_rejected, ["/start"]),
            (notifications.notify_participant_shortlist, ["/start"]),
        ],
    )
    async def test_simple_notifications_have_start(
        self,
        fake_bot: MagicMock,
        fake_app: MagicMock,
        chat_id: uuid.UUID,
        notify_fn,
        expected_commands: list[str],
    ) -> None:
        with patch.object(
            notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=chat_id),
        ), patch.object(
            notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ):
            if notify_fn is notifications.notify_participant_rejected:
                await notify_fn(fake_bot, fake_app, reason="тест")
            else:
                await notify_fn(fake_bot, fake_app)

        fake_bot.send_message.assert_awaited_once()
        kwargs = fake_bot.send_message.await_args.kwargs
        assert "bubbles" in kwargs
        assert _extract_button_commands(kwargs["bubbles"]) == expected_commands

    async def test_fix_needed_has_contacts_and_start(
        self,
        fake_bot: MagicMock,
        fake_app: MagicMock,
        chat_id: uuid.UUID,
    ) -> None:
        with patch.object(
            notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=chat_id),
        ), patch.object(
            notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ):
            await notifications.notify_participant_fix_needed(fake_bot, fake_app)

        kwargs = fake_bot.send_message.await_args.kwargs
        assert _extract_button_commands(kwargs["bubbles"]) == [
            "/menu_contacts",
            "/start",
        ]

    async def test_jury_result_top10_has_start(
        self,
        fake_bot: MagicMock,
        fake_app: MagicMock,
        chat_id: uuid.UUID,
    ) -> None:
        with patch.object(
            notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=chat_id),
        ), patch.object(
            notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ):
            await notifications.notify_participant_jury_result(
                fake_bot, fake_app, in_top_10=True
            )

        kwargs = fake_bot.send_message.await_args.kwargs
        assert _extract_button_commands(kwargs["bubbles"]) == ["/start"]

    async def test_jury_result_out_has_start(
        self,
        fake_bot: MagicMock,
        fake_app: MagicMock,
        chat_id: uuid.UUID,
    ) -> None:
        with patch.object(
            notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=chat_id),
        ), patch.object(
            notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ):
            await notifications.notify_participant_jury_result(
                fake_bot, fake_app, in_top_10=False
            )

        kwargs = fake_bot.send_message.await_args.kwargs
        assert _extract_button_commands(kwargs["bubbles"]) == ["/start"]

    async def test_skips_send_when_no_chat_id(
        self, fake_bot: MagicMock, fake_app: MagicMock
    ) -> None:
        with patch.object(
            notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=None),
        ):
            await notifications.notify_participant_accepted(fake_bot, fake_app)

        fake_bot.send_message.assert_not_awaited()
