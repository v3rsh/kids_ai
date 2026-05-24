"""
Общие хендлеры (не привязаны к ветке сценария).

Содержит:
- ``on_chat_created`` — приветственное сообщение и главное меню при
  создании чата;
- ``/start`` — перерисовка главного меню;
- ``/help`` — короткая справка;
- ``default_message_handler`` — единственный на приложение перехватчик
  свободного текста, реализованный как **диспетчер по FSM-состоянию**.

Контракт диспетчера (см. также ``docs/architecture.md`` →
«Диспетчер default_message_handler»):

- Каждая ветка, которой нужен свободный текст в каком-то FSM-состоянии,
  регистрирует хендлер через ``register_state_handler(state, handler)``
  в момент импорта своего коллектора (``app/handlers/__init__.py``).
- Хендлер — корутина с сигнатурой ``async def h(message, bot)``.
- Диспетчер сначала загружает текущее состояние через
  ``message.state.fsm.get_state()`` (middleware FSM ставит ``current_state =
  None`` до явной загрузки — см. ``app/fsm/middleware.py``), кладёт его
  в ``message.state.current_state`` и ищет в ``STATE_HANDLERS``.
- Если хендлер найден — диспетчер делегирует ему обработку и завершает.
- Если нет — отвечает дефолтным сообщением «понимаю только команды».

Жёстких ограничений на ``state`` нет — это строка из ``MyEnum.value``
(см. ``app/states.py``). Ветки регистрируют значения, например:

    from states import UserIntake
    from handlers.common import register_state_handler

    register_state_handler(UserIntake.user_intake_parent_full_name.value, on_parent_full_name)
"""
from typing import Awaitable, Callable

from loguru import logger
from pybotx import (
    Bot,
    ChatCreatedEvent,
    HandlerCollector,
    IncomingMessage,
)

from fsm import cleanup_middleware, fsm_middleware
from keyboards import main_menu_bubbles
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Реестр обработчиков по FSM-состоянию
# =====================================================================

# Тип хендлера: корутина, совместимая с pybotx (message, bot).
StateHandler = Callable[[IncomingMessage, Bot], Awaitable[None]]

# Ключ — значение FSM-состояния (``Enum.value``), значение — хендлер.
# Заполняется ветками бота в момент импорта их коллекторов.
STATE_HANDLERS: dict[str, StateHandler] = {}


def register_state_handler(state: str, handler: StateHandler) -> None:
    """Зарегистрировать хендлер для FSM-состояния.

    Идемпотентность: повторная регистрация одного и того же ``state``
    логируется как WARNING и переопределяет старый хендлер (это нужно
    для hot-reload в dev-режиме).

    Args:
        state: значение FSM-состояния — ``MyStates.some.value``.
        handler: корутина ``async def h(message, bot)``.
    """
    if state in STATE_HANDLERS:
        logger.warning(
            "Перерегистрация обработчика FSM-состояния",
            state=state,
            previous=STATE_HANDLERS[state].__qualname__,
            new=handler.__qualname__,
        )
    STATE_HANDLERS[state] = handler


# =====================================================================
# Приветствие и базовые команды
# =====================================================================

WELCOME_TEXT = (
    "Привет! Это бот конкурса детского творчества «Безопасные рисунки».\n"
    "Конкурс посвящён цифровой безопасности и безопасному поведению "
    "детей в интернете.\n"
    "Здесь можно подать работу ребёнка на конкурс, посмотреть правила, "
    "сроки и примеры.\n"
    "Перед подачей заявки, пожалуйста, ознакомьтесь с правилами участия."
)


@collector.chat_created
async def on_chat_created(event: ChatCreatedEvent, bot: Bot) -> None:
    """Первое сообщение при создании чата.

    Используется ``bot.send_message``, потому что в момент chat_created
    нет ``IncomingMessage`` для трекинга — это первое сообщение в чате,
    очищать нечего (см. .cursor/rules/message-navigation.mdc, исключения).
    """
    logger.info(
        "Создан новый чат",
        chat_id=str(event.chat.id),
        chat_type=event.chat.type,
    )
    await bot.send_message(
        bot_id=event.bot.id,
        chat_id=event.chat.id,
        body=WELCOME_TEXT,
        bubbles=main_menu_bubbles(),
        wait_callback=False,
    )


@collector.command("/start", description="Главное меню бота")
async def cmd_start(message: IncomingMessage, bot: Bot) -> None:
    """Точка входа: показывает приветствие + главное меню."""
    await reply_to_user(message, bot, WELCOME_TEXT, bubbles=main_menu_bubbles())


@collector.command("/help", description="Справка")
async def cmd_help(message: IncomingMessage, bot: Bot) -> None:
    """Короткая справка по основным командам."""
    await reply_to_user(
        message,
        bot,
        (
            "Доступные команды:\n"
            "/start — главное меню бота\n"
            "/help — эта справка\n\n"
            "Чтобы подать работу — нажмите «Подать работу» в главном меню."
        ),
        bubbles=main_menu_bubbles(),
    )


# =====================================================================
# Диспетчер свободного текста
# =====================================================================

DEFAULT_FALLBACK_TEXT = (
    "Я понимаю только команды. Используй /start или /help."
)


@collector.default_message_handler(middlewares=[fsm_middleware, cleanup_middleware])
async def default_handler(message: IncomingMessage, bot: Bot) -> None:
    """Единственный на приложение перехватчик свободного текста.

    Контракт реализуется через ``STATE_HANDLERS`` — см. модуль docstring
    и docs/architecture.md → «Диспетчер default_message_handler».
    """
    fsm = message.state.fsm
    current_state = await fsm.get_state()
    message.state.current_state = current_state

    if current_state and current_state in STATE_HANDLERS:
        handler = STATE_HANDLERS[current_state]
        logger.debug(
            "default_handler: маршрутизация по FSM-состоянию",
            state=current_state,
            handler=handler.__qualname__,
        )
        await handler(message, bot)
        return

    if current_state:
        logger.debug(
            "default_handler: для текущего состояния нет хендлера, fallback",
            state=current_state,
        )

    await reply_to_user(message, bot, DEFAULT_FALLBACK_TEXT, bubbles=main_menu_bubbles())
