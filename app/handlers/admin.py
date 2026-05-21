"""
Админ-команды конкурса «Безопасные рисунки» (ветка D1).

Содержит технические команды разработчика / тех. админа (§5.3, §27.1):
- ``/disk`` — текущее состояние дискового пространства (всего / занято /
  свободно), процент заполнения и предупреждение, если пора переходить
  в режим LINKS (§28.1);
- ``/intake_mode`` — ручное переключение режима приёма (§33.6).
  Команда доступна и модераторам, и админам — здесь регистрируется
  административная версия (тех. админ всегда имеет доступ), модератор-
  ветке (B) разрешено реализовать свою копию с такими же контрактами;
- ``/admin_state`` — диагностический дамп текущих настроек (intake_mode,
  disk %, последние alert'ы).

Коллектор регистрируется в ``app/handlers/__init__.py`` — последним
в списке ``get_all_collectors()``, после ветки C (жюри).

Правила (`.cursor/rules/bot.mdc`, `message-navigation.mdc`):
- все хендлеры обёрнуты ``@admin_only``;
- ответы — через ``reply_to_user`` (редактируется на месте при кликах
  по кнопкам, отправляется как новое сообщение при текстовом вводе);
- ``wait_callback=False`` всегда;
- никаких ``bubbles=None`` — либо передаём ``BubbleMarkup``, либо не
  передаём параметр вовсе.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from loguru import logger
from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
)
from sqlalchemy import select

from config import DISK_BLOCK_PCT, DISK_WARN_PCT
from database.db import get_session
from database.models import DiskAlert, IntakeMode
from fsm import cleanup_middleware, fsm_middleware
from services.access import admin_only, moderator_only
from services.intake_mode import (
    SYSTEM_HUID,
    get_intake_mode,
    maybe_auto_switch_to_links,
    set_intake_mode,
)
from services.storage import (
    get_disk_usage_bytes,
    get_disk_usage_pct,
)
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


_MOSCOW_TZ = timezone(timedelta(hours=3))


# =====================================================================
# Утилиты форматирования
# =====================================================================


def _fmt_mb(value_bytes: int) -> str:
    """``int → "1234.5 МБ"``."""
    return f"{value_bytes / (1024 * 1024):.1f} МБ"


def _fmt_gb(value_bytes: int) -> str:
    """``int → "9.8 ГБ"``."""
    return f"{value_bytes / (1024 ** 3):.2f} ГБ"


def _intake_mode_bubbles(current: IntakeMode) -> BubbleMarkup:
    """Две кнопки переключения intake_mode (без активной у текущего)."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/intake_mode",
        label=("☑ " if current is IntakeMode.FILES else "→ ") + "FILES (файлы на сервере)",
        data={"mode": IntakeMode.FILES.value},
        new_row=True,
    )
    bubbles.add_button(
        command="/intake_mode",
        label=("☑ " if current is IntakeMode.LINKS else "→ ") + "LINKS (ссылки на облако)",
        data={"mode": IntakeMode.LINKS.value},
        new_row=True,
    )
    return bubbles


async def _format_disk_block(
    *, include_prediction: bool = True
) -> str:
    """Текст состояния диска для ``/disk`` и ``/admin_state``."""
    used, total = get_disk_usage_bytes()
    free = max(total - used, 0)
    pct = get_disk_usage_pct()

    lines = [
        f"Дисковое пространство хранилища заявок:",
        f"- Всего: {_fmt_gb(total)}",
        f"- Занято: {_fmt_gb(used)} ({pct:.1f} %)",
        f"- Свободно: {_fmt_gb(free)}",
        f"- Пороги: WARN {DISK_WARN_PCT} %, BLOCK {DISK_BLOCK_PCT} %",
    ]

    if pct >= DISK_BLOCK_PCT:
        lines.append(
            "🚨 Достигнут BLOCK-порог. Приём файлов автоматически "
            "переключён в режим LINKS (раздел 33.6)."
        )
    elif pct >= DISK_WARN_PCT:
        lines.append(
            "⚠️ Достигнут WARN-порог. Свободного места осталось мало — "
            "следите за командой /disk и рассмотрите переход в LINKS."
        )

    if include_prediction:
        recent = await _recent_alerts()
        if recent:
            lines.append("")
            lines.append("Последние авто-предупреждения:")
            for threshold, when in recent:
                local = when.astimezone(_MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
                lines.append(f"- {threshold} %: {local} (Europe/Moscow)")

    return "\n".join(lines)


async def _recent_alerts(*, limit: int = 5) -> list[tuple[int, datetime]]:
    """Список последних alert'ов из ``disk_alerts``."""
    async with get_session()() as session:
        result = await session.execute(
            select(DiskAlert.threshold_pct, DiskAlert.created_at)
            .order_by(DiskAlert.created_at.desc())
            .limit(limit)
        )
        rows = result.all()
        # Приводим naive datetime к UTC.
        normalised: list[tuple[int, datetime]] = []
        for threshold, when in rows:
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            normalised.append((threshold, when))
        return normalised


# =====================================================================
# /disk — состояние дискового пространства (§27.1, §28.1)
# =====================================================================


@collector.command(
    "/disk",
    description="Состояние дискового пространства хранилища (admin/moderator)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_disk(message: IncomingMessage, bot: Bot) -> None:
    """``/disk`` — занято/всего/свободно/прогноз/intake_mode (§27.1, §28.1).

    Доступна модераторам (§27.1) — admin-only здесь избыточно. Реальные
    разрушительные команды (см. ``cmd_admin_state``) защищены отдельно.
    """
    current_mode = await get_intake_mode()
    body_parts = [
        await _format_disk_block(include_prediction=True),
        "",
        f"Текущий режим приёма: **{current_mode.value.upper()}** "
        f"({'файлы на сервере' if current_mode is IntakeMode.FILES else 'ссылки на облако'})",
    ]

    # Активируем фоновую проверку: если диск пересёк BLOCK — авто-переход.
    try:
        switched = await maybe_auto_switch_to_links(bot=bot)
        if switched:
            body_parts.append(
                "🔁 Режим только что был переключён в LINKS автоматически."
            )
    except Exception:
        logger.exception("Авто-переключение в LINKS не удалось (вызов из /disk)")

    await reply_to_user(message, bot, "\n".join(body_parts))


# =====================================================================
# /intake_mode — переключение режима приёма (§33.6)
# =====================================================================


@collector.command(
    "/intake_mode",
    description="Переключить режим приёма заявок: files | links (admin/moderator)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_intake_mode(message: IncomingMessage, bot: Bot) -> None:
    """``/intake_mode [files|links]`` — переключение режима приёма (§33.6).

    Поведение:
    - без аргумента — показывает текущий режим и две кнопки выбора;
    - ``/intake_mode files`` или ``/intake_mode links`` — переключает;
    - кнопка с ``data={"mode": "files"|"links"}`` — то же, что аргумент.

    Источник истины — таблица ``app_settings``. Изменение переживает
    рестарт контейнера.
    """
    current = await get_intake_mode()

    new_mode_raw: str | None = None

    # Берём значение из data кнопки (приоритет) или из аргументов команды.
    data = getattr(message, "data", None) or {}
    if isinstance(data, dict):
        new_mode_raw = data.get("mode")

    if not new_mode_raw and message.argument:
        # message.argument — строка после "/intake_mode " (pybotx).
        arg = message.argument.strip().lower()
        if arg:
            new_mode_raw = arg.split()[0]

    if new_mode_raw is None:
        await reply_to_user(
            message,
            bot,
            (
                f"Текущий режим приёма: **{current.value.upper()}**.\n"
                "Выберите новый режим:"
            ),
            bubbles=_intake_mode_bubbles(current),
        )
        return

    try:
        new_mode = IntakeMode(new_mode_raw.strip().lower())
    except ValueError:
        await reply_to_user(
            message,
            bot,
            (
                "Не понял режим. Допустимо: `files` или `links`.\n"
                f"Текущий режим: **{current.value.upper()}**."
            ),
            bubbles=_intake_mode_bubbles(current),
        )
        return

    if new_mode is current:
        await reply_to_user(
            message,
            bot,
            f"Режим уже **{current.value.upper()}**. Изменения не нужны.",
            bubbles=_intake_mode_bubbles(current),
        )
        return

    by_huid = _sender_huid(message) or SYSTEM_HUID
    await set_intake_mode(
        new_mode, by_huid=by_huid, reason=f"manual via /intake_mode by {by_huid}"
    )

    await reply_to_user(
        message,
        bot,
        (
            f"Режим приёма переключён: "
            f"**{current.value.upper()} → {new_mode.value.upper()}**."
            + (
                "\n\nНовые заявки будут приниматься по инструкции для режима LINKS "
                "(см. §33.6 ТЗ — родитель присылает ссылку на облако)."
                if new_mode is IntakeMode.LINKS
                else "\n\nНовые заявки будут приниматься как файлы на сервер."
            )
        ),
        bubbles=_intake_mode_bubbles(new_mode),
    )


# =====================================================================
# /admin_state — диагностический дамп (только админ)
# =====================================================================


@collector.command(
    "/admin_state",
    description="Диагностика админа: режимы, диск, последние alert'ы",
    middlewares=[fsm_middleware, cleanup_middleware],
    visible=False,
)
@admin_only
async def cmd_admin_state(message: IncomingMessage, bot: Bot) -> None:
    """``/admin_state`` — внутренняя диагностика (§5.3, не для модераторов).

    Текст содержит дамп: текущий intake_mode, ёмкость диска, последние
    несколько disk_alerts. Используется разработчиком на этапе
    тестирования/поддержки.
    """
    current = await get_intake_mode()
    disk_block = await _format_disk_block(include_prediction=True)
    body = (
        "🛠 Состояние бота (admin diagnostic):\n"
        f"- intake_mode: **{current.value.upper()}**\n\n"
        f"{disk_block}"
    )
    await reply_to_user(message, bot, body)


# =====================================================================
# Утилиты
# =====================================================================


def _sender_huid(message: IncomingMessage) -> UUID | None:
    """Аккуратный доступ к ``message.sender.huid`` (для тестов и safety)."""
    sender = getattr(message, "sender", None)
    if sender is None:
        return None
    huid = getattr(sender, "huid", None)
    if isinstance(huid, UUID):
        return huid
    if isinstance(huid, str):
        try:
            return UUID(huid)
        except ValueError:
            return None
    return None


__all__ = ["collector"]
