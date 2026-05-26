"""Тесты ``utils.deeplink``."""
from __future__ import annotations

from uuid import UUID

import pytest

import utils.deeplink as deeplink_module


@pytest.fixture(autouse=True)
def _reset_deeplink_config(monkeypatch):
    """Изолировать env-шаблон между тестами."""
    monkeypatch.setattr(deeplink_module, "EXPRESS_DEEPLINK_TEMPLATE", "")
    monkeypatch.setattr(deeplink_module, "CTS_URL", "https://cts.example.com")
    yield


class TestBuildBotDeeplink:
    def test_empty_template_returns_none(self):
        assert deeplink_module.build_bot_deeplink(UUID(int=1)) is None

    def test_renders_bot_id_and_cts_url(self, monkeypatch):
        monkeypatch.setattr(
            deeplink_module,
            "EXPRESS_DEEPLINK_TEMPLATE",
            "{cts_url}/chats/personal/{bot_id}",
        )
        bot_id = UUID("00000000-0000-0000-0000-000000000001")
        assert (
            deeplink_module.build_bot_deeplink(bot_id)
            == "https://cts.example.com/chats/personal/00000000-0000-0000-0000-000000000001"
        )

    def test_none_bot_id(self, monkeypatch):
        monkeypatch.setattr(
            deeplink_module,
            "EXPRESS_DEEPLINK_TEMPLATE",
            "express://chat?bot_id={bot_id}",
        )
        assert deeplink_module.build_bot_deeplink(None) is None


class TestBuildFindDeeplink:
    def test_empty_template_returns_none(self):
        bot_id = UUID(int=1)
        assert deeplink_module.build_find_deeplink(bot_id, "BR-2026-0001") is None

    def test_renders_command_placeholders(self, monkeypatch):
        monkeypatch.setattr(
            deeplink_module,
            "EXPRESS_DEEPLINK_TEMPLATE",
            "express://chat?bot_id={bot_id}&text={command_encoded}",
        )
        bot_id = UUID("00000000-0000-0000-0000-000000000002")
        link = deeplink_module.build_find_deeplink(bot_id, "br-2026-0042")
        assert link is not None
        assert "00000000-0000-0000-0000-000000000002" in link
        assert "text=%2Ffind%20BR-2026-0042" in link

    def test_normalizes_br_id(self, monkeypatch):
        monkeypatch.setattr(
            deeplink_module,
            "EXPRESS_DEEPLINK_TEMPLATE",
            "{command}",
        )
        bot_id = UUID(int=3)
        assert (
            deeplink_module.build_find_deeplink(bot_id, "  br-2026-0007 ")
            == "/find BR-2026-0007"
        )

    def test_empty_br_id(self, monkeypatch):
        monkeypatch.setattr(
            deeplink_module,
            "EXPRESS_DEEPLINK_TEMPLATE",
            "{command}",
        )
        assert deeplink_module.build_find_deeplink(UUID(int=4), "") is None

    def test_invalid_template_logs_and_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            deeplink_module,
            "EXPRESS_DEEPLINK_TEMPLATE",
            "{missing_placeholder}",
        )
        assert (
            deeplink_module.build_find_deeplink(UUID(int=5), "BR-2026-0001")
            is None
        )
