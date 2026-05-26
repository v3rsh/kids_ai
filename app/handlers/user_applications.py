"""
Экран «Мои заявки» для участника.

Команды:
- ``/menu_my_applications`` — список заявок родителя;
- ``/my_apps_page`` — пагинация списка;
- ``/my_apps_refresh`` — обновить текущую страницу;
- ``/my_app BR-2026-XXXX`` — карточка одной заявки.
"""
from __future__ import annotations

from pybotx import Bot, HandlerCollector, IncomingMessage

from fsm import cleanup_middleware, fsm_middleware
from keyboards import (
    back_to_main_menu_bubbles,
    my_application_detail_bubbles,
    my_applications_list_bubbles,
)
from services import applications as applications_service
from services.user_application_views import format_application_detail, format_list_item
from utils.bot_utils import reply_to_user

collector = HandlerCollector()

FSM_KEY_MY_APPS_PAGE = "user:my_apps:page"
MY_APPS_PAGE_SIZE = 6


def _split_command_argument(message: IncomingMessage) -> str:
    raw = (message.body or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        parts = raw.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""
    return raw


def _normalize_br_id(token: str) -> str:
    return token.strip().upper() if token else ""


async def _save_page(message: IncomingMessage, page: int) -> None:
    await message.state.fsm.update_data(**{FSM_KEY_MY_APPS_PAGE: int(page)})


async def _load_page(message: IncomingMessage) -> int:
    data = await message.state.fsm.get_data()
    return max(1, int(data.get(FSM_KEY_MY_APPS_PAGE) or 1))


async def _render_list(
    message: IncomingMessage,
    bot: Bot,
    *,
    page: int,
) -> None:
    page_result = await applications_service.list_by_parent_huid(
        message.sender.huid,
        page=page,
        page_size=MY_APPS_PAGE_SIZE,
    )

    if not page_result.items:
        body = (
            "**Мои заявки**\n\n"
            "У вас пока нет заявок на конкурс.\n\n"
            "Нажмите «Подать работу», чтобы отправить работу ребёнка."
        )
        bubbles = my_applications_list_bubbles(
            apps=[],
            page=page_result,
            empty=True,
        )
    else:
        lines = [
            "**Мои заявки**",
            f"Всего: {page_result.total}",
            "",
        ]
        for app in page_result.items:
            lines.append(format_list_item(app))
        body = "\n\n".join(lines)
        bubbles = my_applications_list_bubbles(
            apps=page_result.items,
            page=page_result,
            empty=False,
        )

    await reply_to_user(message, bot, body, bubbles=bubbles)


@collector.command(
    "/menu_my_applications",
    description="Мои заявки на конкурс",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_my_applications(message: IncomingMessage, bot: Bot) -> None:
    """Список заявок участника."""
    await _save_page(message, 1)
    await _render_list(message, bot, page=1)


@collector.command(
    "/my_apps_page",
    description="Страница списка «Мои заявки»",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_my_apps_page(message: IncomingMessage, bot: Bot) -> None:
    """Перейти на страницу списка заявок."""
    raw = (message.data or {}).get("to") if message.data else None
    try:
        page_no = int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        page_no = 1
    page_no = max(page_no, 1)
    await _save_page(message, page_no)
    await _render_list(message, bot, page=page_no)


@collector.command(
    "/my_apps_refresh",
    description="Обновить список «Мои заявки»",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_my_apps_refresh(message: IncomingMessage, bot: Bot) -> None:
    """Перечитать список заявок из БД."""
    page_no = await _load_page(message)
    await _render_list(message, bot, page=page_no)


@collector.command(
    "/my_app",
    description="Карточка заявки участника",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_my_app(message: IncomingMessage, bot: Bot) -> None:
    """Карточка одной заявки с проверкой владельца."""
    arg = _split_command_argument(message)
    br_id = _normalize_br_id(arg.split(maxsplit=1)[0] if arg else "")
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Не удалось открыть заявку. Вернитесь в «Мои заявки».",
            bubbles=back_to_main_menu_bubbles(),
        )
        return

    app = await applications_service.get_for_participant(
        br_id,
        message.sender.huid,
    )
    if app is None:
        empty_page = applications_service.ParentApplicationsPage(
            items=[],
            total=0,
            page=1,
            page_size=MY_APPS_PAGE_SIZE,
        )
        await reply_to_user(
            message,
            bot,
            "Заявка не найдена.",
            bubbles=my_applications_list_bubbles(
                apps=[],
                page=empty_page,
                empty=True,
            ),
        )
        return

    body = await format_application_detail(app)
    await reply_to_user(
        message,
        bot,
        body,
        bubbles=my_application_detail_bubbles(app),
    )


@collector.command(
    "/my_apps_back",
    description="Назад к списку «Мои заявки»",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_my_apps_back(message: IncomingMessage, bot: Bot) -> None:
    """Вернуться к списку заявок с сохранённой страницей."""
    page_no = await _load_page(message)
    await _render_list(message, bot, page=page_no)
