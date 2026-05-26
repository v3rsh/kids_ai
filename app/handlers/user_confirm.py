"""
Согласия, финальное резюме, отправка заявки.

Состояния FSM:
- ``user_intake_consents`` — отрисовка чекбоксов согласий, кнопка
  «Подтвердить и продолжить» появляется только когда оба отмечены.
- ``user_intake_review`` — финальное резюме: кнопки
  «Отправить заявку» / «Заполнить заново».

При нажатии «Отправить заявку»:
1. ``services.applications.create_application`` — создаёт строку в БД,
   присваивает ``br_id``, проверяет возможный дубль по
   ``(parent_huid, title)``.
2. ``services.storage.create_application_folder`` + последовательное
   ``services.storage.rename_and_save_file`` для каждого файла из
   временного каталога (см. ``user_files.py``).
3. ``services.applications.register_application_files`` — INSERT в
   ``application_files`` и reload ``Application`` с ``selectinload``
   (иначе ``write_meta_txt`` упадёт с ``DetachedInstanceError`` на
   ``app.files``).
4. ``services.storage.write_meta_txt`` + ``write_description_txt``.
5. ``services.notifications.notify_participant_accepted`` и
   ``services.notifications.notify_moderation_chat_new_application``.
6. Очистка FSM + временного каталога анкеты.

Все этапы 2–4 обёрнуты в общий try/except: на случай, если
зависимости storage/notifications недоступны (например, поломка диска
или чата модерации), запись в БД остаётся целой — пользователь видит
понятное сообщение об ошибке, никаких частичных эффектов не остаётся.

Актуальный режим приёма прокидывается из
``services.intake_mode.get_intake_mode()`` в ``create_application``,
чтобы заявка корректно записалась с тем режимом, который активен на
момент submit — реестр различает FILES/LINKS по полю «Команда/ссылка
просмотра файлов». Сам ссылочный UX (запрос URL вместо файла) —
отдельная задача, см. ``docs/backlog.md`` → «LINKS-UX».
"""
from typing import Iterable

from loguru import logger
from pybotx import Bot, BubbleMarkup, HandlerCollector, IncomingMessage

from database.models import AgeCategory, FileKind, Track
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from keyboards import consents_bubbles, final_confirm_bubbles, main_menu_bubbles
from services import applications as applications_service
from services import intake_mode as intake_mode_service
from services import notifications as notifications_service
from services import storage as storage_service
from states import UserIntake
from utils.bot_utils import reply_to_user, safe_answer_transient


collector = HandlerCollector()


# =====================================================================
# Тексты экранов согласий / резюме / уведомлений
# =====================================================================

_CONSENTS_PROMPT = (
    "**Согласия**\n\n"
    "Я подтверждаю, что ознакомился(лась) с правилами конкурса и "
    "согласен(на) с условиями участия.\n\n"
    "Я разрешаю публикацию имени ребёнка, возраста ребёнка и "
    "изображения конкурсной работы во внутренних материалах, связанных "
    "с проведением и подведением итогов конкурса.\n\n"
    "Отметьте оба пункта и нажмите «Подтвердить и продолжить»."
)

_REJECTED_TECH_TEMPLATE = (
    "Заявка не может быть принята: **{reason}**.\n\n"
    "Пожалуйста, исправьте данные или загрузите файл в подходящем формате."
)


# =====================================================================
# Шаг согласий
# =====================================================================


async def show_consents(message: IncomingMessage, bot: Bot) -> None:
    """Показать чекбоксы согласий с актуальным состоянием отметок.

    Вызывается:
    - из ``user_files`` при переходе после файла;
    - из самой ветки при перерисовке (toggle).
    """
    fsm = message.state.fsm
    data = await fsm.get_data()
    rules_checked = bool(data.get("consent_rules"))
    publication_checked = bool(data.get("consent_publication"))
    await reply_to_user(
        message,
        bot,
        _CONSENTS_PROMPT,
        bubbles=consents_bubbles(
            rules_checked=rules_checked,
            publication_checked=publication_checked,
        ),
    )


@collector.command(
    "/intake_consent_toggle",
    description="Переключить чекбокс согласия",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_consent_toggle(message: IncomingMessage, bot: Bot) -> None:
    """Toggle одного из двух чекбоксов и перерисовка экрана согласий."""
    fsm = message.state.fsm
    current = await fsm.get_state()
    if current != UserIntake.user_intake_consents.value:
        logger.debug(
            "intake_consent_toggle вне состояния согласий — игнорируем",
            current=current,
        )
        return

    key = (message.data or {}).get("key")
    if key not in ("rules", "publication"):
        logger.warning("intake_consent_toggle: некорректный data.key", key=key)
        return

    data_key = f"consent_{key}"
    data = await fsm.get_data()
    new_value = not bool(data.get(data_key))
    await fsm.update_data(**{data_key: new_value})
    logger.debug(
        "Переключён чекбокс согласия",
        key=key,
        new_value=new_value,
        parent_huid=str(message.sender.huid),
    )
    await show_consents(message, bot)


@collector.command(
    "/intake_consents_confirm",
    description="Подтвердить согласия",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_consents_confirm(
    message: IncomingMessage, bot: Bot
) -> None:
    """Кнопка «Подтвердить и продолжить» — переход к финальному резюме."""
    fsm = message.state.fsm
    current = await fsm.get_state()
    if current != UserIntake.user_intake_consents.value:
        logger.debug(
            "intake_consents_confirm вне состояния согласий — игнорируем",
            current=current,
        )
        return

    data = await fsm.get_data()
    if not (data.get("consent_rules") and data.get("consent_publication")):
        await safe_answer_transient(
            message,
            bot,
            _REJECTED_TECH_TEMPLATE.format(
                reason="не подтверждены обязательные согласия"
            ),
        )
        return

    await fsm.set_state(UserIntake.user_intake_review)
    await _show_review(message, bot)


# =====================================================================
# Финальное резюме
# =====================================================================


def _build_review_text(data: dict) -> str:
    """Сформировать текст финального резюме перед отправкой заявки."""
    try:
        track = Track[data["track"]]
    except (KeyError, TypeError):
        track_label = "?"
    else:
        track_label = track.value

    child_age = data.get("child_age")
    try:
        age_category_label = (
            AgeCategory.from_age(int(child_age)).value if child_age else "?"
        )
    except (ValueError, TypeError):
        age_category_label = "?"

    files = data.get("files") or []
    return (
        "**Проверьте данные заявки:**\n\n"
        f"**Родитель:** {data.get('parent_full_name', '?')}\n"
        f"**Подразделение:** {data.get('parent_division', '?')}\n"
        f"**Контакт:** {data.get('parent_contact', '?')}\n\n"
        f"**Ребёнок:** {data.get('child_name', '?')}, "
        f"{child_age if child_age else '?'}\n"
        f"**Возрастная категория:** {age_category_label}\n\n"
        f"**Название работы:** {data.get('title', '?')}\n"
        f"**Трек:** {track_label}\n"
        f"**Описание:** {data.get('description', '?')}\n\n"
        f"**Файлы:** {len(files)}\n\n"
        "Если всё верно, нажмите «Отправить заявку»."
    )


async def _show_review(message: IncomingMessage, bot: Bot) -> None:
    fsm = message.state.fsm
    data = await fsm.get_data()
    await reply_to_user(
        message,
        bot,
        _build_review_text(data),
        bubbles=final_confirm_bubbles(),
    )


# =====================================================================
# Отправка заявки
# =====================================================================


@collector.command(
    "/intake_submit",
    description="Отправить заявку",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_submit(message: IncomingMessage, bot: Bot) -> None:
    """Финальный submit — создание заявки, сохранение файлов, нотификации."""
    fsm = message.state.fsm
    current = await fsm.get_state()
    if current != UserIntake.user_intake_review.value:
        logger.debug(
            "intake_submit вне состояния review — игнорируем",
            current=current,
        )
        return

    data = await fsm.get_data()
    parent_huid = message.sender.huid

    # ----- Финальные проверки заполненности (защита от рассинхрона) -----
    missing = _validate_required_fields(data)
    if missing:
        await safe_answer_transient(
            message,
            bot,
            _REJECTED_TECH_TEMPLATE.format(
                reason=f"не заполнены поля — {', '.join(missing)}"
            ),
        )
        return

    files_meta: list[dict] = data.get("files") or []
    if not files_meta:
        await safe_answer_transient(
            message,
            bot,
            _REJECTED_TECH_TEMPLATE.format(reason="не загружен файл"),
        )
        return

    # ----- Шаг 1: создание заявки в БД -----
    # Актуальный режим приёма читаем непосредственно перед submit:
    # модератор/админ/диск-монитор могли переключить FILES↔LINKS, пока
    # пользователь шёл по анкете. Сам UX сбора файла vs ссылки — пока
    # только FILES (см. backlog «LINKS-UX»), но в БД запись должна
    # отражать реальный режим, в котором заявка зафиксирована.
    current_intake_mode = await intake_mode_service.get_intake_mode()
    try:
        application = await applications_service.create_application(
            parent_huid=parent_huid,
            parent_full_name=data["parent_full_name"],
            parent_division=data["parent_division"],
            parent_ad_login=getattr(message.sender, "ad_login", None),
            parent_contact=data.get("parent_contact"),
            parent_contact_type=data.get("parent_contact_type"),
            child_name=data["child_name"],
            child_age=int(data["child_age"]),
            track_name=data["track"],
            title=data["title"],
            description=data["description"],
            intake_mode_value=current_intake_mode.value,
        )
    except ValueError as exc:
        logger.warning(
            "Не удалось создать заявку (валидация)",
            parent_huid=str(parent_huid),
            error=str(exc),
        )
        await safe_answer_transient(
            message,
            bot,
            _REJECTED_TECH_TEMPLATE.format(reason=str(exc)),
        )
        return
    except Exception:
        logger.exception(
            "Сбой при создании заявки",
            parent_huid=str(parent_huid),
        )
        await safe_answer_transient(
            message,
            bot,
            _REJECTED_TECH_TEMPLATE.format(
                reason="временная техническая ошибка, попробуйте ещё раз"
            ),
        )
        return

    br_id = application.br_id
    logger.info(
        "Заявка зарегистрирована, начинаем материализацию",
        br_id=br_id,
        parent_huid=str(parent_huid),
        files=len(files_meta),
    )

    # ----- Шаг 2: материализация файлов через services.storage -----
    storage_ok, application = await _materialize_files(
        application, files_meta, data
    )

    # ----- Шаг 3: уведомления участнику и в чат модерации -----
    await _send_notifications(bot, application, storage_ok)

    # ----- Шаг 4: финал — главное меню -----
    from handlers.user_files import _cleanup_intake_temp_dir

    _cleanup_intake_temp_dir(parent_huid)
    await fsm.clear()

    await reply_to_user(
        message,
        bot,
        (
            f"{notifications_service.ACCEPTED_TEMPLATE}\n\n"
            f"**Номер заявки:** {br_id}"
        ),
        bubbles=main_menu_bubbles(huid=parent_huid),
    )


@collector.command(
    "/intake_restart",
    description="Заполнить заново",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
async def cmd_restart(message: IncomingMessage, bot: Bot) -> None:
    """Кнопка «Заполнить заново» — сброс FSM и старт анкеты с нуля.

    Делегирует на ``handlers.user.cmd_apply``, чтобы переиспользовать
    единый flow с подтягиванием профиля из CTS (ФИО/подразделение) и
    переходом сразу к шагу «Контакт». Старый прямой переход в FSM-шаг
    «ФИО родителя» удалён вместе с самим шагом.
    """
    parent_huid = message.sender.huid
    fsm = message.state.fsm

    from handlers.user_files import _cleanup_intake_temp_dir

    _cleanup_intake_temp_dir(parent_huid)
    await fsm.clear()

    logger.info(
        "Анкета перезапущена пользователем",
        parent_huid=str(parent_huid),
    )

    from handlers.user import cmd_apply

    await cmd_apply(message, bot)


# =====================================================================
# Утилиты
# =====================================================================


_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("parent_full_name", "ФИО родителя"),
    ("parent_division", "Подразделение"),
    ("parent_contact", "Контакт для связи"),
    ("child_name", "Имя ребёнка"),
    ("child_age", "Возраст ребёнка"),
    ("track", "Трек"),
    ("title", "Название работы"),
    ("description", "Описание"),
)


def _validate_required_fields(data: dict) -> list[str]:
    """Вернуть список человекочитаемых имён незаполненных полей."""
    missing: list[str] = []
    for key, label in _REQUIRED_FIELDS:
        value = data.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(label)
    return missing


def _file_kinds_for_track(
    track: Track, count: int
) -> list[tuple[FileKind, int | None]]:
    """Сформировать ожидаемый список ``(kind, angle_no)`` по треку.

    Контракт соответствует схеме именования файлов в хранилище заявки:

    - TRADITIONAL: 1 файл → ORIGINAL; 2–4 → ANGLE с N=1..count.
      (Бот не различает «обычный 2D» от «3D» сам, поэтому 1 файл —
      всегда ORIGINAL, а 2+ — все ANGLE с порядковыми номерами.)
    - AI: ровно 1 → AI_IMAGE.
    - HANDMADE_TO_AI: ровно 1 → DIPTYCH.
    """
    if track == Track.TRADITIONAL:
        if count == 1:
            return [(FileKind.ORIGINAL, None)]
        return [(FileKind.ANGLE, idx + 1) for idx in range(count)]
    if track == Track.AI:
        return [(FileKind.AI_IMAGE, None)]
    if track == Track.HANDMADE_TO_AI:
        return [(FileKind.DIPTYCH, None)]
    raise ValueError(f"Неизвестный трек: {track!r}")


async def _materialize_files(
    application, files_meta: list[dict], data: dict
):
    """Перенос временных файлов в постоянное хранилище ``ATTACHMENTS_DIR``.

    Возвращает ``(storage_ok, application)``. ``application`` —
    перезагруженный из БД объект с ``selectinload(files)`` после
    регистрации файлов в ``application_files``; нужен, потому что
    объект, пришедший из ``create_application``, уже detached, и
    обращение к ``app.files`` (например, из ``write_meta_txt``)
    падает с ``DetachedInstanceError``.

    Если какая-то операция ``services.storage`` пока не реализована
    (``NotImplementedError``) или упала, логируем warning/exception
    и возвращаем ``storage_ok=False`` с исходным объектом. Запись в БД
    остаётся в любом случае — администратор сможет повторно донакатить
    файлы из временного каталога.
    """
    from pathlib import Path

    from config import ATTACHMENTS_DIR

    try:
        track = Track[data["track"]]
    except KeyError:
        logger.error("materialize_files: неверный track", data=data.get("track"))
        return False, application

    try:
        await storage_service.create_application_folder(application)
    except NotImplementedError:
        logger.warning(
            "services.storage.create_application_folder ещё не реализован, пропускаем",
            br_id=application.br_id,
        )
        return False, application
    except Exception:
        logger.exception(
            "Не удалось создать папку заявки",
            br_id=application.br_id,
        )
        return False, application

    plan: Iterable[tuple[FileKind, int | None]] = _file_kinds_for_track(
        track, len(files_meta)
    )

    file_specs: list[applications_service.ApplicationFileSpec] = []
    for meta, (kind, angle_no) in zip(files_meta, plan):
        try:
            dst_path = await storage_service.rename_and_save_file(
                application,
                kind,
                angle_no,
                Path(meta["temp_path"]),
                meta["original_filename"],
            )
        except NotImplementedError:
            logger.warning(
                "services.storage.rename_and_save_file ещё не реализован, пропускаем",
                br_id=application.br_id,
                file=meta.get("original_filename"),
            )
            return False, application
        except Exception:
            logger.exception(
                "Не удалось сохранить файл",
                br_id=application.br_id,
                file=meta.get("original_filename"),
            )
            return False, application

        try:
            relative_path = str(Path(dst_path).relative_to(ATTACHMENTS_DIR))
        except ValueError:
            relative_path = str(dst_path)

        file_specs.append(
            applications_service.ApplicationFileSpec(
                kind=kind,
                angle_no=angle_no,
                original_filename=meta["original_filename"],
                stored_filename=Path(dst_path).name,
                relative_path=relative_path,
                size_bytes=int(meta.get("size_bytes") or 0),
                mime_type=str(meta.get("mime_type") or "application/octet-stream"),
            )
        )

    try:
        application = await applications_service.register_application_files(
            br_id=application.br_id,
            files=file_specs,
        )
    except Exception:
        logger.exception(
            "Не удалось зарегистрировать файлы заявки в БД",
            br_id=application.br_id,
        )
        return False, application

    # `application.files` уже материализован через `register_application_files`
    # (populate_existing + list(...) до expunge), поэтому list(...) тут
    # безопасен и не триггерит lazy load на detached-объекте.
    materialized_files = list(application.files or [])
    writer_plan: tuple[tuple, ...] = (
        (storage_service.write_description_txt, "description.txt", {}),
        (
            storage_service.write_meta_txt,
            "meta.txt",
            {"files": materialized_files},
        ),
    )
    for writer, label, writer_kwargs in writer_plan:
        try:
            await writer(application, **writer_kwargs)
        except NotImplementedError:
            logger.warning(
                "services.storage.{} ещё не реализован, пропускаем",
                label,
                br_id=application.br_id,
            )
        except Exception:
            logger.exception(
                "Не удалось записать {}",
                label,
                br_id=application.br_id,
            )

    return True, application


async def _send_notifications(bot, application, storage_ok: bool) -> None:
    """Послать уведомление участнику и в чат модерации.

    Не критическая часть: сбои логируем, но не валим submit — заявка
    уже в БД, и модератор может работать с ней через ``/queue``.
    """
    try:
        await notifications_service.notify_participant_accepted(bot, application)
    except NotImplementedError:
        logger.warning(
            "services.notifications.notify_participant_accepted ещё не реализован",
            br_id=application.br_id,
        )
    except Exception:
        logger.exception(
            "Сбой нотификации участнику",
            br_id=application.br_id,
        )

    try:
        await notifications_service.notify_moderation_chat_new_application(
            bot, application
        )
    except NotImplementedError:
        logger.warning(
            "services.notifications.notify_moderation_chat_new_application ещё не реализован",
            br_id=application.br_id,
        )
    except Exception:
        logger.exception(
            "Сбой нотификации в чат модерации",
            br_id=application.br_id,
        )

    if not storage_ok:
        logger.warning(
            "Заявка сохранена, но материализация файлов не выполнена",
            br_id=application.br_id,
        )
