"""Hybrid edit-in-place poведение карусели жюри.

Покрывает изменения в ``handlers.jury_tasks``:
- ``cmd_jt_vote`` редактирует caption photo-якоря (`bot.edit_message`),
  не двигая фото и хвост, при наличии ``FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID``;
- fallback на полный рендер, если якорь потерян (FSM expired) или edit
  упал (CTS 5xx после ретраев);
- ``cmd_jt_back`` / ветки выхода удаляют якорь явно (через
  ``delete_jury_anchor``) и очищают FSM.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from database.models import AgeCategory, JuryRoundStatus, JuryVoteValue, Track
from fsm.keys import (
    FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID,
    FSM_KEY_JURY_TASK_INDEX,
    FSM_KEY_JURY_TASK_ROUND_ID,
)


def _round_obj(round_no: int = 1) -> MagicMock:
    obj = MagicMock()
    obj.round_no = round_no
    obj.track = Track.TRADITIONAL
    obj.age_category = AgeCategory.AGE_13_18
    obj.status = JuryRoundStatus.OPEN
    return obj


def _candidate(idx: int = 0) -> MagicMock:
    app = MagicMock()
    app.id = UUID(int=idx + 1)
    app.title = f"Работа {idx + 1}"
    app.description = "Описание"
    app.cloud_link = None
    app.br_id = f"BR-2026-{idx + 1:04d}"
    return app


def _message(
    *,
    huid: UUID | None = None,
    bot_id: UUID | None = None,
    fsm_data: dict | None = None,
    data: dict | None = None,
    source_sync_id: UUID | None = None,
) -> MagicMock:
    """Билдер IncomingMessage для тестов жюри-карусели."""
    message = MagicMock()
    message.sender.huid = huid or uuid4()
    message.bot.id = bot_id or uuid4()
    message.data = data
    message.source_sync_id = source_sync_id
    message.body = ""

    fsm = AsyncMock()
    fsm.get_data = AsyncMock(return_value=fsm_data or {})
    fsm.update_data = AsyncMock()
    fsm.set_state = AsyncMock()
    fsm.get_state = AsyncMock(return_value=None)
    fsm.clear = AsyncMock()
    message.state.fsm = fsm
    message.state.transient_source_deleted = False
    return message


@pytest.fixture
def fake_session():
    """async with get_session()() as session: ... — двойная асинхронная обёртка."""
    session = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    get_session_mock = MagicMock(return_value=factory)
    return get_session_mock, session


def _patch_jury_role(huid: UUID):
    """``@jury_only`` декоратор пропускает вызов, если HUID в кэше."""
    return patch("services.access._jury_huids", {str(huid)})


@pytest.mark.asyncio
class TestCmdJtVoteEditInPlace:
    """``cmd_jt_vote`` обновляет caption через edit_message, не двигая фото."""

    async def test_edit_caption_called_when_anchor_in_fsm(self, fake_session):
        from handlers.jury_tasks import cmd_jt_vote

        huid = uuid4()
        round_id = uuid4()
        anchor_sync_id = uuid4()
        bot_id = uuid4()
        message = _message(
            huid=huid,
            bot_id=bot_id,
            data={"vote": JuryVoteValue.YES.name},
            source_sync_id=anchor_sync_id,
            fsm_data={
                FSM_KEY_JURY_TASK_ROUND_ID: str(round_id),
                FSM_KEY_JURY_TASK_INDEX: 0,
                FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID: str(anchor_sync_id),
            },
        )
        bot = MagicMock()
        get_session_mock, _ = fake_session
        candidates = [_candidate(0), _candidate(1)]

        with (
            _patch_jury_role(huid),
            patch("handlers.jury_tasks.get_session", get_session_mock),
            patch(
                "handlers.jury_tasks.jury_service.get_round_candidates_with_drafts",
                new=AsyncMock(
                    return_value=(_round_obj(), candidates, {candidates[1].id: JuryVoteValue.NO}),
                ),
            ),
            patch(
                "handlers.jury_tasks.jury_service.upsert_draft_vote",
                new=AsyncMock(),
            ),
            patch(
                "handlers.jury_tasks.storage_service.get_application_files_for_chat",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "handlers.jury_tasks.resolve_bot_id", return_value=bot_id,
            ),
            patch(
                "handlers.jury_tasks.edit_jury_anchor_caption",
                new=AsyncMock(return_value=True),
            ) as edit_mock,
            patch(
                "handlers.jury_tasks._render_current_view",
                new=AsyncMock(),
            ) as render_mock,
            patch(
                "handlers.jury_tasks._force_cleanup_transient",
                new=AsyncMock(),
            ) as force_cleanup_mock,
            patch(
                "handlers.jury_tasks.send_jury_carousel",
                new=AsyncMock(),
            ) as send_carousel_mock,
        ):
            await cmd_jt_vote(message, bot)

        edit_mock.assert_awaited_once()
        kwargs = edit_mock.await_args.kwargs
        assert kwargs["bot_id"] == bot_id
        assert kwargs["anchor_sync_id"] == anchor_sync_id
        assert "body" in kwargs
        assert kwargs["bubbles"] is not None
        # CRITICAL: ни fallback на render, ни send_jury_carousel не должны
        # сработать в happy-path — иначе фото и хвост пересоздадутся.
        render_mock.assert_not_awaited()
        force_cleanup_mock.assert_not_awaited()
        send_carousel_mock.assert_not_awaited()

    async def test_fallback_when_anchor_missing(self, fake_session):
        """FSM потерян после рестарта — без anchor_sync_id делаем полный рендер."""
        from handlers.jury_tasks import cmd_jt_vote

        huid = uuid4()
        round_id = uuid4()
        message = _message(
            huid=huid,
            data={"vote": JuryVoteValue.YES.name},
            fsm_data={
                FSM_KEY_JURY_TASK_ROUND_ID: str(round_id),
                FSM_KEY_JURY_TASK_INDEX: 0,
                # FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID отсутствует
            },
        )
        bot = MagicMock()
        get_session_mock, _ = fake_session
        candidates = [_candidate(0), _candidate(1)]

        with (
            _patch_jury_role(huid),
            patch("handlers.jury_tasks.get_session", get_session_mock),
            patch(
                "handlers.jury_tasks.jury_service.get_round_candidates_with_drafts",
                new=AsyncMock(
                    return_value=(_round_obj(), candidates, {candidates[1].id: JuryVoteValue.NO}),
                ),
            ),
            patch(
                "handlers.jury_tasks.jury_service.upsert_draft_vote",
                new=AsyncMock(),
            ),
            patch(
                "handlers.jury_tasks.storage_service.get_application_files_for_chat",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "handlers.jury_tasks.edit_jury_anchor_caption",
                new=AsyncMock(return_value=True),
            ) as edit_mock,
            patch(
                "handlers.jury_tasks._render_current_view",
                new=AsyncMock(),
            ) as render_mock,
            patch(
                "handlers.jury_tasks._force_cleanup_transient",
                new=AsyncMock(),
            ) as force_cleanup_mock,
        ):
            await cmd_jt_vote(message, bot)

        edit_mock.assert_not_awaited()
        force_cleanup_mock.assert_awaited_once()
        render_mock.assert_awaited_once()

    async def test_fallback_when_edit_fails(self, fake_session):
        """edit_jury_anchor_caption вернул False (CTS 5xx после retries)."""
        from handlers.jury_tasks import cmd_jt_vote

        huid = uuid4()
        round_id = uuid4()
        anchor_sync_id = uuid4()
        message = _message(
            huid=huid,
            data={"vote": JuryVoteValue.YES.name},
            source_sync_id=anchor_sync_id,
            fsm_data={
                FSM_KEY_JURY_TASK_ROUND_ID: str(round_id),
                FSM_KEY_JURY_TASK_INDEX: 0,
                FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID: str(anchor_sync_id),
            },
        )
        bot = MagicMock()
        get_session_mock, _ = fake_session
        candidates = [_candidate(0), _candidate(1)]

        with (
            _patch_jury_role(huid),
            patch("handlers.jury_tasks.get_session", get_session_mock),
            patch(
                "handlers.jury_tasks.jury_service.get_round_candidates_with_drafts",
                new=AsyncMock(
                    return_value=(_round_obj(), candidates, {candidates[1].id: JuryVoteValue.NO}),
                ),
            ),
            patch(
                "handlers.jury_tasks.jury_service.upsert_draft_vote",
                new=AsyncMock(),
            ),
            patch(
                "handlers.jury_tasks.storage_service.get_application_files_for_chat",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "handlers.jury_tasks.resolve_bot_id", return_value=uuid4(),
            ),
            patch(
                "handlers.jury_tasks.edit_jury_anchor_caption",
                new=AsyncMock(return_value=False),
            ) as edit_mock,
            patch(
                "handlers.jury_tasks._render_current_view",
                new=AsyncMock(),
            ) as render_mock,
            patch(
                "handlers.jury_tasks._force_cleanup_transient",
                new=AsyncMock(),
            ) as force_cleanup_mock,
        ):
            await cmd_jt_vote(message, bot)

        edit_mock.assert_awaited_once()
        force_cleanup_mock.assert_awaited_once()
        render_mock.assert_awaited_once()

    async def test_no_cleanup_middleware_on_jt_vote(self):
        """``cmd_jt_vote`` зарегистрирован БЕЗ ``cleanup_middleware``.

        Если middleware вернётся — хвост из доп. файлов будет удаляться
        при каждом голосе и фикс прокрутки рассыплется.
        """
        from handlers.jury_tasks import collector

        handler = collector._user_commands_handlers.get("/jt_vote")
        assert handler is not None, "/jt_vote handler не найден"

        middlewares = [m.__name__ for m in handler.middlewares]
        assert "cleanup_middleware" not in middlewares
        assert "fsm_middleware" in middlewares


@pytest.mark.asyncio
class TestCmdJtBackDropsAnchor:
    """``cmd_jt_back``: удаляет photo-якорь и очищает FSM."""

    async def test_drops_anchor_when_present(self):
        from handlers.jury_tasks import cmd_jt_back

        huid = uuid4()
        anchor_sync_id = uuid4()
        bot_id = uuid4()
        message = _message(
            huid=huid,
            bot_id=bot_id,
            source_sync_id=anchor_sync_id,
            fsm_data={FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID: str(anchor_sync_id)},
        )
        bot = MagicMock()

        with (
            _patch_jury_role(huid),
            patch(
                "handlers.jury_tasks.resolve_bot_id", return_value=bot_id,
            ),
            patch(
                "handlers.jury_tasks.delete_jury_anchor",
                new=AsyncMock(),
            ) as delete_mock,
            patch(
                "handlers.jury_tasks.cmd_jury_tasks_internal",
                new=AsyncMock(),
            ),
        ):
            await cmd_jt_back(message, bot)

        delete_mock.assert_awaited_once()
        kwargs = delete_mock.await_args.kwargs
        assert kwargs["bot_id"] == bot_id
        assert kwargs["anchor_sync_id"] == anchor_sync_id
        # Source совпадал с anchor → reply_to_user должен пропустить edit.
        assert message.state.transient_source_deleted is True
        message.state.fsm.clear.assert_awaited_once()

    async def test_skips_delete_when_no_anchor(self):
        from handlers.jury_tasks import cmd_jt_back

        huid = uuid4()
        message = _message(huid=huid, fsm_data={})
        bot = MagicMock()

        with (
            _patch_jury_role(huid),
            patch(
                "handlers.jury_tasks.delete_jury_anchor",
                new=AsyncMock(),
            ) as delete_mock,
            patch(
                "handlers.jury_tasks.cmd_jury_tasks_internal",
                new=AsyncMock(),
            ),
        ):
            await cmd_jt_back(message, bot)

        delete_mock.assert_not_awaited()
        message.state.fsm.clear.assert_awaited_once()


@pytest.mark.asyncio
class TestRenderCurrentViewAnchorLifecycle:
    """``_render_current_view``: удаляет старый якорь и записывает новый sync_id."""

    async def test_writes_new_anchor_sync_id_to_fsm(self, fake_session):
        from handlers.jury_tasks import _render_current_view

        huid = uuid4()
        round_id = uuid4()
        old_anchor = uuid4()
        new_anchor = uuid4()
        bot_id = uuid4()
        message = _message(
            huid=huid,
            bot_id=bot_id,
            source_sync_id=old_anchor,
            fsm_data={FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID: str(old_anchor)},
        )
        bot = MagicMock()
        get_session_mock, _ = fake_session
        candidates = [_candidate(0)]

        with (
            patch("handlers.jury_tasks.get_session", get_session_mock),
            patch(
                "handlers.jury_tasks.jury_service.get_round_candidates_with_drafts",
                new=AsyncMock(return_value=(_round_obj(), candidates, {})),
            ),
            patch(
                "handlers.jury_tasks.storage_service.get_application_files_for_chat",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "handlers.jury_tasks.resolve_bot_id", return_value=bot_id,
            ),
            patch(
                "handlers.jury_tasks.delete_jury_anchor",
                new=AsyncMock(),
            ) as delete_mock,
            patch(
                "handlers.jury_tasks.delete_source_message",
                new=AsyncMock(),
            ),
            patch(
                "handlers.jury_tasks.send_jury_carousel",
                new=AsyncMock(return_value=new_anchor),
            ) as send_mock,
        ):
            await _render_current_view(message, bot, round_id, requested_index=0)

        delete_mock.assert_awaited_once()
        assert delete_mock.await_args.kwargs["anchor_sync_id"] == old_anchor
        send_mock.assert_awaited_once()

        # Проверяем, что в FSM записан новый anchor_sync_id (последний update_data).
        anchor_writes = [
            call.kwargs.get(FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID)
            for call in message.state.fsm.update_data.await_args_list
            if FSM_KEY_JURY_TASK_ANCHOR_SYNC_ID in call.kwargs
        ]
        assert anchor_writes, "ANCHOR_SYNC_ID не записан в FSM"
        assert anchor_writes[-1] == str(new_anchor)
