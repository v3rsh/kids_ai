"""
Стаб сервиса автосообщений (Wave 1 → ветка D / notifications).

Все тексты по §18.1–§18.6 (участнику) и §19 (в чат модерации) вынесены
в module-level константы и параметризуются через ``.format(**ctx)``.
Заказчик может переопределить тексты через конфиг без правки кода
(§18.6 примечание).

Принципиально: сами тексты — ниже как ``*_TEMPLATE``, чтобы Wave 2 /
ветка D могла подцепить их к функциям, а Wave 3 — заменить на
финальные строки от заказчика без диффа в логике.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from pybotx import Bot

    from database.models import Application


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка D (notifications)"


# =====================================================================
# Текстовые шаблоны (§18, §19, §28.1)
# =====================================================================

ACCEPTED_TEMPLATE = (
    "Спасибо! Заявка принята и передана на модерацию.\n"
    "Если нам понадобится уточнение или более качественное изображение, "
    "мы свяжемся с вами по указанному контакту."
)

REJECTED_TEMPLATE = (
    "Работа не прошла модерацию, потому что не соответствует условиям "
    "конкурса: {reason}.\nСпасибо за интерес к проекту."
)

FIX_NEEDED_TEMPLATE = (
    "Работа прошла предварительную проверку, но нам нужен файл лучшего "
    "качества / дополнительный ракурс / корректный формат.\n"
    "Пожалуйста, отправьте исправленные материалы до 21 июня "
    "(последний день приёма заявок)."
)
FIX_NEEDED_EXTRA_TEMPLATE = "\n\nУточнение модератора: {extra}"

SHORTLIST_TEMPLATE = (
    "Поздравляем! Работа прошла в шорт-лист конкурса.\n"
    "Она может быть опубликована в подборке для голосования за "
    "приз зрительских симпатий."
)

JURY_RESULT_IN_TOP10_TEMPLATE = (
    "Работа вашего ребёнка вошла в шорт-лист конкурса "
    "«Безопасные рисунки» (топ-10 в своей категории). Итоги — 30 июня."
)
JURY_RESULT_NOT_IN_TOP10_TEMPLATE = (
    "Спасибо за участие в конкурсе «Безопасные рисунки»! По итогам "
    "работы жюри ваша работа не вошла в шорт-лист. Это не оценка "
    "таланта — выбор делался по конкретным критериям конкурса. "
    "Рады, что вы участвовали."
)

NEW_APPLICATION_MODERATION_TEMPLATE = (
    "Новая заявка на конкурс «Безопасные рисунки».\n\n"
    "ID: {br_id}\n"
    "Родитель: {parent_full_name}\n"
    "Ребёнок: {child_name}, {child_age}\n"
    "Возрастная категория: {age_category}\n"
    "Трек: {track}\n"
    "Название работы: {title}\n"
    "Ссылка на папку: {folder_link}"
)

DISK_ALERT_80_TEMPLATE = (
    "⚠️ Хранилище конкурса заполнено на 80 %. Свободно: {free_mb} МБ. "
    "При текущей скорости поступления заявок место закончится через "
    "{hours_left} ч. Рекомендуется: ужесточить отбор отклонения, "
    "рассмотреть переключение на резервный сценарий приёма по ссылкам "
    "(раздел 33.6)."
)


# =====================================================================
# Сигнатуры функций
# =====================================================================


async def notify_participant_accepted(bot: "Bot", app: "Application") -> None:
    """§18.1 — заявка принята и передана на модерацию."""
    raise NotImplementedError(_STUB_MSG)


async def notify_participant_rejected(
    bot: "Bot", app: "Application", reason: str
) -> None:
    """§18.3 — работа не прошла модерацию."""
    raise NotImplementedError(_STUB_MSG)


async def notify_participant_fix_needed(
    bot: "Bot", app: "Application", extra: str | None = None
) -> None:
    """§18.4 — требуется исправление; ``extra`` добавляется отдельным абзацем."""
    raise NotImplementedError(_STUB_MSG)


async def notify_participant_shortlist(bot: "Bot", app: "Application") -> None:
    """§18.5 — работа попала в шорт-лист."""
    raise NotImplementedError(_STUB_MSG)


async def notify_participant_jury_result(
    bot: "Bot", app: "Application", in_top_10: bool
) -> None:
    """§18.6 — итоговое сообщение по жюри (вошла / не вошла в топ-10)."""
    raise NotImplementedError(_STUB_MSG)


async def notify_moderation_chat_new_application(
    bot: "Bot", app: "Application"
) -> None:
    """§19 — служебное сообщение о новой заявке в чат модерации."""
    raise NotImplementedError(_STUB_MSG)


async def notify_moderation_chat_jury_event(
    bot: "Bot",
    *,
    event_kind: str,
    pools: list[tuple[str, str]],
    round_no: int | None,
    deadline_text: str | None = None,
    extra: str | None = None,
) -> None:
    """§19 — события жюри (открытие/закрытие раунда, жребий, шорт-лист).

    Уведомления об открытии/закрытии раундов **агрегируются по моменту
    времени**: одно сообщение со списком пулов. Жребий и шорт-лист —
    индивидуально, не агрегируются (Wave 0).
    """
    raise NotImplementedError(_STUB_MSG)


async def notify_moderation_chat_disk_alert(
    bot: "Bot",
    *,
    threshold_pct: int,
    free_mb: int,
    hours_left: float,
) -> None:
    """§28.1 — автопредупреждение о заполнении диска (80 % / 95 %)."""
    raise NotImplementedError(_STUB_MSG)


__all__ = [
    "ACCEPTED_TEMPLATE",
    "REJECTED_TEMPLATE",
    "FIX_NEEDED_TEMPLATE",
    "FIX_NEEDED_EXTRA_TEMPLATE",
    "SHORTLIST_TEMPLATE",
    "JURY_RESULT_IN_TOP10_TEMPLATE",
    "JURY_RESULT_NOT_IN_TOP10_TEMPLATE",
    "NEW_APPLICATION_MODERATION_TEMPLATE",
    "DISK_ALERT_80_TEMPLATE",
    "notify_participant_accepted",
    "notify_participant_rejected",
    "notify_participant_fix_needed",
    "notify_participant_shortlist",
    "notify_participant_jury_result",
    "notify_moderation_chat_new_application",
    "notify_moderation_chat_jury_event",
    "notify_moderation_chat_disk_alert",
]
