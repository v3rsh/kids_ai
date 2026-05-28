"""Тесты утилит отправки файлов заявки в чат."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pybotx.models.attachments import OutgoingAttachment

from utils.bot_utils import (
    delete_jury_anchor,
    edit_jury_anchor_caption,
    format_anonymous_file_caption,
    format_numbered_file_caption,
    pagination_footer,
    resolve_dm_chat_id,
    send_application_files_with_card,
    send_jury_carousel,
)


def _attachment(name: str) -> OutgoingAttachment:
    return OutgoingAttachment(content=b"x", filename=name)


def _app(br_id: str = "BR-2026-0001") -> MagicMock:
    app = MagicMock()
    app.br_id = br_id
    return app


class TestPaginationFooter:
    def test_empty_when_single_page(self):
        assert pagination_footer(1, 1) == ""

    def test_plain_format(self):
        assert pagination_footer(3, 20) == "\n\n3 из 20"

    def test_with_title(self):
        assert pagination_footer(2, 5, title="Карусель") == (
            "\n\n**Карусель:** 2 из 5"
        )


class TestFileCaptions:
    def test_numbered_caption_includes_br_id_and_filename(self):
        caption = format_numbered_file_caption(
            "BR-2026-0001", 2, 3, "BR-2026-0001_original.jpg"
        )
        assert caption == (
            "📎 BR-2026-0001: файл 2 из 3 — BR-2026-0001_original.jpg"
        )

    def test_anonymous_caption_hides_identifiers(self):
        caption = format_anonymous_file_caption(2, 3)
        assert caption == "📎 Файл 2 из 3"
        assert "BR-" not in caption


@pytest.mark.asyncio
class TestSendApplicationFilesWithCard:
    async def test_returns_false_when_no_attachments(self):
        message = MagicMock()
        bot = MagicMock()
        app = _app()

        with patch(
            "services.storage.get_application_files_for_chat",
            new=AsyncMock(return_value=[]),
        ):
            sent = await send_application_files_with_card(
                message, bot, app=app, body="card", bubbles=MagicMock()
            )

        assert sent is False

    async def test_sends_all_files_with_br_id_captions(self):
        message = MagicMock()
        bot = MagicMock()
        app = _app()
        attachments = [
            _attachment("BR-2026-0001_original.jpg"),
            _attachment("BR-2026-0001_angle-1.jpg"),
        ]
        bubbles = MagicMock()

        with (
            patch(
                "services.storage.get_application_files_for_chat",
                new=AsyncMock(return_value=attachments),
            ),
            patch(
                "utils.bot_utils.delete_source_message",
                new=AsyncMock(),
            ) as delete_mock,
            patch(
                "utils.bot_utils.send_photo_transient",
                new=AsyncMock(),
            ) as send_mock,
        ):
            sent = await send_application_files_with_card(
                message,
                bot,
                app=app,
                body="card",
                bubbles=bubbles,
            )

        assert sent is True
        delete_mock.assert_awaited_once_with(message, bot)
        assert send_mock.await_count == 2
        first_call = send_mock.await_args_list[0].kwargs
        assert first_call["body"] == "card"
        assert first_call["photo"] is attachments[0]
        assert first_call["bubbles"] is bubbles
        second_call = send_mock.await_args_list[1].kwargs
        assert second_call["body"] == (
            "📎 BR-2026-0001: файл 2 из 2 — BR-2026-0001_angle-1.jpg"
        )

    async def test_anonymous_extra_captions_for_jury(self):
        message = MagicMock()
        bot = MagicMock()
        app = _app()
        attachments = [
            _attachment("BR-2026-0001_original.jpg"),
            _attachment("BR-2026-0001_angle-1.jpg"),
        ]

        with (
            patch(
                "services.storage.get_application_files_for_chat",
                new=AsyncMock(return_value=attachments),
            ),
            patch("utils.bot_utils.delete_source_message", new=AsyncMock()),
            patch(
                "utils.bot_utils.send_photo_transient",
                new=AsyncMock(),
            ) as send_mock,
        ):
            sent = await send_application_files_with_card(
                message,
                bot,
                app=app,
                body="task",
                bubbles=MagicMock(),
                anonymous_extra_captions=True,
            )

        assert sent is True
        second_call = send_mock.await_args_list[1].kwargs
        assert second_call["body"] == "📎 Файл 2 из 2"
        assert "BR-" not in second_call["body"]

    async def test_uses_preloaded_attachments_without_storage_call(self):
        message = MagicMock()
        bot = MagicMock()
        app = _app()
        attachments = [
            _attachment("BR-2026-0001_original.jpg"),
            _attachment("BR-2026-0001_angle-1.jpg"),
        ]
        storage_mock = AsyncMock()

        with (
            patch(
                "services.storage.get_application_files_for_chat",
                new=storage_mock,
            ),
            patch("utils.bot_utils.delete_source_message", new=AsyncMock()),
            patch(
                "utils.bot_utils.send_photo_transient",
                new=AsyncMock(),
            ) as send_mock,
        ):
            sent = await send_application_files_with_card(
                message,
                bot,
                app=app,
                body="card",
                bubbles=MagicMock(),
                attachments=attachments,
            )

        assert sent is True
        storage_mock.assert_not_awaited()
        assert send_mock.await_count == 2


@pytest.mark.asyncio
class TestSendJuryCarousel:
    """``send_jury_carousel``: persistent photo-якорь + transient хвост."""

    async def test_returns_text_anchor_sync_id_when_no_attachments(self):
        message = MagicMock()
        bot = MagicMock()
        anchor_sync_id = uuid.uuid4()
        bot.answer_message = AsyncMock(return_value=anchor_sync_id)
        bubbles = MagicMock()

        result = await send_jury_carousel(
            message,
            bot,
            body="task body",
            bubbles=bubbles,
            attachments=None,
        )

        assert result == anchor_sync_id
        bot.answer_message.assert_awaited_once_with(
            "task body",
            wait_callback=False,
            bubbles=bubbles,
        )

    async def test_first_file_is_persistent_rest_are_transient(self):
        message = MagicMock()
        bot = MagicMock()
        attachments = [
            _attachment("BR-2026-0001_original.jpg"),
            _attachment("BR-2026-0001_angle-1.jpg"),
            _attachment("BR-2026-0001_angle-2.jpg"),
        ]
        bubbles = MagicMock()
        anchor_sync_id = uuid.uuid4()

        with (
            patch(
                "utils.bot_utils.send_photo_persistent",
                new=AsyncMock(return_value=anchor_sync_id),
            ) as persistent_mock,
            patch(
                "utils.bot_utils.send_photo_transient",
                new=AsyncMock(return_value=uuid.uuid4()),
            ) as transient_mock,
        ):
            result = await send_jury_carousel(
                message,
                bot,
                body="task body",
                bubbles=bubbles,
                attachments=attachments,
            )

        assert result == anchor_sync_id
        persistent_mock.assert_awaited_once()
        first_call = persistent_mock.await_args.kwargs
        assert first_call["body"] == "task body"
        assert first_call["bubbles"] is bubbles
        assert first_call["photo"] is attachments[0]

        assert transient_mock.await_count == 2
        for idx, call in enumerate(transient_mock.await_args_list, start=2):
            assert call.kwargs["body"] == f"📎 Файл {idx} из 3"
            assert "bubbles" not in call.kwargs

    async def test_does_not_call_delete_source_message(self):
        """Источник снимает хендлер через ``_drop_old_anchor`` / ``delete_source_message``."""
        message = MagicMock()
        bot = MagicMock()

        with (
            patch(
                "utils.bot_utils.delete_source_message",
                new=AsyncMock(),
            ) as delete_mock,
            patch(
                "utils.bot_utils.send_photo_persistent",
                new=AsyncMock(return_value=uuid.uuid4()),
            ),
        ):
            await send_jury_carousel(
                message,
                bot,
                body="task",
                bubbles=MagicMock(),
                attachments=[_attachment("a.jpg")],
            )

        delete_mock.assert_not_awaited()

    async def test_returns_none_when_persistent_send_fails(self):
        message = MagicMock()
        bot = MagicMock()

        with patch(
            "utils.bot_utils.send_photo_persistent",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await send_jury_carousel(
                message,
                bot,
                body="task",
                bubbles=MagicMock(),
                attachments=[_attachment("a.jpg")],
            )

        assert result is None


@pytest.mark.asyncio
class TestEditJuryAnchorCaption:
    """``edit_jury_anchor_caption``: edit body+bubbles, file=Undefined."""

    async def test_edit_message_called_without_file(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()
        bot_id = uuid.uuid4()
        anchor_sync_id = uuid.uuid4()
        bubbles = MagicMock()

        ok = await edit_jury_anchor_caption(
            bot,
            bot_id=bot_id,
            anchor_sync_id=anchor_sync_id,
            body="new caption",
            bubbles=bubbles,
        )

        assert ok is True
        bot.edit_message.assert_awaited_once()
        kwargs = bot.edit_message.await_args.kwargs
        assert kwargs["bot_id"] == bot_id
        assert kwargs["sync_id"] == anchor_sync_id
        assert kwargs["body"] == "new caption"
        assert kwargs["bubbles"] is bubbles
        # CRITICAL: file must NOT be passed — иначе CTS попытается заменить вложение.
        assert "file" not in kwargs

    async def test_edit_message_skips_bubbles_when_none(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock()

        ok = await edit_jury_anchor_caption(
            bot,
            bot_id=uuid.uuid4(),
            anchor_sync_id=uuid.uuid4(),
            body="x",
            bubbles=None,
        )

        assert ok is True
        kwargs = bot.edit_message.await_args.kwargs
        assert "bubbles" not in kwargs

    async def test_returns_false_after_failed_retries(self):
        bot = MagicMock()
        bot.edit_message = AsyncMock(
            side_effect=RuntimeError("CTS 502 Bad Gateway"),
        )

        ok = await edit_jury_anchor_caption(
            bot,
            bot_id=uuid.uuid4(),
            anchor_sync_id=uuid.uuid4(),
            body="x",
            bubbles=MagicMock(),
            retries=2,
            delay=0.0,
        )

        assert ok is False
        assert bot.edit_message.await_count == 2


@pytest.mark.asyncio
class TestDeleteJuryAnchor:
    """``delete_jury_anchor``: безопасное удаление якоря."""

    async def test_calls_bot_delete_message(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock()
        bot_id = uuid.uuid4()
        anchor_sync_id = uuid.uuid4()

        await delete_jury_anchor(
            bot, bot_id=bot_id, anchor_sync_id=anchor_sync_id,
        )

        bot.delete_message.assert_awaited_once_with(
            bot_id=bot_id, sync_id=anchor_sync_id,
        )

    async def test_swallows_not_found_error(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(
            side_effect=RuntimeError("event_not_found"),
        )

        # Не должно бросать.
        await delete_jury_anchor(
            bot, bot_id=uuid.uuid4(), anchor_sync_id=uuid.uuid4(),
        )

    async def test_swallows_unknown_error_with_warning(self):
        bot = MagicMock()
        bot.delete_message = AsyncMock(
            side_effect=RuntimeError("unexpected something"),
        )

        await delete_jury_anchor(
            bot, bot_id=uuid.uuid4(), anchor_sync_id=uuid.uuid4(),
        )


class TestResolveDmChatId:
    def test_returns_chat_id_from_message(self):
        chat_id = uuid.uuid4()
        message = MagicMock()
        message.chat.id = chat_id

        assert resolve_dm_chat_id(message) == chat_id

    def test_returns_none_without_chat(self):
        message = MagicMock()
        message.chat = None

        assert resolve_dm_chat_id(message) is None


def _consume_create_task(coro, **kwargs):
    coro.close()
    return MagicMock()


@pytest.mark.asyncio
class TestStartExportTask:
    async def test_starts_background_task_with_chat_id(self):
        from handlers.admin_export import start_export_task

        chat_id = uuid.uuid4()
        huid = uuid.uuid4()
        bot = MagicMock()

        with (
            patch(
                "handlers.admin_export.resolve_bot_id",
                return_value=uuid.uuid4(),
            ),
            patch(
                "handlers.admin_export.asyncio.create_task",
                side_effect=_consume_create_task,
            ) as create_task_mock,
        ):
            started = await start_export_task(
                bot=bot,
                chat_id=chat_id,
                huid=huid,
                selector_action="export_files_all",
            )

        assert started is True
        create_task_mock.assert_called_once()
        assert create_task_mock.call_args.kwargs["name"] == (
            "attachments_export[all]"
        )

    async def test_returns_false_without_chat_id(self):
        from handlers.admin_export import start_export_task

        bot = MagicMock()

        with (
            patch(
                "handlers.admin_export.resolve_bot_id",
                return_value=uuid.uuid4(),
            ),
            patch(
                "handlers.admin_export.asyncio.create_task",
            ) as create_task_mock,
        ):
            started = await start_export_task(
                bot=bot,
                chat_id=None,
                huid=uuid.uuid4(),
                selector_action="export_files_all",
            )

        assert started is False
        create_task_mock.assert_not_called()
