"""Тесты для функций валидации и санитизации входных данных."""
import sys
import unittest
from pathlib import Path

# Добавляем app/ в sys.path, чтобы импорты работали как в основном коде
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from utils.validation import validate_input, sanitize_input, escape_html  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
