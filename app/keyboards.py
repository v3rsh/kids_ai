"""
Конструкторы клавиатур (BubbleMarkup) для kids_ai.

Здесь хранятся переиспользуемые наборы кнопок: главное меню,
подтверждения, навигация и т.п.

Соглашения (см. .cursor/rules/pybotx-bubbles.mdc):
- НЕ передавать `bubbles=None` — это вызовет 400 от CTS
- Значения в `data` — только строки (str(int_value), enum.value)
- Для удаления кнопок передавать пустой `BubbleMarkup()`
"""
from pybotx import BubbleMarkup

from database.models import Track


# =====================================================================
# Главное меню родителя (§6)
# =====================================================================

# Команды вида /info_* / /menu_* — внутренние; они должны быть скрытыми
# (visible=False у соответствующих хендлеров Wave 2 / ветка user), чтобы
# не засорять CTS-меню. Этот файл их только декларирует — регистрация
# в коллекторе делается в Wave 2.


def main_menu_bubbles() -> BubbleMarkup:
    """Главное меню бота — 6 кнопок по §6.

    Соответствие команд секциям ТЗ:
    - /menu_about    → §7.2 «О конкурсе»;
    - /menu_rules    → §7.4 «Правила участия»;
    - /menu_examples → §7.5 «Примеры работ и промптов»;
    - /apply         → §8 «Подать работу» (точка входа в UserIntake);
    - /menu_dates    → §7.3 «Сроки конкурса»;
    - /menu_contacts → контакты организаторов (текст из конфига).
    """
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/menu_about", label="О конкурсе")
    bubbles.add_button(command="/menu_rules", label="Правила участия")
    bubbles.add_button(
        command="/menu_examples", label="Примеры работ и промптов", new_row=True
    )
    bubbles.add_button(command="/apply", label="Подать работу", new_row=True)
    bubbles.add_button(command="/menu_dates", label="Сроки конкурса", new_row=True)
    bubbles.add_button(
        command="/menu_contacts", label="Контакты организаторов", new_row=True
    )
    return bubbles


# =====================================================================
# Анкета (Wave 2 / ветка user → ссылается на эти конструкторы)
# =====================================================================


def track_selection_bubbles() -> BubbleMarkup:
    """Три кнопки выбора трека (§10, §11.3).

    Передаём ``data["track"]`` = ``Track.<MEMBER>.name`` — UPPER_SNAKE,
    чтобы декодировалось через ``Track[data["track"]]`` без коллизий
    с кириллицей в ``.value``.
    """
    bubbles = BubbleMarkup()
    for track in Track:
        bubbles.add_button(
            command="/intake_track",
            label=track.value,
            data={"track": track.name},
            new_row=True,
        )
    return bubbles


def consents_bubbles(
    *,
    rules_checked: bool = False,
    publication_checked: bool = False,
) -> BubbleMarkup:
    """Чекбоксы согласий (§13) + кнопка подтверждения.

    Кнопки чекбоксов меняют состояние в FSM, перерисовываются после
    клика. Кнопка «Подтвердить» доступна только когда оба отмечены —
    Wave 2 / ветка user перерисовывает с актуальными флагами.
    """
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/intake_consent_toggle",
        label=("☑ " if rules_checked else "☐ ") + "Согласен с правилами конкурса",
        data={"key": "rules"},
        new_row=True,
    )
    bubbles.add_button(
        command="/intake_consent_toggle",
        label=("☑ " if publication_checked else "☐ ")
        + "Разрешаю публикацию имени, возраста и работы",
        data={"key": "publication"},
        new_row=True,
    )
    if rules_checked and publication_checked:
        bubbles.add_button(
            command="/intake_consents_confirm",
            label="Подтвердить и продолжить",
            new_row=True,
        )
    return bubbles


def file_upload_bubbles(*, can_add_more: bool, can_finish: bool) -> BubbleMarkup:
    """Кнопки шага загрузки файлов (§12.1).

    ``can_add_more`` = False, когда достигнут лимит 4 файлов для
    трека «Традиционное» 3D-варианта; ``can_finish`` = True начиная
    с первого успешно принятого файла.
    """
    bubbles = BubbleMarkup()
    if can_add_more:
        bubbles.add_button(
            command="/intake_file_more",
            label="Добавить ещё файл",
            new_row=True,
        )
    if can_finish:
        bubbles.add_button(
            command="/intake_file_done",
            label="Завершить загрузку",
            new_row=True,
        )
    return bubbles


def final_confirm_bubbles() -> BubbleMarkup:
    """Финальное подтверждение заявки (§14)."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/intake_submit", label="Отправить заявку")
    bubbles.add_button(
        command="/intake_restart", label="Заполнить заново", new_row=True
    )
    return bubbles


__all__ = [
    "main_menu_bubbles",
    "track_selection_bubbles",
    "consents_bubbles",
    "file_upload_bubbles",
    "final_confirm_bubbles",
]
