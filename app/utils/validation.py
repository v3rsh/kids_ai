"""Универсальные функции валидации и санитизации пользовательского ввода."""
import html
import re
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ContactValidationError:
    """Результат отказа валидации контакта (для будущей расширяемости сообщений об ошибках)."""

    message: str


def validate_and_normalize_contact(raw: str) -> Tuple[str, str] | None:
    """Валидация и нормализация контакта: email или телефон (RU по умолчанию).

    Использует email-validator (syntax-only, без DNS) и phonenumbers (libphonenumber).
    Ленивый импорт библиотек — не тормозит импорт модуля.

    Для телефонов:
    - default region RU (обрабатывает 8..., 9..., +7... с пробелами/скобками/дефисами)
    - результат в E.164, напр. +79991234567
    - защита от очевидных фейковых номеров (повторяющиеся цифры)

    Для email:
    - нормализация (lowercase, +tags сохраняются, IDN → punycode)
    - без проверки deliverability (чтобы не было I/O/таймаутов в хендлере)

    Returns:
        (normalized_value, "email" | "phone") или None при невалидном вводе.

    Examples:
        "user+tag@example.com" -> ("user+tag@example.com", "email")
        "8 (999) 123-45-67" -> ("+79991234567", "phone")
        "  +7 999 123 45 67 " -> ("+79991234567", "phone")
        "9991234567" -> ("+79991234567", "phone")
        "user@domain" -> None
        "0000000000" -> None
        "11111111111" -> None
    """
    text = (raw or "").strip()
    if len(text) < 4 or len(text) > 100:
        return None

    if "@" in text:
        try:
            from email_validator import EmailNotValidError, validate_email  # type: ignore[import-not-found]

            info = validate_email(text, check_deliverability=False)
            return info.normalized, "email"
        except EmailNotValidError:
            return None
        except Exception:
            return None

    # phone
    try:
        import phonenumbers  # type: ignore[import-not-found]
        from phonenumbers import NumberParseException, PhoneNumberFormat  # type: ignore[import-not-found]

        num = phonenumbers.parse(text, "RU")
        if not phonenumbers.is_valid_number(num):
            return None
        national = str(num.national_number)
        if len(national) >= 7 and len(set(national)) <= 2:
            return None
        normalized = phonenumbers.format_number(num, PhoneNumberFormat.E164)
        return normalized, "phone"
    except NumberParseException:
        return None
    except Exception:
        return None
