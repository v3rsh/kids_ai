"""
Регистрация коллекторов хендлеров.

Каждый модуль-хендлер создаёт свой `collector` и регистрируется в
`get_all_collectors()`. Порядок важен: `default_message_handler`
(перехват свободного текста) должен быть в ПЕРВОМ коллекторе списка.
"""
from .common import collector as common_collector


def get_all_collectors() -> list:
    """Возвращает список коллекторов в порядке регистрации.

    При добавлении новых хендлеров (admin_*, user_*) импортируй их
    коллекторы здесь и добавляй в этот список.
    """
    return [
        common_collector,
    ]


__all__ = ["get_all_collectors"]
