"""Тесты ``config.build_contacts_text``."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

import config


@pytest.fixture(autouse=True)
def _clear_contacts_env(monkeypatch):
    """Изолировать CONTACTS_TEXT между тестами."""
    monkeypatch.delenv("CONTACTS_TEXT", raising=False)
    yield


class TestBuildContactsText:
    def test_default_includes_body_and_mention_footer(self):
        with patch.object(
            config.MentionBuilder,
            "contact",
            return_value="@@Винокурова Екатерина Васильевна",
        ) as contact_mock:
            text = config.build_contacts_text()

        contact_mock.assert_called_once_with(
            entity_id=UUID("16bce2de-8ce7-5e40-ad71-2353a1fede07"),
            name="Винокурова Екатерина Васильевна",
        )
        assert config.CONTACTS_TEXT_BODY in text
        assert "По всем вопросам писать @@Винокурова Екатерина Васильевна." in text
        assert "модерация" not in text

    def test_env_override_skips_mention_builder(self, monkeypatch):
        monkeypatch.setenv("CONTACTS_TEXT", "Кастомный текст контактов")
        with patch.object(
            config.MentionBuilder, "contact", MagicMock()
        ) as contact_mock:
            text = config.build_contacts_text()

        contact_mock.assert_not_called()
        assert text == "Кастомный текст контактов"
