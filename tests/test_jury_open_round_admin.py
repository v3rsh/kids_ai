"""TOP_N gate и auto_shortlist idempotency."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.models import AgeCategory, Track
from utils.contracts import PoolKey


@pytest.mark.asyncio
async def test_auto_shortlist_noop_when_already_fixed() -> None:
    pool = PoolKey(track=Track.TRADITIONAL, age_category=AgeCategory.AGE_7_12)
    session = AsyncMock()

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return False

    with patch("services.jury._open_session_ctx", return_value=_Ctx()), patch(
        "services.jury._count_fixed_top_in_pool",
        AsyncMock(return_value=3),
    ), patch(
        "services.jury.get_pool_applications",
        AsyncMock(),
    ) as get_apps:
        from services import jury

        result = await jury.auto_shortlist_undersized_pool(pool, bot=None)
        assert result == 3
        get_apps.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_shortlist_assigns_v_top_10() -> None:
    pool = PoolKey(track=Track.AI, age_category=AgeCategory.AGE_0_6)
    app = MagicMock(id="a1", created_at=1)

    class _Ctx:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *_):
            return False

    with patch("services.jury._open_session_ctx", return_value=_Ctx()), patch(
        "services.jury._count_fixed_top_in_pool",
        AsyncMock(return_value=0),
    ), patch(
        "services.jury.get_pool_applications",
        AsyncMock(return_value=[app]),
    ), patch(
        "services.jury.maybe_notify_shortlist_ready",
        AsyncMock(),
    ), patch(
        "services.notifications.notify_moderation_chat_undersized_pool",
        AsyncMock(),
    ):
        from services import jury

        count = await jury.auto_shortlist_undersized_pool(
            pool, bot=MagicMock(), session=AsyncMock()
        )
        assert count == 1
