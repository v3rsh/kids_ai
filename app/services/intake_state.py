"""
Состояние «приём заявок открыт/закрыт».

Логически отдельная настройка от ``services.intake_mode`` — режим
хранения (FILES / LINKS) сохраняется, но новые заявки приниматься
не будут, если приём закрыт.

Используется в:
- ``handlers.user.cmd_apply`` — гард на старте анкеты;
- ``handlers.user_confirm.cmd_submit`` — повторный гард на случай
  гонки (юзер начал анкету до закрытия и долго заполнял);
- админ-меню ``handlers.admin`` — кнопка «🔒 Закрыть/Открыть приём».

Источник истины — таблица ``app_settings`` (key=``intake_open``,
value=``"true"`` / ``"false"``). Дефолт при отсутствии записи — открыт
(`is_intake_open()` → True).

После 15.06.2026 админ один раз нажимает «Закрыть приём»; запись
остаётся в БД и переживает рестарт. Уже созданные LINKS-черновики
(``intake_mode=LINKS`` + ``cloud_link IS NULL``) могут дослать ссылку
через ``/resume_link`` — этот сценарий гарду в ``cmd_apply`` не
подвластен.
"""
from __future__ import annotations

from uuid import UUID

from loguru import logger
from sqlalchemy import select

from database.db import get_session
from database.models import AppSetting

INTAKE_OPEN_KEY = "intake_open"
"""Ключ в ``app_settings`` для состояния приёма."""

#: UUID-«отправитель» для системных переключений (например, в будущем,
#: если появится автозакрытие по дате). Сейчас используется только
#: вручную из админ-меню.
SYSTEM_HUID = UUID("00000000-0000-0000-0000-000000000000")


def _parse_bool(raw: str | None) -> bool | None:
    """Разобрать строку из ``app_settings`` в bool.

    Возвращает ``None``, если значение нераспознаваемо — caller
    решает, как обработать (обычно — fallback к дефолту).
    """
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in ("true", "1", "yes", "on", "open"):
        return True
    if normalized in ("false", "0", "no", "off", "closed"):
        return False
    return None


async def is_intake_open() -> bool:
    """Открыт ли сейчас приём заявок.

    По умолчанию (запись отсутствует) — открыт. Это сделано
    специально: до явного закрытия конкурс должен работать.
    """
    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting.value).where(AppSetting.key == INTAKE_OPEN_KEY)
        )
        row = result.first()
        if row is None:
            return True
        parsed = _parse_bool(row[0])
        if parsed is None:
            logger.warning(
                "Неизвестное значение intake_open в БД — fallback на True",
                stored=row[0],
            )
            return True
        return parsed


async def set_intake_open(
    open_: bool,
    *,
    by_huid: UUID,
    reason: str | None = None,
) -> None:
    """UPSERT состояния приёма в ``app_settings``.

    Args:
        open_: новое состояние (True — открыт, False — закрыт).
        by_huid: HUID того, кто переключил (админ или ``SYSTEM_HUID``).
            Используется только в логах; в БД историю не пишем.
        reason: опциональная пояснительная строка для лога.
    """
    value = "true" if open_ else "false"

    async with get_session()() as session:
        result = await session.execute(
            select(AppSetting).where(AppSetting.key == INTAKE_OPEN_KEY)
        )
        setting = result.scalar_one_or_none()
        if setting is None:
            session.add(AppSetting(key=INTAKE_OPEN_KEY, value=value))
        else:
            setting.value = value
        await session.commit()

    logger.info(
        "Состояние приёма заявок переключено",
        new_state="open" if open_ else "closed",
        by_huid=str(by_huid),
        reason=reason or "",
    )


__all__ = [
    "INTAKE_OPEN_KEY",
    "SYSTEM_HUID",
    "is_intake_open",
    "set_intake_open",
]
