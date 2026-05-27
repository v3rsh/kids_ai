"""
Хендлеры архивной выгрузки папки ``data/attachments`` в чат админа.

Команды (все ``visible=False``, доступны только админу):
- ``/admin_export_files`` — запросить выгрузку всех заявок;
- ``/admin_export_shortlist_files`` — выгрузка только шорт-листа
  (``JuryStatus.V_TOP_10``);
- ``/admin_export_app`` — точечная переотправка ZIP по одному BR-ID
  (на случай, когда часть файлов потерялась в чате).

Двухшаговое подтверждение для массовых выгрузок: первая команда
показывает экран «Подтвердите?», подтверждение приходит через
``cmd_admin_confirm`` (см. ``handlers.admin``). Сама выгрузка
запускается фоновым ``asyncio.Task`` через ``start_export_task`` —
обработчик ``/admin_confirm`` мгновенно отдаёт «🚀 запущено»,
архивы прилетают в DM-чат админа по мере готовности.

Rate-limit и формат архива контролируются ``services.attachments_export``;
этот модуль занимается только UI и доставкой.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage
from pybotx.models.attachments import OutgoingAttachment

from config import EXPORT_PAUSE_MS
from fsm import cleanup_middleware, fsm_middleware
from keyboards import admin_confirm_bubbles, admin_system_menu_bubbles
from services.access import admin_only
from services.attachments_export import (
    ExportSelector,
    build_single_app_export,
    iter_attachments_export,
)
from utils.bot_utils import reply_to_user, resolve_bot_id

if TYPE_CHECKING:  # pragma: no cover
    from pybotx.models.message.incoming_message import UserSender


collector = HandlerCollector()


_ACTION_BY_SELECTOR = {
    ExportSelector.ALL: "export_files_all",
    ExportSelector.SHORTLIST: "export_files_shortlist",
}
_SELECTOR_BY_ACTION = {v: k for k, v in _ACTION_BY_SELECTOR.items()}


# =====================================================================
# Команды-приглашения (показывают confirm)
# =====================================================================


@collector.command(
    "/admin_export_files",
    description="Архивная выгрузка всех заявок (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_export_files(
    message: IncomingMessage, bot: Bot
) -> None:
    """Подтверждение выгрузки всех заявок."""
    await reply_to_user(
        message,
        bot,
        (
            "📦 Архивная выгрузка **всех заявок**.\n\n"
            "Бот по очереди соберёт ZIP по каждому BR-ID (один ZIP — "
            "одна заявка) и пришлёт их в этот чат. Файлы готовятся в "
            "памяти, на диск ничего не пишется. В конце придут "
            "`links.txt` и `manifest.csv`.\n\n"
            "Запустить?"
        ),
        bubbles=admin_confirm_bubbles(action=_ACTION_BY_SELECTOR[ExportSelector.ALL]),
    )


@collector.command(
    "/admin_export_shortlist_files",
    description="Архивная выгрузка шорт-листа (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_export_shortlist_files(
    message: IncomingMessage, bot: Bot
) -> None:
    """Подтверждение выгрузки только заявок шорт-листа."""
    await reply_to_user(
        message,
        bot,
        (
            "🏆 Архивная выгрузка **шорт-листа** (заявки в топ-10).\n\n"
            "Состав определяется по статусу жюри ``в топ-10``. "
            "Если шорт-лист ещё пуст — придёт только `manifest.csv` с "
            "summary без архивов.\n\n"
            "Запустить?"
        ),
        bubbles=admin_confirm_bubbles(
            action=_ACTION_BY_SELECTOR[ExportSelector.SHORTLIST]
        ),
    )


@collector.command(
    "/admin_export_app",
    description="Переотправить архив по одной заявке (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_export_app(
    message: IncomingMessage, bot: Bot
) -> None:
    """``/admin_export_app BR-2026-0042`` — повторно прислать ZIP заявки.

    Без двухшагового подтверждения: операция идемпотентна и обходит
    только одну запись.
    """
    arg = (message.argument or "").strip()
    if not arg:
        await reply_to_user(
            message,
            bot,
            (
                "Укажите BR-ID заявки в аргументе:\n"
                "`/admin_export_app BR-2026-0042`"
            ),
            bubbles=admin_system_menu_bubbles(),
        )
        return

    br_id = arg.split()[0]
    try:
        item = await build_single_app_export(br_id)
    except Exception:
        logger.exception("admin_export_app: ошибка сборки", br_id=br_id)
        await reply_to_user(
            message,
            bot,
            f"❌ Не удалось собрать архив по {br_id}. См. логи.",
            bubbles=admin_system_menu_bubbles(),
        )
        return

    if item is None:
        await reply_to_user(
            message,
            bot,
            f"⚠️ Заявка с BR-ID `{br_id}` не найдена.",
            bubbles=admin_system_menu_bubbles(),
        )
        return

    if item.kind == "zip":
        try:
            await bot.answer_message(
                item.caption,
                file=OutgoingAttachment(
                    content=item.payload, filename=item.filename
                ),
                wait_callback=False,
                bubbles=admin_system_menu_bubbles(),
            )
        except Exception:
            logger.exception(
                "admin_export_app: не удалось отправить архив", br_id=br_id
            )
            await reply_to_user(
                message,
                bot,
                f"❌ Не удалось отправить архив {br_id}. См. логи.",
                bubbles=admin_system_menu_bubbles(),
            )
        return

    # summary / status
    await reply_to_user(
        message,
        bot,
        item.caption,
        bubbles=admin_system_menu_bubbles(),
    )


# =====================================================================
# Запуск фоновой задачи (вызывается из cmd_admin_confirm)
# =====================================================================


async def start_export_task(
    *,
    bot: Bot,
    requester: "UserSender",
    selector_action: str,
) -> bool:
    """Запустить фоновую выгрузку и сразу вернуть управление.

    Args:
        bot: текущий ``Bot``.
        requester: ``IncomingMessage.sender`` админа, в чей DM-чат
            будут улетать архивы.
        selector_action: значение ``data.action`` из confirm-кнопки
            (``export_files_all`` или ``export_files_shortlist``).

    Returns:
        ``True`` — задача запущена; ``False`` — селектор нераспознан.
    """
    selector = _SELECTOR_BY_ACTION.get(selector_action)
    if selector is None:
        logger.warning(
            "start_export_task: неизвестный action", action=selector_action
        )
        return False

    bot_id = resolve_bot_id(bot)
    chat_id = getattr(requester, "chat_id", None)
    huid = getattr(requester, "huid", None)
    if bot_id is None or chat_id is None:
        logger.error(
            "start_export_task: не определены bot_id/chat_id",
            bot_id=str(bot_id),
            chat_id=str(chat_id),
            huid=str(huid),
        )
        return False

    asyncio.create_task(
        _run_export(
            bot=bot,
            bot_id=bot_id,
            chat_id=chat_id,
            huid=huid,
            selector=selector,
        ),
        name=f"attachments_export[{selector.value}]",
    )
    return True


async def _run_export(
    *,
    bot: Bot,
    bot_id,
    chat_id,
    huid,
    selector: ExportSelector,
) -> None:
    """Тело фоновой задачи: гонит поток ExportItem и шлёт админу."""
    pause_sec = max(EXPORT_PAUSE_MS, 0) / 1000.0
    sent_zip = 0
    logger.info(
        "attachments_export: старт",
        selector=selector.value,
        chat_id=str(chat_id),
        huid=str(huid),
    )
    try:
        async for item in iter_attachments_export(selector):
            try:
                if item.payload:
                    await bot.send_message(
                        bot_id=bot_id,
                        chat_id=chat_id,
                        body=item.caption or item.filename,
                        file=OutgoingAttachment(
                            content=item.payload, filename=item.filename
                        ),
                        wait_callback=False,
                    )
                else:
                    await bot.send_message(
                        bot_id=bot_id,
                        chat_id=chat_id,
                        body=item.caption or item.filename,
                        wait_callback=False,
                    )
            except Exception:
                logger.exception(
                    "attachments_export: ошибка отправки элемента",
                    selector=selector.value,
                    kind=item.kind,
                    filename=item.filename,
                )
                continue

            if item.kind == "zip":
                sent_zip += 1
            if pause_sec > 0:
                await asyncio.sleep(pause_sec)
    except Exception:
        logger.exception(
            "attachments_export: фатальная ошибка фоновой задачи",
            selector=selector.value,
        )
        try:
            await bot.send_message(
                bot_id=bot_id,
                chat_id=chat_id,
                body=(
                    "❌ Выгрузка прервана из-за ошибки. См. логи бота."
                ),
                wait_callback=False,
                bubbles=admin_system_menu_bubbles(),
            )
        except Exception:
            logger.exception(
                "attachments_export: не удалось сообщить об ошибке"
            )
        return

    logger.info(
        "attachments_export: завершено",
        selector=selector.value,
        sent_zip=sent_zip,
    )


__all__ = ["collector", "start_export_task"]
