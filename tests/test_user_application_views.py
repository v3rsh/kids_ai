"""Тесты экрана «Мои заявки» — форматирование и сервис доступа."""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.models import (
    AgeCategory,
    IntakeMode,
    JuryStatus,
    ModerationStatus,
    Track,
    VotingStatus,
)
from services import applications as applications_service
from services.user_application_views import (
    build_progress_timeline,
    format_list_item,
    resolve_fix_extra,
    short_status_label,
)


def _fake_app(**overrides):
    base = dict(
        id=uuid.uuid4(),
        br_id="BR-2026-0042",
        created_at=datetime(2026, 6, 10, 14, 30),
        parent_huid=uuid.UUID(int=100),
        child_name="Маша",
        child_age=9,
        age_category=AgeCategory.AGE_7_12,
        track=Track.TRADITIONAL,
        title="Безопасный интернет",
        description="Ребёнок нарисовал семью за компьютером.",
        moderation_status=ModerationStatus.NA_MODERATSII,
        moderator_comment=None,
        jury_status=JuryStatus.NE_PEREDANO_ZHYURI,
        voting_status=VotingStatus.NE_UCHASTVUET,
        is_possible_duplicate=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestShortStatusLabel:
    @pytest.mark.parametrize(
        "overrides,expected",
        [
            ({}, "На модерации"),
            ({"moderation_status": ModerationStatus.NUZHNO_ISPRAVIT}, "Нужно исправить"),
            ({"moderation_status": ModerationStatus.OTKLONENO}, "Отклонена"),
            (
                {
                    "moderation_status": ModerationStatus.DOPUSHCHENO,
                    "jury_status": JuryStatus.NA_GOLOSOVANII,
                },
                "На голосовании жюри",
            ),
            (
                {
                    "moderation_status": ModerationStatus.DOPUSHCHENO,
                    "jury_status": JuryStatus.V_TOP_10,
                },
                "В шорт-листе",
            ),
        ],
    )
    def test_labels(self, overrides: dict, expected: str):
        app = _fake_app(**overrides)
        assert short_status_label(app) == expected


class TestFormatListItem:
    def test_contains_br_id_and_status(self):
        app = _fake_app()
        text = format_list_item(app)
        assert "BR-2026-0042" in text
        assert "На модерации" in text
        assert "Безопасный интернет" in text


class TestBuildProgressTimeline:
    def test_admitted_on_jury_voting(self):
        app = _fake_app(
            moderation_status=ModerationStatus.DOPUSHCHENO,
            jury_status=JuryStatus.NA_GOLOSOVANII,
        )
        timeline = build_progress_timeline(app)
        assert "→ Голосование жюри" in timeline

    def test_shortlist_shows_publication(self):
        app = _fake_app(
            moderation_status=ModerationStatus.DOPUSHCHENO,
            jury_status=JuryStatus.V_TOP_10,
            voting_status=VotingStatus.OPUBLIKOVANO,
        )
        timeline = build_progress_timeline(app)
        assert "шорт-листе" in timeline.lower()
        assert "опубликовано" in timeline


class TestResolveFixExtra:
    def test_returns_comment_for_fix_status(self):
        app = _fake_app(
            moderation_status=ModerationStatus.NUZHNO_ISPRAVIT,
            moderator_comment="Нужен более чёткий снимок.",
        )
        assert resolve_fix_extra(app) == "Нужен более чёткий снимок."

    def test_none_for_other_status(self):
        app = _fake_app(moderator_comment="коммент")
        assert resolve_fix_extra(app) is None


class TestListByParentHuid:
    @pytest.mark.asyncio
    async def test_pagination_and_order(self):
        apps = [_fake_app(br_id=f"BR-2026-{i:04d}") for i in range(3)]
        total = 3

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        count_result = MagicMock()
        count_result.scalar_one.return_value = total

        list_result = MagicMock()
        list_result.scalars.return_value.all.return_value = apps[:2]

        mock_session.execute = AsyncMock(side_effect=[count_result, list_result])

        with patch(
            "services.applications.get_session",
            return_value=lambda: mock_session,
        ):
            page = await applications_service.list_by_parent_huid(
                uuid.UUID(int=100),
                page=1,
                page_size=2,
            )

        assert page.total == 3
        assert page.page == 1
        assert page.total_pages == 2
        assert len(page.items) == 2


class TestGetForParticipant:
    @pytest.mark.asyncio
    async def test_returns_none_for_other_parent(self):
        app = _fake_app(parent_huid=uuid.UUID(int=200))

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = app
        mock_session.execute = AsyncMock(return_value=row_result)

        with patch(
            "services.applications.get_session",
            return_value=lambda: mock_session,
        ):
            result = await applications_service.get_for_participant(
                "BR-2026-0042",
                uuid.UUID(int=100),
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_app_for_owner(self):
        owner = uuid.UUID(int=100)
        app = _fake_app(parent_huid=owner)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        row_result = MagicMock()
        row_result.scalar_one_or_none.return_value = app
        mock_session.execute = AsyncMock(return_value=row_result)

        with patch(
            "services.applications.get_session",
            return_value=lambda: mock_session,
        ):
            result = await applications_service.get_for_participant(
                "BR-2026-0042",
                owner,
            )

        assert result is app
