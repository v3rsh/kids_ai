"""
Повторные заявки: один ребёнок — несколько работ в одном треке.

Команда ``/multi_subs`` — сводный отчёт для модератора.
"""
from __future__ import annotations

from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from fsm import cleanup_middleware, fsm_middleware
from fsm.keys import FSM_KEY_MULTI_SUBS_PAGE
from keyboards import back_to_moderator_menu_bubbles
from services import applications as applications_service
from services.access import moderator_only
from utils.bot_utils import reply_to_user
from utils.moderator_nav import ModeratorNavOrigin, find_button_data

collector = HandlerCollector()

PAGE_SIZE = 5


def _format_group(group: applications_service.MultiSubmissionGroup) -> str:
    lines = [
        f"**{group.child_name}**, {group.child_age} лет · "
        f"{group.track.value}",
        f"Родитель: {group.parent_full_name}",
    ]
    for entry in group.entries:
        actual = " · **актуальная**" if entry.is_actual_version else ""
        lines.append(f"  • {entry.br_id} — {entry.moderation_status}{actual}")
    return "\n".join(lines)


def _multi_subs_bubbles(
    *,
    groups: list[applications_service.MultiSubmissionGroup],
    page: int,
    total_pages: int,
) -> BubbleMarkup:
    bubbles = BubbleMarkup()
    multi_origin = ModeratorNavOrigin(kind="multi_subs", multi_subs_page=page)
    start = (page - 1) * PAGE_SIZE
    page_groups = groups[start : start + PAGE_SIZE]
    for group in page_groups:
        primary_br = group.entries[0].br_id
        bubbles.add_button(
            command="/find",
            label=f"📄 {primary_br}",
            data=find_button_data(primary_br, multi_origin),
            new_row=True,
        )
        for entry in group.entries:
            if entry.br_id == primary_br:
                continue
            bubbles.add_button(
                command="/find",
                label=f"↳ {entry.br_id}",
                data=find_button_data(entry.br_id, multi_origin),
            )
        bubbles.add_button(
            command="/multi_subs_mark_actual",
            label="✓ Актуальная: " + primary_br,
            data={"br_id": primary_br},
            new_row=True,
        )
    if page > 1:
        bubbles.add_button(
            command="/multi_subs_page",
            label="← Назад",
            data={"page": str(page - 1)},
        )
    if page < total_pages:
        bubbles.add_button(
            command="/multi_subs_page",
            label="Вперёд →",
            data={"page": str(page + 1)},
            new_row=page <= 1,
        )
    bubbles.add_button(
        command="/moderator",
        label="◀ Меню модератора",
        new_row=True,
    )
    return bubbles


async def _render_multi_subs(
    message: IncomingMessage,
    bot: Bot,
    *,
    page: int,
) -> None:
    groups = await applications_service.find_multi_submission_groups(
        only_active=True
    )
    total = len(groups)
    if total == 0:
        await reply_to_user(
            message,
            bot,
            (
                "**Повторные заявки**\n\n"
                "Нарушений правила «1 работа в трек» не найдено."
            ),
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return

    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    await message.state.fsm.update_data(**{FSM_KEY_MULTI_SUBS_PAGE: page})

    start = (page - 1) * PAGE_SIZE
    chunk = groups[start : start + PAGE_SIZE]
    blocks = [_format_group(g) for g in chunk]
    body = (
        f"**Повторные заявки** ({total} групп)\n"
        f"Страница {page} из {total_pages}\n\n"
        + "\n\n".join(blocks)
    )
    await reply_to_user(
        message,
        bot,
        body,
        bubbles=_multi_subs_bubbles(
            groups=groups, page=page, total_pages=total_pages
        ),
    )


@collector.command(
    "/multi_subs",
    description="Повторные заявки (ребёнок + трек)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_multi_subs(message: IncomingMessage, bot: Bot) -> None:
    """Сводный список групп с более чем одной активной заявкой."""
    data = await message.state.fsm.get_data()
    page = max(1, int(data.get(FSM_KEY_MULTI_SUBS_PAGE) or 1))
    await _render_multi_subs(message, bot, page=page)


@collector.command(
    "/multi_subs_page",
    description="Страница списка повторных заявок",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_multi_subs_page(message: IncomingMessage, bot: Bot) -> None:
    """Пагинация ``/multi_subs``."""
    raw = (message.data or {}).get("page", "1")
    try:
        page = max(1, int(raw))
    except (TypeError, ValueError):
        page = 1
    await _render_multi_subs(message, bot, page=page)


@collector.command(
    "/multi_subs_mark_actual",
    description="Отметить заявку актуальной в группе",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_multi_subs_mark_actual(message: IncomingMessage, bot: Bot) -> None:
    """Пометить выбранную заявку актуальной, остальные в цепочке — нет."""
    br_id = ((message.data or {}).get("br_id") or "").strip().upper()
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Не удалось определить заявку.",
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return
    try:
        await applications_service.mark_as_actual_version(
            br_id=br_id,
            actual=True,
            by_moderator_huid=message.sender.huid,
        )
    except ValueError as exc:
        await reply_to_user(
            message,
            bot,
            str(exc),
            bubbles=back_to_moderator_menu_bubbles(),
        )
        return

    data = await message.state.fsm.get_data()
    page = max(1, int(data.get(FSM_KEY_MULTI_SUBS_PAGE) or 1))
    await reply_to_user(
        message,
        bot,
        f"Заявка **{br_id}** отмечена как актуальная версия.",
        bubbles=BubbleMarkup(),
    )
    await _render_multi_subs(message, bot, page=page)


__all__ = ["collector"]
