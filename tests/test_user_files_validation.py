"""Unit-тесты лимитов загрузки файлов на шаге анкеты."""
from __future__ import annotations

import pytest

from database.models import Track
from handlers.user_files import (
    TRADITIONAL_MAX_FILES,
    build_file_accepted_caption,
    check_file_upload,
    count_raw_attachments,
    validate_files_count_for_track,
)


class TestCheckFileUpload:
    """`check_file_upload` — gate и лимиты по треку на шаге files_collect."""

    def test_first_file_allowed(self):
        assert (
            check_file_upload(
                Track.TRADITIONAL, 0, upload_allowed=True
            )
            is None
        )
        assert (
            check_file_upload(Track.AI, 0, upload_allowed=True) is None
        )

    def test_second_file_without_add_more_button_rejected(self):
        err = check_file_upload(
            Track.TRADITIONAL, 1, upload_allowed=False
        )
        assert err is not None
        assert "один файл" in err.lower()

    def test_single_file_track_rejects_second(self):
        err = check_file_upload(Track.AI, 1, upload_allowed=True)
        assert err is not None
        assert "ровно один файл" in err.lower()

    def test_handmade_to_ai_rejects_second(self):
        err = check_file_upload(
            Track.HANDMADE_TO_AI, 1, upload_allowed=True
        )
        assert err is not None

    def test_traditional_max_files_rejected(self):
        err = check_file_upload(
            Track.TRADITIONAL,
            TRADITIONAL_MAX_FILES,
            upload_allowed=True,
        )
        assert err is not None
        assert str(TRADITIONAL_MAX_FILES) in err

    @pytest.mark.parametrize("files_count", [1, 2, 3])
    def test_traditional_next_file_after_add_more_ok(self, files_count: int):
        assert (
            check_file_upload(
                Track.TRADITIONAL,
                files_count,
                upload_allowed=True,
            )
            is None
        )


class TestValidateFilesCountForTrack:
    """Submit-time проверка количества файлов."""

    @pytest.mark.parametrize("track", [Track.AI, Track.HANDMADE_TO_AI])
    def test_single_file_tracks_require_exactly_one(self, track: Track):
        assert validate_files_count_for_track(track, 1) is None
        assert validate_files_count_for_track(track, 0) is not None
        assert validate_files_count_for_track(track, 2) is not None

    @pytest.mark.parametrize("count", [1, 2, 3, 4])
    def test_traditional_accepts_one_to_four(self, count: int):
        assert (
            validate_files_count_for_track(Track.TRADITIONAL, count) is None
        )

    def test_traditional_rejects_zero_and_five(self):
        assert validate_files_count_for_track(Track.TRADITIONAL, 0) is not None
        assert validate_files_count_for_track(Track.TRADITIONAL, 5) is not None


class TestCountRawAttachments:
    """`count_raw_attachments` — раннее обнаружение нескольких вложений в одном сообщении."""

    def test_none_or_empty_or_missing_key_returns_zero(self):
        assert count_raw_attachments(None) == 0
        assert count_raw_attachments({}) == 0
        assert count_raw_attachments({"command": {}}) == 0

    def test_single_attachment(self):
        raw = {"attachments": [{"type": "image", "data": {"file_name": "a.jpg"}}]}
        assert count_raw_attachments(raw) == 1

    def test_multiple_attachments(self):
        raw = {
            "attachments": [
                {"type": "image", "data": {"file_name": "a.jpg"}},
                {"type": "image", "data": {"file_name": "b.png"}},
            ]
        }
        assert count_raw_attachments(raw) == 2

    def test_non_list_attachments_treated_as_zero(self):
        assert count_raw_attachments({"attachments": "not-a-list"}) == 0
        assert count_raw_attachments({"attachments": None}) == 0


class TestBuildFileAcceptedCaption:
    """`build_file_accepted_caption` — подпись echo-сообщения на шаге 7."""

    def test_traditional_first_file(self):
        caption = build_file_accepted_caption(
            Track.TRADITIONAL, "photo.jpg", 1
        )
        assert "Шаг 7 из 7" in caption
        assert "photo.jpg" in caption
        assert "1/4" in caption
        assert "Добавьте ещё файл" in caption

    def test_traditional_fourth_file(self):
        caption = build_file_accepted_caption(
            Track.TRADITIONAL, "side4.png", TRADITIONAL_MAX_FILES
        )
        assert "4/4" in caption
        assert "side4.png" in caption
        assert "Лимит достигнут" in caption

    def test_ai_track(self):
        caption = build_file_accepted_caption(
            Track.AI, "ai_art.webp", 1
        )
        assert "ai_art.webp" in caption
        assert "Переходим к согласиям" in caption
        assert "коллаж" not in caption.lower()

    def test_handmade_to_ai_track(self):
        caption = build_file_accepted_caption(
            Track.HANDMADE_TO_AI, "collage.jpg", 1
        )
        assert "коллаж" in caption.lower()
        assert "collage.jpg" in caption
        assert "Переходим к согласиям" in caption
