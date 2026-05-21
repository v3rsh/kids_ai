"""Универсальные функции валидации и санитизации пользовательского ввода."""
import html
import re
from typing import Tuple


# Разрешены: буквы (включая кириллицу), цифры, "_", "@", ",", "/", ".", пробелы
ALLOWED_CHARS_PATTERN = re.compile(r"^[\w\s@,/.]+$", re.UNICODE)


def validate_input(text: str) -> Tuple[bool, str]:
    """Проверяет текст на наличие допустимых символов.

    Args:
        text: Текст для проверки.

    Returns:
        (валиден, сообщение об ошибке).
    """
    if not text:
        return True, ""

    if ALLOWED_CHARS_PATTERN.match(text):
        return True, ""

    return (
        False,
        "Текст содержит недопустимые символы. "
        "Разрешены только буквы, цифры, пробелы и символы '_', '@', ',', '/', '.'",
    )


def sanitize_input(text: str) -> str:
    """Удаляет недопустимые символы из текста."""
    if not text:
        return ""

    return re.sub(r"[^\w\s@,/.]+", "", text, flags=re.UNICODE)


def escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы (`html.escape` со стандартным quote=True)."""
    if not text:
        return ""
    return html.escape(text, quote=True)
