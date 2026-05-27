"""
Исправление заявки без нового BR-ID (статус «нужно исправить»).

Команда ``/apply_fix`` — старт анкеты с предзаполнением и тем же номером.
"""
from __future__ import annotations

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.models import ModerationStatus
from fsm import cleanup_middleware, fsm_middleware
from keyboards import intake_closed_bubbles, main_menu_bubbles
from services import applications as applications_service
from services import intake_mode as intake_mode_service
from services import intake_state as intake_state_service
from states import UserIntake
from utils.bot_utils import reply_to_user

collector = HandlerCollector()


@collector.command(
    "/apply_fix",
    description="Исправить заявку",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_apply_fix(message: IncomingMessage, bot: Bot) -> None:
    """Повторная подача материалов по заявке «нужно исправить»."""
    if not await intake_state_service.is_intake_open():
        await reply_to_user(
            message,
            bot,
            (
                "Срок подачи исправлений истёк или приём закрыт. "
                "Контакты организаторов — в главном меню."
            ),
            bubbles=intake_closed_bubbles(),
        )
        return

    br_id = (
        (message.data or {}).get("br_id")
        or (message.body or "").strip().split(maxsplit=1)[-1]
        if (message.body or "").strip().startswith("/")
        else (message.body or "").strip()
    )
    br_id = (br_id or "").strip().upper()
    if not br_id or not br_id.startswith("BR-"):
        await reply_to_user(
            message,
            bot,
            "Не удалось открыть заявку для исправления. "
            "Вернитесь в «Мои заявки».",
            bubbles=main_menu_bubbles(huid=message.sender.huid),
        )
        return

    app = await applications_service.get_for_participant(
        br_id, message.sender.huid
    )
    if app is None:
        await reply_to_user(
            message,
            bot,
            "Заявка не найдена.",
            bubbles=main_menu_bubbles(huid=message.sender.huid),
        )
        return
    if app.moderation_status != ModerationStatus.NUZHNO_ISPRAVIT:
        await reply_to_user(
            message,
            bot,
            (
                f"Заявка **{br_id}** не ожидает исправления "
                f"(статус: {app.moderation_status.value})."
            ),
            bubbles=main_menu_bubbles(huid=message.sender.huid),
        )
        return

    mode = await intake_mode_service.get_intake_mode()
    fsm = message.state.fsm
    await fsm.clear()
    await fsm.set_data(
        {
            "fix_for_br_id": br_id,
            "parent_full_name": app.parent_full_name,
            "parent_division": app.parent_division,
            "parent_contact": app.parent_contact,
            "parent_contact_type": app.parent_contact_type,
            "child_name": app.child_name,
            "child_age": app.child_age,
            "track": app.track.name,
            "title": app.title,
            "description": app.description,
            "rules_consent": True,
            "publication_consent": True,
            "files": [],
        }
    )

    logger.info(
        "Старт исправления заявки",
        br_id=br_id,
        parent_huid=str(message.sender.huid),
        intake_mode=mode.value,
    )

    await fsm.set_state(UserIntake.user_intake_title)
    await reply_to_user(
        message,
        bot,
        (
            f"**Исправление заявки {br_id}**\n\n"
            f"Трек: {app.track.value}\n"
            f"Ребёнок: {app.child_name}, {app.child_age} лет\n\n"
            "Введите название работы (можно оставить прежнее — "
            "скопируйте из карточки заявки)."
        ),
        bubbles=BubbleMarkup(),
    )


__all__ = ["collector"]
