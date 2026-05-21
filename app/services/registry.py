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
from database.models import (
    Application,
    IntakeMode,
    JuryStatus,
)

MSK = ZoneInfo("Europe/Moscow")


# =====================================================================
# Транслитерация и заголовки колонок жюри (Q1 / §2.3.1)
# =====================================================================
#
# Используется паспортный стандарт ICAO Doc 9303 (актуальная редакция
# Приказа МВД РФ № 889 / Приказа МИД РФ № 4271 от 2014).
# Регистр выхода — Title-case (Shcherbak, не SHCHERBAK).

_ICAO_9303_MAP: dict[str, str] = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D",
    "Е": "E", "Ё": "E", "Ж": "ZH", "З": "Z", "И": "I",
    "Й": "I", "К": "K", "Л": "L", "М": "M", "Н": "N",
    "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T",
    "У": "U", "Ф": "F", "Х": "KH", "Ц": "TS", "Ч": "CH",
    "Ш": "SH", "Щ": "SHCH", "Ъ": "IE", "Ы": "Y", "Ь": "",
    "Э": "E", "Ю": "IU", "Я": "IA",
}


def transliterate_icao_9303(text: str) -> str:
    """Транслитерация кириллицы по ICAO Doc 9303 (§2.3.1).

    Прозрачно проходит через символы, не входящие в таблицу
    (латиница, цифры, дефисы и т. п.) — это нужно для fallback'а на
    смешанные ФИО (например, `O'Брайан`).
    """
    out: list[str] = []
    for ch in text:
        if ch.isupper():
            out.append(_ICAO_9303_MAP.get(ch, ch))
        elif ch.islower():
            mapped = _ICAO_9303_MAP.get(ch.upper(), ch)
            out.append(mapped.lower())
        else:
            out.append(ch)
    return "".join(out)


def jury_column_header(full_name: str, round_no: int) -> str:
    """Шапка динамической колонки листа `Голосование жюри` (§2.3.1).

    Шаблон: ``<Фамилия>.<И>_r<N>`` (например, ``Vinokurova.E_r1``).

    Алгоритм разбора ``full_name``:
        1. Разбиваем по whitespace.
        2. Если ≥2 токенов и оба непустые: фамилия = первый токен в
           Title-case, инициал = первая буква второго токена в upper.
        3. Иначе fallback: ``<full_name>_r<N>`` (без транслитерации).
    """
    tokens = full_name.strip().split()
    if len(tokens) >= 2 and tokens[0] and tokens[1]:
        surname_t = transliterate_icao_9303(tokens[0])
        # Инициал ограничиваем РОВНО одной буквой латиницы: если первая
        # буква имени даёт многосимвольную транслитерацию (Ю→IU, Я→IA,
        # Ж→ZH, Х→KH, Ц→TS, Ч→CH, Ш→SH, Щ→SHCH, Ъ→IE) — берём только
        # первый символ результата. Иначе ширина колонки 14 (§5.3.2)
        # рассыпается на длинных инициалах.
        initial_t = transliterate_icao_9303(tokens[1][0])[:1]
        if surname_t and initial_t:
            return f"{surname_t.title()}.{initial_t.upper()}_r{round_no}"
    return f"{full_name.strip()}_r{round_no}"


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
# Helpers значений строк (Q9 / §2.2.2, §11.1, §25.3.3)
# =====================================================================


def view_command_or_link(app: Application) -> str:
    """Значение поля №13 «Команда/ссылка просмотра файлов» (Q9 / §2.2.2).

    - ``IntakeMode.LINKS`` → ``app.cloud_link`` (URL папки участника)
      или пустая строка, если ссылка ещё не получена;
    - ``IntakeMode.FILES`` → ``/files <br_id>`` (текстовая команда
      модератора в чате).

    Та же функция переиспользуется в шорт-листе (§3.1, поле №10).
    """
    if app.intake_mode is IntakeMode.LINKS:
        return app.cloud_link or ""
    return f"/files {app.br_id}"


def contact_field(app: Application) -> str:
    """Значение поля №5 «Контакт» (§11.1).

    - Если у заявителя есть ``parent_ad_login`` — пишем ``@<login>``;
    - иначе — ``HUID: <uuid>`` (HUID всегда доступен).
    """
    if app.parent_ad_login:
        return f"@{app.parent_ad_login}"
    return f"HUID: {app.parent_huid}"


def jury_outcome(app: Application) -> str:
    """Значение поля №27 «Итог по жюри» (§2.2 / §25.3.1, §25.3.3).

    Производное от ``Application.jury_status``:
    - ``не_передано_жюри`` → ``не оценивалась``;
    - ``в_топ-10`` → ``в топ-10``;
    - ``не_вошло_в_топ-10`` → ``не вошло в топ-10``;
    - ``на_голосовании`` → пусто (пул ещё не завершён).
    """
    if app.jury_status is JuryStatus.NE_PEREDANO_ZHYURI:
        return "не оценивалась"
    if app.jury_status is JuryStatus.V_TOP_10:
        return "в топ-10"
    if app.jury_status is JuryStatus.NE_VOSHLO_V_TOP_10:
        return "не вошло в топ-10"
    return ""


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
    "transliterate_icao_9303",
    "jury_column_header",
    "view_command_or_link",
    "contact_field",
    "jury_outcome",
    "build_registry_xlsx",
    "build_shortlist_xlsx",
]
