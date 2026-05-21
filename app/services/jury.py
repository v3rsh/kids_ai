"""
Стаб сервиса жюри (Wave 1 → ветка C / jury).

Алгоритм §35.2 (раунды 1→2→3 + жребий), формирование шорт-листа
(§35.5), синхронизация полей реестра 23–29 и «Статус жюри» (§25.3,
§26). Закрытие раунда по полноте, дедлайну или команде модератора
(§35.4, §27.5).

DTO ``JuryTaskDTO``, ``RoundResult`` и ``PoolKey`` живут в
``utils/contracts.py`` (см. F7 Wave 1) — этот модуль импортирует
их оттуда, чтобы Wave 2 / B3 и Wave 2 / C могли пользоваться
одинаковыми типами.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from database.models import (
        Application,
        AgeCategory,
        JuryMember,
        JuryRound,
        JuryVoteValue,
        Track,
    )
    from utils.contracts import JuryTaskDTO, PoolKey, RoundResult


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка C (jury)"


async def open_round(
    *,
    track: "Track",
    age_category: "AgeCategory",
    round_no: int,
    candidates: list["Application"],
) -> "JuryRound":
    """Открыть новый раунд по пулу (§35.2, §35.6).

    Создаёт запись ``JuryRound`` с дедлайном
    ``opened_at + JURY_ROUND_DEADLINE_HOURS`` и материализует
    задачи у всех судей пула (по ``JuryPoolAssignment``). Порядок
    работ в карусели — ``(created_at ASC, id ASC)``, единый для
    всех судей (Wave 0, §35.3).
    """
    raise NotImplementedError(_STUB_MSG)


async def submit_votes(
    *,
    round_id: UUID,
    jury_huid: UUID,
    votes: dict[UUID, "JuryVoteValue"],
) -> None:
    """Зафиксировать голоса судьи (перевод DRAFT → SUBMITTED).

    Проверяет правило разброса (§35.1, §35.3): есть и YES, и NO.
    После успеха задача исчезает из ``/jury_tasks`` этого судьи.
    Повторная подача в этом раунде невозможна (§35.4).
    """
    raise NotImplementedError(_STUB_MSG)


async def close_round(round_id: UUID) -> "RoundResult":
    """Закрыть раунд (§35.4): по полноте / дедлайну / команде модератора.

    Считает итоги только по ``SUBMITTED``-голосам. Возвращает
    ``RoundResult`` с топ-N, зоной ничьи и флагом ``needs_next_round``.
    """
    raise NotImplementedError(_STUB_MSG)


async def compute_top_n(round_id: UUID) -> list["Application"]:
    """Сформировать топ-N по итогам раунда (§35.2).

    Сортирует по убыванию голосов YES; при строгом неравенстве на
    границе позиции N возвращает топ-N сразу, при ничье — формирует
    список претендентов для следующего раунда.
    """
    raise NotImplementedError(_STUB_MSG)


async def apply_lot_if_needed(round_id: UUID) -> list["Application"]:
    """Автоматический жребий при сохранении ничьи (§35.2 finale).

    Срабатывает после раунда 3 (или раунда 2 в режиме «ускоренный»).
    Случайно выбирает нужное число работ из равных по голосам YES;
    проставляет флаг ``jury_decided_by_lot=True`` в БД и в реестре
    (поле №28, §25.3.1).
    """
    raise NotImplementedError(_STUB_MSG)


async def build_shortlist() -> list["Application"]:
    """Сформировать шорт-лист (§35.5) — когда все 12 пулов завершены.

    Проставляет ``jury_status`` для каждой заявки пула, синхронизирует
    поля №26/№27/№28/№29 в БД, триггерит уведомление в чат модерации
    «Шорт-лист сформирован, доступен по команде /export_shortlist».
    """
    raise NotImplementedError(_STUB_MSG)


async def get_open_tasks_for_jury(jury_huid: UUID) -> list["JuryTaskDTO"]:
    """Список открытых задач судьи (§27.4 ``/jury_tasks``).

    Возвращает только задачи, по которым:
    - есть назначение на пул (``JuryPoolAssignment``);
    - раунд в статусе ``OPEN``;
    - судья ещё не отправил оценки в этом раунде.
    Порядок — единый для всех судей (см. ``open_round``).
    """
    raise NotImplementedError(_STUB_MSG)


__all__ = [
    "open_round",
    "submit_votes",
    "close_round",
    "compute_top_n",
    "apply_lot_if_needed",
    "build_shortlist",
    "get_open_tasks_for_jury",
]
