"""
Стаб сервиса локального файлового хранилища (Wave 1 → ветка D / storage).

Отвечает за:
- создание структуры папок (§21);
- переименование файлов по шаблону §22;
- генерацию текстовых метаданных (description.txt, meta.txt, reason.txt — §23, §24);
- физическое удаление файлов работы при отклонении (§24);
- мониторинг занятого места и автопредупреждения 80/95 % (§28.1, §33.5).

Все пути относительны ``config.ATTACHMENTS_DIR``. Файлы хранятся в
именованном томе ``attachments_volume`` (см. ``docker-compose.yml``).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database.models import Application, ApplicationFile, FileKind


_STUB_MSG = "Wave 1 stub: будет реализовано в Wave 2 / ветка D (storage)"


async def create_application_folder(app: "Application") -> Path:
    """Создать папку заявки по структуре §21.1.

    Путь:
    ``<ATTACHMENTS_DIR>/<YYYY-MM-DD>/<NN_трек>/<возраст>/<ID_Фамилия_Имя_родителя_Имя_ребёнка>/``
    где дата — дата подачи заявки (``Application.created_at`` в TZ
    Europe/Moscow). Если папка уже существует — переиспользуется.
    """
    raise NotImplementedError(_STUB_MSG)


async def rename_and_save_file(
    app: "Application",
    kind: "FileKind",
    angle_no: int | None,
    src_path: Path,
    original_filename: str,
) -> Path:
    """Переименовать и сохранить файл в папку заявки (§22).

    Шаблоны:
    - ``BR-2026-XXXX_original.<ext>`` (FileKind.ORIGINAL);
    - ``BR-2026-XXXX_angle-N.<ext>`` (FileKind.ANGLE, N в 1..4);
    - ``BR-2026-XXXX_ai-image.<ext>`` (FileKind.AI_IMAGE);
    - ``BR-2026-XXXX_diptych.<ext>`` (FileKind.DIPTYCH).
    Исходное расширение сохраняется без конвертации.

    Параллельно создаётся запись в ``ApplicationFile`` с метаданными
    (``original_filename``, размер, MIME). Вызывается после
    ``create_application_folder``.
    """
    raise NotImplementedError(_STUB_MSG)


async def write_meta_txt(app: "Application") -> Path:
    """Сформировать и записать ``meta.txt`` в папку заявки (§23.2).

    Содержит ID, дату подачи (TZ Europe/Moscow), ФИО родителя,
    подразделение, ``@ad_login`` или ``HUID: <uuid>`` (§11.1),
    данные ребёнка, трек, название, описание и таблицу
    «исходное имя → переименованное».
    """
    raise NotImplementedError(_STUB_MSG)


async def write_description_txt(app: "Application") -> Path:
    """Сформировать и записать ``description.txt`` (§23.1).

    Только пользовательское описание работы, без шапки/служебной
    информации. Кодировка UTF-8.
    """
    raise NotImplementedError(_STUB_MSG)


async def write_reason_txt(app: "Application", reason: str) -> Path:
    """Сформировать ``reason.txt`` при отклонении (§24.1).

    ``reason`` пишется в шаблон дословно, без нормализации/обработки
    (см. §27.1 /notify_reject). Шапка содержит ID, дату модерации
    и ФИО модератора.
    """
    raise NotImplementedError(_STUB_MSG)


async def move_to_rejected(app: "Application") -> Path:
    """Перенос метаданных заявки в ``99_Отклонено/<дата_модерации>/`` (§24).

    Физически удаляет файлы работы (``*_original.*``, ``*_angle-*.*``
    и т. п.) и оставляет только ``description.txt`` / ``meta.txt`` /
    ``reason.txt``. Операция оборачивается в транзакцию БД: смена
    статуса коммитится только после успешного ``mv``+``rm`` (§24.3).
    """
    raise NotImplementedError(_STUB_MSG)


async def delete_application_files(app: "Application") -> int:
    """Удалить файлы работы заявки (без переноса метаданных).

    Возвращает число удалённых файлов. Используется как часть
    ``move_to_rejected``, но вынесено отдельной функцией для
    диагностических сценариев.
    """
    raise NotImplementedError(_STUB_MSG)


def get_disk_usage_bytes() -> tuple[int, int]:
    """Получить (used_bytes, total_bytes) для каталога ``ATTACHMENTS_DIR``.

    Чистая функция (``shutil.disk_usage``), без I/O в смысле сети,
    но обращается к файловой системе — поэтому может быть медленной
    на сетевом томе. Не async, чтобы не плодить корутины: для нашего
    случая (1 раз в 30 мин из scheduler) это допустимо.
    """
    raise NotImplementedError(_STUB_MSG)


def should_block_intake() -> bool:
    """Проверка порога ``DISK_BLOCK_PCT`` для блокировки приёма (§28.1).

    True ⇒ бот отвечает родителю «приём временно приостановлен» и
    рекомендует ждать переключения на режим ссылок.
    """
    raise NotImplementedError(_STUB_MSG)


__all__ = [
    "create_application_folder",
    "rename_and_save_file",
    "write_meta_txt",
    "write_description_txt",
    "write_reason_txt",
    "move_to_rejected",
    "delete_application_files",
    "get_disk_usage_bytes",
    "should_block_intake",
]
