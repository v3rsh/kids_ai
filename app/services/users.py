"""
Сервис пользователей: upsert при входящем сообщении + CTS-кэш.

Зачем:
- Таблица ``users`` хранит ``huid → chat_id`` для проактивных DM
  (``services.notifications._resolve_user_chat_id``,
  ``services.discovery._resolve_user_chat_id``). Без записи юзера в
  таблицу любая попытка отправить ему DM (например, «заявка принята»
  при ``parent_huid``) пишет WARNING и не доходит.
- Поля ФИО (``full_name``, ``public_name``) и подразделение
  (``department``) подтягиваются автоматически из CTS — это убирает
  ручные шаги «ФИО родителя» и «Подразделение» из анкеты ``UserIntake``.

Контракт:
- ``upsert_user_from_sender`` — лёгкий апсерт ``huid + chat_id`` +
  ad-данные из ``message.sender``. Дёргается на каждом IncomingMessage
  из личного чата (см. ``handlers._user_sync_middleware``). НЕ ходит
  в CTS.
- ``sync_user_from_cts`` — тяжёлый: вызывает ``bot.search_user_by_huid``,
  заполняет CTS-поля. Не падает наружу — ошибки логируются через
  ``logger.warning``.
- ``ensure_user_profile_loaded`` — blocking-вариант для ``cmd_apply``:
  отдаёт кэш (свежий ≤ ``max_age_sec``) либо синхронно вызывает
  ``sync_user_from_cts`` с таймаутом. Возвращает текущий ``User``
  даже если CTS-вызов упал (тогда CTS-поля могут быть пустыми).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database.db import get_session
from database.models import User
from utils.bot_utils import resolve_bot_id

if TYPE_CHECKING:  # pragma: no cover
    from pybotx import Bot, IncomingMessage


# ============================================================
# Вспомогательные функции
# ============================================================


def _first_email(emails: Any) -> str | None:
    """Первый непустой email из списка (приведённый к lower-case)."""
    if not emails:
        return None
    for raw in emails:
        if not raw:
            continue
        text = str(raw).strip().lower()
        if text:
            return text
    return None


# ============================================================
# Upsert по сообщению (без CTS)
# ============================================================


async def upsert_user_from_sender(
    *,
    sender: Any,
    chat_id: UUID | None,
) -> None:
    """INSERT ... ON CONFLICT (huid) DO UPDATE по данным IncomingMessage.

    Пишет ``huid``, ``chat_id``, ``ad_login``, ``ad_domain``,
    ``username``, ``last_activity=utcnow()``. CTS-поля (email,
    department, ...) не трогает — они приходят отдельно через
    ``sync_user_from_cts``.

    Безопасно вызывать на каждом входящем сообщении из личного чата:
    одна INSERT-операция без чтения, индекс на PK ``huid``.
    """
    huid = getattr(sender, "huid", None)
    if huid is None:
        return

    now = datetime.utcnow()
    values: dict[str, Any] = {
        "huid": huid,
        "chat_id": chat_id,
        "ad_login": getattr(sender, "ad_login", None),
        "ad_domain": getattr(sender, "ad_domain", None),
        "username": getattr(sender, "username", None),
        "last_activity": now,
        "created_at": now,
        "updated_at": now,
    }
    update_set: dict[str, Any] = {
        "last_activity": now,
        "updated_at": now,
    }
    # chat_id обновляем только если пришло непустое значение
    # (защита от затирания при сообщении не из личного чата).
    if chat_id is not None:
        update_set["chat_id"] = chat_id
    # ad-поля обновляем только если пришло непустое (иначе оставляем
    # последнее известное; CTS их потом всё равно перепишет).
    for key in ("ad_login", "ad_domain", "username"):
        if values.get(key):
            update_set[key] = values[key]

    stmt = pg_insert(User).values(**values).on_conflict_do_update(
        index_elements=[User.huid],
        set_=update_set,
    )

    try:
        async with get_session()() as session:
            await session.execute(stmt)
            await session.commit()
    except Exception:
        logger.exception(
            "upsert_user_from_sender: не удалось апсертить пользователя",
            huid=str(huid),
        )


async def upsert_user_from_message(message: "IncomingMessage") -> None:
    """Удобный wrapper над ``upsert_user_from_sender`` для middleware."""
    sender = getattr(message, "sender", None)
    chat = getattr(message, "chat", None)
    chat_id: UUID | None = getattr(chat, "id", None) if chat else None
    if sender is None:
        return
    await upsert_user_from_sender(sender=sender, chat_id=chat_id)


# ============================================================
# Синхронизация с CTS
# ============================================================


def _resolve_full_name(public_name: str | None, username: str | None) -> str:
    """Лучшее доступное ФИО для отображения и подстановки в анкету.

    Дефолтная стратегия — ``public_name`` (часто содержит полное ФИО
    «Фамилия Имя Отчество» в корпоративном CTS) с fallback на
    ``username``. После сбора реальных снимков в проде (через
    DEBUG-лог в ``sync_user_from_cts``) может потребоваться более
    точная эвристика (например, fallback при <3 слов).
    """
    if public_name and public_name.strip():
        return public_name.strip()
    if username and username.strip():
        return username.strip()
    return ""


async def sync_user_from_cts(bot: "Bot", huid: UUID) -> User | None:
    """Подтянуть профиль пользователя из CTS и обновить ``users``.

    Дёргает ``bot.search_user_by_huid``, апсертит CTS-поля
    (``email``, ``ip_phone``, ``other_phone``, ``department``,
    ``company``, ``company_position``, ``public_name``, ``username``)
    + вычисляет финальное ``full_name = public_name or username``.
    Ставит ``cts_synced_at=utcnow()``.

    На любые сбои (нет бота, CTS не ответил, юзер не найден) логирует
    WARNING/EXCEPTION и возвращает существующего ``User`` из БД
    (или ``None``, если в БД его тоже нет).
    """
    bot_id = resolve_bot_id(bot)
    if bot_id is None:
        logger.warning(
            "sync_user_from_cts: не удалось определить bot_id",
            huid=str(huid),
        )
        return await _select_user(huid)

    try:
        cts_user = await bot.search_user_by_huid(bot_id=bot_id, huid=huid)
    except Exception:
        logger.exception(
            "sync_user_from_cts: search_user_by_huid упал",
            huid=str(huid),
        )
        return await _select_user(huid)

    # Отладочный снимок — нужен, чтобы определить, какое из полей
    # CTS (public_name vs username) у нас реально содержит ФИО с
    # отчеством. После сбора 5-10 реальных снимков финальная стратегия
    # выбирается отдельной правкой (см. plan, «Открытые вопросы»).
    logger.info(
        "CTS profile snapshot",
        huid=str(huid),
        username=cts_user.username,
        public_name=cts_user.public_name,
        department=cts_user.department,
        company_position=cts_user.company_position,
        emails=list(cts_user.emails or []),
        ip_phone=cts_user.ip_phone,
        other_phone=cts_user.other_phone,
    )

    now = datetime.utcnow()
    email = _first_email(cts_user.emails)
    full_name = _resolve_full_name(cts_user.public_name, cts_user.username)

    values: dict[str, Any] = {
        "huid": huid,
        "ad_login": cts_user.ad_login,
        "ad_domain": cts_user.ad_domain,
        "username": cts_user.username,
        "email": email,
        "ip_phone": cts_user.ip_phone,
        "other_phone": cts_user.other_phone,
        "department": cts_user.department,
        "company": cts_user.company,
        "company_position": cts_user.company_position,
        "public_name": cts_user.public_name,
        "full_name": full_name,
        "cts_synced_at": now,
        "created_at": now,
        "updated_at": now,
    }
    update_set = {
        k: v for k, v in values.items()
        if k not in ("huid", "created_at")
    }

    stmt = pg_insert(User).values(**values).on_conflict_do_update(
        index_elements=[User.huid],
        set_=update_set,
    )

    try:
        async with get_session()() as session:
            await session.execute(stmt)
            await session.commit()
    except Exception:
        logger.exception(
            "sync_user_from_cts: не удалось записать CTS-профиль в БД",
            huid=str(huid),
        )
        return await _select_user(huid)

    logger.info(
        "CTS профиль синхронизирован",
        huid=str(huid),
        full_name=full_name,
        department=cts_user.department or "",
        email=email or "",
    )
    return await _select_user(huid)


async def _select_user(huid: UUID) -> User | None:
    """SELECT * FROM users WHERE huid = :huid (одиночный SELECT)."""
    try:
        async with get_session()() as session:
            result = await session.execute(
                select(User).where(User.huid == huid)
            )
            user = result.scalar_one_or_none()
            if user is not None:
                session.expunge(user)
            return user
    except Exception:
        logger.exception("_select_user упал", huid=str(huid))
        return None


# ============================================================
# Ensure profile loaded (blocking для /apply)
# ============================================================


async def ensure_user_profile_loaded(
    bot: "Bot",
    huid: UUID,
    *,
    max_age_sec: int = 86400,
    timeout: float = 5.0,
) -> User | None:
    """Гарантировать, что профиль пользователя в ``users`` актуален.

    Если ``users.cts_synced_at`` свежий (``< max_age_sec``) — отдаём
    кэш без сетевого вызова. Иначе вызываем ``sync_user_from_cts`` с
    ``asyncio.wait_for(timeout)``. На таймаут — WARNING и возврат
    последнего известного ``User`` (возможно, с пустыми CTS-полями).

    Используется в ``handlers.user.cmd_apply``, чтобы перед стартом
    анкеты заполнить ``parent_full_name`` и ``parent_division`` из CTS
    без видимой пользователю задержки в случае горячего кэша.

    Args:
        bot: pybotx Bot для CTS API.
        huid: UUID пользователя.
        max_age_sec: TTL кэша в секундах (24 часа по умолчанию).
        timeout: максимальное время ожидания CTS-вызова в секундах.

    Returns:
        ``User`` из БД (со свежими или старыми CTS-полями) или ``None``,
        если юзера в БД ещё нет и CTS-вызов не удался.
    """
    cached = await _select_user(huid)
    if cached is not None and cached.cts_synced_at is not None:
        age = datetime.utcnow() - cached.cts_synced_at
        if age < timedelta(seconds=max_age_sec):
            logger.debug(
                "ensure_user_profile_loaded: используем кэш",
                huid=str(huid),
                age_sec=int(age.total_seconds()),
            )
            return cached

    try:
        return await asyncio.wait_for(
            sync_user_from_cts(bot, huid),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "ensure_user_profile_loaded: CTS-вызов не уложился в таймаут",
            huid=str(huid),
            timeout_sec=timeout,
        )
        return cached


__all__ = [
    "ensure_user_profile_loaded",
    "sync_user_from_cts",
    "upsert_user_from_message",
    "upsert_user_from_sender",
]
