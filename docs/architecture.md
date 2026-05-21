# Архитектура kids_ai

> Этот документ — каркас. Заполняй секции по мере появления функциональности.
> При значимых изменениях кода обновляй соответствующие разделы (см. `.cursor/skills/project-docs/SKILL.md`).

---

## 1. Обзор и стек

**Платформа:** eXpress (CTS / BotX API)
**Язык:** Python 3.10
**Фреймворк бота:** [pybotx](https://github.com/ExpressApp/pybotx)
**Веб-фреймворк:** Starlette + uvicorn
**База данных:** PostgreSQL 15 (через SQLAlchemy 2.0 async + asyncpg)
**FSM Storage:** Redis 7 (primary), SQLite (fallback)
**Логирование:** loguru (см. `app/logging_config.py`)
**Оркестрация:** Docker Compose
**Целевая среда:** Linux amd64 без интернета (см. `docs/deployment.md`)

---

## 2. Структура модулей

```
app/
├── main.py              # Точка входа: Starlette app + lifespan + create_bot()
├── config.py            # ENV-переменные, инициализация loguru
├── logging_config.py    # Настройка loguru + перехват stdlib logging
├── routes.py            # HTTP-эндпоинты: /healthz, /livez, /command, /status, /notification/callback
├── states.py            # FSM-состояния: UserIntake, ModeratorAction, JuryTaskFlow
├── keyboards.py         # Переиспользуемые BubbleMarkup (главное меню, анкета, согласия)
├── handlers/            # Хендлеры команд бота — 14 коллекторов
│   ├── __init__.py      # get_all_collectors() — common первым, далее Wave 2
│   ├── common.py        # /start, /help, on_chat_created, default_message_handler (диспетчер)
│   ├── user.py          # ветка A — главное меню «О конкурсе/Подать заявку/...» (§6–§7)
│   ├── user_intake.py   # ветка A — поэтапная анкета (parent_full_name → ...) (§11)
│   ├── user_files.py    # ветка A — приём файлов (§12, §16)
│   ├── user_confirm.py  # ветка A — согласия + review + submit (§13, §14)
│   ├── moderator.py     # ветка B — главное меню модератора (§27)
│   ├── moderator_queue.py     # ветка B — /queue и /browse (§27.1)
│   ├── moderator_actions.py   # ветка B — /find, /status, /comment, /notify_* (§27.1)
│   ├── moderator_export.py    # ветка B — /export, /export_shortlist, /stats (§25.4, §28)
│   ├── moderator_jury_admin.py# ветка B — /jury_state, /jury_close_round, /jury_finalize (§27.5)
│   ├── jury.py          # ветка C — /jury_menu, /jury_tasks (§35.4)
│   ├── jury_tasks.py    # ветка C — карусель задач, голосование, кнопки (§35.3)
│   ├── jury_status.py   # ветка C — /jury_status (прогресс судьи)
│   └── admin.py         # ветка D — /disk, /intake_mode, /admin_state (§28.1, §33.6, §5.3)
├── services/            # Бизнес-логика и работа с БД (CRUD)
│   ├── access.py        # is_moderator/is_jury/is_admin + декораторы +
│   │                    # sync_role_directories_from_config() (lifespan)
│   ├── applications.py  # жизненный цикл заявки (§11, §15, §20) — реальная импл.
│   ├── storage.py       # файлы заявок, превью жюри, мониторинг диска,
│   │                    # start_disk_monitor_task() (§21–§24, §28.1, §35.3)
│   ├── registry.py      # on-demand Excel-реестр + shortlist + registry_export_filename (§25.4)
│   ├── notifications.py # автосообщения участникам и в чат модерации + jury-event aggregator (§18, §19)
│   ├── jury.py          # алгоритм раундов §35.2 + шорт-лист §35.5
│   ├── pools.py         # пулы трек×возраст (§35.1) + sync_pool_assignments_from_config (§35.6)
│   ├── intake_mode.py   # переключение files/links (§33.6) + maybe_auto_switch_to_links
│   └── moderation.py    # /queue / /status / /comment (§27.1), агрегаты /stats (§28)
├── database/            # SQLAlchemy
│   ├── db.py            # engine, session_maker, get_session()
│   ├── models.py        # Base, User + конкурсные модели и enum'ы (см. §4)
│   └── migrations.py    # run_auto_migrations() — add columns/indexes/enums
├── fsm/                 # FSM (Redis + SQLite fallback)
│   ├── storage.py       # FSMStorage (SQLite), get_fsm_storage()
│   ├── redis_storage.py # RedisFSMStorage
│   ├── middleware.py    # fsm_middleware, personal_chat_only, FSMContext
│   └── cleanup_middleware.py  # Удаление transient-сообщений при навигации
└── utils/
    ├── bot_utils.py     # reply_to_user, safe_answer_transient, send_photo_transient
    ├── contracts.py     # DTO + Protocol-классы под services/* (Wave 2)
    └── message_tracking.py  # Трекинг transient sync_id в Redis (fallback на FSM data)
```

### Конвенции

- Хендлер-файлы: префикс = ветка сценария (`admin_*.py`, `user_*.py`)
- Общие хендлеры — без префикса (`common.py`)
- Главный модуль ветки — без суффикса (`admin.py`, `user.py`)
- Каждый файл создаёт свой `collector`, регистрируется в `handlers/__init__.py`

---

## 3. Точка входа

`app/main.py` собирает Starlette-приложение с pybotx-lifespan.

### Жизненный цикл (lifespan)

**Startup, по порядку:**

1. Проверка совместимости `ENABLE_SCHEDULER × UVICORN_WORKERS` (фоновые
   задачи нельзя дублировать в нескольких web-воркерах — см. §7).
2. `Base.metadata.create_all` — создаёт новые таблицы.
3. `run_auto_migrations()` — добавляет недостающие колонки / индексы /
   enum-значения (`database/migrations.py`).
4. `sync_role_directories_from_config(session)` (`services.access`) —
   идемпотентный upsert `MODERATOR_HUIDS` / `JURY_HUIDS` в таблицы
   `moderators` / `jury_members`. HUID, выпавшие из конфига, помечаются
   `is_active=False` (мягкое удаление; голоса и комментарии не теряем).
5. `sync_pool_assignments_from_config(JURY_POOLS_CONFIG, session)`
   (`services.pools`) — полностью переписывает таблицу
   `jury_pool_assignments` под актуальный JSON (§35.6). Пустой
   конфиг = «все судьи во всех 9 пулах» (fallback из ТЗ §35.6).
6. `init_fsm_storage()` — проверяет доступность Redis, при ошибке
   fallback на SQLite.
7. `create_bot()` — собирает `Bot(collectors=get_all_collectors(), ...)`.
8. Внутри `lifespan_wrapper(bot)`:
   - если `ENABLE_SCHEDULER=true` — стартует
     `start_disk_monitor_task(bot, DISK_CHECK_INTERVAL_SEC)`
     (см. §28.1 ТЗ); алёрты дедуплицируются внутри
     `check_and_alert_disk` через таблицу `disk_alerts`.
   - иначе — фоновый монитор НЕ запускается; модератор должен
     полагаться на ручную команду `/disk` и auto-switch в LINKS
     при достижении блокирующего порога (95 %).

**Shutdown, по порядку:**

1. `disk_monitor_task.cancel()` (если запускался) +
   `await task` с поглощением `CancelledError`.
2. `flush_jury_event_aggregator()` (`services.notifications`) — иначе
   pending-event'ы агрегации открытия/закрытия раундов (§19) теряются.
3. `close_fsm_storage()`.
4. `close_redis()` (message tracking).

Запуск: `uvicorn main:app --host 0.0.0.0 --port 8000 --workers $UVICORN_WORKERS`.

---

## 4. Модель данных

### Перечисления (Wave 1)

Все enum'ы живут в `app/database/models.py`. Имена UPPER_SNAKE_CASE
сохраняются в БД (`sync_enum_values`), значения — русские строки по ТЗ
для UI и реестра.

| Enum | Значения | ТЗ |
|---|---|---|
| `Track` | TRADITIONAL / AI / HANDMADE_TO_AI | §10 |
| `AgeCategory` | AGE_0_6 / AGE_7_12 / AGE_13_18 (+ утилита `from_age`, диапазон 0–18) | §9, §11.2 |
| `IntakeMode` | FILES / LINKS | §33.6 |
| `ModerationStatus` | PRINYATO / NA_MODERATSII / DOPUSHCHENO / NUZHNO_ISPRAVIT / OTKLONENO | §26 |
| `JuryStatus` | NE_PEREDANO_ZHYURI / NA_GOLOSOVANII / V_TOP_10 / NE_VOSHLO_V_TOP_10 | §26 |
| `VotingStatus` | NE_UCHASTVUET / PODGOTOVLENO_K_PUBLIKATSII / OPUBLIKOVANO / PRIZ_ZRITELSKIH_SIMPATIY | §26 |
| `FileKind` | ORIGINAL / ANGLE / AI_IMAGE / DIPTYCH | §22 |
| `JuryRoundStatus` | OPEN / CLOSED / DRAWN_BY_LOT | §35.2 |
| `JuryVoteValue` | YES / NO | §35.1 |
| `JuryVoteState` | DRAFT / SUBMITTED | §35.3 |

### users

Базовая модель пользователя (на этом каркасе стоят health-чек и
проактивные сообщения; в конкурсной логике практически не нужна, но
не удалена сознательно).

| Колонка | Тип | Описание |
|---|---|---|
| huid | UUID, PK | Идентификатор пользователя из eXpress |
| chat_id | UUID, nullable, indexed | ID чата для проактивных сообщений |
| full_name | VARCHAR(255) | ФИО |
| username | VARCHAR(255), nullable | Имя пользователя |
| is_deleted | BOOLEAN, indexed | Soft-delete |
| deleted_at | TIMESTAMP, nullable | Дата удаления |
| last_activity | TIMESTAMP, nullable, indexed | Последняя активность |
| created_at | TIMESTAMP | Дата создания |
| updated_at | TIMESTAMP | Дата обновления |

### applications (§11, §15, §20, §25)

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | Внутренний UUID |
| br_id | VARCHAR(20), UNIQUE, indexed | `BR-2026-NNNN`, источник правды (§20) |
| parent_huid | UUID, indexed | HUID родителя из eXpress |
| parent_full_name | VARCHAR(255) | Полное ФИО (с отчеством) |
| parent_division | VARCHAR(255) | Подразделение |
| parent_ad_login | VARCHAR(255), nullable | AD-логин для записи `@login` в meta/Excel (§11.1) |
| child_name | VARCHAR(255) | Имя ребёнка |
| child_age | INTEGER | Полных лет |
| age_category | Enum `AgeCategory` | Вычисляется автоматически (`AgeCategory.from_age`) |
| track | Enum `Track` | §10 |
| title | VARCHAR(500) | Название работы |
| description | TEXT | Описание работы |
| intake_mode | Enum `IntakeMode` | Режим, в котором подавалась заявка (§33.6) |
| cloud_link | TEXT, nullable | Ссылка на папку в облаке (для `LINKS`) |
| moderation_status | Enum `ModerationStatus` | По умолчанию `NA_MODERATSII` |
| moderator_comment | TEXT, nullable | Поле №15 реестра |
| jury_status | Enum `JuryStatus` | Автополе по итогам пула |
| voting_status | Enum `VotingStatus` | Заполняется модератором/организатором |
| merch_potential | VARCHAR(255), nullable | Поле №19 реестра |
| is_possible_duplicate | BOOLEAN, indexed | Автопометка §15.3 |
| related_application_br_id | VARCHAR(20), nullable | Поле №21 реестра |
| is_actual_version | BOOLEAN | Поле №22 реестра (только модератор) |
| jury_round1_yes / 2 / 3 | INTEGER | Поля №№23–25 реестра |
| jury_final_round | INTEGER, nullable | Поле №26 реестра |
| jury_decided_by_lot | BOOLEAN | Поле №28 реестра |
| pool_position | INTEGER, nullable | Поле №29 реестра |
| created_at, updated_at | TIMESTAMP | Аудиторские поля |

### application_files (§12, §22, §23)

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| application_id | UUID, FK→applications.id, CASCADE | |
| kind | Enum `FileKind` | ORIGINAL / ANGLE / AI_IMAGE / DIPTYCH |
| angle_no | INTEGER, nullable | 1..4 для ANGLE (§12.1) |
| original_filename | VARCHAR(512) | Как прислал родитель |
| stored_filename | VARCHAR(512) | Переименованное по §22 |
| relative_path | VARCHAR(1024) | Путь от `ATTACHMENTS_DIR` |
| size_bytes | INTEGER | |
| mime_type | VARCHAR(100) | |
| uploaded_at | TIMESTAMP | |

### moderators / jury_members (§5.2, §5.4)

Простые справочники с PK по `huid`, полями `full_name`, `is_active`,
`added_at`. Заполняются при старте бота из `MODERATOR_HUIDS` /
`JURY_HUIDS` (см. `app/services/access.py` — проверка доступа всегда
идёт по конфигу, а не по этим таблицам, чтобы не плодить SELECT'ы).

### jury_pool_assignments (§35.6)

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| jury_huid | UUID, FK→jury_members.huid, CASCADE | |
| track | Enum `Track` | |
| age_category | Enum `AgeCategory` | |
| created_at | TIMESTAMP | |

Уникальный индекс `(jury_huid, track, age_category)`. Если в таблице
нет ни одной записи — все активные `JuryMember` участвуют во всех
пулах (`len(Track) × len(AgeCategory)`, после Wave 0 replay
2026-05-21 это 9 пулов; дефолтное поведение из ТЗ §35.6).

### jury_rounds (§35.2, §35.4, §35.6)

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| track | Enum `Track` | |
| age_category | Enum `AgeCategory` | |
| round_no | INTEGER | 1..3 |
| status | Enum `JuryRoundStatus` | OPEN / CLOSED / DRAWN_BY_LOT |
| opened_at | TIMESTAMP | |
| deadline_at | TIMESTAMP | `opened_at + JURY_ROUND_DEADLINE_HOURS` |
| closed_at | TIMESTAMP, nullable | |

Уникальный индекс `(track, age_category, round_no)`.

### jury_votes (§35.3, §35.4)

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| round_id | UUID, FK→jury_rounds.id, CASCADE | |
| application_id | UUID, FK→applications.id, CASCADE | |
| jury_huid | UUID, FK→jury_members.huid, CASCADE | |
| vote | Enum `JuryVoteValue` | YES / NO |
| state | Enum `JuryVoteState` | DRAFT / SUBMITTED (учитывается только SUBMITTED) |
| created_at, submitted_at | TIMESTAMP | |

Уникальный индекс `(round_id, application_id, jury_huid)`.

### app_settings

Key-value runtime-настройки. Минимум — `intake_mode` (`files`/`links`),
чтобы переключение `/intake_mode` или автопереход на 95 % диска
переживало рестарт.

### disk_alerts (§28.1, §33.5)

Журнал автопредупреждений `(threshold_pct, created_at)` — нужен
для дедупликации, чтобы не слать сообщение в чат модерации каждые
30 минут после срабатывания порога.

> При расширении схемы обновляй и эту таблицу, и скилл
> `.cursor/skills/query-server-db/SKILL.md` → «Схема базы данных».

---

## 5. FSM-система

### Хранилище

Двухслойное:
- **Redis** — primary, если задан `REDIS_URL`. Ключ: `fsm:{user_huid}` (Redis hash, поля `state`, `data`), TTL = `FSM_TTL_DAYS`.
- **SQLite** — fallback. Файл `data/states.sqlite3`, таблица `fsm_storage`.

Интерфейс одинаков (`FSMStorage` / `RedisFSMStorage`). Выбор реализации — в `get_fsm_storage()`.

### Middleware

- `fsm_middleware` — инъектирует `FSMContext` в `message.state.fsm` (lazy-load)
- `cleanup_middleware` — при `source_sync_id` (клик по кнопке) удаляет все transient-сообщения
- `personal_chat_only` — фильтр на личные чаты

Подключение в хендлерах:

```python
@collector.command("/cmd", middlewares=[fsm_middleware, cleanup_middleware])
async def handler(message: IncomingMessage, bot: Bot) -> None:
    fsm = message.state.fsm
    await fsm.set_state(MyStates.some_state)
```

### Конвенции состояний

См. `app/states.py` и правило `.cursor/rules/bot.mdc` → «FSM Conventions».

В Wave 1 заведены три класса состояний — по одному на ветку сценария:

| Класс | Состояния | Назначение |
|---|---|---|
| `UserIntake` | parent_full_name → parent_division → child_name → child_age → track → title → description → files_collect → consents → review | Поэтапная анкета родителя (§8, §11–§14) |
| `ModeratorAction` | status_change, comment_input, reject_reason, fix_note | Диалоговые подсказки модератора (§27.1) |
| `JuryTaskFlow` | jury_task_voting, jury_task_confirm_submit | Прохождение задачи жюри (§35.3, §35.4) |

Каждый класс — `class XYZ(str, Enum)` со значениями вида
`{раздел}:{подраздел}:{состояние}`; значения регистрируются в
диспетчере `default_message_handler` (см. §11 ниже).

---

## 6. Навигация в одном сообщении

Принцип: каждый клик по кнопке **редактирует** текущее меню, transient-сообщения (фото, ошибки, уведомления) **удаляются автоматически**.

### Типы сообщений

| Тип | Функция | Поведение |
|-----|---------|-----------|
| Menu | `reply_to_user()` | edit_message при `source_sync_id`, иначе answer_message |
| Transient (текст) | `safe_answer_transient()` | answer_message + трекинг в Redis |
| Transient (фото) | `send_photo_transient()` | answer_message с вложением + трекинг |

### Трекинг

`utils/message_tracking.py`:
- Redis: ключ `bot_transient:{user_huid}`, тип LIST sync_id, TTL 24 часа
- Fallback на FSM data (поле `_transient_messages`) если Redis недоступен

При следующем `source_sync_id` `cleanup_middleware` удаляет все трекаемые сообщения и очищает список.

Подробности — в правиле `.cursor/rules/message-navigation.mdc`.

---

## 7. Scheduler и фоновые задачи

В проекте сейчас **одна** фоновая asyncio-задача — мониторинг диска
(`services.storage._disk_monitor_loop`). Стартует из `main.py` при
`ENABLE_SCHEDULER=true` через `start_disk_monitor_task(bot, interval)`;
интервал — `DISK_CHECK_INTERVAL_SEC` (по умолчанию 1800 c). Сам алёрт
дедуплицируется в `disk_alerts` (раз в 24 ч на каждый порог).

Также агрегатор jury-event'ов (`services.notifications._aggregator_worker`)
стартует **лениво** при первом `_enqueue_jury_event` и
останавливается через `flush_jury_event_aggregator()` в shutdown —
ему отдельный scheduler не нужен.

> ⚠️ Не путать с полноценным APScheduler. Если в будущем появятся
> periodic-задачи, помимо мониторинга диска (например, ежедневные
> отчёты), нужно вынести их в `app/scheduler.py` + отдельный
> scheduler-контейнер (см. `docker-compose.yml`).

Чеклист производительности jobs — в `.cursor/rules/performance.mdc` → «Scheduler Jobs».

---

## 8. Логирование

`app/logging_config.py` настраивает loguru:
- stderr handler (цветной формат для dev, JSON для prod)
- файловый handler в `logs/app.log` с ротацией (10 MB, retention 7 дней, gzip)
- перехват stdlib logging (uvicorn, sqlalchemy, httpx) → перенаправление в loguru
- `httpx`/`httpcore` подняты до WARNING (защита от утечки токенов в логи)
- `diagnose=False`, `backtrace=False` — защита от утечки локальных переменных

Все модули используют:

```python
from loguru import logger
logger.info("Сообщение", key=value)
```

Подробности — в `.cursor/rules/logging.mdc`.

---

## 9. Хранилище файлов и реестр

### Файлы заявок (§21, §22, §23, §24, §33.1)

Файлы конкурса хранятся в именованном Docker-томе `attachments_volume`,
смонтированном в `/app/data/attachments`. Корневой путь в коде —
`config.ATTACHMENTS_DIR`. Структура — день/трек/возраст/папка заявки
(§21.1); имена файлов — по шаблону §22 (см. перечисление `FileKind`).

При отклонении заявки физические файлы работы удаляются (`rm`), а в
`99_Отклонено/<дата_модерации>/` остаются только метаданные
(`description.txt`, `meta.txt`, `reason.txt`) — §24.

### Реестр (§25.4 после Wave 0)

Источник правды — БД. Файл `registry.xlsx` **не хранится на диске** и
**не пересобирается** на каждое событие; он собирается из БД по
запросам `/export` и `/export_shortlist`, отдаётся в чат attachment'ом
и забывается. См. `app/services/registry.py` — функции возвращают
`bytes`. Формат колонок согласуется в `docs/registry-spec.md` (Wave 2).

### Мониторинг диска (§28.1, §33.5)

`services/storage.py` экспонирует `get_disk_usage_bytes()` и
`should_block_intake()`. Пороги — `config.DISK_WARN_PCT` (по умолчанию
80 %) и `config.DISK_BLOCK_PCT` (95 %). При достижении 95 % бот
автоматически переключает `intake_mode` в `LINKS`
(`services/intake_mode.maybe_auto_switch_to_links`). История
автопредупреждений — в таблице `disk_alerts` (дедупликация, чтобы
не слать сообщение раз в 30 минут).

---

## 10. Контракты сервисов (`app/utils/contracts.py`)

Это **единственная точка**, из которой ветки Wave 2 могут импортировать
друг у друга DTO и сигнатуры. Прямой импорт реализаций из соседних
веток запрещён — он разламывает параллельную разработку и приводит к
циклическим зависимостям.

### DTO

| Тип | Назначение |
|---|---|
| `PoolKey(track, age_category)` | Ключ пула жюри, frozen — используется ключом в словарях агрегации §19 |
| `ApplicationDTO` | Лёгкий слепок заявки для листингов (`/queue`, `/find`) |
| `ApplicationFileDTO` | Файл заявки без зависимостей на ORM |
| `JuryTaskDTO` | Задача жюри (превью или ссылка, локальный номер, черновик голоса) |
| `RoundResult` | Итог раунда (top_ids, tie_ids, decided_by_lot, needs_next_round) |

Все DTO — `@dataclass(frozen=True)`. Без pydantic — DTO лёгкие и
хешируемые; при необходимости валидации Wave 2 оборачивает их в
pydantic локально, не меняя контракт.

### Protocols

`ApplicationsService`, `StorageService`, `RegistryService`,
`NotificationsService`, `JuryService`, `PoolsService`,
`IntakeModeService`, `AccessService` — `runtime_checkable` Protocols
ровно по публичному API соответствующих модулей `services/*`. Это даёт
Wave 2 type-чекинг и возможность подменять реализации фейками в тестах.

---

## 11. Диспетчер `default_message_handler`

По правилам pybotx на приложение может быть **только один**
`default_message_handler`. Он живёт в `app/handlers/common.py` и
реализован как диспетчер по FSM-состоянию.

### Контракт для веток Wave 2

1. Каждая ветка, которой нужен свободный текст в каком-то FSM-состоянии,
   регистрирует хендлер через
   `register_state_handler(state: str, handler: StateHandler)` в момент
   импорта своего коллектора в `app/handlers/__init__.py`.
2. `state` — это значение из Enum (`UserIntake.user_intake_child_name.value`).
3. `handler` — корутина с обычной для pybotx сигнатурой
   `async def h(message, bot)`.
4. Диспетчер сам подгружает текущее состояние через
   `message.state.fsm.get_state()` (FSM-middleware ставит
   `message.state.current_state = None` до явной загрузки), кладёт его
   в `message.state.current_state` и ищет в `STATE_HANDLERS`.
5. Если хендлер найден — диспетчер делегирует ему обработку.
6. Если нет — отвечает дефолтным сообщением «понимаю только команды»
   и перерисовывает главное меню.

### Пример регистрации (для Wave 2)

```python
# app/handlers/user_intake.py
from states import UserIntake
from handlers.common import register_state_handler

async def on_parent_full_name(message, bot):
    ...

register_state_handler(UserIntake.user_intake_parent_full_name.value, on_parent_full_name)
```

При повторной регистрации одного и того же `state` диспетчер пишет
WARNING в лог и переопределяет старый хендлер (это нужно для dev-режима
с hot-reload).

---

## 12. Тестирование

Полные инструкции по запуску и архитектуре тестов — `docs/testing.md`.

Кратко:

- Тесты — pytest + pytest-asyncio (`asyncio_mode=auto` в `pytest.ini`).
- `tests/conftest.py` инициализирует env (BOT_ID/CTS_URL/...) и
  добавляет `app/` в `sys.path`, чтобы импорты `from services import ...`
  работали из тестового процесса.
- Тестовые модули (Wave 3 §30.1):
  - `test_application_flow.py` — services.applications (normalize,
    BR-ID, AgeCategory bounds, IntakeMode validation).
  - `test_moderation_flow.py` — services.moderation (parse_status_group,
    enum-by-value, фильтры /queue).
  - `test_jury_flow.py` — инварианты top_n, пулов, детерминизм
    сортировки + `_compute_outcome_from_data`.
  - `test_jury_algorithm.py` (Wave 2/C) — 3 классических кейса
    алгоритма §35.2.
  - `test_registry.py` — registry_export_filename, transliterate,
    jury_column_header, smoke-рендер XLSX без БД.
  - `test_validation.py` — старые валидаторы (есть 1 пре-existing
    fail в `test_sanitize_input`, не относится к Wave 2/3).

Полные интеграционные сценарии с PostgreSQL + advisory_lock + jury-flow
end-to-end остаются под ручным smoke-чек-листом (`docs/testing.md`
→ «Ручной чек-лист»).

## 13. Будущие разделы

По мере роста проекта здесь появятся:
- **Кэширование** — справочники в памяти (если потребуется)
- **Аналитика** — события и метрики
- **Расширение Scheduler** — periodic задачи помимо disk-monitor
- **Интеграции** — внешние API (если потребуются)

После добавления функциональности — обновляй этот документ.
