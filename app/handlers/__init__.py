"""
Регистрация коллекторов хендлеров.

Каждый модуль-хендлер создаёт свой ``collector`` и регистрируется в
``get_all_collectors()``. Порядок важен:

1. ``common_collector`` обязательно первый — в нём живёт
   ``default_message_handler`` (диспетчер по FSM-состоянию). Правило
   pybotx: один ``default_message_handler`` на приложение, второй
   нельзя зарегистрировать.
2. Дальше — ветки в порядке `user → moderator → jury → admin`.
   Эта последовательность зафиксирована планом и нужна, чтобы при
   совпадении имён команд (на случай ошибки) сначала срабатывали
   пользовательские, а потом служебные.

Side-effect импортов:

- модули ``handlers.user_*`` / ``handlers.moderator_*`` / ``handlers.jury_*``
  при импорте регистрируют FSM-state-handler'ы через
  ``handlers.common.register_state_handler``.
- импорт коллекторов здесь = «развешать» обработчики команд на pybotx
  и параллельно подвязать соответствующие state-handler'ы к диспетчеру
  свободного текста из ``handlers.common``.

См. `.cursor/rules/bot.mdc` → «Handler Types» и
`docs/architecture.md` → «Диспетчер default_message_handler».
"""
from .common import collector as common_collector

# Ветка участника: главное меню, анкета, файлы, согласия.
from .user import collector as user_collector
from .user_intake import collector as user_intake_collector
from .user_files import collector as user_files_collector
from .user_confirm import collector as user_confirm_collector

# Ветка модератора: меню, очередь, действия, экспорт, jury-admin.
from .moderator import collector as moderator_collector
from .moderator_queue import collector as moderator_queue_collector
from .moderator_actions import collector as moderator_actions_collector
from .moderator_export import collector as moderator_export_collector
from .moderator_jury_admin import collector as moderator_jury_admin_collector

# Ветка жюри: главное меню, задачи, статус прогресса.
from .jury import collector as jury_collector
from .jury_tasks import collector as jury_tasks_collector
from .jury_status import collector as jury_status_collector

# Технические админ-команды: /disk, /intake_mode, /admin_state.
from .admin import collector as admin_collector

# Админ-роли: discovery-кнопки, выдача/отзыв ролей, чат модерации.
from .admin_roles import collector as admin_roles_collector


def get_all_collectors() -> list:
    """Возвращает список коллекторов в порядке регистрации.

    ``common_collector`` ВСЕГДА первый — в нём диспетчер свободного
    текста (``default_message_handler``). Дальше — ветки бота:
    user → moderator → jury → admin.
    """
    return [
        common_collector,
        # Ветка A — родитель/участник
        user_collector,
        user_intake_collector,
        user_files_collector,
        user_confirm_collector,
        # Ветка B — модератор
        moderator_collector,
        moderator_queue_collector,
        moderator_actions_collector,
        moderator_export_collector,
        moderator_jury_admin_collector,
        # Ветка C — жюри
        jury_collector,
        jury_tasks_collector,
        jury_status_collector,
        # Ветка D — админ-команды
        admin_collector,
        admin_roles_collector,
    ]


__all__ = ["get_all_collectors"]
