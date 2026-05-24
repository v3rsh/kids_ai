"""
FSM-flow анкеты участника.

Поэтапный сбор полей:
1. ФИО родителя (UserIntake.user_intake_parent_full_name)
2. Подразделение (UserIntake.user_intake_parent_division)
3. Имя ребёнка (UserIntake.user_intake_child_name)
4. Возраст ребёнка (UserIntake.user_intake_child_age)
5. Трек (UserIntake.user_intake_track) — выбор кнопкой
6. Название работы (UserIntake.user_intake_title)
7. Описание работы (UserIntake.user_intake_description)
→ передача управления в ``user_files.py`` (state user_intake_files_collect)

Текстовые шаги регистрируются как state-handler'ы через
``handlers.common.register_state_handler`` — этот единственный на всё
приложение перехватчик свободного текста смотрит на
``message.state.current_state`` и роутит в нужный шаг (см.
``.cursor/rules/bot.mdc`` — «один default_message_handler на
приложение»).

Кнопка выбора трека — обычная ``@collector.command("/intake_track")``,
дополнительно защищается проверкой FSM-состояния, чтобы не сработать
на «протухшую» кнопку из старого диалога.

Возрастная категория **не запрашивается у участника** — вычисляется
автоматически из возраста через ``AgeCategory.from_age`` в момент
создания заявки.
"""
from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.models import Track
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import track_selection_bubbles
from states import UserIntake
from utils.bot_utils import reply_to_user, safe_answer_transient


collector = HandlerCollector()


# =====================================================================
# Ограничения и тексты ввода
# =====================================================================
#
# Хранятся как константы рядом с хендлерами — при необходимости можно
# вынести в config / конфигурируемые тексты заказчика.

_MIN_NAME_LEN = 2
_MAX_NAME_LEN = 200
_MIN_DIVISION_LEN = 2
_MAX_DIVISION_LEN = 200
_MIN_CHILD_NAME_LEN = 1
_MAX_CHILD_NAME_LEN = 100
_MIN_TITLE_LEN = 2
_MAX_TITLE_LEN = 200
_MIN_DESCRIPTION_LEN = 10
_MAX_DESCRIPTION_LEN = 2000

_AGE_MIN = 0
_AGE_MAX = 18

_PROMPT_PARENT_NAME = (
    "Шаг 1 из 7. Как вас зовут? Укажите ФИО полностью "
    "(например, «Иванова Анна Сергеевна»)."
)
_PROMPT_PARENT_DIVISION = (
    "Шаг 2 из 7. Укажите ваше подразделение (например, "
    "«Управление информационной безопасности»)."
)
_PROMPT_CHILD_NAME = (
    "Шаг 3 из 7. Как зовут ребёнка? Достаточно имени (например, «Маша»)."
)
_PROMPT_CHILD_AGE = (
    "Шаг 4 из 7. Сколько ребёнку полных лет? Введите число от 0 до 18."
)
_PROMPT_TRACK = (
    "Шаг 5 из 7. Выберите конкурсный трек одной из кнопок ниже:\n\n"
    "• Традиционное рисование — рисунок, открытка, коллаж, аппликация, "
    "комикс, поделка, 3D-модель, фотоинсталляция или другая визуальная "
    "работа.\n"
    "• ИИ-рисунок — итоговое изображение, созданное с помощью "
    "генеративного ИИ.\n"
    "• От руки к ИИ — общий коллаж «до/после»: ручная работа + ИИ-версия."
)
_PROMPT_TITLE = "Шаг 6 из 7. Введите название работы."
_PROMPT_DESCRIPTION_BASE = (
    "Шаг 7 из 7. Опишите работу: что изображено и почему "
    "ребёнок выбрал эту тему."
)
_PROMPT_DESCRIPTION_HANDMADE_TO_AI = (
    "Шаг 7 из 7. Опишите работу.\n\n"
    "Коротко напишите, что было в ручной работе и как ИИ "
    "помог переосмыслить или развить идею."
)


# =====================================================================
# Шаг 1: ФИО родителя
# =====================================================================


async def _handle_parent_full_name(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 1: ФИО родителя (обязательно).

    Минимальная валидация — длина и непустота. Имя содержит пробелы,
    кириллицу, дефисы, поэтому регулярные ограничения utils.validation
    не применяем (там запрет на дефис).
    """
    body = (message.body or "").strip()
    if len(body) < _MIN_NAME_LEN or len(body) > _MAX_NAME_LEN:
        await safe_answer_transient(
            message,
            bot,
            (
                "ФИО слишком короткое или слишком длинное. "
                "Введите ФИО полностью (от 2 до 200 символов)."
            ),
        )
        return

    fsm = message.state.fsm
    await fsm.update_data(parent_full_name=body)
    await fsm.set_state(UserIntake.user_intake_parent_division)
    await reply_to_user(
        message, bot, _PROMPT_PARENT_DIVISION, bubbles=BubbleMarkup()
    )


# =====================================================================
# Шаг 2: Подразделение
# =====================================================================


async def _handle_parent_division(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 2: подразделение родителя (обязательно)."""
    body = (message.body or "").strip()
    if len(body) < _MIN_DIVISION_LEN or len(body) > _MAX_DIVISION_LEN:
        await safe_answer_transient(
            message,
            bot,
            (
                "Подразделение слишком короткое или слишком длинное "
                "(допустимо от 2 до 200 символов)."
            ),
        )
        return

    fsm = message.state.fsm
    await fsm.update_data(parent_division=body)
    await fsm.set_state(UserIntake.user_intake_child_name)
    await reply_to_user(
        message, bot, _PROMPT_CHILD_NAME, bubbles=BubbleMarkup()
    )


# =====================================================================
# Шаг 3: Имя ребёнка
# =====================================================================


async def _handle_child_name(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 3: имя ребёнка (обязательно)."""
    body = (message.body or "").strip()
    if len(body) < _MIN_CHILD_NAME_LEN or len(body) > _MAX_CHILD_NAME_LEN:
        await safe_answer_transient(
            message,
            bot,
            (
                "Имя ребёнка должно быть от 1 до 100 символов. "
                "Введите имя ещё раз."
            ),
        )
        return

    fsm = message.state.fsm
    await fsm.update_data(child_name=body)
    await fsm.set_state(UserIntake.user_intake_child_age)
    await reply_to_user(
        message, bot, _PROMPT_CHILD_AGE, bubbles=BubbleMarkup()
    )


# =====================================================================
# Шаг 4: Возраст ребёнка
# =====================================================================


async def _handle_child_age(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 4: возраст ребёнка, целое число 0..18.

    На некорректный ввод — транзиентное сообщение об ошибке, состояние
    не меняется. Возрастная категория из возраста вычисляется позже
    (см. ``AgeCategory.from_age``).
    """
    body = (message.body or "").strip()
    try:
        age = int(body)
    except ValueError:
        await safe_answer_transient(
            message,
            bot,
            "Возраст должен быть целым числом от 0 до 18. Попробуйте ещё раз.",
        )
        return

    if age < _AGE_MIN or age > _AGE_MAX:
        await safe_answer_transient(
            message,
            bot,
            (
                f"Возраст должен быть от {_AGE_MIN} до {_AGE_MAX} лет. "
                "Введите число ещё раз."
            ),
        )
        return

    fsm = message.state.fsm
    await fsm.update_data(child_age=age)
    await fsm.set_state(UserIntake.user_intake_track)
    await reply_to_user(
        message, bot, _PROMPT_TRACK, bubbles=track_selection_bubbles()
    )


# =====================================================================
# Шаг 5: Трек (кнопка) + текстовый fallback
# =====================================================================


@collector.command(
    "/intake_track",
    description="Выбор конкурсного трека",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_intake_track(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 5: выбор конкурсного трека кнопкой.

    Защищены два сценария:
    1. Старая «протухшая» кнопка из прошлого диалога — handler не в
       состоянии user_intake_track → молча игнорируем
       (логируем DEBUG, чтобы было видно при отладке).
    2. Пустой ``data`` — мягкое сообщение «выбери кнопкой».
    """
    fsm = message.state.fsm
    current = await fsm.get_state()
    if current != UserIntake.user_intake_track.value:
        logger.debug(
            "intake_track вне ожидаемого состояния — игнорируем",
            current=current,
            sender=str(message.sender.huid),
        )
        return

    track_name = (message.data or {}).get("track")
    if not track_name:
        await safe_answer_transient(
            message,
            bot,
            "Выберите трек одной из кнопок ниже.",
            bubbles=track_selection_bubbles(),
        )
        return

    try:
        track = Track[track_name]
    except KeyError:
        logger.warning(
            "intake_track: некорректное значение data.track",
            value=track_name,
        )
        await safe_answer_transient(
            message,
            bot,
            "Не удалось распознать трек. Попробуйте ещё раз.",
            bubbles=track_selection_bubbles(),
        )
        return

    await fsm.update_data(track=track.name)
    await fsm.set_state(UserIntake.user_intake_title)
    logger.debug(
        "Выбран трек",
        track=track.name,
        sender=str(message.sender.huid),
    )
    await reply_to_user(message, bot, _PROMPT_TITLE, bubbles=BubbleMarkup())


async def _handle_track_text_fallback(
    message: IncomingMessage, bot: Bot
) -> None:
    """Текстовый ввод в состоянии выбора трека — мягко возвращаем клавиатуру."""
    await reply_to_user(
        message,
        bot,
        _PROMPT_TRACK,
        bubbles=track_selection_bubbles(),
    )


# =====================================================================
# Шаг 6: Название работы
# =====================================================================


async def _handle_title(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 6: название работы (обязательно)."""
    body = (message.body or "").strip()
    if len(body) < _MIN_TITLE_LEN or len(body) > _MAX_TITLE_LEN:
        await safe_answer_transient(
            message,
            bot,
            (
                "Название слишком короткое или слишком длинное "
                "(допустимо от 2 до 200 символов)."
            ),
        )
        return

    fsm = message.state.fsm
    data = await fsm.get_data()
    track_name = data.get("track")
    description_prompt = _PROMPT_DESCRIPTION_BASE
    if track_name == Track.HANDMADE_TO_AI.name:
        description_prompt = _PROMPT_DESCRIPTION_HANDMADE_TO_AI

    await fsm.update_data(title=body)
    await fsm.set_state(UserIntake.user_intake_description)
    await reply_to_user(message, bot, description_prompt, bubbles=BubbleMarkup())


# =====================================================================
# Шаг 7: Описание работы → передача в user_files
# =====================================================================


async def _handle_description(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 7: описание работы (обязательно).

    После сохранения описания состояние переходит в
    ``user_intake_files_collect`` и управление передаётся в
    ``user_files.prompt_for_files`` — там формулируется инструкция по
    загрузке файлов в зависимости от трека.
    """
    body = (message.body or "").strip()
    if (
        len(body) < _MIN_DESCRIPTION_LEN
        or len(body) > _MAX_DESCRIPTION_LEN
    ):
        await safe_answer_transient(
            message,
            bot,
            (
                "Описание должно быть от 10 до 2000 символов. "
                "Опишите работу подробнее."
            ),
        )
        return

    fsm = message.state.fsm
    data = await fsm.get_data()
    track_name = data.get("track")
    if not track_name:
        # На случай, если состояние сбилось (например, FSM почистился)
        logger.warning(
            "user_intake_description: track отсутствует в FSM — fallback",
            data_keys=list(data.keys()),
        )
        await fsm.set_state(UserIntake.user_intake_track)
        await reply_to_user(
            message, bot, _PROMPT_TRACK, bubbles=track_selection_bubbles()
        )
        return

    await fsm.update_data(description=body)
    await fsm.set_state(UserIntake.user_intake_files_collect)

    from handlers.user_files import prompt_for_files

    try:
        track = Track[track_name]
    except KeyError:
        logger.exception("user_intake_description: некорректный track в FSM")
        await fsm.set_state(UserIntake.user_intake_track)
        await reply_to_user(
            message, bot, _PROMPT_TRACK, bubbles=track_selection_bubbles()
        )
        return

    await prompt_for_files(message, bot, track)


# =====================================================================
# Регистрация state-handler'ов в диспетчере common.default_message_handler
# =====================================================================

register_state_handler(
    UserIntake.user_intake_parent_full_name.value, _handle_parent_full_name
)
register_state_handler(
    UserIntake.user_intake_parent_division.value, _handle_parent_division
)
register_state_handler(
    UserIntake.user_intake_child_name.value, _handle_child_name
)
register_state_handler(UserIntake.user_intake_child_age.value, _handle_child_age)
register_state_handler(
    UserIntake.user_intake_track.value, _handle_track_text_fallback
)
register_state_handler(UserIntake.user_intake_title.value, _handle_title)
register_state_handler(
    UserIntake.user_intake_description.value, _handle_description
)


