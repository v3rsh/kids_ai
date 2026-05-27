"""
Валидация публичной ссылки на облачную папку (резервный режим LINKS).

Согласно §33.6.4 ТЗ бот не имеет интернета и не может проверить
доступность URL — здесь только формальная sanity-проверка:
``http``/``https``, непустой хост, длина в разумных пределах,
без пробелов и переносов строк.

Содержимое по ссылке проверяет модератор вручную, когда открывает
карточку заявки.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


MIN_URL_LEN = 10
MAX_URL_LEN = 2000
_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class CloudLinkValidationError:
    """Результат отказа в валидации с человекочитаемым сообщением."""

    message: str


def parse_cloud_link(raw: str) -> tuple[str | None, CloudLinkValidationError | None]:
    """Распарсить и проверить ссылку на облачную папку.

    Из ``raw`` берётся **первый токен** (``split()[0]``) — на случай,
    если родитель отправил «вот ссылка: <url>» с лишним текстом. Если
    в полученном URL остаются пробелы/переводы строк — отбрасываем.

    Returns:
        ``(url, None)`` — валидный URL, готовый к сохранению;
        ``(None, error)`` — невалидный, ``error.message`` готов
        к показу участнику.
    """
    if raw is None:
        return None, CloudLinkValidationError(
            "Пришлите ссылку на облачную папку одним сообщением."
        )

    text = raw.strip()
    if not text:
        return None, CloudLinkValidationError(
            "Пришлите ссылку на облачную папку одним сообщением."
        )

    # Если в сообщении несколько токенов — берём первый. Это покрывает
    # частый кейс «Вот ссылка: https://…» без отказа в обработке.
    candidate = text.split()[0]

    if any(ch.isspace() for ch in candidate):
        return None, CloudLinkValidationError(
            "Ссылка не должна содержать пробелов. "
            "Скопируйте её из адресной строки браузера."
        )

    if len(candidate) < MIN_URL_LEN or len(candidate) > MAX_URL_LEN:
        return None, CloudLinkValidationError(
            f"Длина ссылки должна быть от {MIN_URL_LEN} до {MAX_URL_LEN} "
            "символов. Скопируйте полную ссылку на папку."
        )

    try:
        parsed = urlparse(candidate)
    except ValueError:
        parsed = None

    if parsed is None or parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None, CloudLinkValidationError(
            "Ссылка должна начинаться с http:// или https://. "
            "Скопируйте её из адресной строки браузера."
        )

    if not parsed.netloc:
        return None, CloudLinkValidationError(
            "Не распознали адрес сайта. Пришлите полную ссылку на "
            "облачную папку (например, https://disk.yandex.ru/d/…)."
        )

    return candidate, None


__all__ = [
    "MIN_URL_LEN",
    "MAX_URL_LEN",
    "CloudLinkValidationError",
    "parse_cloud_link",
]
