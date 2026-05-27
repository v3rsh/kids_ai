"""Юнит-тесты ``services.intake_state``.

Тесты гоняются без реальной БД: подменяем ``get_session`` на фейк-фабрику,
которая возвращает простую in-memory сессию ``_FakeSession``.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from services import intake_state


class _FakeResult:
    def __init__(self, row: tuple | None):
        self._row = row

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        return None if self._row is None else self._row[0]


class _FakeSetting:
    def __init__(self, key: str, storage: dict[str, str]):
        self.key = key
        self._storage = storage

    @property
    def value(self) -> str:
        return self._storage.get(self.key, "")

    @value.setter
    def value(self, new_value: str) -> None:
        self._storage[self.key] = new_value


def _is_select_value_only(stmt) -> bool:
    try:
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    except Exception:
        return False
    sql = str(compiled).lower()
    return "app_settings.value" in sql and ".key," not in sql


def _is_intake_open_lookup(stmt) -> bool:
    try:
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    except Exception:
        return False
    return f"'{intake_state.INTAKE_OPEN_KEY}'" in str(compiled)


class _FakeSession:
    def __init__(self, storage: dict[str, str]):
        self.storage = storage

    async def execute(self, stmt) -> _FakeResult:
        if not _is_intake_open_lookup(stmt):
            return _FakeResult(None)
        value = self.storage.get(intake_state.INTAKE_OPEN_KEY)
        if value is None:
            return _FakeResult(None)
        if _is_select_value_only(stmt):
            return _FakeResult((value,))
        return _FakeResult((_FakeSetting(intake_state.INTAKE_OPEN_KEY, self.storage),))

    def add(self, obj: Any) -> None:
        key = getattr(obj, "key", None)
        value = getattr(obj, "value", None)
        if isinstance(key, str) and isinstance(value, str):
            self.storage[key] = value

    async def commit(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionFactory:
    def __init__(self, storage: dict[str, str]):
        self._session = _FakeSession(storage)

    def __call__(self):
        return self._session


@pytest.fixture
def storage() -> dict[str, str]:
    return {}


@pytest.fixture
def _patch_get_session(monkeypatch, storage):
    factory = lambda: _SessionFactory(storage)  # noqa: E731
    monkeypatch.setattr(intake_state, "get_session", factory)
    return storage


_ADMIN_HUID = UUID("11111111-2222-3333-4444-555555555555")


class TestParseBool:
    @pytest.mark.parametrize("raw", ["true", "1", "yes", "ON", " open "])
    def test_truthy(self, raw):
        assert intake_state._parse_bool(raw) is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", "closed"])
    def test_falsy(self, raw):
        assert intake_state._parse_bool(raw) is False

    @pytest.mark.parametrize("raw", [None, "", "wat", "maybe"])
    def test_unknown(self, raw):
        assert intake_state._parse_bool(raw) is None


class TestIsIntakeOpen:
    @pytest.mark.asyncio
    async def test_default_is_open_when_empty(self, _patch_get_session):
        """Пустая запись в БД ⇒ дефолт ОТКРЫТ (важно: иначе при первом
        запуске бота приём был бы по ошибке закрыт)."""
        assert await intake_state.is_intake_open() is True

    @pytest.mark.asyncio
    async def test_persisted_true(self, _patch_get_session):
        _patch_get_session[intake_state.INTAKE_OPEN_KEY] = "true"
        assert await intake_state.is_intake_open() is True

    @pytest.mark.asyncio
    async def test_persisted_false(self, _patch_get_session):
        _patch_get_session[intake_state.INTAKE_OPEN_KEY] = "false"
        assert await intake_state.is_intake_open() is False

    @pytest.mark.asyncio
    async def test_garbage_falls_back_to_open(self, _patch_get_session):
        """Мусор в значении (например, ручная правка миграцией) ⇒
        дефолт ОТКРЫТ + warning в логах. Лучше «лишний» приём,
        чем неожиданно закрытый."""
        _patch_get_session[intake_state.INTAKE_OPEN_KEY] = "garbage"
        assert await intake_state.is_intake_open() is True


class TestSetIntakeOpen:
    @pytest.mark.asyncio
    async def test_insert_when_missing(self, _patch_get_session):
        await intake_state.set_intake_open(False, by_huid=_ADMIN_HUID)
        assert _patch_get_session[intake_state.INTAKE_OPEN_KEY] == "false"
        assert await intake_state.is_intake_open() is False

    @pytest.mark.asyncio
    async def test_update_existing(self, _patch_get_session):
        _patch_get_session[intake_state.INTAKE_OPEN_KEY] = "false"
        await intake_state.set_intake_open(
            True, by_huid=_ADMIN_HUID, reason="reopen after fix"
        )
        assert _patch_get_session[intake_state.INTAKE_OPEN_KEY] == "true"
        assert await intake_state.is_intake_open() is True

    @pytest.mark.asyncio
    async def test_idempotent_to_same_value(self, _patch_get_session):
        await intake_state.set_intake_open(False, by_huid=_ADMIN_HUID)
        await intake_state.set_intake_open(False, by_huid=_ADMIN_HUID)
        assert _patch_get_session[intake_state.INTAKE_OPEN_KEY] == "false"
