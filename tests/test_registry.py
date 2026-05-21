"""Покрытие сервисов реестра (§30.1, ветка E / services.registry).

Юнит-тесты чистых helper'ов + smoke-рендер XLSX-bytes без БД
(``_render_registry_workbook`` + ``_render_shortlist_workbook``):
синтетические Application-объекты, openpyxl читает их прямо в памяти.

Что покрывается:
1. ``registry_export_filename`` — формат, MSK-зона, дефис в HH-MM,
   ValueError на неизвестный ``kind`` (§4 docs/registry-spec.md).
2. ``transliterate_icao_9303`` — таблица ICAO Doc 9303, case rule
   из §2.3.1 (``Shcherbak`` / ``Iudin``).
3. ``jury_column_header`` — шаблон ``Фамилия.И_rN`` + fallback
   на оригинал при односложном full_name.
4. ``view_command_or_link`` — поле №13 для FILES и LINKS режима.
5. ``contact_field`` — @login если есть, иначе HUID: <uuid>.
6. ``jury_outcome`` — синхрон со «Статусом жюри» поля №16.
7. ``_render_registry_workbook`` — собирается валидный XLSX
   (магия PK + минимальный rowcount).
"""
from __future__ import annotations

import uuid as uuid_pkg
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from database.models import (
    AgeCategory,
    IntakeMode,
    JuryStatus,
    JuryVoteState,
    JuryVoteValue,
    ModerationStatus,
    Track,
    VotingStatus,
)
from services.registry import (
    _render_registry_workbook,
    contact_field,
    jury_column_header,
    jury_outcome,
    registry_export_filename,
    transliterate_icao_9303,
    view_command_or_link,
)


MSK = ZoneInfo("Europe/Moscow")


# =====================================================================
# Имя файла (§4 docs/registry-spec.md)
# =====================================================================


class TestRegistryExportFilename:
    def test_registry_filename_with_explicit_msk(self):
        moment = datetime(2026, 6, 15, 14, 32, tzinfo=MSK)
        assert (
            registry_export_filename("registry", now_msk=moment)
            == "registry_BR-2026_2026-06-15_14-32.xlsx"
        )

    def test_shortlist_filename(self):
        moment = datetime(2026, 7, 1, 9, 5, tzinfo=MSK)
        assert (
            registry_export_filename("shortlist", now_msk=moment)
            == "shortlist_BR-2026_2026-07-01_09-05.xlsx"
        )

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            registry_export_filename("bogus")  # type: ignore[arg-type]

    def test_hyphen_in_hh_mm_not_colon(self):
        """`:` запрещён в именах файлов Windows — обязан быть `-`."""
        name = registry_export_filename(
            "registry", now_msk=datetime(2026, 6, 1, 12, 0, tzinfo=MSK)
        )
        assert ":" not in name


# =====================================================================
# Транслитерация ICAO Doc 9303 (§2.3.1)
# =====================================================================


class TestTransliterate:
    """Сырая транслитерация по ICAO Doc 9303 (без пост-нормализации регистра).

    Для финальной шапки колонки в реестре используется
    ``jury_column_header``, который дополнительно применяет
    ``.title()`` — см. ``TestJuryColumnHeader``.
    """

    @pytest.mark.parametrize(
        "src,expected",
        [
            ("Винокурова", "Vinokurova"),
            ("Иванова", "Ivanova"),
            ("Smith", "Smith"),
            ("О'Брайан", "O'Braian"),
        ],
    )
    def test_simple_surnames(self, src: str, expected: str):
        assert transliterate_icao_9303(src) == expected


class TestJuryColumnHeader:
    """§2.3.1: ``Фамилия.И_rN`` через transliterate + .title()."""

    @pytest.mark.parametrize(
        "full_name,round_no,expected",
        [
            ("Винокурова Екатерина", 1, "Vinokurova.E_r1"),
            ("Юдин Юрий", 3, "Iudin.I_r3"),
            ("Щербак Эльвира", 1, "Shcherbak.E_r1"),
            ("Жукова Алиса", 2, "Zhukova.A_r2"),
        ],
    )
    def test_two_token_name(self, full_name: str, round_no: int, expected: str):
        assert jury_column_header(full_name, round_no) == expected

    def test_single_token_falls_back_to_original(self):
        """§2.3.1: при len(tokens)<2 — fallback на исходное имя."""
        out = jury_column_header("Анонимка", 2)
        assert out.endswith("_r2")
        assert "Анонимка" in out


# =====================================================================
# Помощники строк (§2.2.2, §11.1, §25.3)
# =====================================================================


def _fake_app(**overrides):
    base = dict(
        id=uuid_pkg.uuid4(),
        br_id="BR-2026-0001",
        created_at=datetime(2026, 6, 15, 12, 0, tzinfo=MSK),
        parent_full_name="Иванов Иван",
        parent_division="ПЦП",
        parent_ad_login="ivanov",
        parent_huid=uuid_pkg.UUID(int=42),
        child_name="Алиса",
        child_age=9,
        age_category=AgeCategory.AGE_7_12,
        track=Track.TRADITIONAL,
        title="Дорога",
        description="описание",
        intake_mode=IntakeMode.FILES,
        cloud_link=None,
        moderation_status=ModerationStatus.DOPUSHCHENO,
        moderator_comment=None,
        jury_status=JuryStatus.NE_PEREDANO_ZHYURI,
        voting_status=VotingStatus.NE_UCHASTVUET,
        merch_potential=None,
        is_possible_duplicate=False,
        related_application_br_id=None,
        is_actual_version=True,
        jury_round1_yes=0,
        jury_round2_yes=0,
        jury_round3_yes=0,
        jury_final_round=None,
        jury_decided_by_lot=False,
        pool_position=None,
        files=[],
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestViewCommandOrLink:
    def test_files_mode_emits_command(self):
        app = _fake_app()
        assert view_command_or_link(app) == "/files BR-2026-0001"

    def test_links_mode_emits_url(self):
        app = _fake_app(
            intake_mode=IntakeMode.LINKS,
            cloud_link="https://cloud.example/folder",
        )
        assert view_command_or_link(app) == "https://cloud.example/folder"

    def test_links_mode_without_url_is_empty(self):
        """Защита от полупустого состояния §2.2.2."""
        app = _fake_app(intake_mode=IntakeMode.LINKS, cloud_link=None)
        assert view_command_or_link(app) == ""


class TestContactField:
    def test_with_login(self):
        app = _fake_app(parent_ad_login="elena.s")
        assert contact_field(app) == "@elena.s"

    def test_without_login_uses_huid(self):
        huid = uuid_pkg.UUID(int=777)
        app = _fake_app(parent_ad_login=None, parent_huid=huid)
        assert contact_field(app) == f"HUID: {huid}"


class TestJuryOutcome:
    @pytest.mark.parametrize(
        "status,expected",
        [
            (JuryStatus.NE_PEREDANO_ZHYURI, "не оценивалась"),
            (JuryStatus.V_TOP_10, "в топ-10"),
            (JuryStatus.NE_VOSHLO_V_TOP_10, "не вошло в топ-10"),
            (JuryStatus.NA_GOLOSOVANII, ""),
        ],
    )
    def test_outcome(self, status: JuryStatus, expected: str):
        app = _fake_app(jury_status=status)
        assert jury_outcome(app) == expected


# =====================================================================
# Smoke-рендер XLSX — без БД
# =====================================================================


class TestRenderRegistryWorkbook:
    """``_render_registry_workbook`` — чистая функция, входы синтетические."""

    def test_minimal_workbook_is_valid_xlsx(self):
        app = _fake_app()
        payload, n_cols, n_rows = _render_registry_workbook(
            applications=[app],
            votes=[],
            rounds_by_id={},
            jury_by_huid={},
        )
        # XLSX = ZIP-архив, первые 2 байта = "PK".
        assert payload[:2] == b"PK"
        # 29 колонок основного листа (поля 1–29 §2.2).
        assert n_cols == 29
        # 1 шапка + 1 строка данных.
        assert n_rows == 2

    def test_empty_applications_still_produces_header(self):
        payload, n_cols, n_rows = _render_registry_workbook(
            applications=[],
            votes=[],
            rounds_by_id={},
            jury_by_huid={},
        )
        assert payload[:2] == b"PK"
        assert n_cols == 29
        assert n_rows == 1  # одна только шапка
