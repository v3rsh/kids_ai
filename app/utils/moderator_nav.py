"""
Контекст навигации модератора по карточке заявки.

Origin передаётся в ``data`` кнопок (``/find``, действия карточки).
FSM-кеш origin — только на время FSM-диалога (comment/reject).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pybotx import BubbleMarkup

from database.models import Application, ModerationStatus
from fsm.keys import (
    FSM_KEY_MODERATOR_TARGET_BR_ID,
    FSM_KEY_MOD_NAV_ORIGIN_BR_ID,
    FSM_KEY_MOD_NAV_ORIGIN_DATA,
    FSM_KEY_MOD_NAV_ORIGIN_KIND,
)

ModeratorNavKind = Literal[
    "queue", "browse", "section", "multi_subs", "admin_find", "direct"
]

_VALID_KINDS: frozenset[str] = frozenset(
    {"queue", "browse", "section", "multi_subs", "admin_find", "direct"}
)

_BACK_LABELS: dict[ModeratorNavKind, str] = {
    "section": "◀ К списку",
    "queue": "◀ К очереди",
    "browse": "◀ К карусели",
    "multi_subs": "◀ К повторным",
    "admin_find": "◀ В админку",
}


@dataclass(frozen=True)
class ModeratorNavOrigin:
    """Откуда модератор открыл карточку заявки."""

    kind: ModeratorNavKind = "direct"
    section_status: str | None = None
    section_track: str | None = None
    section_age: str | None = None
    section_page: int = 1
    multi_subs_page: int = 1

    def to_data(self) -> dict[str, str]:
        """Сериализация в payload кнопки pybotx."""
        payload: dict[str, str] = {"from": self.kind}
        if self.kind == "section":
            if self.section_status:
                payload["st"] = self.section_status
            if self.section_track:
                payload["tr"] = self.section_track
            if self.section_age:
                payload["ag"] = self.section_age
            payload["p"] = str(self.section_page)
        elif self.kind == "multi_subs":
            payload["p"] = str(self.multi_subs_page)
        return payload

    def to_dict(self) -> dict[str, Any]:
        """Сериализация для FSM-кеша."""
        return {
            "kind": self.kind,
            "section_status": self.section_status,
            "section_track": self.section_track,
            "section_age": self.section_age,
            "section_page": self.section_page,
            "multi_subs_page": self.multi_subs_page,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> ModeratorNavOrigin:
        if not raw:
            return cls()
        kind = raw.get("kind") or "direct"
        if kind not in _VALID_KINDS:
            kind = "direct"
        return cls(
            kind=kind,  # type: ignore[arg-type]
            section_status=raw.get("section_status"),
            section_track=raw.get("section_track"),
            section_age=raw.get("section_age"),
            section_page=max(1, int(raw.get("section_page") or 1)),
            multi_subs_page=max(1, int(raw.get("multi_subs_page") or 1)),
        )


def parse_origin(data: dict[str, str] | None) -> ModeratorNavOrigin:
    """Разобрать origin из ``message.data`` кнопки."""
    if not data:
        return ModeratorNavOrigin()

    kind = data.get("from") or "direct"
    if kind not in _VALID_KINDS:
        kind = "direct"

    page_raw = data.get("p")
    try:
        page = max(1, int(page_raw)) if page_raw is not None else 1
    except (TypeError, ValueError):
        page = 1

    if kind == "section":
        return ModeratorNavOrigin(
            kind="section",
            section_status=data.get("st"),
            section_track=data.get("tr"),
            section_age=data.get("ag"),
            section_page=page,
        )
    if kind == "multi_subs":
        return ModeratorNavOrigin(kind="multi_subs", multi_subs_page=page)
    return ModeratorNavOrigin(kind=kind)  # type: ignore[arg-type]


def find_button_data(br_id: str, origin: ModeratorNavOrigin) -> dict[str, str]:
    """Payload для кнопки открытия карточки ``/find``."""
    return {"br_id": br_id, **origin.to_data()}


def action_button_data(br_id: str, origin: ModeratorNavOrigin) -> dict[str, str]:
    """Payload для кнопок действий карточки (comment/files/…)."""
    return find_button_data(br_id, origin)


def append_context_back_button(
    bubbles: BubbleMarkup, origin: ModeratorNavOrigin
) -> None:
    """Добавить контекстную кнопку «назад» (не для ``direct``)."""
    if origin.kind == "direct":
        return

    label = _BACK_LABELS.get(origin.kind, "◀ Назад")
    command, data = _back_command_and_data(origin)
    bubbles.add_button(command=command, label=label, data=data, new_row=True)


def append_moderator_menu_button(bubbles: BubbleMarkup) -> None:
    """Кнопка возврата в меню модератора."""
    bubbles.add_button(
        command="/moderator",
        label="◀ Меню модератора",
        new_row=True,
    )


def append_card_navigation(
    bubbles: BubbleMarkup, origin: ModeratorNavOrigin
) -> None:
    """Контекстный «назад» + меню модератора."""
    append_context_back_button(bubbles, origin)
    append_moderator_menu_button(bubbles)


def build_back_bubbles(origin: ModeratorNavOrigin) -> BubbleMarkup:
    """Только навигационные кнопки (back + меню)."""
    bubbles = BubbleMarkup()
    append_card_navigation(bubbles, origin)
    return bubbles


def _back_command_and_data(
    origin: ModeratorNavOrigin,
) -> tuple[str, dict[str, str] | None]:
    if origin.kind == "section":
        data: dict[str, str] = {}
        if origin.section_status:
            data["st"] = origin.section_status
        if origin.section_track:
            data["tr"] = origin.section_track
        if origin.section_age:
            data["ag"] = origin.section_age
        data["p"] = str(origin.section_page)
        return "/m_list", data
    if origin.kind == "queue":
        return "/m_q_refresh", None
    if origin.kind == "browse":
        return "/m_b_refresh", None
    if origin.kind == "multi_subs":
        return "/multi_subs", None
    if origin.kind == "admin_find":
        return "/admin", None
    return "/moderator", None


def post_action_bubbles(origin: ModeratorNavOrigin) -> BubbleMarkup:
    """Клавиатура после успешного действия модератора."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/queue_next",
        label="▶ Следующая заявка",
        new_row=True,
    )
    append_card_navigation(bubbles, origin)
    return bubbles


def card_action_buttons(
    app: Application,
    origin: ModeratorNavOrigin,
    *,
    with_navigation: bool = True,
) -> BubbleMarkup:
    """Инлайн-кнопки карточки заявки + опциональная навигация."""
    bubbles = BubbleMarkup()
    action_data = action_button_data(app.br_id, origin)

    if app.moderation_status is ModerationStatus.OTKLONENO:
        bubbles.add_button(
            command="/comment",
            label="💬 Комментарий",
            data=action_data,
            new_row=True,
        )
        if with_navigation:
            append_card_navigation(bubbles, origin)
        return bubbles

    bubbles.add_button(
        command="/files",
        label="📂 Файлы",
        data=action_data,
        new_row=True,
    )
    bubbles.add_button(
        command="/status",
        label="✅ Допустить",
        data={
            **action_data,
            "group": "moderation",
            "value": ModerationStatus.DOPUSHCHENO.value,
        },
    )
    bubbles.add_button(
        command="/notify_fix",
        label="✏️ На исправление",
        data=action_data,
    )
    bubbles.add_button(
        command="/notify_reject",
        label="🚫 Отклонить",
        data=action_data,
        new_row=True,
    )
    bubbles.add_button(
        command="/comment",
        label="💬 Комментарий",
        data=action_data,
    )
    if with_navigation:
        append_card_navigation(bubbles, origin)
    return bubbles


def fsm_dialog_prompt_bubbles(
    br_id: str, origin: ModeratorNavOrigin
) -> BubbleMarkup:
    """Кнопки промпта FSM-диалога модератора."""
    bubbles = BubbleMarkup()
    bubbles.add_button(
        command="/find",
        label="◀ К карточке",
        data=find_button_data(br_id, origin),
        new_row=True,
    )
    bubbles.add_button(
        command="/m_cancel_dialog",
        label="◀ Отмена",
        new_row=True,
    )
    return bubbles


async def save_dialog_origin(fsm: Any, *, origin: ModeratorNavOrigin, br_id: str) -> None:
    """Кеш origin перед FSM-диалогом модератора."""
    await fsm.update_data(
        **{
            FSM_KEY_MOD_NAV_ORIGIN_KIND: origin.kind,
            FSM_KEY_MOD_NAV_ORIGIN_DATA: json.dumps(origin.to_dict()),
            FSM_KEY_MOD_NAV_ORIGIN_BR_ID: br_id,
        }
    )


async def clear_dialog_state(fsm: Any) -> None:
    """Сброс FSM-диалога модератора без потери данных очереди/фильтров."""
    await fsm.set_state(None)
    await fsm.update_data(
        **{
            FSM_KEY_MOD_NAV_ORIGIN_KIND: None,
            FSM_KEY_MOD_NAV_ORIGIN_DATA: None,
            FSM_KEY_MOD_NAV_ORIGIN_BR_ID: None,
            FSM_KEY_MODERATOR_TARGET_BR_ID: None,
        }
    )


async def load_dialog_origin(fsm: Any) -> tuple[ModeratorNavOrigin, str]:
    """Прочитать origin из FSM data (после FSM-диалога)."""
    data = await fsm.get_data()
    kind = data.get(FSM_KEY_MOD_NAV_ORIGIN_KIND) or "direct"
    raw_json = data.get(FSM_KEY_MOD_NAV_ORIGIN_DATA)
    br_id = (data.get(FSM_KEY_MOD_NAV_ORIGIN_BR_ID) or "").strip().upper()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            origin = ModeratorNavOrigin.from_dict(parsed)
        except (json.JSONDecodeError, TypeError, ValueError):
            origin = ModeratorNavOrigin(
                kind=kind if kind in _VALID_KINDS else "direct"  # type: ignore[arg-type]
            )
    else:
        origin = ModeratorNavOrigin(
            kind=kind if kind in _VALID_KINDS else "direct"  # type: ignore[arg-type]
        )
    return origin, br_id


__all__ = [
    "ModeratorNavKind",
    "ModeratorNavOrigin",
    "parse_origin",
    "find_button_data",
    "action_button_data",
    "append_context_back_button",
    "append_moderator_menu_button",
    "append_card_navigation",
    "build_back_bubbles",
    "post_action_bubbles",
    "card_action_buttons",
    "fsm_dialog_prompt_bubbles",
    "save_dialog_origin",
    "load_dialog_origin",
    "clear_dialog_state",
]
