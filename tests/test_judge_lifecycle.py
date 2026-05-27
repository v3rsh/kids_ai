"""Revoke: purge OPEN votes, submit PermissionError."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.models import JuryVoteValue


@pytest.mark.asyncio
async def test_purge_only_open_round_votes() -> None:
    session = AsyncMock()
    huid = uuid.uuid4()
    open_ids = [uuid.uuid4()]

    def _mock_result(items):
        mock = MagicMock()
        mock.scalars.return_value.all.return_value = items
        return mock

    session.execute = AsyncMock(
        side_effect=[
            _mock_result(open_ids),
            _mock_result(open_ids),
            MagicMock(),
        ]
    )

    from services import jury

    affected = await jury.purge_inactive_jury_votes_in_open_rounds(
        huid, session=session
    )
    assert affected == open_ids
    assert session.execute.await_count == 3


@pytest.mark.asyncio
async def test_purge_filters_by_open_status_in_sql() -> None:
    """Хелпер строит WHERE по ``JuryRound.status == OPEN`` —
    CLOSED-раунды и их aggregates остаются нетронутыми."""
    captured_stmts: list = []

    class _CaptureSession:
        async def execute(self, stmt):
            captured_stmts.append(stmt)
            mock = MagicMock()
            mock.scalars.return_value.all.return_value = []
            return mock

    from services import jury

    session = _CaptureSession()
    affected = await jury.purge_inactive_jury_votes_in_open_rounds(
        uuid.uuid4(), session=session
    )
    assert affected == []

    # Первый запрос — select id из jury_rounds where status='OPEN'
    assert len(captured_stmts) == 1
    sql = str(
        captured_stmts[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "jury_rounds" in sql.lower()
    assert "open" in sql.lower()
    # Никаких DELETE по jury_round_aggregates не выполняем.
    assert "jury_round_aggregates" not in sql.lower()


@pytest.mark.asyncio
async def test_purge_does_not_touch_aggregates_when_only_closed_rounds() -> None:
    """Если открытых раундов нет — DELETE по голосам тоже не выполняем."""
    captured_stmts: list = []

    class _CaptureSession:
        async def execute(self, stmt):
            captured_stmts.append(stmt)
            mock = MagicMock()
            mock.scalars.return_value.all.return_value = []
            return mock

    from services import jury

    session = _CaptureSession()
    affected = await jury.purge_inactive_jury_votes_in_open_rounds(
        uuid.uuid4(), session=session
    )
    assert affected == []
    # Только один запрос — поиск OPEN раундов. DELETE не вызван.
    assert len(captured_stmts) == 1


@pytest.mark.asyncio
async def test_submit_votes_raises_when_revoked_after_flush() -> None:
    from database.models import AgeCategory, JuryRoundStatus, Track

    round_id = uuid.uuid4()
    jury_huid = uuid.uuid4()
    app_id = uuid.uuid4()

    round_obj = MagicMock()
    round_obj.status = JuryRoundStatus.OPEN
    round_obj.track = Track.TRADITIONAL
    round_obj.age_category = AgeCategory.AGE_7_12
    round_obj.id = round_id

    session = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    session.commit = AsyncMock()

    empty_votes = MagicMock()
    empty_votes.scalars.return_value.all.return_value = []

    session.execute = AsyncMock(return_value=empty_votes)

    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_):
            return False

    with patch("services.jury._open_session_ctx", return_value=_Ctx()), patch(
        "services.jury._get_round",
        AsyncMock(return_value=round_obj),
    ), patch(
        "services.jury._get_round_candidates",
        AsyncMock(return_value=[MagicMock(id=app_id)]),
    ), patch(
        "services.access.is_jury",
        return_value=False,
    ):
        from services import jury

        with pytest.raises(PermissionError):
            await jury.submit_votes(
                round_id=round_id,
                jury_huid=jury_huid,
                votes={app_id: JuryVoteValue.YES},
            )
