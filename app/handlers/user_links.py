"""
Резервный сценарий приёма работ по ссылкам (§33.6 ТЗ).

State: ``UserIntake.user_intake_link_collect``.

Жизненный цикл шага:

1. ``handlers.user_confirm.cmd_submit`` создаёт заявку с
   ``cloud_link=None``, выдаёт реальный BR-ID, переводит FSM в
   ``user_intake_link_collect`` и вызывает ``prompt_for_cloud_link`` —
   бот показывает инструкцию по §33.6.2 с уже известным BR-ID и
   именем папки на основе ФИО родителя + имени ребёнка.
2. Участник присылает текст со ссылкой → state-handler:
   - валидирует URL через ``utils.cloud_link.parse_cloud_link``
     (формальная sanity, без HTTP-запроса — у бота нет интернета);
   - вызывает ``applications.set_application_cloud_link`` (UPDATE +
     reload с ``selectinload(files)``);
   - пишет ``meta.txt`` (теперь в нём есть ``cloud_link``);
   - шлёт уведомления участнику и в чат модерации (это первый момент,
     когда у модератора есть полная карточка с URL — раньше слать
     было бессмысленно);
   - очищает FSM и возвращает участника в главное меню.
3. Если присылают файл вместо текста — отвечаем transient'ом «нужна
   ссылка, не файл», state не меняем.

Resume-сценарий (``cmd_resume_link``): кнопка из «Мои заявки» для
заявок ``LINKS`` с пустым ``cloud_link`` (Redis-FSM мог пропасть после
рестарта). Восстанавливает state и подгружает данные из БД, чтобы
``prompt_for_cloud_link`` показал ту же инструкцию.
"""
from __future__ import annotations

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.models import Application, IntakeMode, Track
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import back_to_main_menu_bubbles, main_menu_bubbles
from services import applications as applications_service
from services import notifications as notifications_service
from services import storage as storage_service
from states import UserIntake
from utils.bot_utils import reply_to_user, safe_answer_transient
from utils.cloud_link import parse_cloud_link


collector = HandlerCollector()


# =====================================================================
# Текст инструкции (по §33.6.2 ТЗ)
# =====================================================================

_INTRO = (
    "**Из-за технических ограничений** сервер конкурса временно не "
    "принимает файлы. Чтобы подать работу, выложите её в любое "
    "доступное облако (Я.Диск, iCloud Drive, Google Drive — любой "
    "сервис на ваш выбор) и пришлите одну ссылку."
)

_FILENAME_HINT_BY_TRACK: dict[Track, str] = {
    Track.TRADITIONAL: (
        "• для рисунка/открытки/коллажа/аппликации/комикса — один файл "
        "`{br_id}_original.<ext>`\n"
        "• для поделки/3D-модели/фотоинсталляции — от 2 до 4 файлов "
        "`{br_id}_angle-1.<ext>`, `{br_id}_angle-2.<ext>` …"
    ),
    Track.AI: "• один файл `{br_id}_ai-image.<ext>`",
    Track.HANDMADE_TO_AI: (
        "• один файл-коллаж «до/после» `{br_id}_diptych.<ext>`"
    ),
}

_INSTRUCTION_TEMPLATE = (
    "{intro}\n\n"
    "**Ваш номер заявки:** `{br_id}`\n\n"
    "Чтобы мы корректно собрали работы:\n\n"
    "1. Создайте у себя в облаке папку и назовите её строго так:\n"
    "   `{folder_name}`\n\n"
    "2. Положите в папку файлы по шаблону для вашего трека ({track_label}):\n"
    "{files_hint}\n\n"
    "   _Расширение `<ext>` — реальный формат файла "
    "(jpg/jpeg/png/heic/webp/pdf)._\n\n"
    "3. Откройте доступ к папке **по ссылке, на чтение, без авторизации**.\n\n"
    "4. Пришлите ссылку этим сообщением одной строкой."
)


_FILE_REJECT_TEXT = (
    "Сейчас сервер не принимает файлы — нужна **ссылка** на облачную "
    "папку. Пришлите ссылку из адресной строки браузера одним "
    "сообщением."
)


def _build_folder_name(app: Application) -> str:
    """Имя облачной папки по §33.6.2 — то же, что хранит сервер.

    Делегируем helper'ам ``services.storage`` (``_split_parent_name``
    и ``_format_application_folder_name`` через построитель), чтобы
    шаблон в инструкции и фактическое имя локальной папки совпадали
    1-в-1 (модератор будет искать по имени из реестра).
    """
    # Прямой вызов приватного helper'а из storage сознателен:
    # _format_application_folder_name детерминирован и обновляется
    # вместе со схемой реестра, дублировать логику здесь нельзя.
    from services.storage import _format_application_folder_name

    return _format_application_folder_name(app)


def _build_instruction(app: Application, track: Track) -> str:
    """Сформировать полный текст инструкции с подстановкой данных заявки."""
    folder_name = _build_folder_name(app)
    files_hint = _FILENAME_HINT_BY_TRACK[track].format(br_id=app.br_id)
    return _INSTRUCTION_TEMPLATE.format(
        intro=_INTRO,
        br_id=app.br_id,
        folder_name=folder_name,
        track_label=track.value,
        files_hint=files_hint,
    )


# =====================================================================
# Публичная точка входа (вызывается из user_confirm.cmd_submit)
# =====================================================================


async def prompt_for_cloud_link(
    message: IncomingMessage,
    bot: Bot,
    *,
    application: Application,
    data: dict,
    track: Track,
) -> None:
    """Показать инструкцию по §33.6.2 и ждать URL.

    Args:
        message: входящее сообщение (нужен для reply_to_user).
        bot: pybotx Bot.
        application: только что созданная заявка с ``cloud_link=None``.
        data: текущая FSM-data (для возможных будущих подсказок —
            пока не используется, оставлено для расширения).
        track: трек заявки (нужен для шаблона имён файлов).
    """
    text = _build_instruction(application, track)
    await reply_to_user(message, bot, text, bubbles=BubbleMarkup())


# =====================================================================
# State-handler: приём URL
# =====================================================================


async def _handle_link_collect(
    message: IncomingMessage, bot: Bot
) -> None:
    """Обработка входящего сообщения в состоянии ожидания ссылки.

    - файл → transient (нужна ссылка, не файл);
    - валидная ссылка → UPDATE cloud_link → meta.txt → нотификации →
      сброс FSM → главное меню;
    - невалидный текст → transient с пояснением.
    """
    if message.file is not None:
        await safe_answer_transient(message, bot, _FILE_REJECT_TEXT)
        return

    fsm = message.state.fsm
    raw = (message.body or "").strip()
    url, error = parse_cloud_link(raw)
    if url is None:
        assert error is not None
        await safe_answer_transient(message, bot, error.message)
        return

    data = await fsm.get_data()
    br_id = (data.get("br_id") or "").strip()
    if not br_id:
        # FSM «уехал» — например, юзер дошёл сюда из старого FSM-снепшота
        # без сохранённого br_id. Просим перейти через /menu_my_applications.
        logger.warning(
            "user_link_collect: в FSM нет br_id — нечего обновлять",
            parent_huid=str(message.sender.huid),
            keys=list(data.keys()),
        )
        await fsm.clear()
        await reply_to_user(
            message,
            bot,
            (
                "Не нашли вашу заявку. Откройте «Мои заявки» и нажмите "
                "«Прислать ссылку на папку» рядом с нужной заявкой."
            ),
            bubbles=main_menu_bubbles(huid=message.sender.huid),
        )
        return

    try:
        application = await applications_service.set_application_cloud_link(
            br_id=br_id,
            url=url,
        )
    except ValueError as exc:
        logger.warning(
            "user_link_collect: не удалось обновить cloud_link",
            br_id=br_id,
            error=str(exc),
        )
        await safe_answer_transient(
            message,
            bot,
            (
                "Не удалось сохранить ссылку (заявка не найдена). "
                "Попробуйте позже или обратитесь к организаторам."
            ),
        )
        return
    except Exception:
        logger.exception(
            "user_link_collect: сбой UPDATE cloud_link",
            br_id=br_id,
        )
        await safe_answer_transient(
            message,
            bot,
            (
                "Временная техническая ошибка при сохранении ссылки. "
                "Попробуйте отправить ссылку ещё раз через минуту."
            ),
        )
        return

    # meta.txt — пишем сейчас, чтобы в нём появилось поле
    # "Ссылка на папку (cloud)". В LINKS-режиме файлов нет, передаём
    # пустой список — _format_files_block корректно отрисует «(нет)».
    try:
        await storage_service.write_meta_txt(application, files=[])
    except NotImplementedError:
        logger.warning(
            "write_meta_txt ещё не реализован, пропускаем",
            br_id=br_id,
        )
    except Exception:
        logger.exception(
            "user_link_collect: сбой write_meta_txt",
            br_id=br_id,
        )

    # Нотификации — только сейчас, когда у модератора в карточке
    # уже есть URL. Сбои не критичны: заявка в БД, ссылка сохранена.
    try:
        await notifications_service.notify_participant_accepted(
            bot, application
        )
    except NotImplementedError:
        logger.warning(
            "notify_participant_accepted ещё не реализован",
            br_id=br_id,
        )
    except Exception:
        logger.exception(
            "user_link_collect: сбой нотификации участнику",
            br_id=br_id,
        )

    try:
        await notifications_service.notify_moderation_chat_new_application(
            bot, application
        )
    except NotImplementedError:
        logger.warning(
            "notify_moderation_chat_new_application ещё не реализован",
            br_id=br_id,
        )
    except Exception:
        logger.exception(
            "user_link_collect: сбой нотификации в чат модерации",
            br_id=br_id,
        )

    await fsm.clear()
    logger.info(
        "Ссылка на работу сохранена, LINKS-flow завершён",
        br_id=br_id,
        parent_huid=str(message.sender.huid),
    )

    await reply_to_user(
        message,
        bot,
        (
            f"{notifications_service.ACCEPTED_TEMPLATE}\n\n"
            f"**Номер заявки:** {br_id}\n"
            f"**Ссылка:** {url}"
        ),
        bubbles=main_menu_bubbles(huid=message.sender.huid),
    )


# =====================================================================
# Resume-сценарий: «Прислать ссылку на папку» из «Мои заявки»
# =====================================================================


@collector.command(
    "/resume_link",
    description="Прислать ссылку для заявки LINKS",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_resume_link(message: IncomingMessage, bot: Bot) -> None:
    """Восстановить шаг сбора ссылки для незавершённой LINKS-заявки.

    Вызывается из карточки заявки в «Мои заявки» (см.
    ``keyboards.my_application_detail_bubbles``) для заявок
    ``intake_mode = LINKS`` + ``cloud_link is None``.

    Данные FSM (br_id) могли потеряться после рестарта Redis — здесь
    мы их восстанавливаем из БД.
    """
    br_id_raw = (message.data or {}).get("br_id") if message.data else None
    br_id = (br_id_raw or "").strip().upper()
    if not br_id:
        await safe_answer_transient(
            message,
            bot,
            "Не удалось определить заявку. Откройте её карточку ещё раз.",
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
            bubbles=back_to_main_menu_bubbles(),
        )
        return

    if app.intake_mode is not IntakeMode.LINKS:
        await safe_answer_transient(
            message,
            bot,
            (
                "Эта заявка подана с файлами — ссылка не требуется. "
                "Откройте «Мои заявки» для деталей."
            ),
        )
        return

    if app.cloud_link:
        await safe_answer_transient(
            message,
            bot,
            (
                "Ссылка для этой заявки уже сохранена. Если нужно "
                "поменять — напишите модераторам через «Контакты "
                "организаторов»."
            ),
        )
        return

    fsm = message.state.fsm
    await fsm.set_state(UserIntake.user_intake_link_collect)
    await fsm.update_data(br_id=app.br_id)
    logger.info(
        "Resume LINKS-flow для незавершённой заявки",
        br_id=app.br_id,
        parent_huid=str(message.sender.huid),
    )
    await prompt_for_cloud_link(
        message, bot, application=app, data={}, track=app.track
    )


# =====================================================================
# Регистрация state-handler'а
# =====================================================================

register_state_handler(
    UserIntake.user_intake_link_collect.value, _handle_link_collect
)


__all__ = [
    "collector",
    "prompt_for_cloud_link",
]
