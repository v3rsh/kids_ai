"""
Хендлеры раздела «Статистика» админки.
"""
from __future__ import annotations

from pybotx import Bot, HandlerCollector, IncomingMessage

from fsm import cleanup_middleware, fsm_middleware
from keyboards import admin_stats_menu_bubbles
from services.access import admin_only
from services.admin import build_admin_stats_report
from states import AdminFlow
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


def _fmt_mb(value_bytes: int) -> str:
    return f"{value_bytes / (1024 ** 2):.1f} МБ"


def _format_admin_stats(report) -> str:
    """Текст расширенной статистики."""
    u = report.users
    j = report.jury
    d = report.disk
    o = report.overview

    lines = [
        "**📊 Статистика админки**",
        "",
        "**Пользователи:**",
        f"• Всего: {u.total}",
        f"• С chat_id: {u.with_chat_id}",
        f"• Новых за 24 ч: {u.new_last_24h}",
        f"• Активных за 24 ч: {u.active_last_24h}",
        "",
        "**Заявки:**",
        f"• Сегодня: {report.apps_today}",
        f"• Всего: {report.apps_total}",
        f"• Конверсия (заявки/пользователи): "
        f"{(report.apps_total / u.total * 100):.1f} %"
        if u.total
        else "• Конверсия: —",
        "",
        "**По трекам:**",
    ]
    for name in sorted(report.by_track):
        lines.append(f"  • {name}: {report.by_track[name]}")
    lines.append("")
    lines.append("**По возрастам:**")
    for name in sorted(report.by_age):
        lines.append(f"  • {name}: {report.by_age[name]}")
    lines.append("")
    lines.append("**По статусам модерации:**")
    for name in sorted(report.by_moderation_status):
        lines.append(f"  • {name}: {report.by_moderation_status[name]}")

    lines.extend(
        [
            "",
            "**Жюри:**",
            f"• Открытых раундов: {j.open_rounds}",
            f"• Закрытых: {j.closed_rounds}",
            f"• Жребий: {j.drawn_by_lot}",
            f"• В топ-10: {j.top10_applications}",
            f"• SUBMITTED-голосов (открытые раунды): {j.submitted_votes}",
            "",
            "**Диск и система:**",
            f"• Режим приёма: **{o.intake_mode}**",
            f"• Заполнено: {d.pct:.1f} % "
            f"({_fmt_mb(d.free_bytes)} свободно)",
            f"• Заявок за 7 д: {d.apps_last_7d}",
        ]
    )
    if d.hours_left is not None:
        lines.append(f"• Прогноз исчерпания: ~{d.hours_left:.0f} ч")
    lines.append(f"• disk_alerts за 7 д: {report.disk_alerts_7d}")
    return "\n".join(lines)


@collector.command(
    "/admin_stats",
    description="Расширенная статистика (admin)",
    visible=False,
    middlewares=[fsm_middleware, cleanup_middleware],
)
@admin_only
async def cmd_admin_stats(message: IncomingMessage, bot: Bot) -> None:
    """Расширенная аналитика для администратора."""
    await message.state.fsm.set_state(AdminFlow.admin_menu)
    report = await build_admin_stats_report()
    await reply_to_user(
        message,
        bot,
        _format_admin_stats(report),
        bubbles=admin_stats_menu_bubbles(),
    )


__all__ = ["collector"]
