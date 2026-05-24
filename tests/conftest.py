"""Общий конфиг pytest для проекта kids_ai.

Действия при импорте:
- проставляем минимальный набор env-переменных, который читает
  ``app/config.py`` при импорте (BOT_ID / CTS_URL / BOT_SECRET_KEY);
- добавляем ``app/`` в ``sys.path`` — это совместимо со схемой
  размещения юнит-тестов (``tests/test_validation.py``,
  ``tests/test_jury_algorithm.py``);
- регистрируем asyncio-backend по умолчанию.

Полный список тестовых модулей — см. ``docs/testing.md``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("BOT_ID", "00000000-0000-0000-0000-000000000000")
os.environ.setdefault("CTS_URL", "http://localhost")
os.environ.setdefault("BOT_SECRET_KEY", "test-secret")
os.environ.setdefault("ATTACHMENTS_DIR", "/tmp/kids_ai_tests_attachments")

_APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

import pytest  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """Регистрируем кастомные маркеры."""
    config.addinivalue_line(
        "markers",
        "slow: помечает медленные тесты (рендер большого XLSX и т.п.).",
    )
