"""
Утилиты для построения eXpress-deeplink на DM с ботом.

Шаблон и параметры — только из env (``EXPRESS_DEEPLINK_TEMPLATE``,
``EXPRESS_ETS_ID``). Если шаблон пуст, кнопки-ссылки не добавляются.

Поддерживаемые плейсхолдеры в шаблоне:
- ``{bot_id}`` — UUID бота (обычно совпадает с ``BOT_ID``);
- ``{ets_id}`` — из ``EXPRESS_ETS_ID`` (Beeline link.buzz.beeline.ru);
- ``{cts_url}`` — ``CTS_URL`` без trailing slash;
- ``{br_id}``, ``{command}``, ``{command_encoded}`` — для ``build_find_deeplink``.
"""
from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from loguru import logger

try:
    from config import CTS_URL, EXPRESS_DEEPLINK_TEMPLATE, EXPRESS_ETS_ID
except ImportError:  # pragma: no cover - safety net
    CTS_URL = ""
    EXPRESS_DEEPLINK_TEMPLATE = ""
    EXPRESS_ETS_ID = ""


def _deeplink_placeholders(
    bot_id: UUID | str,
    *,
    br_id: str = "",
    command: str = "",
) -> dict[str, str]:
    """Словарь плейсхолдеров для ``EXPRESS_DEEPLINK_TEMPLATE``."""
    return {
        "bot_id": str(bot_id),
        "ets_id": (EXPRESS_ETS_ID or "").strip(),
        "cts_url": (CTS_URL or "").rstrip("/"),
        "br_id": br_id,
        "command": command,
        "command_encoded": quote(command, safe="") if command else "",
    }


def _render_deeplink_template(**kwargs: str) -> str | None:
    """Отрендерить ``EXPRESS_DEEPLINK_TEMPLATE`` или вернуть None при ошибке."""
    if not EXPRESS_DEEPLINK_TEMPLATE:
        return None
    try:
        return EXPRESS_DEEPLINK_TEMPLATE.format(**kwargs)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "Не удалось отрендерить EXPRESS_DEEPLINK_TEMPLATE",
            template=EXPRESS_DEEPLINK_TEMPLATE,
            error=str(exc),
        )
        return None


def build_bot_deeplink(bot_id: UUID | str | None) -> str | None:
    """Сформировать deeplink на профиль/DM с ботом."""
    if bot_id is None:
        return None
    return _render_deeplink_template(**_deeplink_placeholders(bot_id))


def build_find_deeplink(
    bot_id: UUID | str | None,
    br_id: str,
) -> str | None:
    """Deeplink на бота; в шаблон подставляются ``/find <br_id>`` при наличии плейсхолдеров.

    Для Beeline (``open/profile/{bot_id}?ets_id=…``) команда в URL обычно
    не передаётся — модератор открывает бота по ссылке, карточку берёт
    из текста уведомления или кнопки «📄 Карточка» в чате модерации.
    """
    if bot_id is None:
        return None

    needle = (br_id or "").strip().upper()
    if not needle:
        return None

    command = f"/find {needle}"
    return _render_deeplink_template(
        **_deeplink_placeholders(bot_id, br_id=needle, command=command)
    )


__all__ = ["build_bot_deeplink", "build_find_deeplink"]
