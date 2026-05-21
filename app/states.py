"""
FSM-состояния kids_ai.

Здесь определяются классы состояний для веток сценариев.
Каждая ветка — отдельный класс на базе `str, Enum`.

Соглашение об именовании (см. .cursor/rules/bot.mdc):
- Класс: `UserReg`, `UserProfile`, `AdminCity` — одна ветка = один класс
- Атрибут: `{раздел}_{подраздел}_{состояние}` — `user_reg_name`
- Значение: `"{раздел}:{подраздел}:{состояние}"` — `"user:reg:name"`

Пример:

    from enum import Enum

    class UserReg(str, Enum):
        '''Регистрация пользователя'''
        user_reg_privacy = "user:reg:privacy"
        user_reg_name = "user:reg:name"

При добавлении новой ветки обновляй `docs/architecture.md` → «FSM-система».
"""
