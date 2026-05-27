"""
Похожие работы: сравнение заявок одного родителя в одном треке.

Команда ``/similar_apps`` — экран для модератора с кнопками перехода
к карточкам и файлам связанных заявок.
"""
from __future__ import annotations

from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage, MentionBuilder

from fsm import cleanup_middleware, fsm_middleware
from keyboards import back_to_moderator_menu_bubbles
from services import applications as applications_service
from services.access import moderator_only
from utils.bot_utils import reply_to_user
from utils.moderator_nav import (
    ModeratorNavOrigin,
    append_moderator_menu_button,
    find_button_data,
    similar_apps_find_data,
)

collector = HandlerCollector()


def _format_entry_line(entry: applications_service.ParentTrackEntry) -> str:
    actual = " · **актуальная**" if entry.is_actual_version else ""
    return (
        f"  • **{entry.br_id}** — {entry.child_name}, "
        f"{entry.child_age} — «{entry.title}» — "
        f"{entry.moderation_status}{actual}"
    )


def _format_similar_body(
    anchor: applications_service.Application,
    related: list[applications_service.ParentTrackEntry],
) -> str:
    parent_mention = MentionBuilder.contact(
        entity_id=anchor.parent_huid,
        name=anchor.parent_full_name,
    )
    strict = [e for e in related if e.is_strict_match]
    loose = [e for e in related if not e.is_strict_match]

    lines = [
        "🔍 **Похожие работы**",
        "",
        f"Просматриваете: **{anchor.br_id}**",
        f"Родитель: {parent_mention} · **Трек:** {anchor.track.value}",
        "",
    ]
    if strict:
        lines.append("🔴 **Тот же ребёнок** (имя + возраст):")
        lines.extend(_format_entry_line(e) for e in strict)
        lines.append("")
    if loose:
        lines.append("🟡 **Другие заявки родителя в этом треке:**")
        lines.extend(_format_entry_line(e) for e in loose)
    if not strict and not loose:
        lines.append("Связанных заявок не найдено.")
    return "\n".join(lines)


def _similar_apps_bubbles(
    *,
    anchor: applications_service.Application,
    related: list[applications_service.ParentTrackEntry],
    anchor_return: ModeratorNavOrigin | None,
) -> BubbleMarkup:
    bubbles = BubbleMarkup()
    src = anchor.br_id
    for entry in related:
        find_data = similar_apps_find_data(
            entry.br_id,
            anchor_src_br_id=src,
            anchor_return=anchor_return,
        )
        bubbles.add_button(
            command="/find",
            label=f"📄 {entry.br_id}",
            data=find_data,
            new_row=True,
        )
        bubbles.add_button(
            command="/files",
            label="📂 Файлы",
            data=find_data,
        )

    if anchor_return is not None and anchor_return.kind != "direct":
        bubbles.add_button(
            command="/find",
            label=f"◀ К карточке {src}",
            data=find_button_data(src, anchor_return),
            new_row=True,
        )
    else:
        bubbles.add_button(
            command="/find",
            label=f"◀ К карточке {src}",
            data={"br_id": src},
            new_row=True,
        )
    append_moderator_menu_button(bubbles)
    return bubbles


def _parse_anchor_return(data: dict[str, str]) -> ModeratorNavOrigin | None:
    from utils.moderator_nav import decode_anchor_return

    return decode_anchor_return(data)


async def _render_similar_apps(
    message: IncomingMessage,
    bot: Bot,
    *,
    src_br_id: str,
    anchor_return: ModeratorNavOrigin | None,
) -> None:
    anchor, related = await applications_service.find_related_for_moderator(
        src_br_id
    )
    if anchor is None:
        await reply_to_user(
            message,
            bot,
            "Заявка не найдена.",
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return

    if not related:
        await reply_to_user(
            message,
            bot,
            (
                f"🔍 **Похожие работы**\n\n"
                f"У заявки **{anchor.br_id}** нет других активных заявок "
                f"этого родителя в треке «{anchor.track.value}»."
            ),
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return

    body = _format_similar_body(anchor, related)
    await reply_to_user(
        message,
        bot,
        body,
        bubbles=_similar_apps_bubbles(
            anchor=anchor,
            related=related,
            anchor_return=anchor_return,
        ),
    )


@collector.command(
    "/similar_apps",
    description="Похожие работы (родитель + трек)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_similar_apps(message: IncomingMessage, bot: Bot) -> None:
    """Экран сравнения связанных заявок."""
    data = message.data or {}
    src = (data.get("src") or "").strip().upper()
    if not src:
        await reply_to_user(
            message,
            bot,
            "Не удалось определить заявку.",
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return
    anchor_return = _parse_anchor_return(data)
    await _render_similar_apps(
        message,
        bot,
        src_br_id=src,
        anchor_return=anchor_return,
    )


__all__ = ["collector"]
