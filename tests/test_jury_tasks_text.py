"""Тесты текста экрана задачи жюри."""
from __future__ import annotations

from unittest.mock import MagicMock

from database.models import AgeCategory, Track
from handlers.jury_tasks import _render_task_text
from utils.contracts import PoolKey


def _app() -> MagicMock:
    app = MagicMock()
    app.title = "Скоро цветы распустятся вновь"
    app.description = "Описание работы."
    return app


def _render(*, attachment_count: int = 0, cloud_link: str | None = None) -> str:
    return _render_task_text(
        pool=PoolKey(track=Track.TRADITIONAL, age_category=AgeCategory.AGE_13_18),
        round_no=1,
        index=0,
        total=10,
        app=_app(),
        current_vote=None,
        progress_yes=0,
        progress_no=0,
        cloud_link=cloud_link,
        can_submit=False,
        attachment_count=attachment_count,
    )


class TestRenderTaskTextMultiFilesNotice:
    def test_no_notice_for_single_file(self):
        text = _render(attachment_count=1)
        assert "Внимание" not in text

    def test_no_notice_without_files(self):
        text = _render(attachment_count=0)
        assert "Внимание" not in text

    def test_notice_for_two_files(self):
        text = _render(attachment_count=2)
        assert "В этой работе 2 файла, они находятся под меню." in text

    def test_notice_for_four_files(self):
        text = _render(attachment_count=4)
        assert "В этой работе 4 файла, они находятся под меню." in text

    def test_notice_after_instruction(self):
        text = _render(attachment_count=3)
        instruction_pos = text.index("**Инструкция:**")
        notice_pos = text.index("**Внимание!**")
        assert notice_pos > instruction_pos
