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
    """Поэтапная подача заявки родителем (§8, §11–§14)."""

    user_intake_parent_full_name = "user:intake:parent_full_name"
    user_intake_parent_division = "user:intake:parent_division"
    user_intake_child_name = "user:intake:child_name"
    user_intake_child_age = "user:intake:child_age"
    user_intake_track = "user:intake:track"
    user_intake_title = "user:intake:title"
    user_intake_description = "user:intake:description"
    user_intake_files_collect = "user:intake:files_collect"
    user_intake_consents = "user:intake:consents"
    user_intake_review = "user:intake:review"


class ModeratorAction(str, Enum):
    """Диалоговые подсказки в действиях модератора (§27.1)."""

    moderator_action_status_change = "moderator:action:status_change"
    moderator_action_comment_input = "moderator:action:comment_input"
    moderator_action_reject_reason = "moderator:action:reject_reason"
    moderator_action_fix_note = "moderator:action:fix_note"


class JuryTaskFlow(str, Enum):
    """Прохождение задачи жюри (§35.3, §35.4).

    Карусель с черновиками голосов идёт под одним состоянием
    ``jury_task_voting`` — судья навигирует кнопками внутри пула;
    перед `Отправить оценки` выставляется ``jury_task_confirm_submit``
    для финального подтверждения отправки.
    """

    jury_task_voting = "jury:task:voting"
    jury_task_confirm_submit = "jury:task:confirm_submit"
