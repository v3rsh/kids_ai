"""Unit-тесты лимитов загрузки файлов на шаге анкеты."""
from __future__ import annotations

import pytest

from database.models import Track
from handlers.user_files import (
    TRADITIONAL_MAX_FILES,
    check_file_upload,
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
