"""is_global_shortlist_ready и идемпотентность shortlist_announced."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services import jury_settings


@pytest.mark.asyncio
async def test_shortlist_announced_idempotent_guard() -> None:
    fake_bot = AsyncMock()
    with patch.object(
        jury_settings, "get_shortlist_announced", AsyncMock(return_value=True)
    ), patch(
        "services.notifications._send_jury_event_single",
        AsyncMock(),
    ) as send_single:
        from services import notifications

        await notifications.notify_moderation_chat_jury_event(
            fake_bot,
            event_kind="shortlist_ready",
            pools=[],
            round_no=None,
        )
        send_single.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_global_shortlist_ready_false_when_one_pool_open() -> None:
    with patch(
        "services.jury.is_pool_done",
        AsyncMock(side_effect=[True] * 8 + [False]),
    ):
        from services import jury

        assert await jury.is_global_shortlist_ready() is False


@pytest.mark.asyncio
async def test_is_global_shortlist_ready_true_when_all_done() -> None:
    with patch(
        "services.jury.is_pool_done",
        AsyncMock(return_value=True),
    ):
        from services import jury

        assert await jury.is_global_shortlist_ready() is True
