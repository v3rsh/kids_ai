"""
Главное меню модератора (Wave 2 / B).

Точка входа в ветку модератора. Содержит:

- ``/moderator`` — главное меню модератора (видимая команда; защищена
  через ``moderator_only`` — все остальные пользователи получают
  отказ согласно §27.2);
- скрытая команда ``/m_help`` для текстовой справки по командам ветки.

Никакого FSM/диспетчера здесь нет — модерация работает по принципу
«поштучных команд» (§27 «Модерация выполняется поштучно»). Свободный
текст модератора, нужный для шагов ``/comment`` / ``/notify_fix`` /
``/notify_reject`` без аргумента, обрабатывает диспетчер
``handlers.common.default_handler`` через
``register_state_handler`` (см. ``moderator_actions.py``).

WAVE3-TODO: подключить ``collector`` в
``app/handlers/__init__.py → get_all_collectors()`` после
``common_collector`` и (если уже есть) ``user_collector``.
"""
from __future__ import annotations

from loguru import logger
from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
)

from fsm import cleanup_middleware, fsm_middleware
from services.access import moderator_only
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Тексты
# =====================================================================

MODERATOR_MENU_TEXT = (
    "Меню модератора «Безопасные рисунки».\n\n"
    "Выберите действие или введите команду вручную. Полный список — /m_help."
)

MODERATOR_HELP_TEXT = (
    "Команды модератора (§27.1, §27.5):\n\n"
    "Очередь и карточки:\n"
    "  /queue — список заявок на модерации (по 5, фильтры/пагинация)\n"
    "  /browse — карусель просмотра заявок\n"
    "  /find BR-2026-XXXX — карточка заявки\n"
    "  /files BR-2026-XXXX — получить файлы заявки в чат\n\n"
    "Действия по карточке:\n"
    "  /status <ID> <группа> <значение> — сменить статус\n"
    "    (группы: модерация / голосование / мерч; жюри —\n"
    "    автоматически, ручное редактирование запрещено §25.3.3)\n"
    "  /comment <ID> <текст> — комментарий модератора\n"
    "  /notify_fix <ID> [текст_уточнения] — §18.4\n"
    "  /notify_reject <ID> <причина> — §18.3 + 99_Отклонено/\n"
    "  /notify_shortlist <ID> — §18.5\n\n"
    "Выгрузки и статистика:\n"
    "  /export — реестр в XLSX\n"
    "  /export_shortlist — шорт-лист в XLSX\n"
    "  /stats today — статистика за сегодня\n"
    "  /stats all — статистика за весь период\n\n"
    "Жюри-логика (только модератор, §27.5):\n"
    "  /jury_state — текущий статус процесса\n"
    "  /jury_close_round <пул|all> — досрочное закрытие раунда\n"
    "  /jury_finalize — аварийная финализация процесса"
)


# =====================================================================
# Клавиатуры
# =====================================================================


def _moderator_menu_bubbles() -> BubbleMarkup:
    """Кнопки главного меню модератора.

    Отдельный конструктор живёт здесь (не в ``app/keyboards.py``), чтобы
    не мешать с пользовательскими клавиатурами и чётко отграничить
    модераторские кнопки от родительских (§5.1 vs §5.2).
    """
    bubbles = BubbleMarkup()
    bubbles.add_button(command="/queue", label="📋 Очередь")
    bubbles.add_button(command="/browse", label="🖼️ Карусель", new_row=True)
    bubbles.add_button(command="/stats today", label="📈 Статистика — сегодня", new_row=True)
    bubbles.add_button(command="/stats all", label="📊 Статистика — весь период", new_row=True)
    bubbles.add_button(command="/export", label="📤 Реестр (XLSX)", new_row=True)
    bubbles.add_button(command="/export_shortlist", label="🏆 Шорт-лист (XLSX)", new_row=True)
    bubbles.add_button(command="/jury_state", label="⚖️ Состояние жюри", new_row=True)
    bubbles.add_button(command="/m_help", label="❔ Справка по командам", new_row=True)
    return bubbles


# =====================================================================
# Хендлеры
# =====================================================================


@collector.command(
    "/moderator",
    description="Меню модератора (только для модераторов)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_moderator_menu(message: IncomingMessage, bot: Bot) -> None:
    """Главное меню модератора (§27).

    Защищено ``moderator_only`` (см. services.access). Не-модератор
    получит ответ «Команда доступна только модераторам» (§27.2).
    """
    logger.info(
        "Модератор открыл меню",
        huid=str(message.sender.huid),
    )
    await reply_to_user(
        message,
        bot,
        MODERATOR_MENU_TEXT,
        bubbles=_moderator_menu_bubbles(),
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
        bubbles=_moderator_menu_bubbles(),
    )


__all__ = ["collector"]
