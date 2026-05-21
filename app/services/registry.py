"""
Генератор Excel-выгрузок «Безопасные рисунки» (Wave 2 / ветка E).

Принципиальное правило (Wave 0, §25.4): ``registry.xlsx`` **не хранится
на диске** и не пересобирается на каждое событие. Файл собирается из
БД по запросу `/export` и `/export_shortlist`, отдаётся в чат
attachment'ом и забывается. Сервис возвращает ``bytes``.

Источник правды по формату Excel — `docs/registry-spec.md` (12
решений Q1–Q12 в design-фазе Wave 2 / E1).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from config import COMPETITION_YEAR

MSK = ZoneInfo("Europe/Moscow")


# =====================================================================
# Публичные helpers (Q4 / §4)
# =====================================================================


def registry_export_filename(
    kind: Literal["registry", "shortlist"],
    now_msk: datetime | None = None,
) -> str:
    """Имя файла on-demand выгрузки (§4 ``docs/registry-spec.md``).

    Шаблон: ``{kind}_BR-{COMPETITION_YEAR}_{YYYY-MM-DD}_{HH-MM}.xlsx``.

    Примеры:
        >>> from datetime import datetime
        >>> registry_export_filename(
        ...     "registry",
        ...     now_msk=datetime(2026, 6, 15, 14, 32, tzinfo=MSK),
        ... )
        'registry_BR-2026_2026-06-15_14-32.xlsx'

    Аргументы:
        kind: тип выгрузки — ``registry`` (основной реестр) или
            ``shortlist`` (шорт-лист топ-10 по пулам).
        now_msk: момент вызова в ``Europe/Moscow``; если ``None`` —
            берётся ``datetime.now(MSK)``. Параметр явно вынесен наружу,
            чтобы тесты получали стабильные имена.

    Возвращает:
        Имя файла, готовое для передачи в pybotx attachment.

    Бросает:
        ``ValueError`` — если ``kind`` не входит в допустимый набор.
    """
    if kind not in ("registry", "shortlist"):
        raise ValueError(f"Unknown registry kind: {kind!r}")
    now = now_msk if now_msk is not None else datetime.now(MSK)
    return (
        f"{kind}_BR-{COMPETITION_YEAR}_"
        f"{now:%Y-%m-%d}_{now:%H-%M}.xlsx"
    )


# =====================================================================
# Стабы — будут реализованы в следующих коммитах ветки E
# =====================================================================


_STUB_MSG = "Будет реализовано в следующих коммитах Wave 2 / ветка E"


async def build_registry_xlsx() -> bytes:
    """Собрать полный реестр заявок (§25.1, §25.3) в XLSX-bytes."""
    raise NotImplementedError(_STUB_MSG)


async def build_shortlist_xlsx() -> bytes:
    """Собрать XLSX шорт-листа (§35.5) — топ-10 по каждому пулу."""
    raise NotImplementedError(_STUB_MSG)


__all__ = [
    "MSK",
    "registry_export_filename",
    "build_registry_xlsx",
    "build_shortlist_xlsx",
]
