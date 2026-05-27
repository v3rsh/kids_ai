"""Идемпотентность действий модератора — без повторных уведомлений."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from database.models import ModerationStatus, Track
from handlers.moderator_actions import _apply_reject, _send_notify_fix, cmd_status
from services.moderation import ChangeStatusResult


def _message(*, data: dict | None = None) -> MagicMock:
    message = MagicMock()
    message.data = data or {"br_id": "BR-2026-0042", "from": "queue"}
    message.body = ""
    message.sender.huid = uuid4()
    message.state.fsm = AsyncMock()
    return message


def _application(*, status: ModerationStatus) -> MagicMock:
    app = MagicMock()
    app.br_id = "BR-2026-0042"
    app.moderation_status = status
    app.title = "Работа"
    app.child_name = "Маша"
    app.child_age = 10
    app.track = Track.TRADITIONAL
    app.age_category = MagicMock(value="7–12 лет")
    return app


@pytest.mark.asyncio
class TestCmdStatusIdempotency:
    async def test_dopushcheno_does_not_notify_when_unchanged(self) -> None:
        message = _message(
            data={
                "br_id": "BR-2026-0042",
                "group": "moderation",
                "value": ModerationStatus.DOPUSHCHENO.value,
            }
        )
        bot = MagicMock()
        app = _application(status=ModerationStatus.DOPUSHCHENO)
        result = ChangeStatusResult(
            ok=True,
            application=app,
            previous_value=ModerationStatus.DOPUSHCHENO.value,
            new_value=ModerationStatus.DOPUSHCHENO.value,
        )
        mod_huid = {str(message.sender.huid)}

        with patch("services.access._moderator_huids", mod_huid), patch(
            "handlers.moderator_actions.change_status",
            new=AsyncMock(return_value=result),
        ), patch(
            "handlers.moderator_actions._show_action_confirmation",
            new=AsyncMock(),
        ) as confirm_mock, patch(
            "services.notifications.notify_participant_moderation_passed",
            new=AsyncMock(),
        ) as notify_mock:
            await cmd_status(message, bot)

        notify_mock.assert_not_awaited()
        _, kwargs = confirm_mock.call_args
        assert "Повторное уведомление не отправлено" in kwargs["headline"]


@pytest.mark.asyncio
class TestNotifyFixIdempotency:
    async def test_already_fix_does_not_notify(self) -> None:
        message = _message()
        bot = MagicMock()
        app = _application(status=ModerationStatus.NUZHNO_ISPRAVIT)

        with patch(
            "handlers.moderator_actions.change_status",
            new=AsyncMock(),
        ) as change_mock, patch(
            "handlers.moderator_actions.find_by_br_id",
            new=AsyncMock(return_value=app),
        ), patch(
            "handlers.moderator_actions._show_action_confirmation",
            new=AsyncMock(),
        ) as confirm_mock, patch(
            "services.notifications.notify_participant_fix_needed",
            new=AsyncMock(),
        ) as notify_mock:
            await _send_notify_fix(message, bot, app=app, extra=None)

        change_mock.assert_not_awaited()
        notify_mock.assert_not_awaited()
        _, kwargs = confirm_mock.call_args
        assert "Повторное уведомление не отправлено" in kwargs["headline"]


@pytest.mark.asyncio
class TestApplyRejectIdempotency:
    async def test_already_rejected_skips_storage_and_notify(self) -> None:
        message = _message()
        bot = MagicMock()
        app = _application(status=ModerationStatus.OTKLONENO)

        with patch(
            "handlers.moderator_actions.find_by_br_id",
            new=AsyncMock(return_value=app),
        ), patch(
            "handlers.moderator_actions.build_full_card",
            new=AsyncMock(return_value="card"),
        ), patch(
            "handlers.moderator_actions.reply_to_user",
            new=AsyncMock(),
        ) as reply_mock, patch(
            "services.storage.write_reason_txt",
            new=AsyncMock(),
        ) as storage_mock, patch(
            "services.notifications.notify_participant_rejected",
            new=AsyncMock(),
        ) as notify_mock:
            await _apply_reject(
                message,
                bot,
                br_id="BR-2026-0042",
                reason="причина",
            )

        storage_mock.assert_not_awaited()
        notify_mock.assert_not_awaited()
        reply_mock.assert_awaited_once()
        assert "уже отклонена" in reply_mock.call_args[0][2]
