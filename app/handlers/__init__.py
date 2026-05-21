"""
Регистрация коллекторов хендлеров.

Каждый модуль-хендлер создаёт свой `collector` и регистрируется в
`get_all_collectors()`. Порядок важен:

1. `common_collector` обязательно первый — в нём живёт
   `default_message_handler` (диспетчер по FSM-состоянию). Правило
   pybotx: один `default_message_handler` на приложение, второй
   нельзя зарегистрировать.
2. Ветки Wave 2 добавляются в порядке `user → moderator → jury → admin`.
   Эта последовательность зафиксирована планом и нужна, чтобы при
   совпадении имён команд (на случай ошибки) сначала срабатывали
   пользовательские, а потом служебные.

См. .cursor/rules/bot.mdc → «Handler Types» и docs/architecture.md
→ «Диспетчер default_message_handler».
"""
from .common import collector as common_collector

# TODO Wave 2 — добавить в порядке user → moderator → jury → admin:
# from .user import collector as user_collector
# from .moderator import collector as moderator_collector
# from .jury import collector as jury_collector
# from .admin import collector as admin_collector


def get_all_collectors() -> list:
    """Возвращает список коллекторов в порядке регистрации.

    `common_collector` ВСЕГДА первый — в нём диспетчер свободного
    текста (`default_message_handler`). Ветки Wave 2 вставляются
    ПОСЛЕ него.
    """
    return [
        common_collector,
        # TODO Wave 2: user_collector,
        # TODO Wave 2: moderator_collector,
        # TODO Wave 2: jury_collector,
        # TODO Wave 2: admin_collector,
    ]


__all__ = ["get_all_collectors"]
