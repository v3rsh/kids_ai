"""maybe_announce_next_task_to_judge: sequential invariant и порядок пулов."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from database.models import AgeCategory, Track
from services import jury_notifications
from utils.contracts import JuryTaskDTO, PoolKey


def _task(pool: PoolKey, round_no: int, round_id: uuid.UUID | None = None) -> JuryTaskDTO:
    return JuryTaskDTO(
        round_id=round_id or uuid.uuid4(),
        application_id=uuid.uuid4(),
        pool=pool,
        round_no=round_no,
        local_no=1,
        title="t",
        description="d",
        cloud_link=None,
        draft_vote=None,
    )


@pytest.fixture
def fake_bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


class TestMaybeAnnounceNextTask:
    async def test_open_round_sends_dm_only_when_first_task(
        self, fake_bot: MagicMock
    ) -> None:
        jury_huid = uuid.uuid4()
        pools = list(__import__("services.pools", fromlist=["all_pools"]).all_pools())
        tasks = [_task(pools[0], 1), _task(pools[1], 1)]

        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch.object(
            jury_notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(side_effect=[[tasks[0]], tasks]),
        ):
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="open_round"
            )

        fake_bot.send_message.assert_awaited_once()

    async def test_open_round_skips_when_already_has_tasks(
        self, fake_bot: MagicMock
    ) -> None:
        jury_huid = uuid.uuid4()
        pool = PoolKey(track=Track.TRADITIONAL, age_category=AgeCategory.AGE_7_12)
        tasks = [_task(pool, 1), _task(pool, 2, round_id=uuid.uuid4())]

        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(return_value=tasks),
        ):
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="open_round"
            )

        fake_bot.send_message.assert_not_awaited()

    async def test_submit_votes_announces_next(self, fake_bot: MagicMock) -> None:
        jury_huid = uuid.uuid4()
        pools = list(__import__("services.pools", fromlist=["all_pools"]).all_pools())
        tasks = [_task(pools[1], 1)]

        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch.object(
            jury_notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(return_value=tasks),
        ):
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="submit_votes"
            )

        fake_bot.send_message.assert_awaited_once()

    async def test_group_order_follows_all_pools(self) -> None:
        pools = list(__import__("services.pools", fromlist=["all_pools"]).all_pools())
        tasks = [
            _task(pools[2], 1),
            _task(pools[0], 1),
            _task(pools[1], 2, round_id=uuid.uuid4()),
        ]
        grouped = jury_notifications._group_open_rounds(tasks)
        assert grouped[0].pool == pools[0]
        assert grouped[1].pool == pools[1]
        assert grouped[2].pool == pools[2]

    async def test_sequential_not_gather_regression(self, fake_bot: MagicMock) -> None:
        """open_round: DM только при переходе 0→1 открытая задача."""
        jury_huid = uuid.uuid4()
        pools = list(__import__("services.pools", fromlist=["all_pools"]).all_pools())
        side_effect = [
            [_task(pools[0], 1)],
            [_task(pools[0], 1), _task(pools[1], 1)],
        ]

        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch.object(
            jury_notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(side_effect=side_effect),
        ):
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="open_round"
            )
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="open_round"
            )

        assert fake_bot.send_message.await_count == 1

    async def test_nine_pools_sequential_one_dm_per_judge(
        self, fake_bot: MagicMock
    ) -> None:
        """Последовательное открытие 9 пулов: ровно один DM судье на первый пул."""
        jury_huid = uuid.uuid4()
        pools = list(__import__("services.pools", fromlist=["all_pools"]).all_pools())
        assert len(pools) == 9

        # На i-й итерации `get_open_tasks_for_jury` возвращает задачи
        # для первых (i+1) пулов — последовательное накопление.
        side_effect = [
            [_task(pools[j], 1) for j in range(i + 1)]
            for i in range(9)
        ]

        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch.object(
            jury_notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(side_effect=side_effect),
        ):
            for _ in range(9):
                await jury_notifications.maybe_announce_next_task_to_judge(
                    fake_bot, jury_huid=jury_huid, trigger="open_round"
                )

        assert fake_bot.send_message.await_count == 1

    async def test_judge_added_dm_when_at_least_one_open_task(
        self, fake_bot: MagicMock
    ) -> None:
        """judge_added: DM, если ``count_after >= 1``."""
        jury_huid = uuid.uuid4()
        pools = list(__import__("services.pools", fromlist=["all_pools"]).all_pools())
        tasks = [
            _task(pools[2], 1),
            _task(pools[0], 1),
        ]

        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch.object(
            jury_notifications, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(return_value=tasks),
        ):
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="judge_added"
            )

        fake_bot.send_message.assert_awaited_once()
        kwargs = fake_bot.send_message.await_args.kwargs
        # DM указывает на первый пул по порядку `all_pools`, а не на
        # порядок добавления задач.
        assert pools[0].track.value in kwargs["body"]

    async def test_judge_added_skips_when_no_open_tasks(
        self, fake_bot: MagicMock
    ) -> None:
        """judge_added без открытых задач — DM не отправляем."""
        jury_huid = uuid.uuid4()
        with patch.object(
            jury_notifications,
            "_resolve_user_chat_id",
            AsyncMock(return_value=uuid.uuid4()),
        ), patch(
            "services.jury.get_open_tasks_for_jury",
            AsyncMock(return_value=[]),
        ):
            await jury_notifications.maybe_announce_next_task_to_judge(
                fake_bot, jury_huid=jury_huid, trigger="judge_added"
            )

        fake_bot.send_message.assert_not_awaited()
