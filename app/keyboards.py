"""
Конструкторы клавиатур (BubbleMarkup) для kids_ai.

Здесь хранятся переиспользуемые наборы кнопок: главное меню,
подтверждения, навигация и т.п.

Соглашения (см. .cursor/rules/pybotx-bubbles.mdc):
- НЕ передавать `bubbles=None` — это вызовет 400 от CTS
- Значения в `data` — только строки (str(int_value), enum.value)
- Для удаления кнопок передавать пустой `BubbleMarkup()`
"""
from uuid import UUID

from pybotx import BubbleMarkup

from database.models import Track


# =====================================================================
# Главное меню родителя
# =====================================================================

# Команды вида /info_* / /menu_* — внутренние; они должны быть скрытыми
# (visible=False у соответствующих хендлеров), чтобы не засорять CTS-меню.
# Этот файл их только декларирует — регистрация в коллекторе делается
# в соответствующих модулях ``app/handlers/user*.py``.


def main_menu_bubbles(*, huid: UUID | str | None = None) -> BubbleMarkup:
    """Главное меню бота — 6 базовых кнопок + ролевые при наличии.

    Базовые кнопки (для всех):
    - /menu_about    → «О конкурсе»;
    - /menu_rules    → «Правила участия»;
    - /menu_examples → «Примеры работ и промптов»;
    - /apply         → «Подать работу» (точка входа в UserIntake);
    - /menu_dates    → «Сроки конкурса»;
    - /menu_contacts → контакты организаторов (текст из конфига).

    Дополнительно, если передан ``huid`` и пользователь в роли:
    - модератор → «🛡 Модерация» (/moderator);
    - жюри      → «⚖️ Жюри»      (/jury).

    Чтобы избежать кругового импорта (services.access → database.models →
    keyboards в некоторых сценариях прогрева кэша) импорт проверок ролей
    делается lazy внутри функции.
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

    if huid is not None:
        from services.access import is_jury, is_moderator

        if is_moderator(huid):
            bubbles.add_button(
                command="/moderator", label="🛡 Модерация", new_row=True
            )
        if is_jury(huid):
            bubbles.add_button(command="/jury", label="⚖️ Жюри", new_row=True)

    return bubbles


def back_to_main_menu_bubbles() -> BubbleMarkup:
    """Одна кнопка «◀ Назад в главное меню» — возврат на /start.

    Используется на инфо-экранах главного меню (О конкурсе, Правила,
    Примеры, Сроки, Контакты), чтобы не дублировать всё главное меню
    под текстом раздела — навигация явно показывает, что пользователь
    провалился в подраздел и может вернуться.
    """
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/start", label="◀ Назад в главное меню", new_row=True
    )
    return bubbles


# =====================================================================
# Меню роли (модератор / жюри)
# =====================================================================
#
# Конструкторы живут в этом модуле, а не в ``handlers/moderator.py`` /
# ``handlers/jury.py``, потому что меню роли строит ещё и
# ``services/discovery.py`` для welcome-DM сразу после одобрения роли
# админом. Импорт из handlers в services создал бы цикл
# (handlers → services.discovery → handlers).


def moderator_menu_bubbles() -> BubbleMarkup:
    """Кнопки главного меню модератора.

    Разделы по статусам разнесены по отдельным кнопкам:
    «Очередь» показывает только новые (на разборе), а «Принятые /
    На рассмотрении / Отклонённые» — карты соответствующих статусов
    с навигацией трек → возрастная категория → список.
    """
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/queue", label="📋 Очередь")
    bubbles.add_button(
        command="/m_accepted", label="✅ Принятые заявки", new_row=True
    )
    bubbles.add_button(
        command="/m_review", label="✏️ На рассмотрении", new_row=True
    )
    bubbles.add_button(
        command="/m_rejected", label="🚫 Отклонённые заявки", new_row=True
    )
    bubbles.add_button(
        command="/stats today", label="📈 Статистика — сегодня", new_row=True
    )
    bubbles.add_button(
        command="/stats all", label="📊 Статистика — весь период", new_row=True
    )
    bubbles.add_button(command="/export", label="📤 Реестр (XLSX)", new_row=True)
    bubbles.add_button(
        command="/export_shortlist", label="🏆 Шорт-лист (XLSX)", new_row=True
    )
    bubbles.add_button(
        command="/jury_state", label="⚖️ Состояние жюри", new_row=True
    )
    bubbles.add_button(
        command="/m_help", label="❔ Справка по командам", new_row=True
    )
    bubbles.add_button(
        command="/start", label="◀ Назад в главное меню", new_row=True
    )
    return bubbles


def jury_menu_bubbles() -> BubbleMarkup:
    """Кнопки главного меню жюри."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury_tasks", label="📋 Мои задачи", new_row=True)
    bubbles.add_button(command="/jury_status", label="📊 Прогресс", new_row=True)
    bubbles.add_button(
        command="/start", label="◀ Назад в главное меню", new_row=True
    )
    return bubbles


# =====================================================================
# Анкета (используется хендлерами в ``app/handlers/user*.py``)
# =====================================================================


def track_selection_bubbles() -> BubbleMarkup:
    """Три кнопки выбора трека.

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
    """Чекбоксы согласий + кнопка подтверждения.

    Кнопки чекбоксов меняют состояние в FSM, перерисовываются после
    клика. Кнопка «Подтвердить» доступна только когда оба отмечены —
    хендлер ветки user перерисовывает экран с актуальными флагами.
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
    """Кнопки шага загрузки файлов.

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
    """Финальное подтверждение заявки."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/intake_submit", label="Отправить заявку")
    bubbles.add_button(
        command="/intake_restart", label="Заполнить заново", new_row=True
    )
    return bubbles


__all__ = [
    "main_menu_bubbles",
    "back_to_main_menu_bubbles",
    "moderator_menu_bubbles",
    "jury_menu_bubbles",
    "track_selection_bubbles",
    "consents_bubbles",
    "file_upload_bubbles",
    "final_confirm_bubbles",
]
