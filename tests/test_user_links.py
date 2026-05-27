"""Покрытие резервного сценария LINKS (§33.6 ТЗ).

Тесты — unit-уровень: валидатор URL и форматирование инструкции
участнику. Интеграция с FSM и БД проверяется ручным smoke
(см. ``docs/testing.md`` → пункт 19).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from database.models import Track
from handlers.user_links import (
    _FILENAME_HINT_BY_TRACK,
    _build_instruction,
)
from utils.cloud_link import (
    MAX_URL_LEN,
    MIN_URL_LEN,
    parse_cloud_link,
)


class TestParseCloudLink:
    """``parse_cloud_link`` — формальная sanity-проверка URL без HTTP."""

    @pytest.mark.parametrize(
        "raw",
        [
            "https://disk.yandex.ru/d/abc123",
            "http://example.com/folder",
            "https://drive.google.com/drive/folders/XYZ?usp=sharing",
            "https://icloud.com/share/0001",
        ],
    )
    def test_valid_urls(self, raw: str):
        url, error = parse_cloud_link(raw)
        assert error is None
        assert url == raw

    def test_strips_trailing_text(self):
        """«https://… смотри здесь» — берём первый токен (URL)."""
        url, error = parse_cloud_link(
            "https://disk.yandex.ru/d/abc смотри здесь"
        )
        assert error is None
        assert url == "https://disk.yandex.ru/d/abc"

    def test_first_token_must_be_url(self):
        """Если первый токен — не URL, валидация падает (никакого fuzzy)."""
        url, error = parse_cloud_link("Вот ссылка: https://disk.yandex.ru/d/abc")
        assert url is None
        assert error is not None

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", None],
    )
    def test_empty_returns_error(self, raw):
        url, error = parse_cloud_link(raw)
        assert url is None
        assert error is not None
        assert "ссылк" in error.message.lower()

    def test_missing_scheme(self):
        url, error = parse_cloud_link("disk.yandex.ru/d/abc")
        assert url is None
        assert error is not None

    def test_ftp_scheme_rejected(self):
        url, error = parse_cloud_link("ftp://server.example/abc")
        assert url is None
        assert error is not None

    def test_too_short(self):
        short = "http://a/"  # длина 9 при MIN_URL_LEN=10
        assert len(short) < MIN_URL_LEN
        url, error = parse_cloud_link(short)
        assert url is None
        assert error is not None

    def test_too_long(self):
        too_long = "https://example.com/" + ("x" * (MAX_URL_LEN + 1))
        url, error = parse_cloud_link(too_long)
        assert url is None
        assert error is not None

    def test_scheme_only_no_host(self):
        url, error = parse_cloud_link("https:///path")
        assert url is None
        assert error is not None


class TestBuildInstruction:
    """``_build_instruction`` подставляет BR-ID, имя папки и шаблон файлов."""

    @staticmethod
    def _fake_app(track: Track) -> SimpleNamespace:
        # SimpleNamespace вместо реального Application: helper
        # `_format_application_folder_name` обращается только к
        # `parent_full_name`, `child_name`, `br_id`.
        return SimpleNamespace(
            br_id="BR-2026-0042",
            parent_full_name="Иванов Сергей Петрович",
            child_name="Анна",
        )

    def test_traditional_template(self):
        text = _build_instruction(self._fake_app(Track.TRADITIONAL), Track.TRADITIONAL)
        assert "BR-2026-0042" in text
        assert "Иванов" in text
        assert "Сергей" in text
        assert "Анна" in text
        # Имя файлов трека
        assert "BR-2026-0042_original" in text
        assert "BR-2026-0042_angle-1" in text

    def test_ai_template(self):
        text = _build_instruction(self._fake_app(Track.AI), Track.AI)
        assert "BR-2026-0042_ai-image" in text
        assert "ai-image" in text
        assert "original" not in text
        assert "angle" not in text

    def test_handmade_to_ai_template(self):
        text = _build_instruction(
            self._fake_app(Track.HANDMADE_TO_AI), Track.HANDMADE_TO_AI
        )
        assert "BR-2026-0042_diptych" in text
        assert "diptych" in text

    def test_intro_mentions_temporary_restriction(self):
        text = _build_instruction(self._fake_app(Track.AI), Track.AI)
        # Тон §33.6.2: «сервер временно не принимает файлы».
        assert "временно" in text.lower()
        assert "ссылк" in text.lower()

    def test_folder_name_strips_patronymic(self):
        """В имени папки только Фамилия+Имя родителя (§21.2)."""
        text = _build_instruction(self._fake_app(Track.AI), Track.AI)
        # Отчество «Петрович» в имя папки не идёт.
        assert "Петрович" not in text

    def test_track_label_present(self):
        for track in (Track.TRADITIONAL, Track.AI, Track.HANDMADE_TO_AI):
            text = _build_instruction(self._fake_app(track), track)
            assert track.value in text


class TestFilenameHintCoverage:
    """Sanity: на каждый трек есть хинт по именам файлов."""

    def test_all_tracks_covered(self):
        for track in Track:
            assert track in _FILENAME_HINT_BY_TRACK
