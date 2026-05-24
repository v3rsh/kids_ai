"""
FSM-flow анкеты участника.

Поэтапный сбор полей (горячий путь, ФИО и подразделение пришли из CTS):
1. Контакт (UserIntake.user_intake_parent_contact)
2. Имя ребёнка (UserIntake.user_intake_child_name)
3. Возраст ребёнка (UserIntake.user_intake_child_age)
4. Трек (UserIntake.user_intake_track) — выбор кнопкой
5. Название работы (UserIntake.user_intake_title)
6. Описание работы (UserIntake.user_intake_description)
→ передача управления в ``user_files.py`` (state user_intake_files_collect)

Fallback-шаги (включаются, только если CTS не вернул соответствующее поле):
- user_intake_parent_full_name_fb — ручной ввод ФИО, если CTS пустой
- user_intake_parent_division_fb — ручной ввод подразделения

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
import re
from typing import Tuple

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

_MIN_CONTACT_LEN = 4
_MAX_CONTACT_LEN = 100
_MIN_PHONE_DIGITS = 10
_MAX_PHONE_DIGITS = 15

_AGE_MIN = 0
_AGE_MAX = 18

# Минимальный email-regex: запрещает пробелы и требует один '@' с
# доменом и точкой. Этого достаточно для UX-проверки; жёсткой
# RFC-валидации не делаем — почту всё равно проверит модератор/MTA.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_NORMALIZE_RE = re.compile(r"[^\d+]")

# 6 нумерованных шагов. Загрузка файлов отдельным этапом и нумеруется
# не как «Шаг X», а как самостоятельный экран (см. user_files.py).
_PROMPT_PARENT_CONTACT_BASE = (
    "Шаг 1 из 6. Контакт для связи — укажите телефон "
    "(например, «+79991234567») или email."
)
_PROMPT_CHILD_NAME = (
    "Шаг 2 из 6. Как зовут ребёнка? Достаточно имени (например, «Маша»)."
)
_PROMPT_CHILD_AGE = (
    "Шаг 3 из 6. Сколько ребёнку полных лет? Введите число от 0 до 18."
)
_PROMPT_TRACK = (
    "Шаг 4 из 6. Выберите конкурсный трек одной из кнопок ниже:\n\n"
    "• Традиционное рисование — рисунок, открытка, коллаж, аппликация, "
    "комикс, поделка, 3D-модель, фотоинсталляция или другая визуальная "
    "работа.\n"
    "• ИИ-рисунок — итоговое изображение, созданное с помощью "
    "генеративного ИИ.\n"
    "• От руки к ИИ — общий коллаж «до/после»: ручная работа + ИИ-версия."
)
_PROMPT_TITLE = "Шаг 5 из 6. Введите название работы."
_PROMPT_DESCRIPTION_BASE = (
    "Шаг 6 из 6. Опишите работу: что изображено и почему "
    "ребёнок выбрал эту тему."
)
_PROMPT_DESCRIPTION_HANDMADE_TO_AI = (
    "Шаг 6 из 6. Опишите работу.\n\n"
    "Коротко напишите, что было в ручной работе и как ИИ "
    "помог переосмыслить или развить идею."
)

# Fallback-приглашения (вне сквозной нумерации шагов — они появляются,
# только если CTS не дал данных, и предшествуют шагу 1).
_PROMPT_PARENT_FULL_NAME_FB = (
    "Не нашли ваше ФИО в профиле eXpress. Укажите ФИО полностью "
    "(например, «Иванова Анна Сергеевна»)."
)
_PROMPT_PARENT_DIVISION_FB = (
    "Не нашли ваше подразделение в профиле eXpress. Введите его "
    "вручную (например, «Управление информационной безопасности»)."
)


# =====================================================================
# Шаг 1: Контакт для связи (email или телефон)
# =====================================================================


def _build_contact_prompt(data: dict) -> str:
    """Сформировать приглашение «Контакт» c подсказкой из CTS-профиля.

    Если в FSM есть ``cts_email_hint`` / ``cts_ip_phone_hint`` /
    ``cts_other_phone_hint`` — добавим блок «Из вашего профиля eXpress».
    Пользователь всё равно вводит контакт текстом; CTS-данные служат
    подсказкой и параллельно живут в ``users.email/ip_phone/other_phone``
    для аудита и проактивных DM.
    """
    hints = []
    email_hint = data.get("cts_email_hint")
    if email_hint:
        hints.append(f"• email: {email_hint}")
    ip_phone_hint = data.get("cts_ip_phone_hint")
    if ip_phone_hint:
        hints.append(f"• внутренний телефон: {ip_phone_hint}")
    other_phone_hint = data.get("cts_other_phone_hint")
    if other_phone_hint:
        hints.append(f"• телефон: {other_phone_hint}")
    if not hints:
        return _PROMPT_PARENT_CONTACT_BASE
    return (
        _PROMPT_PARENT_CONTACT_BASE
        + "\n\nИз вашего профиля eXpress (можете указать любой свой):\n"
        + "\n".join(hints)
    )


def _detect_contact_type(value: str) -> Tuple[str, str] | None:
    """Определить тип контакта и нормализовать значение.

    Returns:
        ``(normalized, "email")`` для валидного email;
        ``(normalized, "phone")`` для валидного телефона;
        ``None`` если не похоже ни на то, ни на другое.
    """
    text = (value or "").strip()
    if not text:
        return None

    if "@" in text:
        candidate = text.lower()
        if _EMAIL_RE.match(candidate):
            return candidate, "email"
        return None

    # Телефон: оставляем только '+' и цифры. '+' допускается только
    # один раз и только в начале — иначе считаем мусором.
    cleaned = _PHONE_NORMALIZE_RE.sub("", text)
    if cleaned.count("+") > 1 or (
        "+" in cleaned and not cleaned.startswith("+")
    ):
        return None
    digits = cleaned.lstrip("+")
    if not digits.isdigit():
        return None
    if len(digits) < _MIN_PHONE_DIGITS or len(digits) > _MAX_PHONE_DIGITS:
        return None
    return cleaned, "phone"


async def _handle_parent_contact(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 1: контакт для связи — телефон или email.

    Автоопределение типа по наличию '@'. На ошибку валидации —
    транзиентное сообщение, состояние не меняется.
    """
    body = (message.body or "").strip()
    if len(body) < _MIN_CONTACT_LEN or len(body) > _MAX_CONTACT_LEN:
        await safe_answer_transient(
            message,
            bot,
            (
                f"Контакт должен быть длиной от {_MIN_CONTACT_LEN} до "
                f"{_MAX_CONTACT_LEN} символов. Введите телефон или email."
            ),
        )
        return

    detected = _detect_contact_type(body)
    if detected is None:
        await safe_answer_transient(
            message,
            bot,
            (
                "Не удалось распознать контакт. Введите телефон в формате "
                "«+79991234567» или email вида «name@example.com»."
            ),
        )
        return

    normalized, contact_type = detected
    fsm = message.state.fsm
    await fsm.update_data(
        parent_contact=normalized,
        parent_contact_type=contact_type,
    )
    await fsm.set_state(UserIntake.user_intake_child_name)
    await reply_to_user(
        message, bot, _PROMPT_CHILD_NAME, bubbles=BubbleMarkup()
    )


# =====================================================================
# Fallback-шаги: ФИО / подразделение, если CTS не дал данных
# =====================================================================


async def _handle_parent_full_name_fb(
    message: IncomingMessage, bot: Bot
) -> None:
    """Fallback: ручной ввод ФИО, если CTS вернул пустое имя.

    После успешной валидации проверяем, нужен ли ещё fallback-шаг для
    подразделения, и идём дальше — либо к нему, либо сразу к шагу
    «Контакт».
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
    data = await fsm.get_data()
    await _advance_after_fallback(message, bot, data)


async def _handle_parent_division_fb(
    message: IncomingMessage, bot: Bot
) -> None:
    """Fallback: ручной ввод подразделения, если CTS вернул пустое."""
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
    data = await fsm.get_data()
    await _advance_after_fallback(message, bot, data)


async def _advance_after_fallback(
    message: IncomingMessage, bot: Bot, data: dict
) -> None:
    """Перейти к следующему незаполненному fallback-шагу или к контакту."""
    fsm = message.state.fsm
    if not (data.get("parent_full_name") or "").strip():
        await fsm.set_state(UserIntake.user_intake_parent_full_name_fb)
        await reply_to_user(
            message, bot, _PROMPT_PARENT_FULL_NAME_FB, bubbles=BubbleMarkup()
        )
        return
    if not (data.get("parent_division") or "").strip():
        await fsm.set_state(UserIntake.user_intake_parent_division_fb)
        await reply_to_user(
            message, bot, _PROMPT_PARENT_DIVISION_FB, bubbles=BubbleMarkup()
        )
        return
    await fsm.set_state(UserIntake.user_intake_parent_contact)
    await reply_to_user(
        message, bot, _build_contact_prompt(data), bubbles=BubbleMarkup()
    )


# =====================================================================
# Шаг 2: Имя ребёнка
# =====================================================================


async def _handle_child_name(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 2: имя ребёнка (обязательно)."""
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
# Шаг 3: Возраст ребёнка
# =====================================================================


async def _handle_child_age(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 3: возраст ребёнка, целое число 0..18.

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
# Шаг 4: Трек (кнопка) + текстовый fallback
# =====================================================================


@collector.command(
    "/intake_track",
    description="Выбор конкурсного трека",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_intake_track(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 4: выбор конкурсного трека кнопкой.

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
# Шаг 5: Название работы
# =====================================================================


async def _handle_title(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 5: название работы (обязательно)."""
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
# Шаг 6: Описание работы → передача в user_files
# =====================================================================


async def _handle_description(message: IncomingMessage, bot: Bot) -> None:
    """Шаг 6: описание работы (обязательно).

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
    UserIntake.user_intake_parent_contact.value, _handle_parent_contact
)
register_state_handler(
    UserIntake.user_intake_parent_full_name_fb.value, _handle_parent_full_name_fb
)
register_state_handler(
    UserIntake.user_intake_parent_division_fb.value, _handle_parent_division_fb
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


# Экспорт «горячих» промптов для cmd_apply (горячий путь и fallback'и).
__all__ = [
    "collector",
    "_PROMPT_PARENT_FULL_NAME_FB",
    "_PROMPT_PARENT_DIVISION_FB",
    "_build_contact_prompt",
]
