"""
FSM-состояния kids_ai.

Здесь определяются классы состояний для веток сценариев.
Каждая ветка — отдельный класс на базе `str, Enum`.

Соглашение об именовании (см. .cursor/rules/bot.mdc):
- Класс: `UserReg`, `UserProfile`, `AdminCity` — одна ветка = один класс
- Атрибут: `{раздел}_{подраздел}_{состояние}` — `user_reg_name`
- Значение: `"{раздел}:{подраздел}:{состояние}"` — `"user:reg:name"`

При добавлении новой ветки обновляй `docs/architecture.md` → «FSM-система».
"""
from enum import Enum


class UserIntake(str, Enum):
    """Поэтапная подача заявки родителем.

    ФИО и подразделение участника подтягиваются автоматически из CTS
    (``services.users.ensure_user_profile_loaded``) при старте анкеты в
    ``handlers.user.cmd_apply``. Если CTS не дал какое-то поле — анкета
    включает один fallback-шаг под него (``*_fb``-состояния ниже).
    Стандартный «горячий» путь: contact → child_name → child_age →
    track → title → description → files_collect → consents → review.
    """

    # Контакт для связи: email или телефон, автоопределение по '@'.
    user_intake_parent_contact = "user:intake:parent_contact"
    user_intake_child_name = "user:intake:child_name"
    user_intake_child_age = "user:intake:child_age"
    user_intake_track = "user:intake:track"
    user_intake_title = "user:intake:title"
    user_intake_description = "user:intake:description"
    user_intake_files_collect = "user:intake:files_collect"
    user_intake_consents = "user:intake:consents"
    user_intake_review = "user:intake:review"

    # Fallback-шаги: включаются, только если соответствующее поле не
    # пришло из CTS. На горячем пути не используются.
    user_intake_parent_full_name_fb = "user:intake:parent_full_name_fb"
    user_intake_parent_division_fb = "user:intake:parent_division_fb"


class ModeratorAction(str, Enum):
    """Диалоговые подсказки в действиях модератора."""

    moderator_action_status_change = "moderator:action:status_change"
    moderator_action_comment_input = "moderator:action:comment_input"
    moderator_action_reject_reason = "moderator:action:reject_reason"
    moderator_action_fix_note = "moderator:action:fix_note"


class JuryTaskFlow(str, Enum):
    """Прохождение задачи жюри.

    Карусель с черновиками голосов идёт под одним состоянием
    ``jury_task_voting`` — судья навигирует кнопками внутри пула;
    перед `Отправить оценки` выставляется ``jury_task_confirm_submit``
    для финального подтверждения отправки.
    """

    jury_task_voting = "jury:task:voting"
    jury_task_confirm_submit = "jury:task:confirm_submit"
