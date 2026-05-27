"""
Хендлеры раздела «Настройки жюри» в админ-меню.

Управляет двумя runtime-настройками (хранятся в ``app_settings``):

- ``jury_max_round`` — порог раунда, начиная с которого срабатывает
  автоматический жребий (если включён ``jury_auto_lot``).
- ``jury_auto_lot`` — тумблер автожребия. ``off`` отключает жребий
  полностью: раунды продолжаются, пока ничья не разрешится сама.

Команды:

- ``/admin_jury_settings`` — показывает текущие значения + кнопки
  «Изменить порог» и «Включить/выключить жребий».
- ``/admin_jury_set_max_round`` — переход в FSM-шаг ввода нового
  числа (валидация: 1..50).
- ``/admin_jury_toggle_auto_lot`` — мгновенный переключатель (без
  ввода текста).

Регистрируется в ``handlers/__init__.py`` сразу за
``admin_stats_collector``.
"""
from __future__ import annotations

from loguru import logger
from pybotx import Bot, HandlerCollector, IncomingMessage

from fsm import cleanup_middleware, fsm_middleware
from handlers.admin import _sender_huid
from handlers.common import register_state_handler
from keyboards import admin_jury_settings_bubbles, admin_system_menu_bubbles
from services.access import admin_only
from services.jury_settings import (
    JURY_MAX_ROUND_HARD_LIMIT,
    get_jury_auto_lot,
    get_jury_max_round,
    set_jury_auto_lot,
    set_jury_max_round,
)
from states import AdminAction, AdminFlow
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


def _btn_data(message: IncomingMessage) -> dict:
    data = getattr(message, "data", None)
    return data if isinstance(data, dict) else {}


async def _render_settings_screen(message: IncomingMessage, bot: Bot) -> None:
    """Перерисовать экран настроек жюри с актуальными значениями."""
    max_round = await get_jury_max_round()
    auto_lot = await get_jury_auto_lot()
    auto_lot_label = "включён" if auto_lot else "выключен"
    if auto_lot:
        behaviour = (
            f"После раунда **{max_round}** при сохранении ничьи бот "
            "случайно выбирает работы из tie-зоны на оставшиеся вакансии "
            "и закрывает пул."
        )
    else:
        behaviour = (
            "Жребий **отключён**: раунды по пулу продолжаются неограниченно, "
            "пока судьи не разрешат ничью сами. Порог раундов в этом режиме "
            "используется только для отображения и команды `/jury_finalize`."
        )

    text = (
        "**Настройки жюри**\n\n"
        f"• Порог раундов до автожребия: **{max_round}** "
        f"(допустимо 1..{JURY_MAX_ROUND_HARD_LIMIT}).\n"
        f"• Автоматический жребий: **{auto_lot_label}**.\n\n"
        f"{behaviour}\n\n"
        "Изменения применяются сразу и переживают рестарт бота."
    )

    await reply_to_user(
        message,
        bot,
        text,
        bubbles=admin_jury_settings_bubbles(
            max_round=max_round, auto_lot=auto_lot
        ),
    )


@collector.command(
    "/admin_jury_settings",
    description="Настройки жюри (порог раундов и автожребий)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_jury_settings(message: IncomingMessage, bot: Bot) -> None:
    """Показать раздел «Настройки жюри»."""
    await message.state.fsm.set_state(AdminFlow.admin_menu)
    await _render_settings_screen(message, bot)


@collector.command(
    "/admin_jury_set_max_round",
    description="Изменить порог раундов жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_jury_set_max_round(
    message: IncomingMessage, bot: Bot
) -> None:
    """FSM-шаг ввода нового порога.

    Поддерживает два способа:
    - кнопка → выводим промпт + переводим в FSM-state;
    - текст команды с аргументом (``/admin_jury_set_max_round 5``) —
      сразу пытаемся применить.
    """
    raw = (message.argument or "").strip()
    if raw:
        await _try_apply_max_round(raw, message, bot)
        return

    await message.state.fsm.set_state(AdminAction.admin_action_jury_max_round_input)
    await reply_to_user(
        message,
        bot,
        (
            "Введите новое значение порога раундов жюри одним числом "
            f"(допустимо 1..{JURY_MAX_ROUND_HARD_LIMIT}).\n\n"
            "После порога при включённом автожребии "
            "бот добивает оставшиеся вакансии случайным выбором "
            "из tie-зоны."
        ),
        bubbles=admin_jury_settings_bubbles(
            max_round=await get_jury_max_round(),
            auto_lot=await get_jury_auto_lot(),
        ),
    )


async def _try_apply_max_round(
    raw: str, message: IncomingMessage, bot: Bot
) -> None:
    """Парс + валидация + сохранение, единый код для FSM-шага и аргумента."""
    try:
        value = int(raw)
    except ValueError:
        await reply_to_user(
            message,
            bot,
            "Нужно целое число (например, `5`).",
            bubbles=admin_jury_settings_bubbles(
                max_round=await get_jury_max_round(),
                auto_lot=await get_jury_auto_lot(),
            ),
        )
        return

    try:
        await set_jury_max_round(value, by_huid=_sender_huid(message))
    except ValueError as exc:
        await reply_to_user(
            message,
            bot,
            f"Не сохранено: {exc}",
            bubbles=admin_jury_settings_bubbles(
                max_round=await get_jury_max_round(),
                auto_lot=await get_jury_auto_lot(),
            ),
        )
        return

    await message.state.fsm.clear()
    await message.state.fsm.set_state(AdminFlow.admin_menu)
    await _render_settings_screen(message, bot)


@collector.command(
    "/admin_jury_toggle_auto_lot",
    description="Переключатель автожребия жюри",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_jury_toggle_auto_lot(
    message: IncomingMessage, bot: Bot
) -> None:
    """Переключить тумблер автожребия.

    Целевое состояние передаётся в ``data["enabled"]`` (``"1"``/``"0"``).
    Если не передано — просто инвертируем текущее.
    """
    data = _btn_data(message)
    raw = (data.get("enabled") or "").strip()
    current = await get_jury_auto_lot()
    if raw in ("1", "on", "true", "yes"):
        target = True
    elif raw in ("0", "off", "false", "no"):
        target = False
    else:
        target = not current

    if target == current:
        await _render_settings_screen(message, bot)
        return

    await set_jury_auto_lot(target, by_huid=_sender_huid(message))
    logger.info(
        "admin_jury_toggle_auto_lot: переключено",
        new_value=target,
        by_huid=str(_sender_huid(message)) if _sender_huid(message) else "",
    )
    await _render_settings_screen(message, bot)


async def state_handle_jury_max_round_input(
    message: IncomingMessage, bot: Bot
) -> None:
    """FSM-обработчик ввода нового порога раундов."""
    raw = (message.body or "").strip()
    if not raw:
        await reply_to_user(
            message,
            bot,
            "Пустой ввод. Введите целое число "
            f"(1..{JURY_MAX_ROUND_HARD_LIMIT}).",
            bubbles=admin_jury_settings_bubbles(
                max_round=await get_jury_max_round(),
                auto_lot=await get_jury_auto_lot(),
            ),
        )
        return
    await _try_apply_max_round(raw, message, bot)


register_state_handler(
    AdminAction.admin_action_jury_max_round_input.value,
    state_handle_jury_max_round_input,
)


__all__ = ["collector"]
