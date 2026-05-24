"""
Утилиты для построения eXpress-deeplink на DM с ботом.

Точный синтаксис deeplink зависит от версии CTS-клиента eXpress.
Шаблон вынесен в env (``EXPRESS_DEEPLINK_TEMPLATE``) — если переменная
не задана, ``build_bot_deeplink`` возвращает None и кнопка-ссылка
в outbound-уведомлениях просто не добавляется (graceful degradation,
текстовая команда в теле сообщения остаётся).

Поддерживаемые плейсхолдеры в шаблоне:
- ``{bot_id}`` — UUID бота;
- ``{cts_url}`` — базовый URL CTS-сервера (без trailing slash).
"""
from __future__ import annotations

from uuid import UUID

from loguru import logger

try:
    from config import CTS_URL, EXPRESS_DEEPLINK_TEMPLATE
except ImportError:  # pragma: no cover - safety net
    CTS_URL = ""
    EXPRESS_DEEPLINK_TEMPLATE = ""


def build_bot_deeplink(bot_id: UUID | str | None) -> str | None:
    """Сформировать deeplink на DM с ботом.

    Возвращает None, если:
    - шаблон не задан (``EXPRESS_DEEPLINK_TEMPLATE`` пуст);
    - ``bot_id`` пуст;
    - в шаблоне отсутствует обязательный плейсхолдер.

    В случае любой ошибки рендеринга логируем WARNING и возвращаем None
    — выше по стеку этот None трактуется как «не добавляем кнопку».
    """
    if not EXPRESS_DEEPLINK_TEMPLATE:
        return None
    if bot_id is None:
        return None

    cts_url = (CTS_URL or "").rstrip("/")
    try:
        return EXPRESS_DEEPLINK_TEMPLATE.format(
            bot_id=str(bot_id),
            cts_url=cts_url,
        )
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "Не удалось отрендерить EXPRESS_DEEPLINK_TEMPLATE",
            template=EXPRESS_DEEPLINK_TEMPLATE,
            error=str(exc),
        )
        return None


__all__ = ["build_bot_deeplink"]
