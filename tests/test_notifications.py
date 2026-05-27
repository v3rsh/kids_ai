"""Проактивные уведомления: клавиатура в DM участнику; чат модерации без bubbles."""
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


def _participant_dm_commands(br_id: str = "BR-2026-0001") -> list[str]:
    return [
        f"/my_app {br_id}",
        "/menu_contacts",
        "/start",
    ]


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
        "notify_fn",
        [
            notifications.notify_participant_accepted,
            notifications.notify_participant_moderation_passed,
            notifications.notify_participant_rejected,
            notifications.notify_participant_shortlist,
        ],
    )
    async def test_simple_notifications_have_participant_dm(
        self,
        fake_bot: MagicMock,
        fake_app: MagicMock,
        chat_id: uuid.UUID,
        notify_fn,
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
        assert _extract_button_commands(kwargs["bubbles"]) == _participant_dm_commands()

    async def test_fix_needed_has_participant_dm(
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
        assert _extract_button_commands(kwargs["bubbles"]) == _participant_dm_commands()

    async def test_jury_result_top10_has_participant_dm(
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
        assert _extract_button_commands(kwargs["bubbles"]) == _participant_dm_commands()

    async def test_jury_result_out_has_participant_dm(
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
        assert _extract_button_commands(kwargs["bubbles"]) == _participant_dm_commands()

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


class TestModerationChatOutboundOnly:
    async def test_new_application_sends_without_bubbles(
        self, fake_bot: MagicMock, fake_app: MagicMock
    ) -> None:
        mod_chat_id = uuid.uuid4()
        with patch.object(
            notifications,
            "get_moderation_chat_id",
            return_value=mod_chat_id,
        ), patch.object(
            notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch.object(
            notifications.MentionBuilder,
            "contact",
            return_value="@@Parent",
        ):
            await notifications.notify_moderation_chat_new_application(
                fake_bot, fake_app
            )

        fake_bot.send_message.assert_awaited_once()
        kwargs = fake_bot.send_message.await_args.kwargs
        assert "bubbles" not in kwargs


class TestJuryRoundTemplates:
    """Шаблоны round_opened / round_closed используют новые поля.

    Покрывает §5.1 и §5.2 плана админ-меню жюри: «N претендентов на M
    мест» при открытии, детализация при закрытии и опциональный хвост
    «Топ-N пула определён» при ``pool_completed``.
    """

    def test_round_opened_single_template_uses_candidates_and_slots(self) -> None:
        body = notifications.JURY_ROUND_OPENED_SINGLE_TEMPLATE.format(
            pool="Традиционное рисование / 7–12",
            round_no=1,
            candidates_n=15,
            slots_n=10,
        )
        assert "15" in body
        assert "10" in body
        assert "раунд 1 открыт" in body
        assert "дедлайн" not in body.lower()

    def test_round_opened_aggregate_pool_line_renders_numbers(self) -> None:
        line = notifications.JURY_ROUND_OPENED_POOL_LINE.format(
            track="Традиционное",
            age="7–12",
            candidates_n=12,
            slots_n=10,
        )
        assert "12 претендентов на 10 мест" in line

    def test_round_closed_single_template_renders_detail(self) -> None:
        body = notifications.JURY_ROUND_CLOSED_SINGLE_TEMPLATE.format(
            pool="ИИ-рисунок / 0–6",
            round_no=2,
            candidates_n=8,
            fixed_top_n_in_round=3,
            tie_n=2,
            losers_n=3,
            remaining_slots_after=4,
        )
        assert "Кандидатов: 8" in body
        assert "В шорт-лист пула сразу: 3" in body
        assert "В зоне ничьи (уходят в след. раунд): 2" in body
        assert "Выбыло: 3" in body
        assert "Осталось мест в шорт-листе пула: 4" in body

    def test_pool_completed_tail_renders_lot_label_and_pool_top_n(self) -> None:
        tail = notifications.JURY_POOL_COMPLETED_TAIL_TEMPLATE.format(
            top_n=10,
            lot_label="да",
            pool_top_n=10,
        )
        assert "Топ-10 пула определён" in tail
        assert "жребий: да" in tail
        assert "В шорт-листе пула: 10 работ" in tail

    def test_round_closed_aggregate_pool_line_includes_all_metrics(self) -> None:
        line = notifications.JURY_ROUND_CLOSED_POOL_LINE.format(
            track="От руки к ИИ",
            age="13–18",
            candidates_n=12,
            fixed_top_n_in_round=8,
            tie_n=4,
            losers_n=0,
            remaining_slots_after=2,
        )
        assert "канд.12" in line
        assert "в шорт-лист 8" in line
        assert "ничья 4" in line
        assert "выбыло 0" in line
        assert "осталось мест 2" in line

    def test_undersized_pool_template_uses_works_and_top_n(self) -> None:
        body = notifications.JURY_UNDERSIZED_POOL_TEMPLATE.format(
            pool="ИИ-рисунок / 7–12",
            works_n=4,
            top_n=10,
        )
        assert "работ 4 < TOP_N=10" in body
        assert "Все 4 работ — в шорт-листе." in body
