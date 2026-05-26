"""
Форматирование экранов «Мои заявки» для участника.

Содержит только presentation-логику: краткие статусы, карточки
заявок и ленту прогресса по этапам конкурса. Не показывает файлы,
служебные поля жюри и внутренние комментарии модератора.
"""
from __future__ import annotations

from datetime import datetime

from database.models import (
    Application,
    JuryStatus,
    ModerationStatus,
    VotingStatus,
)
from services.notifications import (
    FIX_NEEDED_EXTRA_TEMPLATE,
    FIX_NEEDED_TEMPLATE,
    JURY_RESULT_NOT_IN_TOP10_TEMPLATE,
)


LIST_TITLE_MAX_LEN = 60


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def _truncate(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def short_status_label(app: Application) -> str:
    """Краткий статус для строки списка «Мои заявки»."""
    mod = app.moderation_status
    jury = app.jury_status

    if mod == ModerationStatus.NA_MODERATSII:
        return "На модерации"
    if mod == ModerationStatus.NUZHNO_ISPRAVIT:
        return "Нужно исправить"
    if mod == ModerationStatus.OTKLONENO:
        return "Отклонена"
    if mod == ModerationStatus.PRINYATO:
        return "Принята"

    if jury == JuryStatus.V_TOP_10:
        return "В шорт-листе"
    if jury == JuryStatus.NE_VOSHLO_V_TOP_10:
        return "Не в шорт-листе"
    if jury == JuryStatus.NA_GOLOSOVANII:
        return "На голосовании жюри"
    if mod == ModerationStatus.DOPUSHCHENO:
        return "Допущена"

    return mod.value


def format_list_item(app: Application) -> str:
    """Одна строка списка заявок."""
    title = _truncate(app.title, LIST_TITLE_MAX_LEN)
    return (
        f"• **{app.br_id}** · «{title}»\n"
        f"  {short_status_label(app)}"
    )


def _format_common_fields(app: Application) -> str:
    return (
        f"📄 **{app.br_id}**\n\n"
        f"**Подана:** {_format_dt(app.created_at)}\n\n"
        f"**Ребёнок:** {app.child_name}, {app.child_age} "
        f"({app.age_category.value})\n\n"
        f"**Трек:** {app.track.value}\n"
        f"**Название:** {app.title}\n"
        f"**Описание:** {app.description}"
    )


def _duplicate_hint(app: Application) -> str:
    if not app.is_possible_duplicate:
        return ""
    return (
        "\n\nℹ️ По этому ребёнку и треку у вас есть ещё одна заявка."
    )


def _status_section_pending() -> str:
    return (
        "\n\n**Статус:** заявка на проверке модератором.\n\n"
        "Мы сообщим вам, когда проверка завершится."
    )


def _status_section_fix(*, fix_extra: str | None) -> str:
    lines = [
        "",
        "**Статус:** нужно исправить материалы.",
        "",
        FIX_NEEDED_TEMPLATE,
    ]
    if fix_extra:
        lines.append(FIX_NEEDED_EXTRA_TEMPLATE.format(extra=fix_extra))
    lines.append(
        "\n\nЧтобы отправить исправленную работу, нажмите "
        "«Подать исправленную работу» — будет создана новая заявка "
        "с новым номером."
    )
    return "\n".join(lines)


def _status_section_rejected(*, rejection_reason: str | None) -> str:
    reason = (rejection_reason or "").strip()
    if reason:
        reason_block = f"**Причина:** {reason}"
    else:
        reason_block = (
            "Если нужны подробности, напишите организаторам — "
            "контакты в главном меню."
        )
    return (
        "\n\n**Статус:** работа не прошла модерацию.\n\n"
        f"{reason_block}\n\n"
        "Спасибо за интерес к конкурсу."
    )


def _step_marker(*, done: bool, current: bool) -> str:
    if done:
        return "✓"
    if current:
        return "→"
    return "○"


def build_progress_timeline(app: Application) -> str:
    """Лента прогресса для заявок, прошедших модерацию."""
    mod = app.moderation_status
    jury = app.jury_status
    voting = app.voting_status

    submitted_done = True
    moderation_done = mod not in {
        ModerationStatus.NA_MODERATSII,
        ModerationStatus.NUZHNO_ISPRAVIT,
    }
    admitted_done = mod == ModerationStatus.DOPUSHCHENO or (
        moderation_done and mod != ModerationStatus.OTKLONENO
    )
    jury_voting_current = jury == JuryStatus.NA_GOLOSOVANII
    jury_voting_done = jury in {
        JuryStatus.V_TOP_10,
        JuryStatus.NE_VOSHLO_V_TOP_10,
    }
    in_shortlist = jury == JuryStatus.V_TOP_10
    out_shortlist = jury == JuryStatus.NE_VOSHLO_V_TOP_10

    lines = ["\n\n**Ход конкурса:**", ""]

    steps: list[tuple[str, bool, bool]] = [
        ("Заявка принята", submitted_done, False),
        (
            "Проверка модератором",
            moderation_done,
            not moderation_done and mod == ModerationStatus.NA_MODERATSII,
        ),
        (
            "Допущена к оценке жюри",
            admitted_done and mod == ModerationStatus.DOPUSHCHENO,
            mod == ModerationStatus.DOPUSHCHENO
            and jury == JuryStatus.NE_PEREDANO_ZHYURI,
        ),
        (
            "Голосование жюри",
            jury_voting_done,
            jury_voting_current,
        ),
    ]

    for label, done, current in steps:
        marker = _step_marker(done=done, current=current)
        lines.append(f"{marker} {label}")

    if in_shortlist:
        lines.append(f"✓ В шорт-листе (топ-10 в категории)")
        lines.append("")
        lines.append("Итоги конкурса — **30 июня**.")
    elif out_shortlist:
        lines.append("✓ Итог жюри")
        lines.append("")
        lines.append(_truncate(JURY_RESULT_NOT_IN_TOP10_TEMPLATE, 280))

    if in_shortlist and voting != VotingStatus.NE_UCHASTVUET:
        lines.append("")
        lines.append(f"**Публикация:** {voting.value}")

    return "\n".join(lines)


def _status_section_admitted(app: Application) -> str:
    return build_progress_timeline(app)


async def resolve_rejection_reason(app: Application) -> str | None:
    """Причина отклонения: БД → fallback ``reason.txt``."""
    if app.moderation_status != ModerationStatus.OTKLONENO:
        return None
    if app.moderator_comment and app.moderator_comment.strip():
        return app.moderator_comment.strip()
    from services import storage

    return await storage.read_rejection_reason(app)


def resolve_fix_extra(app: Application) -> str | None:
    """Уточнение модератора для статуса «нужно исправить»."""
    if app.moderation_status != ModerationStatus.NUZHNO_ISPRAVIT:
        return None
    if app.moderator_comment and app.moderator_comment.strip():
        return app.moderator_comment.strip()
    return None


async def format_application_detail(app: Application) -> str:
    """Полный текст карточки заявки для участника."""
    body = _format_common_fields(app)
    body += _duplicate_hint(app)

    mod = app.moderation_status
    if mod == ModerationStatus.NA_MODERATSII:
        body += _status_section_pending()
    elif mod == ModerationStatus.NUZHNO_ISPRAVIT:
        body += _status_section_fix(fix_extra=resolve_fix_extra(app))
    elif mod == ModerationStatus.OTKLONENO:
        reason = await resolve_rejection_reason(app)
        body += _status_section_rejected(rejection_reason=reason)
    elif mod in {ModerationStatus.DOPUSHCHENO, ModerationStatus.PRINYATO}:
        body += _status_section_admitted(app)
    else:
        body += f"\n\n**Статус:** {mod.value}"

    return body


__all__ = [
    "format_application_detail",
    "format_list_item",
    "build_progress_timeline",
    "resolve_fix_extra",
    "resolve_rejection_reason",
    "short_status_label",
]
