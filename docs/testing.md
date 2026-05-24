# Тестирование kids_ai

> Этот документ — единственный источник правды по тестированию проекта:
> что и **как запускать**, что **покрыто** автотестами, и какой
> минимальный сценарий нужно прогнать вручную перед деплоем (см. §6 ниже).
> Исторический контекст оригинального чек-листа — в архиве
> [`docs/history/ТЗ.md`](history/ТЗ.md), к нему не возвращаемся.

---

## 1. Установка зависимостей

```bash
pip install -r requirements.txt
```

Тестовый блок в `requirements.txt`:

```
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

> ⚠️ Если ставите в системный Python (без venv) и видите конфликт
> с `aiogram` / `pydantic>=2` — поднимите venv: тесты не зависят от
> aiogram, но pip ругается. Pytest при этом запускается нормально.

---

## 2. Запуск всех тестов

Из корня репозитория:

```bash
python -m pytest tests/ -q
```

`asyncio_mode=auto` (см. `pytest.ini`) — все `async def`-тесты
запускаются без явного маркера `@pytest.mark.asyncio`.

Параметры:

- `-x` — остановиться на первой ошибке (CI/быстрый локальный smoke);
- `-k <pattern>` — выборочный запуск по имени;
- `--lf` — перепрогон только упавших с прошлого запуска.

### Пример: только flow-тесты

```bash
python -m pytest tests/test_application_flow.py tests/test_jury_flow.py -q
```

---

## 3. Структура тестов

| Файл | Что покрывает | Связанные документы |
|------|---------------|---------------------|
| `tests/conftest.py` | Общий setup: env (`BOT_ID`/`CTS_URL`/...), `sys.path` для `from services import ...`, маркер `slow` | — |
| `tests/test_validation.py` | Валидаторы и нормализаторы пользовательского ввода (анкета участника). **1 пре-existing fail в `test_sanitize_input`** — оставлен под отдельную правку | [`architecture.md`](architecture.md) → «FSM-система» |
| `tests/test_jury_algorithm.py` | 3 классических кейса алгоритма раунда: 1 раунд без ничьи, ничья на границе, эскалация в раунд 3 | [`architecture.md`](architecture.md) → «Модель данных» (jury_rounds, jury_votes) |
| `tests/test_application_flow.py` | `services.applications`: normalize_child_name, AgeCategory.from_age, _select_next_br_number (мок-сессия), find_possible_duplicate edge-cases, валидация `intake_mode` | [`architecture.md`](architecture.md) → «applications» |
| `tests/test_moderation_flow.py` | `services.moderation`: parse_status_group алиасы, _moderation_status_by_value / _voting_status_by_value, _build_queue_where_clauses, DEFAULT_QUEUE_STATUSES | [`architecture.md`](architecture.md) → «applications» (поля статусов) |
| `tests/test_jury_flow.py` | Расширение `test_jury_algorithm`: размер top_n, above_tie == TOP_N, детерминизм сортировки, `services.pools.all_pools()` = 3×3 = 9 пулов | [`architecture.md`](architecture.md) → «jury_pool_assignments» |
| `tests/test_registry.py` | `services.registry`: `registry_export_filename`, `transliterate_icao_9303`, `jury_column_header`, `view_command_or_link`, `contact_field`, `jury_outcome` + smoke-рендер XLSX через `_render_registry_workbook` (без БД) | [`registry-spec.md`](registry-spec.md) → §2.2, §2.3.1, §4 |

---

## 4. Архитектурные принципы тестов

1. **Без поднятия PostgreSQL.** Модели используют PG-специфичные
   фичи (`UUID(as_uuid=True)`, `JSONB`, `pg_advisory_xact_lock`),
   поэтому полная интеграция остаётся в **ручном smoke-чек-листе**
   (см. §6). Юнит-тесты гоняем на чистых функциях и мок-сессиях.
2. **Без поднятия Redis.** FSM-storage в тестах не нужен — мы тестируем
   слой сервисов, а не handler'ы pybotx.
3. **Pure functions сначала.** Где можно — тестируется чистая функция
   (например, `normalize_child_name`, `_compute_outcome_from_data`,
   `transliterate_icao_9303`). Это даёт быстрый и стабильный сигнал
   на регресс алгоритмов.
4. **Async через `AsyncMock`.** Где функция требует session — отдаём ей
   мок `MagicMock`/`AsyncMock` с предсказанным `execute` →
   `scalar_one_or_none`. Цель — проверить ветвление логики, а не SQL.
5. **XLSX без записи на диск.** `_render_registry_workbook` пишет в
   `BytesIO`; тесты проверяют сигнатуру `b"PK"` (ZIP-magic) и
   количество колонок/строк. Это smoke на «реестр собирается».

---

## 5. Что НЕ покрыто автотестами и почему

- **Полный pybotx-flow** (нажатие кнопок, edit_message, transient
  cleanup) — требует поднятого pybotx-тест-стенда. Идёт по ручному
  чек-листу §6.
- **PostgreSQL advisory_xact_lock + конкурентный `assign_br_id`** —
  требует реального PostgreSQL, идёт по ручному чек-листу + ревью.
- **Реальная отправка XLSX в чат** — pybotx `bot.answer_message` mock'ом
  не покрывается, проверяется ручным `/export` / `/export_shortlist`.
- **Жюри-flow «открытие раунда → голосование → закрытие → жребий»**
  end-to-end — модели жюри связаны с PostgreSQL JSONB-снимками. Тесты
  алгоритма (`_compute_outcome_from_data`) покрывают всю математику;
  координацию проверяет ручной сценарий.

---

## 6. Ручной приёмочный чек-лист

Минимальный сценарий перед каждым релизом. Полный набор покрывается
обычной приёмкой; здесь — обязательный smoke по всем веткам бота.

**Участник:**
1. `/start` в личке → главное меню → «Подать заявку» → выбор трека.
2. Пройти анкету до шага «Файлы» — загрузить 1 файл и затем серию
   2–4 файлов (для треков с несколькими ракурсами).
3. Попробовать PDF/HEIC, отказать MP4 (неподдерживаемый формат) и
   файл > `MAX_FILE_SIZE_MB` (по умолчанию 10 МБ).
4. Поставить согласия → подтвердить → получить сообщение «Заявка
   принята и передана на модерацию» с присвоенным `BR-2026-NNNN`.
5. Повторно подать заявку на того же ребёнка → ожидаем автопометку
   «возможный дубль» в карточке модератора.

**Модератор:**
6. `/queue` (дефолт `на_модерации + нужно_исправить`), затем `/queue all`.
7. `/find BR-2026-0001` → карточка заявки с инлайн-кнопками.
8. `/status BR-... модерация допущено`, `/comment BR-... <текст>`.
9. `/notify_fix BR-... <причина>`, `/notify_reject BR-... <причина>` —
   проверяем доставку шаблонных сообщений участнику.
10. `/files BR-...` в режиме `files` → файлы приходят в чат
    attachment'ами.
11. `/export` → XLSX `registry_BR-2026_YYYY-MM-DD_HH-MM.xlsx`
    (см. [`registry-spec.md`](registry-spec.md) §4).
12. `/export_shortlist` → XLSX `shortlist_BR-...`.
13. `/stats today`, `/stats all`.

**Жюри:**
14. `/jury_tasks` → карусель задач, голос «Да»/«Нет» на каждой.
15. Кнопка «Отправить оценки» неактивна до полного покрытия + наличия
    обоих типов голосов (правило разброса — есть и YES, и NO).
16. Закрыть пул досрочно `/jury_close_round <pool>`, проверить
    обновление колонок 23–29 в `/export`.
17. Принудительно вызвать жребий через искусственный 3-й раунд с ничьёй.

**Админ:**
18. `/disk` — занятость диска + строка «приём блокируется при ≥ 95%».
19. `/intake_mode links` → переключение в резервный режим. UX
    «пришлите ссылку на облачную папку» для участников описан как
    отложенная задача в [`backlog.md`](backlog.md) → `WAVE4-LINKS-UX`.
20. `/admin_state` → диагностика FSM и пулов.

---

## 7. Расширение покрытия

- Новые модули `tests/test_*.py` — следовать паттерну из
  `tests/conftest.py` (env + `sys.path`).
- Для async-тестов **не** добавлять `@pytest.mark.asyncio` вручную:
  `asyncio_mode=auto` сделает это за тебя.
- Маркер `@pytest.mark.slow` зарезервирован для медленных тестов
  (рендер большого XLSX, генерация превью с реальной PIL и т. п.) —
  при появлении конкретных слоёв задавай явно, чтобы CI мог
  селективно запускать `python -m pytest -m "not slow"` для быстрого
  обратного цикла.
