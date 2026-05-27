"""Юнит-тесты ``services.jury_settings`` (runtime-конфиг жюри).

Проверяем:
- дефолты при отсутствии записи в ``app_settings`` и при мусоре в БД;
- кэш в памяти + инвалидация на set;
- валидация диапазона (1..``JURY_MAX_ROUND_HARD_LIMIT``);
- парсинг ``on``/``off``/``true``/``false`` и т.п.

Тесты гоняются без реальной БД: мокаем ``get_session`` на async-фейк,
который отдаёт нужное значение из подменяемого `dict`-хранилища.
"""
from __future__ import annotations

from typing import Any

import pytest

from services import jury_settings as jsettings


class _FakeResult:
    """Имитация ``Result`` из SQLAlchemy: first / scalar_one_or_none."""

    def __init__(self, row: tuple | None):
        self._row = row

    def first(self):
        return self._row

    def scalar_one_or_none(self):
        if self._row is None:
            return None
        return self._row[0]


def _extract_lookup_key(stmt) -> str | None:
    """Достать ключ из WHERE-условия ``AppSetting.key == :key``.

    SQLAlchemy биндит ключ как параметр, поэтому ``str(stmt)`` его не
    содержит. Используем ``compile(literal_binds=True)`` — простейший
    способ получить SQL с подставленными значениями.
    """
    try:
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    except Exception:
        return None
    sql = str(compiled)
    for key in (jsettings.JURY_MAX_ROUND_KEY, jsettings.JURY_AUTO_LOT_KEY):
        if f"'{key}'" in sql:
            return key
    return None


def _is_select_value_only(stmt) -> bool:
    """`select(AppSetting.value)` — отдельная сигнатура без модели."""
    try:
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
    except Exception:
        return False
    return "app_settings.value" in str(compiled).lower() and (
        "from app_settings" in str(compiled).lower()
    ) and (
        # ровно одна колонка, без SELECT app_settings.*
        ".key," not in str(compiled).lower()
    )


class _FakeSetting:
    """Маленький суррогат `AppSetting` с записью назад в storage."""

    def __init__(self, key: str, storage: dict[str, str]):
        self.key = key
        self._storage = storage

    @property
    def value(self) -> str:
        return self._storage.get(self.key, "")

    @value.setter
    def value(self, new_value: str) -> None:
        self._storage[self.key] = new_value


class _FakeSession:
    """Мини-сессия: выполняет фейковые SELECT и тривиальный add/commit."""

    def __init__(self, storage: dict[str, str]):
        self.storage = storage
        self.added: list = []

    async def execute(self, stmt) -> _FakeResult:
        key = _extract_lookup_key(stmt)
        if key is None:
            return _FakeResult(None)
        value = self.storage.get(key)
        if value is None:
            return _FakeResult(None)
        if _is_select_value_only(stmt):
            return _FakeResult((value,))
        return _FakeResult((_FakeSetting(key, self.storage),))

    def add(self, obj: Any) -> None:
        self.added.append(obj)
        # ``AppSetting`` из database.models — атрибуты key/value доступны
        # напрямую. Записываем сразу, не дожидаясь commit().
        key = getattr(obj, "key", None)
        value = getattr(obj, "value", None)
        if isinstance(key, str) and isinstance(value, str):
            self.storage[key] = value

    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SessionFactory:
    """``get_session()()`` → async-CM. Возвращаем общий ``_FakeSession``."""

    def __init__(self, storage: dict[str, str]):
        self._session = _FakeSession(storage)

    def __call__(self):
        return self._session


@pytest.fixture(autouse=True)
def _reset_cache():
    """Каждому тесту чистый кэш — без него тесты влияли бы друг на друга."""
    jsettings.reset_cache()
    yield
    jsettings.reset_cache()


@pytest.fixture
def storage():
    return {}


@pytest.fixture
def _patch_get_session(monkeypatch, storage):
    factory = lambda: _SessionFactory(storage)  # noqa: E731
    monkeypatch.setattr(jsettings, "get_session", factory)
    return storage


class TestJuryMaxRound:
    @pytest.mark.asyncio
    async def test_default_when_empty(self, _patch_get_session):
        """Пустой `app_settings` → дефолт из config."""
        from config import JURY_MAX_ROUND_DEFAULT

        value = await jsettings.get_jury_max_round()
        assert value == JURY_MAX_ROUND_DEFAULT

    @pytest.mark.asyncio
    async def test_persisted_value(self, _patch_get_session):
        await jsettings.set_jury_max_round(5)
        value = await jsettings.get_jury_max_round()
        assert value == 5

    @pytest.mark.asyncio
    async def test_invalid_value_raises(self, _patch_get_session):
        with pytest.raises(ValueError):
            await jsettings.set_jury_max_round(0)
        with pytest.raises(ValueError):
            await jsettings.set_jury_max_round(jsettings.JURY_MAX_ROUND_HARD_LIMIT + 1)
        with pytest.raises(ValueError):
            await jsettings.set_jury_max_round("abc")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_garbage_in_db_falls_back_to_default(self, _patch_get_session):
        """Нечисловая запись в БД → дефолт + warning в логи."""
        from config import JURY_MAX_ROUND_DEFAULT

        _patch_get_session[jsettings.JURY_MAX_ROUND_KEY] = "not-a-number"
        value = await jsettings.get_jury_max_round()
        assert value == JURY_MAX_ROUND_DEFAULT

    @pytest.mark.asyncio
    async def test_out_of_range_in_db_falls_back(self, _patch_get_session):
        from config import JURY_MAX_ROUND_DEFAULT

        _patch_get_session[jsettings.JURY_MAX_ROUND_KEY] = "9999"
        value = await jsettings.get_jury_max_round()
        assert value == JURY_MAX_ROUND_DEFAULT


class TestJuryAutoLot:
    @pytest.mark.asyncio
    async def test_default_when_empty(self, _patch_get_session):
        from config import JURY_AUTO_LOT_DEFAULT

        value = await jsettings.get_jury_auto_lot()
        assert value is bool(JURY_AUTO_LOT_DEFAULT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stored,expected",
        [
            ("on", True),
            ("true", True),
            ("1", True),
            ("yes", True),
            ("off", False),
            ("false", False),
            ("0", False),
            ("no", False),
        ],
    )
    async def test_parsing_synonyms(
        self, _patch_get_session, stored: str, expected: bool
    ):
        _patch_get_session[jsettings.JURY_AUTO_LOT_KEY] = stored
        value = await jsettings.get_jury_auto_lot()
        assert value is expected

    @pytest.mark.asyncio
    async def test_garbage_in_db_falls_back_to_default(self, _patch_get_session):
        from config import JURY_AUTO_LOT_DEFAULT

        _patch_get_session[jsettings.JURY_AUTO_LOT_KEY] = "maybe"
        value = await jsettings.get_jury_auto_lot()
        assert value is bool(JURY_AUTO_LOT_DEFAULT)

    @pytest.mark.asyncio
    async def test_toggle(self, _patch_get_session):
        await jsettings.set_jury_auto_lot(False)
        assert await jsettings.get_jury_auto_lot() is False
        await jsettings.set_jury_auto_lot(True)
        assert await jsettings.get_jury_auto_lot() is True


class TestCache:
    @pytest.mark.asyncio
    async def test_cache_returns_same_value_without_db_call(
        self, _patch_get_session
    ):
        """Повторный вызов get_jury_max_round не обращается в БД повторно."""
        await jsettings.set_jury_max_round(7)
        # Прогреваем кэш: первый get читает из БД, дальше — из кэша.
        assert await jsettings.get_jury_max_round() == 7
        # Меняем хранилище мимо API — кэш должен сохранить старое значение.
        _patch_get_session[jsettings.JURY_MAX_ROUND_KEY] = "42"
        value = await jsettings.get_jury_max_round()
        assert value == 7

    @pytest.mark.asyncio
    async def test_set_invalidates_cache(self, _patch_get_session):
        """``set_*`` сбрасывает кэш — следующий ``get_*`` читает БД."""
        await jsettings.set_jury_max_round(7)
        assert await jsettings.get_jury_max_round() == 7
        await jsettings.set_jury_max_round(11)
        assert await jsettings.get_jury_max_round() == 11

    @pytest.mark.asyncio
    async def test_reset_cache_helper(self, _patch_get_session):
        await jsettings.set_jury_max_round(7)
        _patch_get_session[jsettings.JURY_MAX_ROUND_KEY] = "12"
        jsettings.reset_cache()
        assert await jsettings.get_jury_max_round() == 12
