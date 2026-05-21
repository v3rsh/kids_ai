"""
Админ-команды модератора над жюри-логикой (Wave 2 / B, §27.5).

Реализует:

- ``/jury_state`` — таблица текущего состояния всех пулов: открытый
  раунд, число судей, отправивших/назначенных, остаток до дедлайна;
- ``/jury_close_round <пул>`` — досрочное закрытие текущего открытого
  раунда указанного пула;
- ``/jury_close_round all`` — закрыть текущий открытый раунд во всех
  пулах разом;
- ``/jury_finalize`` — аварийная финализация: остановить процесс,
  зафиксировать топы и применить жребий, где нужно (§35.5).

**Формат пула** (зафиксирован в этой команде; ответы пользователю в
docstring):

- Канонический: ``<track_slug>/<age_slug>``,
  например ``traditional/7-12``, ``ai/0-6``, ``handmade_to_ai/13-18``.
- Альтернативы (модератор может набрать «как помнит»):
  - ``Традиционное/7-12`` или ``Традиционное / 7–12`` (с длинным тире);
  - английский алиас: ``traditional / 7-12``;
  - регистронезависимо.

Все DB-агрегации сделаны без N+1: ``/jury_state`` строится двумя
запросами (открытые раунды + один GROUP BY по голосам), ``close_round``
вызывает ``services.jury.close_round`` (стаб Wave 1, заполнит C2).

WAVE3-TODO: подключить ``collector`` в
``app/handlers/__init__.py → get_all_collectors()`` за
``handlers/moderator_export.py`` (последний файл ветки B).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from loguru import logger
from pybotx import (
    Bot,
    HandlerCollector,
    IncomingMessage,
)
from sqlalchemy import func, select

from database.db import get_session
from database.models import (
    AgeCategory,
    JuryPoolAssignment,
    JuryRound,
    JuryRoundStatus,
    JuryVote,
    JuryVoteState,
    Track,
)
from fsm import cleanup_middleware, fsm_middleware
from services.access import moderator_only
from utils.bot_utils import reply_to_user


collector = HandlerCollector()


# =====================================================================
# Алиасы пулов
# =====================================================================

# Все пулы конкурса (`len(Track) × len(AgeCategory)`, §35.1). После
# Wave 0 replay 2026-05-21 это 9 пулов (3 × 3). Хардкода числа нет —
# при правках enum сервис перестраивается автоматически. Локальный
# список используется здесь, потому что `services.pools.all_pools()`
# пока стаб; в Wave 3 хендлер переедет на сервис без правок алгоритма.
ALL_POOLS: list[tuple[Track, AgeCategory]] = [
    (track, age) for track in Track for age in AgeCategory
]


_TRACK_ALIASES: dict[str, Track] = {
    "traditional": Track.TRADITIONAL,
    "trad": Track.TRADITIONAL,
    "традиционное": Track.TRADITIONAL,
    "традиционный": Track.TRADITIONAL,
    "традиционное рисование": Track.TRADITIONAL,
    "ai": Track.AI,
    "ии": Track.AI,
    "ии-рисунок": Track.AI,
    "handmade_to_ai": Track.HANDMADE_TO_AI,
    "handmade": Track.HANDMADE_TO_AI,
    "от руки к ии": Track.HANDMADE_TO_AI,
    "от-руки-к-ии": Track.HANDMADE_TO_AI,
    "refine": Track.HANDMADE_TO_AI,
}

_AGE_ALIASES: dict[str, AgeCategory] = {
    "0-6": AgeCategory.AGE_0_6,
    "0–6": AgeCategory.AGE_0_6,
    "age_0_6": AgeCategory.AGE_0_6,
    "7-12": AgeCategory.AGE_7_12,
    "7–12": AgeCategory.AGE_7_12,
    "age_7_12": AgeCategory.AGE_7_12,
    "13-18": AgeCategory.AGE_13_18,
    "13–18": AgeCategory.AGE_13_18,
    "age_13_18": AgeCategory.AGE_13_18,
}


def _track_slug(track: Track) -> str:
    return track.name.lower()


def _age_slug(cat: AgeCategory) -> str:
    return cat.value.replace("–", "-")


def _format_pool(track: Track, cat: AgeCategory) -> str:
    return f"{_track_slug(track)}/{_age_slug(cat)}"


def parse_pool_token(token: str) -> tuple[Track, AgeCategory] | None:
    """Распознать строку формата ``<track>/<age>``.

    Допускает: латинский slug (``traditional``, ``ai``, ``handmade_to_ai``),
    русские названия (``Традиционное``, ``ИИ``, ``От руки к ИИ``),
    короткое/длинное тире в возрасте (``7-12`` / ``7–12``), любой
    регистр.
    """
    if not token:
        return None
    needle = token.strip().lower().replace("—", "-").replace("–", "-")
    needle = needle.replace(" / ", "/").replace(" /", "/").replace("/ ", "/")
    if "/" not in needle:
        return None
    track_part, age_part = needle.split("/", maxsplit=1)
    track_part = track_part.strip()
    age_part = age_part.strip()
    track = _TRACK_ALIASES.get(track_part)
    if track is None:
        # Запасной путь: поиск по `Track.name.lower()` / `Track.value`.
        for t in Track:
            if t.name.lower() == track_part or t.value.lower() == track_part:
                track = t
                break
    age = _AGE_ALIASES.get(age_part)
    if age is None:
        for a in AgeCategory:
            if a.value.lower() == age_part or a.name.lower() == age_part:
                age = a
                break
    if track is None or age is None:
        return None
    return track, age


def _split_command_argument(message: IncomingMessage) -> str:
    raw = (message.body or "").strip()
    if not raw:
        return ""
    if raw.startswith("/"):
        parts = raw.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""
    return raw


# =====================================================================
# /jury_state
# =====================================================================


@collector.command(
    "/jury_state",
    description="Состояние процесса жюри по всем пулам (§27.5)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_jury_state(message: IncomingMessage, bot: Bot) -> None:
    """Сводка по всем пулам без N+1.

    Делает 3 запроса:
    1. Последние раунды по пулам (по одному на пул, по `round_no DESC`).
    2. Назначения судей по пулам — один SELECT по всем парам.
    3. SUBMITTED-голоса по раундам — один GROUP BY round_id × jury_huid.
    Затем агрегирует в памяти.
    """
    async with get_session()() as session:
        rounds_stmt = select(JuryRound)
        all_rounds = list((await session.execute(rounds_stmt)).scalars().all())

        assign_stmt = select(JuryPoolAssignment.track, JuryPoolAssignment.age_category, func.count())
        assign_stmt = assign_stmt.group_by(
            JuryPoolAssignment.track, JuryPoolAssignment.age_category
        )
        assignments_count: dict[tuple[Track, AgeCategory], int] = {
            (track, age): int(cnt)
            for track, age, cnt in (await session.execute(assign_stmt)).all()
        }

        votes_stmt = (
            select(
                JuryVote.round_id,
                func.count(func.distinct(JuryVote.jury_huid)),
            )
            .where(JuryVote.state == JuryVoteState.SUBMITTED)
            .group_by(JuryVote.round_id)
        )
        votes_per_round: dict = {
            row[0]: int(row[1])
            for row in (await session.execute(votes_stmt)).all()
        }

    # latest round per pool (по round_no DESC)
    latest_round_per_pool: dict[tuple[Track, AgeCategory], JuryRound] = {}
    for r in all_rounds:
        key = (r.track, r.age_category)
        cur = latest_round_per_pool.get(key)
        if cur is None or r.round_no > cur.round_no:
            latest_round_per_pool[key] = r

    now = datetime.utcnow()
    lines = [
        "⚖️ Состояние жюри (§35, §27.5).",
        "Формат строки: <пул> · раунд · статус · "
        "судей submitted/назначено · до дедлайна.",
        "",
    ]
    any_open = False
    for track, cat in ALL_POOLS:
        pool_label = _format_pool(track, cat)
        round_obj = latest_round_per_pool.get((track, cat))
        assigned = assignments_count.get((track, cat), 0)
        if round_obj is None:
            lines.append(f"• {pool_label}: раундов ещё не было")
            continue
        votes = votes_per_round.get(round_obj.id, 0)
        delta = round_obj.deadline_at - now
        deadline_str = _format_deadline(round_obj.deadline_at, delta)
        status_str = round_obj.status.value
        if round_obj.status == JuryRoundStatus.OPEN:
            any_open = True
        lines.append(
            f"• {pool_label}: р{round_obj.round_no} · {status_str} · "
            f"{votes}/{assigned} судей · {deadline_str}"
        )
    if not any_open:
        lines.append("")
        lines.append("Открытых раундов нет.")

    await reply_to_user(message, bot, "\n".join(lines))


def _format_deadline(deadline_at: datetime, delta) -> str:
    if delta.total_seconds() < 0:
        return f"истёк {deadline_at:%Y-%m-%d %H:%M} UTC"
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)
    return f"осталось {hours} ч {minutes} мин (до {deadline_at:%Y-%m-%d %H:%M} UTC)"


# =====================================================================
# /jury_close_round <пул> | all
# =====================================================================


@collector.command(
    "/jury_close_round",
    description="Досрочное закрытие текущего раунда жюри (§27.5)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_jury_close_round(message: IncomingMessage, bot: Bot) -> None:
    """Закрытие открытого раунда (§35.4 пункт «в»).

    Аргумент:

    - ``all`` — закрыть открытые раунды во всех пулах разом;
    - ``<track>/<age>`` — закрыть открытый раунд указанного пула.

    Формат пула — см. модуль docstring.
    """
    arg = _split_command_argument(message).strip()
    if not arg:
        await reply_to_user(
            message,
            bot,
            (
                "Команда: /jury_close_round <пул>  или  /jury_close_round all\n"
                "Пример: /jury_close_round traditional/7-12"
            ),
        )
        return

    if arg.casefold() == "all":
        await _close_open_rounds(message, bot, target=None)
        return

    parsed = parse_pool_token(arg)
    if parsed is None:
        await reply_to_user(
            message,
            bot,
            (
                f"Не понимаю пул «{arg}». Формат: <track>/<age>, "
                "например traditional/7-12."
            ),
        )
        return

    await _close_open_rounds(message, bot, target=parsed)


async def _close_open_rounds(
    message: IncomingMessage,
    bot: Bot,
    *,
    target: Optional[tuple[Track, AgeCategory]],
) -> None:
    async with get_session()() as session:
        stmt = select(JuryRound).where(JuryRound.status == JuryRoundStatus.OPEN)
        if target is not None:
            stmt = stmt.where(
                JuryRound.track == target[0],
                JuryRound.age_category == target[1],
            )
        rounds = list((await session.execute(stmt)).scalars().all())

    if not rounds:
        if target is None:
            text = "Открытых раундов нет — закрывать нечего."
        else:
            text = (
                f"В пуле {_format_pool(*target)} нет открытого раунда — "
                "закрывать нечего."
            )
        await reply_to_user(message, bot, text)
        return

    closed: list[str] = []
    failed: list[str] = []
    not_implemented = False

    try:
        from services import jury  # runtime-импорт (ветка C)
    except ImportError:
        jury = None  # type: ignore[assignment]

    for r in rounds:
        pool_label = _format_pool(r.track, r.age_category)
        if jury is None:
            failed.append(f"{pool_label} р{r.round_no} — services.jury недоступен")
            continue
        try:
            await jury.close_round(r.id)
        except NotImplementedError:
            not_implemented = True
            failed.append(
                f"{pool_label} р{r.round_no} — services.jury.close_round() — стаб (Wave 2 / C)"
            )
            continue
        except Exception:
            logger.exception(
                "Не удалось закрыть раунд жюри",
                pool=pool_label,
                round_no=r.round_no,
            )
            failed.append(f"{pool_label} р{r.round_no} — ошибка, см. логи")
            continue
        closed.append(f"{pool_label} р{r.round_no}")

    lines = []
    if closed:
        lines.append("✅ Закрыты раунды:")
        lines.extend(f"  • {item}" for item in closed)
    if failed:
        lines.append("⚠️ Не закрыты:")
        lines.extend(f"  • {item}" for item in failed)
    if not_implemented:
        lines.append("")
        lines.append(
            "Сервис жюри пока стаб (Wave 1) — закрытие фактически не выполнено. "
            "Подождите Wave 2 / C."
        )
    if not lines:
        lines.append("Ничего не сделано.")

    await reply_to_user(message, bot, "\n".join(lines))


# =====================================================================
# /jury_finalize
# =====================================================================


@collector.command(
    "/jury_finalize",
    description="Аварийная финализация процесса жюри (§27.5)",
    middlewares=[fsm_middleware, cleanup_middleware],
)
@moderator_only
async def cmd_jury_finalize(message: IncomingMessage, bot: Bot) -> None:
    """Финализация процесса (§35.5).

    Если по пулу остался открытый раунд — применяется жребий на текущей
    стадии (логика — внутри ``services.jury.build_shortlist()``).
    """
    try:
        from services import jury  # runtime-импорт (ветка C)

        result = await jury.build_shortlist()
    except NotImplementedError:
        await reply_to_user(
            message,
            bot,
            "⏳ services.jury.build_shortlist() пока стаб (Wave 2 / C). "
            "Финализация будет доступна, когда ветка C подключит сервис.",
        )
        return
    except Exception:
        logger.exception("Не удалось финализировать процесс жюри")
        await reply_to_user(
            message,
            bot,
            "❌ Не удалось финализировать процесс жюри. См. логи.",
        )
        return

    count = len(result) if result is not None else 0
    await reply_to_user(
        message,
        bot,
        (
            f"🏁 Процесс жюри финализирован.\n"
            f"В шорт-лист вошло заявок: {count}.\n"
            "Файл доступен по команде /export_shortlist."
        ),
    )


__all__ = ["collector", "parse_pool_token", "ALL_POOLS"]
