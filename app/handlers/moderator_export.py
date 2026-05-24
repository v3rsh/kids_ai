"""
Выгрузки и статистика модератора.

Реализует:

- ``/export`` — собрать ``registry.xlsx`` из БД и отправить в чат
  attachment'ом (on-demand, без кэша);
- ``/export_shortlist`` — то же для шорт-листа после финализации жюри;
- ``/stats today`` / ``/stats all`` — статистика по заявкам.

Аргументы команды ``/stats`` парсятся вручную (``today`` / ``all``);
команда задекларирована один раз, чтобы она была видимой в меню — её
варианты с разными аргументами уходят в подпись для пользователя
(``/stats today`` и ``/stats all`` корректно роутятся pybotx, потому
что первый токен совпадает с зарегистрированной командой).

Имя XLSX-файла собирается через ``registry.registry_export_filename(...)``
(см. ``docs/registry-spec.md`` — единый источник правды по формату).
"""
from __future__ import annotations

from loguru import logger
from pybotx import (
    Bot,
    HandlerCollector,
    IncomingMessage,
)
from pybotx.models.attachments import OutgoingAttachment

from fsm import cleanup_middleware, fsm_middleware
from services.access import moderator_only
from services.moderation import StatsCounters, StatsPeriod, count_stats
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Утилиты разбора аргументов
# =====================================================================


def _split_command_argument(message: IncomingMessage) -> str:
    raw = (message.body or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        parts = raw.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""
    return raw


def _parse_stats_period(arg: str) -> StatsPeriod | None:
    """Распознать период `/stats today` / `/stats all`."""
    needle = (arg or "").strip().casefold()
    aliases: dict[str, StatsPeriod] = {
        "today": "today",
        "сегодня": "today",
        "today,": "today",  # тривиальная защита от запятой в аргументе
        "all": "all",
        "весь": "all",
        "всё": "all",
        "all_period": "all",
    }
    if not needle:
        return None
    return aliases.get(needle)


def _format_stats(stats: StatsCounters) -> str:
    """Текстовое представление статистики по заявкам."""
    period_text = stats.period_label
    if stats.period_from and stats.period_to:
        period_text += (
            f" ({stats.period_from.strftime('%Y-%m-%d')}…"
            f"{stats.period_to.strftime('%Y-%m-%d')})"
        )
    lines = [f"📊 Статистика — {period_text}", f"Всего заявок: {stats.total}"]
    if stats.by_track:
        lines.append("")
        lines.append("По трекам:")
        for name in sorted(stats.by_track):
            lines.append(f"  • {name}: {stats.by_track[name]}")
    if stats.by_age_category:
        lines.append("")
        lines.append("По возрастным категориям:")
        for name in sorted(stats.by_age_category):
            lines.append(f"  • {name}: {stats.by_age_category[name]}")
    if stats.by_moderation_status:
        lines.append("")
        lines.append("По статусам модерации:")
        for name in sorted(stats.by_moderation_status):
            lines.append(f"  • {name}: {stats.by_moderation_status[name]}")
    lines.append("")
    lines.append(f"Требует исправления: {stats.needs_fix}")
    lines.append(f"Отклонено: {stats.rejected}")
    return "\n".join(lines)


# =====================================================================
# /export
# =====================================================================


@collector.command(
    "/export",
    description="Прислать актуальный реестр заявок (XLSX)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_export(message: IncomingMessage, bot: Bot) -> None:
    """On-demand выгрузка ``registry.xlsx`` (без кэша)."""
    from services import registry

    try:
        payload = await registry.build_registry_xlsx()
    except Exception:
        logger.exception("Ошибка генерации registry.xlsx")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось сформировать registry.xlsx. См. логи.",
        )
        return

    attachment = OutgoingAttachment(
        content=payload,
        filename=registry.registry_export_filename("registry"),
    )
    try:
        await bot.answer_message(
            "📤 Реестр заявок (актуально на момент запроса).",
            file=attachment,
            wait_callback=False,
        )
    except Exception:
        logger.exception("Не удалось отправить XLSX-реестр")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось отправить XLSX-реестр в чат. См. логи.",
        )


# =====================================================================
# /export_shortlist
# =====================================================================


@collector.command(
    "/export_shortlist",
    description="Прислать XLSX шорт-листа",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_export_shortlist(message: IncomingMessage, bot: Bot) -> None:
    """On-demand выгрузка XLSX шорт-листа по итогам жюри."""
    from services import registry

    try:
        payload = await registry.build_shortlist_xlsx()
    except Exception:
        logger.exception("Ошибка генерации shortlist.xlsx")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось сформировать XLSX шорт-листа. См. логи.",
        )
        return

    attachment = OutgoingAttachment(
        content=payload,
        filename=registry.registry_export_filename("shortlist"),
    )
    try:
        await bot.answer_message(
            "🏆 Шорт-лист по результатам жюри.",
            file=attachment,
            wait_callback=False,
        )
    except Exception:
        logger.exception("Не удалось отправить XLSX шорт-листа")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось отправить XLSX шорт-листа в чат. См. логи.",
        )


# =====================================================================
# /stats today | /stats all
# =====================================================================


@collector.command(
    "/stats",
    description="Статистика по заявкам: /stats today | /stats all",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_stats(message: IncomingMessage, bot: Bot) -> None:
    """Статистика по заявкам — сегодня или за весь период."""
    arg = _split_command_argument(message)
    period = _parse_stats_period(arg)
    if period is None:
        await reply_to_user(
            message,
            bot,
            "Команда: /stats today  или  /stats all",
        )
        return

    stats = await count_stats(period=period)
    body = _format_stats(stats)
    await reply_to_user(message, bot, body)


__all__ = ["collector"]
