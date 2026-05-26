"""
Точечные действия модератора по заявке.

Команды:

- ``/find BR-2026-XXXX`` — карточка заявки + кнопки действий;
- ``/status <ID> <группа> <значение>`` — смена статуса в одной из
  четырёх групп (модерация / жюри / голосование / попадание в шорт-лист);
- ``/comment <ID> <текст>`` — комментарий модератора;
- ``/notify_fix <ID> [текст_уточнения]`` — уведомление участнику
  «требуется исправление» (с предупреждением, если до дедлайна приёма
  заявок осталось меньше 24 ч);
- ``/notify_reject <ID> <причина>`` — уведомление об отклонении
  + перенос метаданных в ``99_rejected/<дата_модерации>/`` +
  физическое удаление файлов работы (через ``services.storage``);
- ``/files <ID>`` — отдать модератору файлы в чат
  (в режиме ``files`` — вложениями, в режиме ``links`` — ссылку
  на папку участника).

Команда ``/notify_shortlist`` удалена: попадание работы в шорт-лист
теперь определяется автоматически по результатам жюри-сценария.
Функция ``services.notifications.notify_participant_shortlist``
остаётся живой и вызывается из ``services.jury``.

Если команда вызвана **с инлайн-кнопки карточки** без обязательного
аргумента (``/notify_reject``, ``/comment``) или с пустым опциональным
(``/notify_fix``) — модератор переходит в FSM-режим, и его следующее
текстовое сообщение становится этим аргументом. FSM-состояния:

- ``ModeratorAction.moderator_action_reject_reason`` — ждём причину;
- ``ModeratorAction.moderator_action_comment_input`` — ждём текст;
- ``ModeratorAction.moderator_action_fix_note`` — ждём опц. уточнение.

Регистрация state-handler'ов выполняется в момент импорта модуля через
``handlers.common.register_state_handler``. ``default_message_handler``
не создаётся — диспетчер живёт в ``handlers.common``.

``collector`` подключается в
``app/handlers/__init__.py → get_all_collectors()`` за
``handlers/moderator_queue.py``.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import aiofiles
from loguru import logger
from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
)
from pybotx.models.attachments import OutgoingAttachment

from config import ATTACHMENTS_DIR, COMPETITION_YEAR
from database.models import Application, IntakeMode, ModerationStatus
from fsm import cleanup_middleware, fsm_middleware
from handlers.common import register_state_handler
from handlers.moderator_queue import _full_card, render_application_card
from services.access import moderator_only
from services.moderation import (
    add_comment,
    change_status,
    find_by_br_id,
    parse_status_group,
)
from states import ModeratorAction
from utils.bot_utils import reply_to_user, safe_answer_transient


collector = HandlerCollector()


# =====================================================================
# Константы дедлайнов и форматов
# =====================================================================

# Дедлайн исправлений совпадает с финальной датой приёма заявок.
# Год берётся из COMPETITION_YEAR (config), месяц/день — 21 июня.
INTAKE_DEADLINE = date(COMPETITION_YEAR, 6, 21)

# Предупреждение модератору, если до дедлайна < 24 ч.
INTAKE_DEADLINE_WARNING_HOURS = 24


def _hours_left_to_intake_deadline(now: datetime | None = None) -> float:
    """Часы до полуночи 22 июня (т.е. конца 21 июня) от now."""
    now = now or datetime.now()
    deadline_dt = datetime.combine(
        INTAKE_DEADLINE + timedelta(days=1), datetime.min.time()
    )
    return (deadline_dt - now).total_seconds() / 3600.0


# =====================================================================
# Утилиты разбора аргументов
# =====================================================================


def _split_command_argument(message: IncomingMessage) -> str:
    """Получить «всё, что после команды» как строку.

    pybotx кладёт сырой текст в ``message.body``. Команды типа
    ``/comment BR-2026-0001 длинный текст`` парсятся в первую очередь
    как ``argument``, но мы хотим контролировать, как разбираются
    аргументы (например, у ``/notify_reject`` причина может содержать
    пробелы).
    """
    raw = (message.body or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        parts = raw.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""
    return raw


def _split_id_and_rest(arg: str) -> tuple[str, str]:
    """Отделить первый токен (ID/группу) от остального."""
    if not arg:
        return "", ""
    parts = arg.split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def _normalize_br_id(token: str) -> str:
    """Привести ``BR-...`` к канонической форме (UPPER + strip)."""
    return token.strip().upper() if token else ""


# =====================================================================
# Карточка действий
# =====================================================================


def _card_action_buttons(app: Application) -> BubbleMarkup:
    """Полный набор инлайн-кнопок активной карточки заявки.

    Состав зависит от статуса модерации:
    - Для отклонённой заявки (``OTKLONENO``) кнопки действий (Допустить,
      На исправление, Отклонить, Файлы) скрыты — файлы удалены с диска,
      работать с такой заявкой моделирующее действие нельзя.
    - Для активных статусов отображаются все стандартные действия.
    """
    bubbles = BubbleMarkup()
    if app.moderation_status is ModerationStatus.OTKLONENO:
        bubbles.add_button(
            command=f"/comment {app.br_id}",
            label="💬 Комментарий",
            new_row=True,
        )
        bubbles.add_button(
            command="/queue",
            label="📋 К очереди",
            new_row=True,
        )
        return bubbles

    bubbles.add_button(
        command=f"/files {app.br_id}",
        label="📂 Файлы",
        new_row=True,
    )
    bubbles.add_button(
        command=f"/status {app.br_id} модерация допущено",
        label="✅ Допустить",
    )
    bubbles.add_button(
        command=f"/notify_fix {app.br_id}",
        label="✏️ На исправление",
    )
    bubbles.add_button(
        command=f"/notify_reject {app.br_id}",
        label="🚫 Отклонить",
        new_row=True,
    )
    bubbles.add_button(
        command=f"/comment {app.br_id}",
        label="💬 Комментарий",
    )
    bubbles.add_button(
        command="/queue",
        label="📋 К очереди",
        new_row=True,
    )
    return bubbles


def _moderation_action_headline(new_status_value: str) -> str:
    """Текст подтверждения по итогу смены статуса модерации."""
    value = (new_status_value or "").strip().casefold()
    if value == ModerationStatus.DOPUSHCHENO.value.casefold():
        return "✅ **Заявка допущена.** Участник уведомлён."
    if value == ModerationStatus.OTKLONENO.value.casefold():
        return "🚫 **Заявка отклонена.**"
    if value == ModerationStatus.NUZHNO_ISPRAVIT.value.casefold():
        return "✏️ **Запрошены исправления.** Участник уведомлён."
    if value == ModerationStatus.NA_MODERATSII.value.casefold():
        return "↩ **Заявка возвращена на модерацию.**"
    if value == ModerationStatus.PRINYATO.value.casefold():
        return "🟢 **Заявка принята.**"
    return f"**Статус модерации обновлён:** «{new_status_value or '—'}»."


async def _show_action_confirmation(
    message: IncomingMessage,
    bot: Bot,
    *,
    app: Application,
    headline: str,
    extra: str | None = None,
) -> None:
    """Показать единое подтверждение действия модератора.

    Состав сообщения:
    - явный заголовок-плашка (что именно сделано);
    - короткая сводка по заявке (BR-ID + название + ребёнок);
    - опциональная строка ``extra`` (например, причина отклонения);
    - клавиатура ``_post_action_bubbles`` (без кнопок действий — они
      уже выполнены, осталась только навигация).
    """
    lines = [
        headline,
        "",
        f"**ID:** {app.br_id}",
        f"**Работа:** «{app.title}»",
        f"**Ребёнок:** {app.child_name}, {app.child_age}",
        f"**Трек:** {app.track.value} · {app.age_category.value}",
        f"**Статус модерации:** {app.moderation_status.value}",
    ]
    if extra:
        lines.append("")
        lines.append(extra)
    await reply_to_user(
        message,
        bot,
        "\n".join(lines),
        bubbles=_post_action_bubbles(app),
    )


def _post_action_bubbles(app: Application) -> BubbleMarkup:
    """Клавиатура после успешного действия модератора.

    Действия (допустить / отклонить / на исправление) исключают заявку
    из активной очереди, поэтому кнопки этих действий мы убираем и
    показываем нав-кнопки: «следующая в очереди», «к очереди»,
    «карточка», «меню модератора».
    """
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/queue_next",
        label="▶ Следующая заявка",
        new_row=True,
    )
    bubbles.add_button(
        command="/queue",
        label="📋 К очереди",
        new_row=True,
    )
    bubbles.add_button(
        command=f"/find {app.br_id}",
        label="📄 Карточка заявки",
    )
    bubbles.add_button(
        command="/moderator",
        label="◀ В меню модератора",
        new_row=True,
    )
    return bubbles


# =====================================================================
# /find BR-2026-XXXX
# =====================================================================


@collector.command(
    "/find",
    description="Карточка заявки по BR-ID",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_find(message: IncomingMessage, bot: Bot) -> None:
    """Карточка заявки по ``BR-2026-XXXX``.

    Форматы вызова:

    - ``/find BR-2026-0001`` — текстом или с кнопки.
    """
    arg = _split_command_argument(message)
    br_id, _ = _split_id_and_rest(arg)
    br_id = _normalize_br_id(br_id)
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Команда: /find BR-2026-XXXX",
        )
        return
    app = await find_by_br_id(br_id)
    if app is None:
        await reply_to_user(
            message,
            bot,
            f"Заявка {br_id} не найдена.",
        )
        return
    await render_application_card(
        message,
        bot,
        app=app,
        bubbles=_card_action_buttons(app),
    )


# =====================================================================
# /status <ID> <группа> <значение>
# =====================================================================


@collector.command(
    "/status",
    description="Сменить статус заявки (модерация/голосование/мерч)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_status(message: IncomingMessage, bot: Bot) -> None:
    """Смена статуса заявки в одной из групп.

    Группа ``жюри`` — read-only, меняется автоматически по итогам
    раундов жюри: сервис вернёт ошибку, если попытаться её
    модифицировать вручную. Допустимые группы (любой регистр):
    ``модерация``, ``голосование``, ``мерч``, ``moderation``,
    ``voting``, ``merch``.

    Пример: ``/status BR-2026-0001 модерация допущено``.
    """
    arg = _split_command_argument(message)
    br_id_token, rest = _split_id_and_rest(arg)
    group_token, value_token = _split_id_and_rest(rest)
    br_id = _normalize_br_id(br_id_token)

    if not br_id or not group_token or not value_token:
        await reply_to_user(
            message,
            bot,
            (
                "Команда: /status <ID> <группа> <значение>\n"
                "Группы: модерация, голосование, мерч.\n"
                "Пример: /status BR-2026-0001 модерация допущено"
            ),
        )
        return

    group = parse_status_group(group_token)
    if group is None:
        await reply_to_user(
            message,
            bot,
            (
                f"Не понимаю группу «{group_token}». "
                "Допустимые: модерация, голосование, мерч."
            ),
        )
        return

    result = await change_status(
        br_id=br_id,
        group=group,
        new_value=value_token,
        by_huid=message.sender.huid,
    )
    if not result.ok:
        await reply_to_user(message, bot, f"❌ {result.error}")
        return

    if group == "moderation":
        await _show_action_confirmation(
            message,
            bot,
            app=result.application,
            headline=_moderation_action_headline(result.new_value or ""),
        )
        return

    body = (
        f"**Статус заявки {br_id} обновлён.**\n\n"
        f"**Группа:** {group} · «{result.previous_value or '—'}» → "
        f"«{result.new_value or '—'}»."
    )
    await reply_to_user(
        message,
        bot,
        body + "\n\n" + _full_card(result.application),
        bubbles=_card_action_buttons(result.application),
    )


# =====================================================================
# /comment <ID> <текст>
# =====================================================================


@collector.command(
    "/comment",
    description="Комментарий модератора к заявке",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_comment(message: IncomingMessage, bot: Bot) -> None:
    """Добавить/перезаписать комментарий модератора к заявке.

    Если текст не указан — переходим в FSM-режим
    ``moderator_action_comment_input``: следующее текстовое сообщение
    модератора станет комментарием.
    """
    arg = _split_command_argument(message)
    br_id_token, rest = _split_id_and_rest(arg)
    br_id = _normalize_br_id(br_id_token)
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Команда: /comment BR-2026-XXXX <текст>",
        )
        return

    if not rest:
        await message.state.fsm.set_state(
            ModeratorAction.moderator_action_comment_input
        )
        await message.state.fsm.update_data(moderator_target_br_id=br_id)
        await reply_to_user(
            message,
            bot,
            (
                f"Введите новый комментарий к заявке {br_id} следующим "
                "сообщением. Чтобы очистить — отправьте «-» или «нет»."
            ),
        )
        return

    await _apply_comment(message, bot, br_id=br_id, text=rest)


async def _apply_comment(
    message: IncomingMessage,
    bot: Bot,
    *,
    br_id: str,
    text: str,
) -> None:
    cleared = text.strip().casefold() in {"-", "—", "нет", "none", ""}
    new_text = "" if cleared else text
    app = await add_comment(
        br_id=br_id, text=new_text, by_huid=message.sender.huid
    )
    if app is None:
        await reply_to_user(message, bot, f"Заявка {br_id} не найдена.")
        return
    if cleared:
        body = f"Комментарий к {br_id} удалён."
    else:
        body = f"Комментарий к {br_id} сохранён."
    await reply_to_user(
        message,
        bot,
        body + "\n\n" + _full_card(app),
        bubbles=_card_action_buttons(app),
    )


async def _state_handle_comment(message: IncomingMessage, bot: Bot) -> None:
    """Обработчик состояния ``moderator_action_comment_input``.

    Регистрируется через ``register_state_handler``; вызывается
    диспетчером ``default_message_handler``.
    """
    fsm = message.state.fsm
    data = await fsm.get_data()
    br_id = _normalize_br_id(data.get("moderator_target_br_id") or "")
    text = (message.body or "").strip()
    await fsm.clear()
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Контекст комментария потерян. Используйте /comment <ID> <текст>.",
        )
        return
    await _apply_comment(message, bot, br_id=br_id, text=text)


# =====================================================================
# /notify_fix <ID> [текст_уточнения]
# =====================================================================


@collector.command(
    "/notify_fix",
    description="Сообщение участнику: требуется исправление",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_notify_fix(message: IncomingMessage, bot: Bot) -> None:
    """Уведомление участнику «Требуется исправление».

    Сигнатура: ``/notify_fix <ID> [текст_уточнения]``. ``текст_уточнения``
    — опциональный, добавляется отдельным абзацем «Уточнение модератора: …».

    Если до дедлайна 21 июня осталось < 24 ч — модератор получает
    предупреждение перед отправкой.
    """
    arg = _split_command_argument(message)
    br_id_token, rest = _split_id_and_rest(arg)
    br_id = _normalize_br_id(br_id_token)
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Команда: /notify_fix BR-2026-XXXX [текст_уточнения]",
        )
        return

    app = await find_by_br_id(br_id)
    if app is None:
        await reply_to_user(message, bot, f"Заявка {br_id} не найдена.")
        return

    extra = rest.strip() or None
    await _send_notify_fix(message, bot, app=app, extra=extra)


async def _send_notify_fix(
    message: IncomingMessage,
    bot: Bot,
    *,
    app: Application,
    extra: str | None,
) -> None:
    hours_left = _hours_left_to_intake_deadline()
    deadline_warning = ""
    if hours_left < INTAKE_DEADLINE_WARNING_HOURS:
        deadline_warning = (
            "⚠️ Внимание: до конца приёма заявок 21 июня осталось "
            f"{max(0, hours_left):.1f} ч. Окно для родителя короткое.\n\n"
        )

    # Изменим статус на «нужно исправить»: это синхронизирует
    # поле статуса с фактическим действием.
    if app.moderation_status != ModerationStatus.NUZHNO_ISPRAVIT:
        await change_status(
            br_id=app.br_id,
            group="moderation",
            new_value=ModerationStatus.NUZHNO_ISPRAVIT.value,
            by_huid=message.sender.huid,
        )

    try:
        from services import notifications  # runtime-импорт (ветка D)

        await notifications.notify_participant_fix_needed(
            bot, app=app, extra=extra
        )
    except NotImplementedError:
        # Сервис уведомлений ещё не реализован — модератор получает
        # явный диагностический ответ, и статус заявки всё равно
        # переключён, чтобы очередь оставалась консистентной.
        await reply_to_user(
            message,
            bot,
            deadline_warning
            + (
                "⏳ Сервис уведомлений ещё не реализован.\n"
                f"Заявка {app.br_id}: статус переведён в «нужно исправить»."
            ),
        )
        return
    except Exception:
        logger.exception(
            "Не удалось отправить участнику сообщение «требуется исправление»",
            br_id=app.br_id,
        )
        await reply_to_user(
            message,
            bot,
            f"❌ Не удалось отправить участнику сообщение по заявке {app.br_id}.",
        )
        return

    refreshed = await find_by_br_id(app.br_id) or app
    extra_block: str | None = None
    if deadline_warning:
        extra_block = deadline_warning.strip()
    if extra:
        extra_line = f"**Уточнение:** {extra}"
        extra_block = f"{extra_block}\n\n{extra_line}" if extra_block else extra_line
    await _show_action_confirmation(
        message,
        bot,
        app=refreshed,
        headline="✏️ **Запрошены исправления.** Участник уведомлён.",
        extra=extra_block,
    )


# =====================================================================
# /notify_reject <ID> <причина>
# =====================================================================


@collector.command(
    "/notify_reject",
    description="Отклонить заявку и перенести в 99_rejected/",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_notify_reject(message: IncomingMessage, bot: Bot) -> None:
    """Отклонить заявку: перенести метаданные и удалить файлы.

    Причина обязательная и пишется в ``reason.txt`` дословно.
    Если не указана — модератор переходит в FSM-режим
    ``moderator_action_reject_reason``.
    """
    arg = _split_command_argument(message)
    br_id_token, rest = _split_id_and_rest(arg)
    br_id = _normalize_br_id(br_id_token)
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Команда: /notify_reject BR-2026-XXXX <причина>",
        )
        return

    if not rest.strip():
        await message.state.fsm.set_state(
            ModeratorAction.moderator_action_reject_reason
        )
        await message.state.fsm.update_data(moderator_target_br_id=br_id)
        await reply_to_user(
            message,
            bot,
            (
                f"Отправьте причину отклонения заявки {br_id} следующим "
                "сообщением. Текст уйдёт в reason.txt дословно."
            ),
        )
        return

    await _apply_reject(message, bot, br_id=br_id, reason=rest)


async def _apply_reject(
    message: IncomingMessage,
    bot: Bot,
    *,
    br_id: str,
    reason: str,
) -> None:
    app = await find_by_br_id(br_id)
    if app is None:
        await reply_to_user(message, bot, f"Заявка {br_id} не найдена.")
        return

    storage_done = False
    notify_done = False
    error_lines: list[str] = []

    try:
        from services import storage  # runtime-импорт (ветка D)

        await storage.write_reason_txt(app, reason)
        await storage.move_to_rejected(app)
        await storage.delete_application_files(app)
        storage_done = True
    except NotImplementedError:
        error_lines.append(
            "Сервис storage ещё не реализован: метаданные "
            "не перенесены, файлы не удалены."
        )
    except Exception:
        logger.exception(
            "Ошибка переноса/удаления файлов отклонённой заявки",
            br_id=br_id,
        )
        error_lines.append(
            "Ошибка ФС при переносе в 99_rejected/ или удалении файлов "
            "(см. логи)."
        )

    # Смена статуса — даже если storage не реализован, модератор
    # должен видеть статус ОТКЛОНЕНО в очереди.
    status_result = await change_status(
        br_id=br_id,
        group="moderation",
        new_value=ModerationStatus.OTKLONENO.value,
        by_huid=message.sender.huid,
    )
    if not status_result.ok:
        error_lines.append(
            f"Не удалось обновить статус: {status_result.error}"
        )

    try:
        from services import notifications  # runtime-импорт (ветка D)

        await notifications.notify_participant_rejected(
            bot, app=app, reason=reason
        )
        notify_done = True
    except NotImplementedError:
        error_lines.append(
            "Сервис уведомлений ещё не реализован: "
            "сообщение участнику не отправлено."
        )
    except Exception:
        logger.exception(
            "Не удалось отправить отказное сообщение участнику",
            br_id=br_id,
        )
        error_lines.append("Не удалось отправить участнику сообщение.")

    refreshed = status_result.application or app
    if storage_done and notify_done and not error_lines:
        headline = "🚫 **Заявка отклонена.** Файлы удалены, участник уведомлён."
    else:
        headline = "🚫 **Заявка отклонена.**"

    extra_lines: list[str] = [f"**Причина:** {reason}"]
    if error_lines:
        extra_lines.append("")
        extra_lines.append("**Замечания:**")
        extra_lines.extend(f"• {line}" for line in error_lines)

    await _show_action_confirmation(
        message,
        bot,
        app=refreshed,
        headline=headline,
        extra="\n".join(extra_lines),
    )


async def _state_handle_reject_reason(
    message: IncomingMessage, bot: Bot
) -> None:
    """FSM-обработчик: получаем причину после ``/notify_reject <ID>``."""
    fsm = message.state.fsm
    data = await fsm.get_data()
    br_id = _normalize_br_id(data.get("moderator_target_br_id") or "")
    reason = (message.body or "").strip()
    await fsm.clear()
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Контекст отклонения потерян. Используйте /notify_reject <ID> <причина>.",
        )
        return
    if not reason:
        await reply_to_user(
            message,
            bot,
            "Причина не может быть пустой.",
        )
        return
    await _apply_reject(message, bot, br_id=br_id, reason=reason)


async def _state_handle_fix_note(
    message: IncomingMessage, bot: Bot
) -> None:
    """FSM-обработчик опц. уточнения после ``/notify_fix <ID>``."""
    fsm = message.state.fsm
    data = await fsm.get_data()
    br_id = _normalize_br_id(data.get("moderator_target_br_id") or "")
    text = (message.body or "").strip()
    await fsm.clear()
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Контекст потерян. Используйте /notify_fix <ID> [текст].",
        )
        return
    app = await find_by_br_id(br_id)
    if app is None:
        await reply_to_user(message, bot, f"Заявка {br_id} не найдена.")
        return
    extra = text or None
    if extra and extra.casefold() in {"-", "—", "нет"}:
        extra = None
    await _send_notify_fix(message, bot, app=app, extra=extra)


# =====================================================================
# /files <ID>
# =====================================================================
#
# Команда «🏆 В шорт-лист» (`/notify_shortlist`) удалена из ветки
# модератора: попадание в шорт-лист теперь определяется автоматически
# из жюри-сценария (см. services.jury). Сама нотификация
# `notify_participant_shortlist` остаётся в services.notifications и
# вызывается жюри-сервисом при формировании шорт-листа.


@collector.command(
    "/files",
    description="Получить файлы заявки в чат",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_files(message: IncomingMessage, bot: Bot) -> None:
    """Выдача файлов модератору.

    В режиме приёма ``files`` — каждое вложение отдельным сообщением
    (``OutgoingAttachment``). В режиме ``links`` — текстовое
    сообщение со ссылкой на папку участника.
    """
    arg = _split_command_argument(message)
    br_id_token, _ = _split_id_and_rest(arg)
    br_id = _normalize_br_id(br_id_token)
    if not br_id:
        await reply_to_user(
            message,
            bot,
            "Команда: /files BR-2026-XXXX",
        )
        return

    app = await find_by_br_id(br_id)
    if app is None:
        await reply_to_user(message, bot, f"Заявка {br_id} не найдена.")
        return

    if app.intake_mode == IntakeMode.LINKS:
        link = app.cloud_link or "—"
        body = (
            f"🔗 Заявка {app.br_id} — режим приёма «links».\n"
            f"Ссылка на папку участника: {link}"
        )
        await reply_to_user(message, bot, body, bubbles=_card_action_buttons(app))
        return

    if not app.files:
        await reply_to_user(
            message,
            bot,
            f"У заявки {app.br_id} нет сохранённых файлов в хранилище.",
            bubbles=_card_action_buttons(app),
        )
        return

    sent = 0
    failed: list[str] = []
    for file in app.files:
        try:
            attachment = await _read_application_file(file.relative_path, file.stored_filename)
        except FileNotFoundError:
            failed.append(file.stored_filename)
            continue
        except Exception:
            logger.exception(
                "Ошибка чтения файла заявки",
                br_id=app.br_id,
                file=file.stored_filename,
            )
            failed.append(file.stored_filename)
            continue
        try:
            await bot.answer_message(
                f"📎 {app.br_id}: {file.stored_filename}",
                file=attachment,
                wait_callback=False,
            )
            sent += 1
        except Exception:
            logger.exception(
                "Не удалось отправить вложение",
                br_id=app.br_id,
                file=file.stored_filename,
            )
            failed.append(file.stored_filename)

    summary_lines = [
        f"📂 Файлы заявки {app.br_id}: отправлено {sent} из {len(app.files)}."
    ]
    if failed:
        summary_lines.append("Не удалось: " + ", ".join(failed))
    await safe_answer_transient(
        message,
        bot,
        "\n".join(summary_lines),
    )


async def _read_application_file(
    relative_path: str, stored_filename: str
) -> OutgoingAttachment:
    """Прочитать файл из ``ATTACHMENTS_DIR`` и завернуть в OutgoingAttachment.

    ``relative_path`` — путь относительно ``ATTACHMENTS_DIR``, заданный
    сервисом storage в момент сохранения файла. Имя
    результирующего вложения берём из ``stored_filename`` для
    консистентности с тем, что лежит на диске.
    """
    full_path = (ATTACHMENTS_DIR / relative_path).resolve()
    base = ATTACHMENTS_DIR.resolve()
    try:
        full_path.relative_to(base)
    except ValueError as exc:
        raise FileNotFoundError(
            f"relative_path вышло за ATTACHMENTS_DIR: {relative_path}"
        ) from exc
    if not full_path.exists():
        raise FileNotFoundError(str(full_path))
    async with aiofiles.open(full_path, "rb") as f:
        content = await f.read()
    return OutgoingAttachment(content=content, filename=stored_filename)


# =====================================================================
# Регистрация state-handler'ов в общем диспетчере
# =====================================================================

register_state_handler(
    ModeratorAction.moderator_action_comment_input.value,
    _state_handle_comment,
)
register_state_handler(
    ModeratorAction.moderator_action_reject_reason.value,
    _state_handle_reject_reason,
)
register_state_handler(
    ModeratorAction.moderator_action_fix_note.value,
    _state_handle_fix_note,
)


__all__ = ["collector"]
