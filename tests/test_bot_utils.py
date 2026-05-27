"""Тесты утилит отправки файлов заявки в чат."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pybotx.models.attachments import OutgoingAttachment

from utils.bot_utils import (
    format_anonymous_file_caption,
    format_numbered_file_caption,
    pagination_footer,
    send_application_files_with_card,
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
        assert "angle" not in second_call["body"]
