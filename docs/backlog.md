# Backlog

Документ-ровесник реестра: всё, что сознательно отложено и должно быть
подобрано в одном из следующих релизов. Каждая запись — короткий
контракт: что нужно сделать, какие файлы трогать, какие сценарии
тестировать. Без этого риск, что задача потеряется.

Ссылки внутри пунктов идут на актуальные разделы документации
([`architecture.md`](architecture.md), [`registry-spec.md`](registry-spec.md),
[`deployment.md`](deployment.md), [`testing.md`](testing.md)) — это
единственный источник правды по требованиям.

## Done

### Закрытие приёма заявок и архивный экспорт каталога — ✅ выполнено

Реализовано двумя независимыми, но идущими в одном PR блоками — в
ответ на требования заказчика «после 15.06 закрыть приём» и «забрать
весь каталог при 95 % занятого диска без SSH к серверу». См.
[`architecture.md`](architecture.md) → разделы «Закрытие приёма
заявок» и «Архивная выгрузка `data/attachments`».

Состав изменений:

- `app/services/intake_state.py` — UPSERT/READ ключа `intake_open` в
  `app_settings` (дефолт — открыт); тесты `tests/test_intake_state.py`.
- Гарды: `handlers/user.cmd_apply` и `handlers/user_confirm.cmd_submit`
  показывают `INTAKE_CLOSED_TEXT` + `intake_closed_bubbles`. LINKS-черновики
  (cloud_link=NULL) продолжают работать через `/resume_link`.
- Команда `/admin_intake_open` (раздел «🖥 Система»), ветки
  `close_intake`/`reopen_intake` в `cmd_admin_confirm`, бейдж
  `🔒closed`/`open` в `admin_main_menu_bubbles`, строки в `/admin_state`
  и `/disk`.
- `app/services/attachments_export.py` — `iter_attachments_export(SHORTLIST)`
  (через `services.registry.fetch_shortlist_applications`), tar.gz на
  пул `(track, age_category)` в памяти с группировкой и split-ом по
  `EXPORT_MAX_PART_BYTES`, `manifest.csv` (UTF-8+BOM, `;`), `links.txt`,
  `summary`. LINKS-заявки попадают в tar.gz пула с `meta.txt` +
  `cloud_link.txt`; без ссылки — `manifest.status=pending_link`. При
  превышении лимита на одну заявку — `oversize_meta_only`.
- `app/services/attachments_archive.py` — полный архив на диск
  `data/archive/bd-full.tar.gz` со всеми BR-ID-каталогами + manifest +
  summary внутри tar и рядом. Pre-flight `ArchiveBudgetExceeded` при
  >= `ARCHIVE_DISK_CAP_PCT`.
- `app/handlers/admin_export.py` — `/admin_export_shortlist_files`
  (двухшаговое подтверждение, фоновый `asyncio.Task`),
  `/admin_export_app BR-...` для точечной переотправки. Пауза
  `EXPORT_PAUSE_MS` между сообщениями.
- env-параметры: `EXPORT_PAUSE_MS` (800 мс) и
  `EXPORT_MAX_PART_BYTES` (90 МБ) в [`deployment.md`](deployment.md).
- Тесты: `tests/test_intake_state.py` (toggle/persistence),
  `tests/test_attachments_export.py` (FILES/LINKS/oversize/manifest/links).

### Что не вошло (follow-up при необходимости)

1. **Автоматическое закрытие приёма по дате** (cron/scheduler-job на
   00:00 16.06 МСК → `set_intake_open(False)`). Сейчас закрывается
   вручную одной кнопкой; явное действие админа полезно для аудита.
2. **Прогресс-бар выгрузки** (`/admin_export_status`). Сейчас фоновая
   задача шлёт архивы по одному, summary приходит в конце; для десятков
   заявок этого хватает, для тысяч можно добавить промежуточные
   «n/total отправлено» сообщения.
3. **Хеши + дедупликация tar.gz**. Если выгрузка прерывается и
   запускается повторно — текущая реализация шлёт всё заново.
   На наших объёмах не критично, но для миграции к большому конкурсу
   стоит добавить idempotency-key в `manifest.csv` и пропуск
   уже отправленных BR-ID.

### LINKS-UX — пользовательский UX режима LINKS — ✅ выполнено

Реализовано по варианту C (submit → BR-ID в БД → инструкция со ссылкой).
См. [`architecture.md`](architecture.md) → «Резервный сценарий приёма
по ссылкам» и `app/handlers/user_links.py`.

Состав изменений (для исторического контекста):

- FSM `UserIntake.user_intake_link_collect` (`app/states.py`); развилка
  в `user_intake._handle_description`: при `intake_mode = LINKS` шаг
  загрузки файлов пропускается и сразу показываются согласия.
- `user_confirm.cmd_submit` в LINKS-ветке создаёт заявку с
  `cloud_link=None`, не материализует файлы, не шлёт уведомления и
  переводит FSM в `user_intake_link_collect`.
- Новый модуль `app/handlers/user_links.py`: инструкция по §33.6.2 ТЗ
  с реальным BR-ID и именем папки (на основе ФИО+имени ребёнка),
  валидация URL через `app/utils/cloud_link.parse_cloud_link`, UPDATE
  через `applications.set_application_cloud_link`, отложенный
  `write_meta_txt` и нотификации только после получения URL.
- Resume-кнопка «🔗 Прислать ссылку на папку» в карточке заявки
  (`keyboards.my_application_detail_bubbles`) — для случая, когда FSM
  потерян после рестарта Redis. Аналогичный индикатор «Ожидает ссылку»
  в `services.user_application_views`.
- Карточка модератора (`moderator_queue._full_card`,
  `moderator_actions.cmd_files`) показывает «⏳ Ожидает ссылку от
  участника» вместо «Ссылка на папку: —».
- Тесты: `tests/test_user_links.py` (валидатор URL, форматирование
  инструкции), `tests/test_application_flow.TestSetApplicationCloudLink`.

### Что не вошло (follow-up при необходимости)

1. **История ссылок при исправлениях** (§33.6.4). Сейчас
   `set_application_cloud_link` устроена под перезапись (логирует
   старый/новый URL), но отдельной таблицы истории нет. Если заказчик
   захочет видеть все версии — добавить `application_cloud_link_history`
   с `(br_id, url, set_at, set_by_huid)` и писать INSERT перед UPDATE.
2. **Cleanup-команда для abandoned-заявок** (`intake_mode=LINKS AND
   cloud_link IS NULL` старше N часов). На текущей шкале (десятки
   заявок) — лечится точечно админом, не масштабная проблема.
3. **Жёсткий запрет двух параллельных LINKS-черновиков у одного
   родителя**. Сейчас `/apply` сбрасывает FSM, а первая заявка остаётся
   в БД (видна через «Мои заявки» с кнопкой «Прислать ссылку»).
   Если будут жалобы — добавить guard в `cmd_apply`.
