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
