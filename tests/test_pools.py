"""Тесты ``services.pools``."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from database.models import AgeCategory, Track
from services.pools import all_pools, count_jury_by_pool


def _mock_result(rows):
    mock = MagicMock()
    mock.all.return_value = rows
    return mock


@pytest.mark.asyncio
async def test_count_jury_by_pool_fallback_when_no_assignments() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _mock_result([]),
            MagicMock(scalar_one=MagicMock(return_value=3)),
        ]
    )

    counts = await count_jury_by_pool(session=session)

    assert len(counts) == len(all_pools())
    assert all(value == 3 for value in counts.values())
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_count_jury_by_pool_uses_explicit_assignments() -> None:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            _mock_result(
                [
                    (Track.AI, AgeCategory.AGE_7_12, 2),
                ]
            ),
            MagicMock(scalar_one=MagicMock(return_value=5)),
        ]
    )

    counts = await count_jury_by_pool(session=session)

    assert counts[(Track.AI, AgeCategory.AGE_7_12)] == 2
    assert counts[(Track.TRADITIONAL, AgeCategory.AGE_0_6)] == 5
