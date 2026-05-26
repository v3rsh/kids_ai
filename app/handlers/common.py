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
import asyncio
from typing import Awaitable, Callable

from loguru import logger
from pybotx import (
    Bot,
    ChatCreatedEvent,
    ChatTypes,
    HandlerCollector,
    IncomingMessage,
)

from fsm import cleanup_middleware, fsm_middleware
from keyboards import jury_menu_bubbles, main_menu_bubbles, moderator_menu_bubbles
from services import discovery
from services import users as users_service
from states import JuryFlow, ModeratorFlow
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
    "Привет! Это бот конкурса детского творчества «Безопасные рисунки».\n\n"
    "Конкурс посвящён цифровой безопасности и безопасному поведению "
    "детей в интернете.\n\n"
    "Здесь можно подать работу ребёнка на конкурс, посмотреть правила, "
    "сроки и примеры.\n\n"
    "Перед подачей заявки, пожалуйста, ознакомьтесь с правилами участия."
)


@collector.chat_created
async def on_chat_created(event: ChatCreatedEvent, bot: Bot) -> None:
    """Первое сообщение при создании чата.

    - PERSONAL_CHAT → eager-апсерт ``users.chat_id`` (чтобы DM от бота
      админу/модератору проходили сразу), welcome + главное меню.
    - Любой групповой чат → discovery-карточка админу с кнопкой
      «Сделать этот чат чатом модерации». Карточка приходит **всегда**
      (включая случай, когда чат уже совпадает с текущим
      ``moderation_chat_id`` — это полезно как подтверждение и помогает
      повторно одобрить чат, если бота переподключали). Дедуп на 1 час
      внутри ``services.discovery._dedup_should_skip``.

    Бот в групповых чатах не отвечает на входящие (см. ``fsm/chat_gate.py``);
    единственная инициатива в чате модерации — это outbound-нотификации.
    """
    chat_id = event.chat.id
    chat_type = event.chat.type
    logger.info(
        "Создан новый чат",
        chat_id=str(chat_id),
        chat_type=str(chat_type),
    )

    if chat_type == ChatTypes.PERSONAL_CHAT:
        creator_huid = getattr(event, "creator_id", None)
        # Зафиксировать chat_id ДО welcome — иначе если админ/модератор
        # инициирует discovery в первый же клик, у нас всё ещё не будет
        # его chat_id и карточка ему не дойдёт.
        if creator_huid is not None:
            try:
                await users_service.set_user_chat_id(creator_huid, chat_id)
            except Exception:
                logger.exception(
                    "on_chat_created: не удалось зафиксировать chat_id",
                    huid=str(creator_huid),
                    chat_id=str(chat_id),
                )
            # Fire-and-forget прогрев CTS-кэша: к моменту первого /apply
            # у нас уже будут ФИО и подразделение из CTS без задержки.
            asyncio.create_task(
                users_service.sync_user_from_cts(bot, creator_huid)
            )
        await bot.send_message(
            bot_id=event.bot.id,
            chat_id=chat_id,
            body=WELCOME_TEXT,
            bubbles=main_menu_bubbles(huid=creator_huid),
            wait_callback=False,
        )
        return

    creator_huid = getattr(event, "creator_id", None)
    chat_name = getattr(event, "chat_name", "") or ""
    await discovery.notify_admin_moderation_chat_candidate(
        bot,
        chat_id=chat_id,
        chat_name=chat_name,
        creator_huid=creator_huid,
    )


@collector.command(
    "/start",
    description="Главное меню бота",
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_start(message: IncomingMessage, bot: Bot) -> None:
    """Точка входа: показывает приветствие + главное меню.

    Сбрасывает FSM-состояние, чтобы возврат в главное меню из любой
    ветки (анкета, меню роли) гарантированно очищал контекст —
    «Назад в главное меню» обязан означать выход из текущей ветки.

    Параллельно (fire-and-forget) прогревает CTS-кэш профиля юзера —
    к моменту нажатия «Подать работу» в `cmd_apply` мы получим ФИО
    и подразделение из БД без задержки.
    """
    await message.state.fsm.clear()
    asyncio.create_task(
        users_service.sync_user_from_cts(bot, message.sender.huid)
    )
    await reply_to_user(
        message,
        bot,
        WELCOME_TEXT,
        bubbles=main_menu_bubbles(huid=message.sender.huid),
    )


@collector.command("/help", description="Справка")
async def cmd_help(message: IncomingMessage, bot: Bot) -> None:
    """Короткая справка по основным командам."""
    await reply_to_user(
        message,
        bot,
        (
            "**Справка**\n\n"
            "Доступные команды:\n"
            "/start — главное меню бота\n"
            "/help — эта справка\n\n"
            "Чтобы подать работу — нажмите «Подать работу» в главном меню."
        ),
        bubbles=main_menu_bubbles(huid=message.sender.huid),
    )


# =====================================================================
# Диспетчер свободного текста
# =====================================================================

DEFAULT_FALLBACK_TEXT = (
    "Я понимаю только команды.\n\n"
    "Используй /start или /help."
)

# Legacy-состояния, удалённые из enum UserIntake (см. план
# «Fix materialize and contact flow» → C0). У юзеров, застрявших
# в середине старой анкеты, в Redis может оставаться такое значение —
# при первом же входящем сбрасываем FSM и предлагаем начать заново.
_LEGACY_FSM_STATES: frozenset[str] = frozenset(
    {
        "user:intake:parent_full_name",
        "user:intake:parent_division",
    }
)

_LEGACY_RESET_TEXT = (
    "**Анкета обновилась**\n\n"
    "Теперь ФИО и подразделение подтягиваются автоматически из вашего "
    "профиля eXpress.\n\n"
    "Начните подачу заявки заново через /apply."
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

    if current_state in _LEGACY_FSM_STATES:
        logger.info(
            "default_handler: сброс legacy FSM-состояния анкеты",
            state=current_state,
            user_huid=str(message.sender.huid),
        )
        await fsm.clear()
        await reply_to_user(
            message,
            bot,
            _LEGACY_RESET_TEXT,
            bubbles=main_menu_bubbles(huid=message.sender.huid),
        )
        return

    if current_state and current_state in STATE_HANDLERS:
        handler = STATE_HANDLERS[current_state]
        logger.debug(
            "default_handler: маршрутизация по FSM-состоянию",
            state=current_state,
            handler=handler.__qualname__,
        )
        await handler(message, bot)
        return

    # Меню роли: свободный текст не выкидывает в главное меню — мы
    # перерисовываем именно меню роли, чтобы у модератора/судьи под
    # рукой остались его кнопки. FSM-state не трогаем.
    if current_state == ModeratorFlow.moderator_menu.value:
        await reply_to_user(
            message,
            bot,
            DEFAULT_FALLBACK_TEXT,
            bubbles=moderator_menu_bubbles(),
        )
        return

    if current_state == JuryFlow.jury_menu.value:
        await reply_to_user(
            message,
            bot,
            DEFAULT_FALLBACK_TEXT,
            bubbles=jury_menu_bubbles(),
        )
        return

    if current_state:
        logger.debug(
            "default_handler: для текущего состояния нет хендлера, fallback",
            state=current_state,
        )

    await reply_to_user(
        message,
        bot,
        DEFAULT_FALLBACK_TEXT,
        bubbles=main_menu_bubbles(huid=message.sender.huid),
    )
