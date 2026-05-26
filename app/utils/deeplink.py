"""
Утилиты для построения eXpress-deeplink на DM с ботом.

Точный синтаксис deeplink зависит от версии CTS-клиента eXpress.
Шаблон вынесен в env (``EXPRESS_DEEPLINK_TEMPLATE``) — если переменная
не задана, функции рендера возвращают None и кнопка-ссылка
в outbound-уведомлениях просто не добавляется (graceful degradation,
текстовая команда в теле сообщения остаётся).

Поддерживаемые плейсхолдеры в шаблоне:
- ``{bot_id}`` — UUID бота;
- ``{cts_url}`` — базовый URL CTS-сервера (без trailing slash);
- ``{br_id}`` — ID заявки (только ``build_find_deeplink``);
- ``{command}`` — команда ``/find BR-…`` (только ``build_find_deeplink``);
- ``{command_encoded}`` — URL-encoded ``{command}`` (только ``build_find_deeplink``).
"""
from __future__ import annotations

from urllib.parse import quote
from uuid import UUID

from loguru import logger

try:
    from config import CTS_URL, EXPRESS_DEEPLINK_TEMPLATE
except ImportError:  # pragma: no cover - safety net
    CTS_URL = ""
    EXPRESS_DEEPLINK_TEMPLATE = ""


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
    """Сформировать deeplink на DM с ботом.

    Возвращает None, если:
    - шаблон не задан (``EXPRESS_DEEPLINK_TEMPLATE`` пуст);
    - ``bot_id`` пуст;
    - в шаблоне отсутствует обязательный плейсхолдер.

    В случае любой ошибки рендеринга логируем WARNING и возвращаем None
    — выше по стеку этот None трактуется как «не добавляем кнопку».
    """
    if bot_id is None:
        return None

    cts_url = (CTS_URL or "").rstrip("/")
    return _render_deeplink_template(
        bot_id=str(bot_id),
        cts_url=cts_url,
        br_id="",
        command="",
        command_encoded="",
    )


def build_find_deeplink(
    bot_id: UUID | str | None,
    br_id: str,
) -> str | None:
    """Deeplink на DM с ботом и командой ``/find <br_id>`` в шаблоне.

    Использует те же плейсхолдеры, что ``build_bot_deeplink``, плюс
    ``{br_id}``, ``{command}``, ``{command_encoded}``. Если шаблон
    не использует командные плейсхолдеры — достаточно ``{bot_id}``.
    """
    if bot_id is None:
        return None

    needle = (br_id or "").strip().upper()
    if not needle:
        return None

    command = f"/find {needle}"
    cts_url = (CTS_URL or "").rstrip("/")
    return _render_deeplink_template(
        bot_id=str(bot_id),
        cts_url=cts_url,
        br_id=needle,
        command=command,
        command_encoded=quote(command, safe=""),
    )


__all__ = ["build_bot_deeplink", "build_find_deeplink"]
