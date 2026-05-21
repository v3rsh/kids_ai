"""
Ветка родителя/участника: главное меню и информационные экраны (§6, §7).

Файлы ветки A:
- ``user.py`` (этот файл) — главное меню, кнопки «О конкурсе», «Правила»,
  «Примеры», «Сроки», «Контакты», и точка входа в анкету ``/apply``;
- ``user_intake.py`` — поэтапный сбор полей анкеты (ФИО → ... → описание);
- ``user_files.py`` — приём файлов работы по треку (§12);
- ``user_confirm.py`` — согласия (§13), резюме (§14), submit (§15, §18).

Соглашения:
- все экраны редактируются на месте через ``reply_to_user`` —
  см. ``.cursor/rules/message-navigation.mdc``;
- информационные экраны сразу возвращают главное меню под текстом,
  чтобы пользователь мог перейти куда угодно одним кликом;
- команды ``/menu_*`` и ``/apply`` помечены ``visible=False`` — они
  вызываются только кнопками главного меню, в CTS-меню их не показываем.

Импорты ``handlers.user_intake`` / ``handlers.user_files`` /
``handlers.user_confirm`` делаются ниже как side-effect, чтобы при
сборке коллекторов в Wave 3 все четыре модуля были загружены и
зарегистрировали свои хендлеры и FSM-state-handler'ы.
"""
from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from config import CONTACTS_TEXT
from fsm import cleanup_middleware, fsm_middleware
from keyboards import main_menu_bubbles
from states import UserIntake
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Статичные тексты (§7)
# =====================================================================

ABOUT_TEXT = (
    "«Безопасные рисунки» — конкурс детского творчества для детей "
    "сотрудников компании.\n\n"
    "Мы предлагаем детям показать, как они понимают безопасность "
    "в интернете:\n"
    "— как мама или папа помогают быть в безопасности в сети;\n"
    "— что помогает самому ребёнку быть внимательным и осторожным "
    "в интернете.\n\n"
    "Работу можно подать в одном из трёх треков:\n"
    "1. Традиционное рисование\n"
    "2. ИИ-рисунок\n"
    "3. От руки к ИИ"
)

RULES_TEXT = (
    "К участию принимаются работы детей сотрудников компании.\n"
    "Работа должна соответствовать теме конкурса и быть создана "
    "ребёнком самостоятельно.\n"
    "Взрослый может помочь технически: сфотографировать работу, "
    "загрузить файл, помочь оформить идею для ИИ-трека.\n\n"
    "Текст как самостоятельная работа не принимается, но к каждой "
    "работе нужно добавить короткое описание: что изображено и почему "
    "ребёнок выбрал эту тему."
)

DATES_TEXT = (
    "Сроки конкурса:\n\n"
    "• 1 июня — старт конкурса\n"
    "• 1–21 июня — приём работ\n"
    "• 22–29 июня — работа жюри и голосование\n"
    "• 30 июня — объявление итогов"
)

# §7.5 — два блока (вопросы для вдохновения + ИИ-подсказки) объединены в
# один экран. Тексты примеров промптов сокращены, чтобы помещались
# в один пузырь чата без обрезания: полный текст ТЗ — в §7.5.2.1/2.
EXAMPLES_TEXT = (
    "Если ребёнку сложно придумать идею, можно начать с вопросов:\n"
    "— Что помогает тебе не попасться мошенникам в игре?\n"
    "— Что мама или папа запрещают делать в интернете и почему?\n"
    "— Что ты делаешь, если тебе пишет незнакомый человек?\n"
    "— Что такое «безопасно в интернете» для тебя?\n"
    "— Кто помогает тебе, если в интернете происходит что-то странное?\n"
    "— Что нельзя рассказывать незнакомым людям в игре или мессенджере?\n"
    "— Почему нельзя отправлять пароль, фото, адрес или номер телефона?\n"
    "— Как ты понимаешь, что сайту, игре или человеку можно доверять?\n"
    "— Что бы делал супергерой, который защищает детей в интернете?\n"
    "— Как выглядит безопасный компьютер, телефон или игра?\n\n"
    "Как помочь ребёнку с ИИ-треком без подмены авторства\n"
    "Общий принцип: идея должна принадлежать ребёнку.\n\n"
    "Родитель может помочь технически: записать мысль ребёнка, "
    "уточнить детали, ввести промпт в ИИ-сервис, сохранить и "
    "отправить результат.\n"
    "Родитель не должен полностью придумывать сюжет, стиль, "
    "персонажей и смысл работы за ребёнка.\n\n"
    "Лучше сначала задать ребёнку вопросы, записать его ответы "
    "простыми словами, а затем превратить эти ответы в промпт."
)

# §5.2, §7 — текст «Контакты организаторов» вынесен в env-переменную
# `CONTACTS_TEXT` (см. `app/config.py` → `CONTACTS_TEXT`). Дефолт совпадает
# с прежним хардкодом; заказчик может поправить формулировку без диффа в
# коде, переопределив переменную в `.env`.


# =====================================================================
# Информационные экраны (§7.2–§7.5)
# =====================================================================
#
# Каждый экран сразу показывает главное меню под текстом — пользователь
# может одним кликом перейти куда угодно. «Назад в меню» не нужно, т.к.
# меню уже на месте (см. message-navigation.mdc).


@collector.command(
    "/menu_about",
    description="О конкурсе",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_about(message: IncomingMessage, bot: Bot) -> None:
    """§7.2 — «О конкурсе»."""
    await reply_to_user(message, bot, ABOUT_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_rules",
    description="Правила участия",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_rules(message: IncomingMessage, bot: Bot) -> None:
    """§7.4 — «Правила участия»."""
    await reply_to_user(message, bot, RULES_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_examples",
    description="Примеры работ и промптов",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_examples(message: IncomingMessage, bot: Bot) -> None:
    """§7.5 — «Примеры работ и промптов»."""
    await reply_to_user(message, bot, EXAMPLES_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_dates",
    description="Сроки конкурса",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_dates(message: IncomingMessage, bot: Bot) -> None:
    """§7.3 — «Сроки конкурса»."""
    await reply_to_user(message, bot, DATES_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_contacts",
    description="Контакты организаторов",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_contacts(message: IncomingMessage, bot: Bot) -> None:
    """§7 — контакты организаторов."""
    await reply_to_user(message, bot, CONTACTS_TEXT, bubbles=main_menu_bubbles())


# =====================================================================
# Точка входа в анкету (§8 шаг 3)
# =====================================================================


@collector.command(
    "/apply",
    description="Подать работу",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_apply(message: IncomingMessage, bot: Bot) -> None:
    """§8 шаг 3 — старт анкеты «Подать работу».

    Сбрасывает FSM (на случай прерванной предыдущей сессии) и
    переключает на первое поле — ФИО родителя. Кнопки удаляются
    (передаём пустой ``BubbleMarkup()``), чтобы пользователь видел
    текстовое приглашение без отвлекающего меню — см.
    ``.cursor/rules/pybotx-bubbles.mdc``.
    """
    fsm = message.state.fsm
    await fsm.clear()
    await fsm.set_state(UserIntake.user_intake_parent_full_name)
    await fsm.set_data({})

    logger.info(
        "Старт анкеты UserIntake",
        parent_huid=str(message.sender.huid),
    )

    await reply_to_user(
        message,
        bot,
        (
            "Подаём работу.\n\n"
            "Шаг 1 из 7. Как вас зовут? Укажите ФИО полностью "
            "(например, «Иванова Анна Сергеевна»)."
        ),
        bubbles=BubbleMarkup(),
    )


# =====================================================================
# Side-effect импорты — регистрируют свои коллекторы и state-handler'ы
# =====================================================================
#
# При импорте этого модуля в handlers/__init__.py подгружаются все
# модули ветки A, и каждый регистрирует:
# - свой ``collector`` (в ``handlers/__init__.py:get_all_collectors()``);
# - свои хендлеры FSM-состояний через
#   ``handlers.common.register_state_handler``.

from handlers import user_intake as _user_intake  # noqa: E402, F401
from handlers import user_files as _user_files  # noqa: E402, F401
from handlers import user_confirm as _user_confirm  # noqa: E402, F401
