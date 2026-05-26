"""
Главное меню модератора.

Точка входа в ветку модератора. Содержит:

- ``/moderator`` — главное меню модератора (видимая команда; защищена
  через ``moderator_only`` — все остальные пользователи получают
  отказ «Команда доступна только модераторам»);
- скрытая команда ``/m_help`` для текстовой справки по командам ветки.

Никакого FSM/диспетчера здесь нет — модерация работает поштучно. Свободный
текст модератора, нужный для шагов ``/comment`` / ``/notify_fix`` /
``/notify_reject`` без аргумента, обрабатывает диспетчер
``handlers.common.default_handler`` через
``register_state_handler`` (см. ``moderator_actions.py``).

``collector`` подключается в
``app/handlers/__init__.py → get_all_collectors()`` после
пользовательских модулей и перед остальными модераторскими подмодулями.
"""
from __future__ import annotations

from loguru import logger
from pybotx import (
    Bot,
    HandlerCollector,
    IncomingMessage,
)

from fsm import cleanup_middleware, fsm_middleware
from keyboards import moderator_menu_bubbles
from services import discovery
from services.access import is_moderator, moderator_only
from states import ModeratorFlow
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Тексты
# =====================================================================

MODERATOR_MENU_TEXT = (
    "**Меню модератора** «Безопасные рисунки».\n\n"
    "Выберите действие или введите команду вручную. "
    "Полный список — /m_help."
)

MODERATOR_HELP_TEXT = (
    "**Команды модератора**\n\n"
    "**Очередь и карточки:**\n"
    "  /queue — список заявок на модерации (по 5, фильтры/пагинация)\n"
    "  /browse — карусель просмотра заявок\n"
    "  /find BR-2026-XXXX — карточка заявки\n"
    "  /files BR-2026-XXXX — получить файлы заявки в чат\n\n"
    "**Действия по карточке:**\n"
    "  /status <ID> <группа> <значение> — сменить статус\n"
    "    (группы: модерация / голосование / мерч; жюри — заполняется\n"
    "    автоматически по итогам раундов, ручное редактирование запрещено)\n"
    "  /comment <ID> <текст> — комментарий модератора\n"
    "  /notify_fix <ID> [текст_уточнения] — уведомление «требуется исправление»\n"
    "  /notify_reject <ID> <причина> — отклонить + перенести в 99_rejected/\n"
    "  /notify_shortlist <ID> — уведомление «работа в шорт-листе»\n\n"
    "**Выгрузки и статистика:**\n"
    "  /export — реестр в XLSX\n"
    "  /export_shortlist — шорт-лист в XLSX\n"
    "  /stats today — статистика за сегодня\n"
    "  /stats all — статистика за весь период\n\n"
    "**Жюри-логика (только модератор):**\n"
    "  /jury_state — текущий статус процесса\n"
    "  /jury_close_round <пул|all> — досрочное закрытие раунда\n"
    "  /jury_finalize — аварийная финализация процесса"
)


# =====================================================================
# Хендлеры
# =====================================================================


@collector.command(
    "/moderator",
    description="Меню модератора",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_moderator_menu(message: IncomingMessage, bot: Bot) -> None:
    """Главное меню модератора.

    Точка входа в ветку. В отличие от внутренних команд ветки (`/queue`,
    `/find`, …) — здесь НЕ используется молчаливый ``moderator_only``.
    Поведение:

    - Если sender — модератор → показываем меню.
    - Иначе → шлём админу discovery-карточку с профилем + кнопками
      «Назначить модератором / Отклонить» (через ``services.discovery``),
      а пользователю отвечаем «Запрос отправлен администратору».
    """
    huid = message.sender.huid
    if is_moderator(huid):
        logger.info("Модератор открыл меню", huid=str(huid))
        await message.state.fsm.set_state(ModeratorFlow.moderator_menu)
        await reply_to_user(
            message,
            bot,
            MODERATOR_MENU_TEXT,
            bubbles=moderator_menu_bubbles(),
        )
        return

    logger.info(
        "Запрос доступа к /moderator от не-модератора",
        huid=str(huid),
    )
    await discovery.notify_admin_role_candidate(
        bot, huid=huid, role="moderator"
    )
    await reply_to_user(
        message,
        bot,
        (
            "**Доступ ограничен**\n\n"
            "Доступ к меню модератора ограничен.\n"
            "Запрос отправлен администратору на одобрение."
        ),
    )


@collector.command(
    "/m_help",
    description="Справка по командам модератора",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_moderator_help(message: IncomingMessage, bot: Bot) -> None:
    """Скрытая текстовая справка для модератора."""
    await reply_to_user(
        message,
        bot,
        MODERATOR_HELP_TEXT,
        bubbles=moderator_menu_bubbles(),
    )


__all__ = ["collector"]
