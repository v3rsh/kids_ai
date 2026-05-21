"""
Конструкторы клавиатур (BubbleMarkup) для kids_ai.

Здесь хранятся переиспользуемые наборы кнопок: главное меню,
подтверждения, навигация и т.п.

Соглашения (см. .cursor/rules/pybotx-bubbles.mdc):
- НЕ передавать `bubbles=None` — это вызовет 400 от CTS
- Значения в `data` — только строки (str(int_value), enum.value)
- Для удаления кнопок передавать пустой `BubbleMarkup()`

Пример:

    from pybotx import BubbleMarkup

    def main_menu_bubbles() -> BubbleMarkup:
        bubbles = BubbleMarkup()
        bubbles.add_button(command="/profile", label="Профиль")
        bubbles.add_button(command="/help", label="Помощь")
        return bubbles
"""
