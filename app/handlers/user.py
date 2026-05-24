"""
Ветка участника: главное меню и информационные экраны.

Файлы ветки:
- ``user.py`` (этот файл) — главное меню, кнопки «О конкурсе», «Правила»,
  «Примеры», «Сроки», «Контакты», и точка входа в анкету ``/apply``;
- ``user_intake.py`` — поэтапный сбор полей анкеты (ФИО → ... → описание);
- ``user_files.py`` — приём файлов работы по треку;
- ``user_confirm.py`` — согласия, финальное резюме и submit.

Соглашения:
- все экраны редактируются на месте через ``reply_to_user`` —
  см. ``.cursor/rules/message-navigation.mdc``;
- информационные экраны сразу возвращают главное меню под текстом,
  чтобы пользователь мог перейти куда угодно одним кликом;
- команды ``/menu_*`` и ``/apply`` помечены ``visible=False`` — они
  вызываются только кнопками главного меню, в CTS-меню их не показываем.

Импорты ``handlers.user_intake`` / ``handlers.user_files`` /
``handlers.user_confirm`` делаются ниже как side-effect, чтобы при
сборке коллекторов все четыре модуля были загружены и зарегистрировали
свои хендлеры и FSM-state-handler'ы.
"""
from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from config import CONTACTS_TEXT
from fsm import cleanup_middleware, fsm_middleware
from keyboards import main_menu_bubbles
from services import users as users_service
from states import UserIntake
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Статичные тексты информационных экранов
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

# Экран «Примеры работ и промптов» объединяет два блока — вопросы для
# вдохновения ребёнка и советы по ИИ-подсказкам. Тексты сокращены, чтобы
# помещаться в один пузырь чата без обрезания.
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

# Текст «Контакты организаторов» вынесен в env-переменную
# `CONTACTS_TEXT` (см. `app/config.py` → `CONTACTS_TEXT`). Дефолт совпадает
# с прежним хардкодом; заказчик может поправить формулировку без диффа в
# коде, переопределив переменную в `.env`.


# =====================================================================
# Информационные экраны
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
    """Экран «О конкурсе»."""
    await reply_to_user(message, bot, ABOUT_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_rules",
    description="Правила участия",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_rules(message: IncomingMessage, bot: Bot) -> None:
    """Экран «Правила участия»."""
    await reply_to_user(message, bot, RULES_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_examples",
    description="Примеры работ и промптов",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_examples(message: IncomingMessage, bot: Bot) -> None:
    """Экран «Примеры работ и промптов»."""
    await reply_to_user(message, bot, EXAMPLES_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_dates",
    description="Сроки конкурса",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_dates(message: IncomingMessage, bot: Bot) -> None:
    """Экран «Сроки конкурса»."""
    await reply_to_user(message, bot, DATES_TEXT, bubbles=main_menu_bubbles())


@collector.command(
    "/menu_contacts",
    description="Контакты организаторов",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_menu_contacts(message: IncomingMessage, bot: Bot) -> None:
    """Экран «Контакты организаторов»."""
    await reply_to_user(message, bot, CONTACTS_TEXT, bubbles=main_menu_bubbles())


# =====================================================================
# Точка входа в анкету
# =====================================================================


@collector.command(
    "/apply",
    description="Подать работу",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_apply(message: IncomingMessage, bot: Bot) -> None:
    """Старт анкеты «Подать работу».

    ФИО и подразделение подтягиваются из CTS-кэша
    (``services.users.ensure_user_profile_loaded`` с 24-часовым TTL).
    Если CTS вернул не всё — включаются fallback-шаги для пропущенных
    полей. На горячем пути сразу переходим к шагу «Контакт».

    Кнопки удаляются (передаём пустой ``BubbleMarkup()``), чтобы
    пользователь видел текстовое приглашение без отвлекающего меню —
    см. ``.cursor/rules/pybotx-bubbles.mdc``.
    """
    fsm = message.state.fsm
    await fsm.clear()

    # Подтянем профиль из CTS (или из горячего кэша). Таймаут 5 сек —
    # если CTS лёг, пользователь идёт по fallback-веткам ручного ввода
    # ФИО/подразделения, заявка не блокируется.
    user = await users_service.ensure_user_profile_loaded(
        bot, message.sender.huid, max_age_sec=86400, timeout=5.0
    )
    full_name = ((user.full_name if user else "") or "").strip()
    department = ((user.department if user else "") or "").strip()
    email_hint = ((user.email if user else "") or "").strip()
    ip_phone_hint = ((user.ip_phone if user else "") or "").strip()
    other_phone_hint = ((user.other_phone if user else "") or "").strip()

    await fsm.set_data(
        {
            "parent_full_name": full_name,
            "parent_division": department,
            "cts_email_hint": email_hint,
            "cts_ip_phone_hint": ip_phone_hint,
            "cts_other_phone_hint": other_phone_hint,
        }
    )

    logger.info(
        "Старт анкеты UserIntake",
        parent_huid=str(message.sender.huid),
        cts_full_name_present=bool(full_name),
        cts_department_present=bool(department),
    )

    # Импорт здесь, чтобы избежать циклической зависимости user → user_intake.
    from handlers.user_intake import (
        _PROMPT_PARENT_DIVISION_FB,
        _PROMPT_PARENT_FULL_NAME_FB,
        _build_contact_prompt,
    )

    if not full_name:
        await fsm.set_state(UserIntake.user_intake_parent_full_name_fb)
        await reply_to_user(
            message, bot, _PROMPT_PARENT_FULL_NAME_FB, bubbles=BubbleMarkup()
        )
        return

    if not department:
        await fsm.set_state(UserIntake.user_intake_parent_division_fb)
        await reply_to_user(
            message, bot, _PROMPT_PARENT_DIVISION_FB, bubbles=BubbleMarkup()
        )
        return

    data = await fsm.get_data()
    await fsm.set_state(UserIntake.user_intake_parent_contact)
    await reply_to_user(
        message, bot, _build_contact_prompt(data), bubbles=BubbleMarkup()
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
