"""Тесты для функций валидации и санитизации входных данных."""
import sys
import unittest
from pathlib import Path

# Добавляем app/ в sys.path, чтобы импорты работали как в основном коде
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest

from utils.validation import (  # noqa: E402
    ContactValidationError,
    escape_html,
    sanitize_input,
    validate_and_normalize_contact,
    validate_input,
)


class TestValidation(unittest.TestCase):
    """Проверка функций валидации."""

    def test_validate_input_valid(self):
        valid_inputs = [
            "Иван Иванов",
            "user@example.com",
            "Москва, БЦ Метрополис",
            "менеджер, отдел продаж",
            "123456789",
            "user_name",
            "Петр Петров, @petr_petrov",
            "Разработчик/PHP",
        ]
        for input_text in valid_inputs:
            is_valid, _ = validate_input(input_text)
            self.assertTrue(is_valid, f"Текст {input_text!r} должен быть валидным")

    def test_validate_input_invalid(self):
        invalid_inputs = [
            "<script>alert('XSS')</script>",
            "Иван <b>Иванов</b>",
            "user; DROP TABLE users;",
            "Москва & Санкт-Петербург",
            "123+456=579",
            "Имя$Фамилия",
            "<img src='x' onerror='alert(1)'>",
            "Разработчик ')",
        ]
        for input_text in invalid_inputs:
            is_valid, _ = validate_input(input_text)
            self.assertFalse(is_valid, f"Текст {input_text!r} должен быть невалидным")

    def test_sanitize_input(self):
        test_cases = [
            ("<script>alert('XSS')</script>", "scriptalertXSS"),
            ("Иван <b>Иванов</b>", "Иван bИвановb"),
            ("user; DROP TABLE users;", "user DROP TABLE users"),
            ("Москва & Санкт-Петербург", "Москва  СанктПетербург"),
            ("123+456=579", "123456579"),
            ("Имя$Фамилия", "ИмяФамилия"),
            ("<img src='x' onerror='alert(1)'>", "img srcx onerrorsalert1"),
            ("Разработчик ')", "Разработчик "),
        ]
        for input_text, expected in test_cases:
            self.assertEqual(sanitize_input(input_text), expected)

    def test_escape_html(self):
        """`escape_html` использует `html.escape(quote=True)`."""
        cases = [
            ("<script>alert('XSS')</script>",
             "&lt;script&gt;alert(&#x27;XSS&#x27;)&lt;/script&gt;"),
            ("Иван <b>Иванов</b>", "Иван &lt;b&gt;Иванов&lt;/b&gt;"),
            ("A & B", "A &amp; B"),
            ('Пример "кавычек"', "Пример &quot;кавычек&quot;"),
        ]
        for input_text, expected in cases:
            self.assertEqual(escape_html(input_text), expected)


class TestContactValidation:
    """Проверка validate_and_normalize_contact (телефон RU + email syntax-only)."""

    @pytest.mark.parametrize(
        "raw,expected_type,expected_normalized",
        [
            # email
            ("user@example.com", "email", "user@example.com"),
            ("User+Tag@ExAmPlE.com", "email", "User+Tag@example.com"),
            ("  name@sub.domain.ru  ", "email", "name@sub.domain.ru"),
            # RU phones: 8, 7, +7, 9xx, с форматированием
            ("8 999 123-45-67", "phone", "+79991234567"),
            ("+7 (999) 123-45-67", "phone", "+79991234567"),
            ("  79991234567  ", "phone", "+79991234567"),
            ("9991234567", "phone", "+79991234567"),
            ("+380 50 123 45 67", "phone", "+380501234567"),  # UA example
            # international
            ("+44 20 8366 1177", "phone", "+442083661177"),
        ],
    )
    def test_valid_contacts_normalize_correctly(
        self, raw: str, expected_type: str, expected_normalized: str
    ):
        result = validate_and_normalize_contact(raw)
        assert result is not None
        normalized, ctype = result
        assert ctype == expected_type
        assert normalized == expected_normalized

    @pytest.mark.parametrize(
        "raw",
        [
            "",  # empty
            "   ",
            "a@b",  # no dot in domain
            "user@domain",  # invalid email
            "123",  # too short
            "0" * 20,  # long but fake
            "1111111111",  # fake phone (repeating)
            "0000000000",
            "8abc123",  # letters
            "<script>@evil.com",
            "tel:+7999",  # not pure phone
            "+7 000 000 00 00",  # fake all zero after norm
        ],
    )
    def test_invalid_contacts_return_none(self, raw: str):
        assert validate_and_normalize_contact(raw) is None

    def test_contact_validation_error_dataclass(self):
        err = ContactValidationError(message="bad")
        assert err.message == "bad"
        # frozen
        with pytest.raises(Exception):  # dataclass frozen raises on assign
            err.message = "x"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
