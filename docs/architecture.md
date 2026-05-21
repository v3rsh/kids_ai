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
├── handlers/            # Хендлеры команд бота
│   ├── __init__.py      # get_all_collectors() — common первым, далее Wave 2
│   └── common.py        # /start, /help, on_chat_created, default_message_handler (диспетчер)
├── services/            # Бизнес-логика и работа с БД (CRUD)
│   ├── access.py        # is_moderator/is_jury/is_admin + декораторы хендлеров
│   ├── applications.py  # стаб: жизненный цикл заявки (§11, §15, §20)
│   ├── storage.py       # стаб: файлы заявок, mv/rm/meta, мониторинг диска (§21–§24, §28.1)
│   ├── registry.py      # стаб: on-demand Excel-реестр (§25.4)
│   ├── notifications.py # стаб: автосообщения участникам и в чат модерации (§18, §19)
│   ├── jury.py          # стаб: алгоритм раундов и шорт-лист (§35)
│   ├── pools.py         # стаб: пулы трек×возраст (§35.1)
│   └── intake_mode.py   # стаб: переключение files/links (§33.6)
├── database/            # SQLAlchemy
│   ├── db.py            # engine, session_maker, init_db
│   ├── models.py        # Base, User + конкурсные модели и enum'ы (см. §4)
│   └── migrations.py    # run_auto_migrations() — add columns/indexes/enums
├── fsm/                 # FSM (Redis + SQLite fallback)
│   ├── storage.py       # FSMStorage (SQLite), get_fsm_storage()
│   ├── redis_storage.py # RedisFSMStorage
│   ├── middleware.py    # fsm_middleware, personal_chat_only, FSMContext
│   └── cleanup_middleware.py  # Удаление transient-сообщений при навигации
└── utils/
    ├── bot_utils.py     # reply_to_user, safe_answer_transient, send_photo_transient
    ├── contracts.py     # DTO (PoolKey, ApplicationDTO, JuryTaskDTO, RoundResult)
    │                    # + Protocol-классы под services/* для Wave 2
    └── message_tracking.py  # Трекинг transient sync_id в Redis (fallback на FSM data)
```

### Конвенции

- Хендлер-файлы: префикс = ветка сценария (`admin_*.py`, `user_*.py`)
- Общие хендлеры — без префикса (`common.py`)
- Главный модуль ветки — без суффикса (`admin.py`, `user.py`)
- Каждый файл создаёт свой `collector`, регистрируется в `handlers/__init__.py`

---

## 3. Точка входа

`app/main.py`:

1. Загружает env (`config.py`)
2. В lifespan:
   - `Base.metadata.create_all` — создаёт новые таблицы
   - `run_auto_migrations()` — добавляет недостающие колонки/индексы/enum-значения
   - `init_fsm_storage()` — проверяет доступность Redis
   - `create_bot()` — собирает `Bot(collectors=get_all_collectors(), ...)`
   - Если `ENABLE_SCHEDULER=true` — подключает scheduler (когда будет добавлен)
3. При shutdown:
   - `close_fsm_storage()`
   - `close_redis()` (message tracking)

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
| `AgeCategory` | AGE_4_6 / AGE_7_10 / AGE_11_13 / AGE_14_18 (+ утилита `from_age`) | §9, §11.2 |
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
нет ни одной записи — все активные `JuryMember` участвуют во всех 12
пулах (дефолтное поведение из ТЗ).

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

## 7. Scheduler

> Отсутствует на старте проекта.

Когда появятся periodic задачи:
1. Создать `app/scheduler.py` с определением jobs (через APScheduler)
2. Создать `app/scheduler_worker.py` — точка входа для отдельного контейнера
3. Подключить `setup_scheduler(bot)` в `main.py` lifespan под `if ENABLE_SCHEDULER`
4. Раскомментировать сервис `scheduler` в `docker-compose.yml`

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

## 12. Будущие разделы

По мере роста проекта здесь появятся:
- **Кэширование** — справочники в памяти (если потребуется)
- **Аналитика** — события и метрики
- **Scheduler** — periodic задачи (§7 уже зарезервировал место)
- **Интеграции** — внешние API (если потребуются)

После добавления функциональности — обновляй этот документ.
