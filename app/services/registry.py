"""
Стаб генератора Excel-реестра (Wave 1 → ветка E / registry).

Принципиальное правило (Wave 0, §25.4): ``registry.xlsx`` **не хранится
на диске** и не пересобирается на каждое событие. Файл собирается из
БД по запросу `/export` и `/export_shortlist`, отдаётся в чат
attachment'ом и забывается. Никакой записи на диск — сервис возвращает
``bytes``.

Конкретный формат колонок, заголовков и имени файла будет согласован
в Wave 2 / E1 (design-фаза) и зафиксирован в ``docs/registry-spec.md``.
"""
from __future__ import annotations


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка E (registry)"


async def build_registry_xlsx() -> bytes:
    """Собрать полный реестр заявок (§25.1, §25.3) в XLSX-bytes.

    Лист ``Реестр`` — поля 1–22 + агрегаты жюри 23–29.
    Лист ``Голосование жюри`` — детализация по членам жюри по каждому
    пройденному раунду (§25.3.2). Колонки динамические по составу
    жюри и числу проведённых раундов.

    Один запрос с ``selectinload`` для связей; для листа детализации —
    один запрос с агрегацией голосов по (application_id, jury_huid,
    round_no). Возвращает байты — Wave 2 / B3 отдаёт их в чат через
    pybotx attachment.
    """
    raise NotImplementedError(_STUB_MSG)


async def build_shortlist_xlsx() -> bytes:
    """Собрать XLSX шорт-листа (§35.5) — топ-10 по каждому из 12 пулов.

    В колонках — те же поля, что и в основном реестре, плюс пометка
    «определено жребием» (поле №28). Точный состав согласуется в
    design-фазе Wave 2 / E1.
    """
    raise NotImplementedError(_STUB_MSG)


__all__ = ["build_registry_xlsx", "build_shortlist_xlsx"]
