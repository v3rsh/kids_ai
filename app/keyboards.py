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

from database.models import Application, ModerationStatus, Track


# =====================================================================
# Главное меню родителя
# =====================================================================

# Команды вида /info_* / /menu_* — внутренние; они должны быть скрытыми
# (visible=False у соответствующих хендлеров), чтобы не засорять CTS-меню.
# Этот файл их только декларирует — регистрация в коллекторе делается
# в соответствующих модулях ``app/handlers/user*.py``.


def main_menu_bubbles(*, huid: UUID | str | None = None) -> BubbleMarkup:
    """Главное меню бота — 7 базовых кнопок + ролевые при наличии.

    Базовые кнопки (для всех):
    - /menu_about    → «О конкурсе»;
    - /menu_rules    → «Правила участия»;
    - /menu_examples → «Примеры работ и промптов»;
    - /apply         → «Подать работу» (точка входа в UserIntake);
    - /menu_my_applications → «Мои заявки»;
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
    bubbles.add_button(
        command="/menu_my_applications", label="Мои заявки", new_row=True
    )
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


def my_applications_list_bubbles(
    *,
    apps: list[Application],
    page,
    empty: bool,
) -> BubbleMarkup:
    """Кнопки списка «Мои заявки»."""
    bubbles = BubbleMarkup()
    if empty:
        bubbles.add_button(command="/apply", label="Подать работу", new_row=True)
        bubbles.add_button(
            command="/start", label="◀ Назад в главное меню", new_row=True
        )
        return bubbles

    for app in apps:
        title = app.title.strip()
        if len(title) > 28:
            title = title[:27].rstrip() + "…"
        bubbles.add_button(
            command=f"/my_app {app.br_id}",
            label=f"{app.br_id} · {title}",
            new_row=True,
        )

    has_prev = page.page > 1
    has_next = page.page < page.total_pages
    if has_prev:
        bubbles.add_button(
            command="/my_apps_page",
            label="← Назад",
            data={"to": str(page.page - 1)},
            new_row=True,
        )
    bubbles.add_button(
        command="/my_apps_refresh",
        label=f"{page.page} из {page.total_pages}",
        new_row=not has_prev,
    )
    if has_next:
        bubbles.add_button(
            command="/my_apps_page",
            label="Вперёд →",
            data={"to": str(page.page + 1)},
        )
    bubbles.add_button(
        command="/start", label="◀ Назад в главное меню", new_row=True
    )
    return bubbles


def my_application_detail_bubbles(app: Application) -> BubbleMarkup:
    """Кнопки карточки заявки — набор зависит от статуса модерации."""
    bubbles = BubbleMarkup()
    if app.moderation_status == ModerationStatus.NUZHNO_ISPRAVIT:
        bubbles.add_button(
            command="/apply",
            label="Подать исправленную работу",
            new_row=True,
        )
        bubbles.add_button(
            command="/menu_contacts",
            label="Контакты организаторов",
            new_row=True,
        )
    elif app.moderation_status == ModerationStatus.OTKLONENO:
        bubbles.add_button(
            command="/menu_contacts",
            label="Контакты организаторов",
            new_row=True,
        )
    bubbles.add_button(
        command="/my_apps_back",
        label="◀ К списку заявок",
        new_row=True,
    )
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


def back_to_moderator_menu_bubbles() -> BubbleMarkup:
    """Одна кнопка возврата в меню модератора."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/moderator", label="🛡 Меню модератора", new_row=True
    )
    return bubbles


def back_to_jury_menu_bubbles() -> BubbleMarkup:
    """Одна кнопка возврата в меню жюри."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/jury", label="⚖️ Меню жюри", new_row=True)
    return bubbles


def back_to_admin_menu_bubbles() -> BubbleMarkup:
    """Одна кнопка возврата в панель администратора."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/admin", label="◀ В админку", new_row=True)
    return bubbles


def fix_needed_notification_bubbles() -> BubbleMarkup:
    """Клавиатура DM «нужна правка»: контакты + главное меню."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/menu_contacts",
        label="📞 Контакты организаторов",
        new_row=True,
    )
    bubbles.add_button(
        command="/start", label="◀ Назад в главное меню", new_row=True
    )
    return bubbles


# =====================================================================
# Меню администратора
# =====================================================================


def admin_back_bubble(bubbles: BubbleMarkup) -> None:
    """Кнопка «◀ В админку» в конец раздела."""
    bubbles.add_button(command="/admin", label="◀ В админку", new_row=True)


def admin_confirm_bubbles(
    *,
    action: str,
    payload: dict[str, str] | None = None,
    confirm_command: str = "/admin_confirm",
) -> BubbleMarkup:
    """Двушаговое подтверждение опасной или деструктивной операции."""
    bubbles = BubbleMarkup()
    data_yes = {"action": action, "confirm": "yes"}
    data_no = {"action": action, "confirm": "no"}
    if payload:
        data_yes.update(payload)
        data_no.update(payload)
    bubbles.add_button(
        command=confirm_command,
        label="✅ Да, выполнить",
        data=data_yes,
        new_row=True,
    )
    bubbles.add_button(
        command=confirm_command,
        label="❌ Отмена",
        data=data_no,
        new_row=True,
    )
    return bubbles


def admin_main_menu_bubbles(
    *,
    moderators_count: int = 0,
    jury_count: int = 0,
    chat_configured: bool = False,
    intake_mode: str = "FILES",
    disk_pct: float = 0.0,
) -> BubbleMarkup:
    """Главное меню админки с бейджами на кнопках."""
    chat_label = "настроен" if chat_configured else "не настроен"
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/admin_section",
        label=f"👥 Роли (M: {moderators_count} · J: {jury_count})",
        data={"section": "roles"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_section",
        label=f"💬 Чат модерации ({chat_label})",
        data={"section": "chat"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_section",
        label=f"🖥 Система ({intake_mode} · диск {disk_pct:.0f}%)",
        data={"section": "system"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_section",
        label="🙋 Пользователи",
        data={"section": "users"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_section",
        label="📊 Статистика",
        data={"section": "stats"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_section",
        label="🛡 Меню модератора",
        data={"section": "moderator"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_section",
        label="⚠️ Опасные операции",
        data={"section": "dangerous"},
        new_row=True,
    )
    bubbles.add_button(command="/admin_help", label="❔ Справка", new_row=True)
    bubbles.add_button(command="/start", label="◀ В главное меню", new_row=True)
    return bubbles


def admin_roles_menu_bubbles() -> BubbleMarkup:
    """Раздел «Роли»."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/admin_roles",
        label="📋 Все роли (список)",
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_add",
        label="➕ Назначить по HUID",
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_resend_welcome",
        label="🔁 Повторить welcome-DM",
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_chat_menu_bubbles() -> BubbleMarkup:
    """Раздел «Чат модерации»."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/admin_chat_status",
        label="📍 Статус чата",
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_chat_test",
        label="📨 Тестовое сообщение",
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_chat_rediscover",
        label="🔁 Переоткрыть discovery",
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_danger",
        label="🗑 Сбросить чат…",
        data={"action": "clear_chat"},
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_system_menu_bubbles() -> BubbleMarkup:
    """Раздел «Система»."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/disk", label="📦 Диск", new_row=True)
    bubbles.add_button(command="/intake_mode", label="🔁 Режим приёма", new_row=True)
    bubbles.add_button(command="/admin_state", label="🩺 Диагностика", new_row=True)
    bubbles.add_button(
        command="/admin_disk_alerts",
        label="📜 История disk_alerts",
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_jury_flush",
        label="🚿 Сброс буфера жюри",
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_users_menu_bubbles() -> BubbleMarkup:
    """Раздел «Пользователи»."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/admin_user_find",
        label="🔎 Найти по HUID",
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_stats_menu_bubbles() -> BubbleMarkup:
    """Раздел «Статистика»."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/admin_stats", label="🔄 Обновить", new_row=True)
    bubbles.add_button(command="/stats today", label="📈 /stats today", new_row=True)
    bubbles.add_button(command="/stats all", label="📊 /stats all", new_row=True)
    admin_back_bubble(bubbles)
    return bubbles


def admin_moderator_shortcuts_bubbles() -> BubbleMarkup:
    """Шорткаты модераторского функционала."""
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/queue", label="📋 Очередь", new_row=True)
    bubbles.add_button(command="/browse", label="🖼 Карусель", new_row=True)
    bubbles.add_button(
        command="/admin_shortcut_find",
        label="🔍 Найти заявку",
        new_row=True,
    )
    bubbles.add_button(command="/export", label="📤 Реестр (XLSX)", new_row=True)
    bubbles.add_button(
        command="/export_shortlist",
        label="🏆 Шорт-лист (XLSX)",
        new_row=True,
    )
    bubbles.add_button(command="/jury_state", label="⚖️ Состояние жюри", new_row=True)
    admin_back_bubble(bubbles)
    return bubbles


def admin_dangerous_menu_bubbles() -> BubbleMarkup:
    """Раздел «Опасные операции»."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/admin_danger",
        label="🔁 Force switch → LINKS…",
        data={"action": "force_links"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_danger",
        label="🧹 Очистить disk_alerts (>30 д)…",
        data={"action": "cleanup_disk_alerts"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_danger",
        label="🗑 Сбросить moderation_chat…",
        data={"action": "clear_chat"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_danger",
        label="🚿 Flush jury aggregator…",
        data={"action": "flush_jury"},
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_user_card_bubbles(huid: str) -> BubbleMarkup:
    """Действия на карточке пользователя."""
    bubbles = BubbleMarkup()
    payload = {"huid": huid}
    bubbles.add_button(
        command="/admin_user_resync",
        label="🔄 Resync CTS",
        data=payload,
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_user_apps",
        label="📂 Заявки родителя",
        data=payload,
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_approve",
        label="➕ Назначить модератором",
        data={"role": "moderator", "huid": huid},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_approve",
        label="➕ Назначить жюри",
        data={"role": "jury", "huid": huid},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_revoke",
        label="🗑 Отозвать модератора",
        data={"role": "moderator", "huid": huid},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_revoke",
        label="🗑 Отозвать жюри",
        data={"role": "jury", "huid": huid},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_resend_welcome",
        label="🔁 Welcome-DM",
        data={"huid": huid},
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_add_role_role_bubbles() -> BubbleMarkup:
    """Выбор роли при назначении по HUID."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/admin_role_add",
        label="🛡 Модератор",
        data={"role": "moderator"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_add",
        label="⚖️ Жюри",
        data={"role": "jury"},
        new_row=True,
    )
    admin_back_bubble(bubbles)
    return bubbles


def admin_resend_welcome_role_bubbles(huid: str) -> BubbleMarkup:
    """Выбор роли для повторной welcome-DM."""
    bubbles = BubbleMarkup()
    payload = {"huid": huid}
    bubbles.add_button(
        command="/admin_role_resend_welcome",
        label="🛡 Модератор",
        data={**payload, "role": "moderator"},
        new_row=True,
    )
    bubbles.add_button(
        command="/admin_role_resend_welcome",
        label="⚖️ Жюри",
        data={**payload, "role": "jury"},
        new_row=True,
    )
    admin_back_bubble(bubbles)
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
    "my_applications_list_bubbles",
    "my_application_detail_bubbles",
    "moderator_menu_bubbles",
    "jury_menu_bubbles",
    "back_to_moderator_menu_bubbles",
    "back_to_jury_menu_bubbles",
    "back_to_admin_menu_bubbles",
    "fix_needed_notification_bubbles",
    "admin_main_menu_bubbles",
    "admin_confirm_bubbles",
    "admin_roles_menu_bubbles",
    "admin_chat_menu_bubbles",
    "admin_system_menu_bubbles",
    "admin_users_menu_bubbles",
    "admin_stats_menu_bubbles",
    "admin_moderator_shortcuts_bubbles",
    "admin_dangerous_menu_bubbles",
    "admin_user_card_bubbles",
    "admin_add_role_role_bubbles",
    "admin_resend_welcome_role_bubbles",
    "track_selection_bubbles",
    "consents_bubbles",
    "file_upload_bubbles",
    "final_confirm_bubbles",
]
