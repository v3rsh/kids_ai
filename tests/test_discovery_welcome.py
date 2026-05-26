"""Покрытие welcome-DM после одобрения роли (``services.discovery``).

Что покрывается:

1. ``send_welcome_dm_to_moderator`` — DM уходит с меню модератора
   (кнопки ``/queue``, ``/m_help``, …, «Назад в главное меню»);
   параллельно в FSM-хранилище ставится ``moderator:menu``.
2. ``send_welcome_dm_to_jury`` — аналогично, меню жюри
   (``/jury_tasks``, ``/jury_status``, «Назад в главное меню»);
   FSM-state — ``jury:menu``.
3. Если у пользователя нет ``users.chat_id`` (не писал ``/start``)
   — отправка DM не делается, но FSM-state всё равно записывается:
   пусть, когда юзер позже зайдёт сам через ``/moderator`` или
   ``/jury``, он сразу получит меню роли без повторного запроса.

Тесты — мок-уровень: ``bot.send_message`` / Redis-хранилище FSM
подменяются ``AsyncMock``; PostgreSQL / Redis не поднимаем.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import discovery


def _extract_button_commands(bubbles) -> list[str]:
    """Развернуть ``BubbleMarkup`` в плоский список команд кнопок.

    ``BubbleMarkup`` — итерируемый по строкам кнопок (``__iter__``
    отдаёт ``list[list[Button]]``); внутренний ``_buttons`` не
    трогаем, чтобы тест не зависел от приватных атрибутов pybotx.
    """
    commands: list[str] = []
    for row in bubbles:
        for button in row:
            commands.append(button.command)
    return commands


@pytest.fixture
def fake_bot() -> MagicMock:
    """Минимальный мок ``Bot`` с заглушкой ``send_message``."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def fake_storage() -> MagicMock:
    """Мок ``RedisFSMStorage`` для перехвата ``set_state``."""
    storage = MagicMock()
    storage.set_state = AsyncMock()
    return storage


class TestModeratorWelcomeDM:
    """``send_welcome_dm_to_moderator``: меню модератора + FSM-state."""

    async def test_dm_carries_moderator_menu_and_sets_state(
        self, fake_bot: MagicMock, fake_storage: MagicMock
    ) -> None:
        huid = uuid.uuid4()
        chat_id = uuid.uuid4()

        with patch.object(
            discovery, "_resolve_user_chat_id", AsyncMock(return_value=chat_id)
        ), patch.object(
            discovery, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "fsm.storage.get_fsm_storage", return_value=fake_storage
        ):
            ok = await discovery.send_welcome_dm_to_moderator(fake_bot, huid)

        assert ok is True
        fake_storage.set_state.assert_awaited_once_with(huid, "moderator:menu")

        fake_bot.send_message.assert_awaited_once()
        call_kwargs = fake_bot.send_message.await_args.kwargs
        assert call_kwargs["chat_id"] == chat_id
        assert call_kwargs["wait_callback"] is False

        commands = _extract_button_commands(call_kwargs["bubbles"])
        assert "/queue" in commands
        assert "/m_help" in commands
        assert "/start" in commands  # «Назад в главное меню»

    async def test_state_set_even_without_chat_id(
        self, fake_bot: MagicMock, fake_storage: MagicMock
    ) -> None:
        """Юзер не писал ``/start`` (нет chat_id) — DM нет, но state есть."""
        huid = uuid.uuid4()

        with patch.object(
            discovery, "_resolve_user_chat_id", AsyncMock(return_value=None)
        ), patch.object(
            discovery, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "fsm.storage.get_fsm_storage", return_value=fake_storage
        ):
            ok = await discovery.send_welcome_dm_to_moderator(fake_bot, huid)

        assert ok is False
        fake_storage.set_state.assert_awaited_once_with(huid, "moderator:menu")
        fake_bot.send_message.assert_not_awaited()


class TestJuryWelcomeDM:
    """``send_welcome_dm_to_jury``: меню жюри + FSM-state."""

    async def test_dm_carries_jury_menu_and_sets_state(
        self, fake_bot: MagicMock, fake_storage: MagicMock
    ) -> None:
        huid = uuid.uuid4()
        chat_id = uuid.uuid4()

        with patch.object(
            discovery, "_resolve_user_chat_id", AsyncMock(return_value=chat_id)
        ), patch.object(
            discovery, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "fsm.storage.get_fsm_storage", return_value=fake_storage
        ):
            ok = await discovery.send_welcome_dm_to_jury(fake_bot, huid)

        assert ok is True
        fake_storage.set_state.assert_awaited_once_with(huid, "jury:menu")

        fake_bot.send_message.assert_awaited_once()
        call_kwargs = fake_bot.send_message.await_args.kwargs
        assert call_kwargs["chat_id"] == chat_id

        commands = _extract_button_commands(call_kwargs["bubbles"])
        assert "/jury_tasks" in commands
        assert "/jury_status" in commands
        assert "/start" in commands

    async def test_send_failure_logs_and_returns_false(
        self, fake_bot: MagicMock, fake_storage: MagicMock
    ) -> None:
        """Если ``bot.send_message`` упал — функция возвращает False."""
        huid = uuid.uuid4()
        chat_id = uuid.uuid4()
        fake_bot.send_message = AsyncMock(side_effect=RuntimeError("network"))

        with patch.object(
            discovery, "_resolve_user_chat_id", AsyncMock(return_value=chat_id)
        ), patch.object(
            discovery, "resolve_bot_id", return_value=uuid.uuid4()
        ), patch(
            "fsm.storage.get_fsm_storage", return_value=fake_storage
        ):
            ok = await discovery.send_welcome_dm_to_jury(fake_bot, huid)

        assert ok is False
        fake_storage.set_state.assert_awaited_once_with(huid, "jury:menu")
