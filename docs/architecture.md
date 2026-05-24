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
**FSM Storage:** Redis 7 (контейнер из docker-compose, AOF на named volume `redisdata`)
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
│   ├── __init__.py      # get_all_collectors() — common первым, далее остальные коллекторы
│   ├── common.py        # /start, /help, on_chat_created, default_message_handler (диспетчер)
│   ├── user.py          # ветка участника — главное меню «О конкурсе/Подать заявку/...»
│   ├── user_intake.py   # поэтапная анкета (parent_contact → child_name → ... ; ФИО/подразделение из CTS)
│   ├── user_files.py    # приём файлов работы по треку
│   ├── user_confirm.py  # согласия + финальное резюме + submit
│   ├── moderator.py     # главное меню модератора
│   ├── moderator_queue.py     # /queue (постранично) и /browse (карусель)
│   ├── moderator_actions.py   # /find, /status, /comment, /notify_fix, /notify_reject, /notify_shortlist
│   ├── moderator_export.py    # /export, /export_shortlist, /stats
│   ├── moderator_jury_admin.py# /jury_state, /jury_close_round, /jury_finalize
│   ├── jury.py          # /jury_menu, /jury_tasks (точка входа в роль)
│   ├── jury_tasks.py    # карусель задач, голосование «Да/Нет», кнопка «Отправить оценки»
│   ├── jury_status.py   # /jury_status — прогресс судьи
│   ├── admin.py         # /disk, /intake_mode, /admin_state
│   └── admin_roles.py   # discovery-кнопки: /admin_role_approve|reject,
│                        # /admin_chat_approve|reject, /admin_roles, /admin_role_revoke
├── services/            # Бизнес-логика и работа с БД (CRUD)
│   ├── access.py        # is_moderator/is_jury/is_admin + декораторы +
│   │                    # in-memory кэш + reload_access_cache +
│   │                    # seed_access_from_config_if_empty (bootstrap)
│   ├── discovery.py     # карточки админу для обнаружения новых модераторов /
│   │                    # жюри / чата модерации; add_moderator_to_chat,
│   │                    # send_welcome_dm_to_moderator / _jury
│   ├── applications.py  # жизненный цикл заявки (нормализация имени, BR-ID, дубль)
│   ├── storage.py       # файлы заявок, превью жюри, мониторинг диска,
│   │                    # start_disk_monitor_task()
│   ├── registry.py      # on-demand Excel-реестр + shortlist + registry_export_filename
│   ├── notifications.py # автосообщения участникам и в чат модерации + jury-event aggregator
│   ├── jury.py          # алгоритм раундов + формирование шорт-листа
│   ├── pools.py         # пулы (Track × AgeCategory) + sync_pool_assignments_from_config
│   ├── intake_mode.py   # переключение files/links + maybe_auto_switch_to_links
│   └── moderation.py    # /queue / /status / /comment, агрегаты /stats
├── database/            # SQLAlchemy
│   ├── db.py            # engine, session_maker, get_session()
│   ├── models.py        # Base, User + конкурсные модели и enum'ы (см. раздел 4 «Модель данных»)
│   └── migrations.py    # run_auto_migrations() — add columns/indexes/enums
├── fsm/                 # FSM (Redis only, AOF на named volume)
│   ├── storage.py       # фабрика get_fsm_storage()/init/close
│   ├── redis_storage.py # RedisFSMStorage — единственное хранилище
│   ├── middleware.py    # fsm_middleware, personal_chat_only, FSMContext
│   ├── chat_gate.py     # GLOBAL middleware: пропускает только PERSONAL_CHAT
│   └── cleanup_middleware.py  # Удаление transient-сообщений при навигации
└── utils/
    ├── bot_utils.py     # reply_to_user, safe_answer_transient, send_photo_transient
    ├── contracts.py     # DTO + Protocol-классы под services/* (общий контракт для веток бота)
    ├── deeplink.py      # build_bot_deeplink — рендер EXPRESS_DEEPLINK_TEMPLATE
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
   задачи нельзя дублировать в нескольких web-воркерах — см. раздел 7
   «Scheduler и фоновые задачи»).
2. `Base.metadata.create_all` — создаёт новые таблицы.
3. `run_auto_migrations()` — добавляет недостающие колонки / индексы /
   enum-значения (`database/migrations.py`).
4. `seed_access_from_config_if_empty(session)` (`services.access`) —
   ПЕРВИЧНЫЙ bootstrap из env: только если таблицы `moderators` /
   `jury_members` пусты и/или нет настройки `app_settings.moderation_chat_id`,
   подгружает соответствующие значения из `MODERATOR_HUIDS` /
   `JURY_HUIDS` / `MODERATION_CHAT_ID`. Дальше env игнорируется —
   управление списками идёт через discovery-кнопки админа в боте
   (`handlers/admin_roles.py`).
4а. `reload_access_cache(session)` — перечитывает таблицы и атомарно
    подменяет in-memory кэш ролей (`_moderator_huids`, `_jury_huids`,
    `_moderation_chat_id`). После этого `is_moderator()` / `is_jury()` /
    `get_moderation_chat_id()` отвечают O(1) без обращения в БД —
    критично для hot path (chat-gate middleware, `/moderator`,
    `/jury_menu`).
5. `sync_pool_assignments_from_config(JURY_POOLS_CONFIG, session)`
   (`services.pools`) — полностью переписывает таблицу
   `jury_pool_assignments` под актуальный JSON. Пустая строка в
   `JURY_POOLS_CONFIG` трактуется как «все активные судьи во всех
   пулах» (3 трека × 3 возрастные категории = 9 пулов).
6. `init_fsm_storage()` — проверяет доступность Redis; при ошибке
   бот падает на старте (без `REDIS_URL`/Redis работа невозможна).
7. `create_bot()` — собирает `Bot(collectors=get_all_collectors(), ...)`.
8. Внутри `lifespan_wrapper(bot)`:
   - если `ENABLE_SCHEDULER=true` — стартует
     `start_disk_monitor_task(bot, DISK_CHECK_INTERVAL_SEC)`;
     алёрты дедуплицируются внутри `check_and_alert_disk` через
     таблицу `disk_alerts` (одно уведомление на порог в сутки).
   - иначе — фоновый монитор НЕ запускается; модератор должен
     полагаться на ручную команду `/disk` и auto-switch в LINKS
     при достижении блокирующего порога (`DISK_BLOCK_PCT`, по
     умолчанию 95 %).

**Shutdown, по порядку:**

1. `disk_monitor_task.cancel()` (если запускался) +
   `await task` с поглощением `CancelledError`.
2. `flush_jury_event_aggregator()` (`services.notifications`) — иначе
   pending-event'ы агрегации открытия/закрытия раундов жюри (которые
   склеиваются в одно уведомление в чат модерации) теряются.
3. `close_fsm_storage()`.
4. `close_redis()` (message tracking).

Запуск: `uvicorn main:app --host 0.0.0.0 --port 8000 --workers $UVICORN_WORKERS`.

---

## 4. Модель данных

### Перечисления

Все enum'ы живут в `app/database/models.py`. Имена UPPER_SNAKE_CASE
сохраняются в БД (`sync_enum_values`), значения — русские строки для
UI и реестра.

| Enum | Значения | Назначение |
|---|---|---|
| `Track` | TRADITIONAL / AI / HANDMADE_TO_AI | Конкурсный трек |
| `AgeCategory` | AGE_0_6 / AGE_7_12 / AGE_13_18 (+ утилита `from_age`, диапазон полных лет 0–18) | Возрастная категория (вычисляется ботом по возрасту ребёнка, ручного выбора нет) |
| `IntakeMode` | FILES / LINKS | Режим приёма заявок: файлы вложением или ссылка на облачную папку |
| `ModerationStatus` | PRINYATO / NA_MODERATSII / DOPUSHCHENO / NUZHNO_ISPRAVIT / OTKLONENO | Состояние заявки на модерации |
| `JuryStatus` | NE_PEREDANO_ZHYURI / NA_GOLOSOVANII / V_TOP_10 / NE_VOSHLO_V_TOP_10 | Автополе по итогам процесса голосования по пулу |
| `VotingStatus` | NE_UCHASTVUET / PODGOTOVLENO_K_PUBLIKATSII / OPUBLIKOVANO / PRIZ_ZRITELSKIH_SIMPATIY | Статус народного голосования |
| `FileKind` | ORIGINAL / ANGLE / AI_IMAGE / DIPTYCH | Тип файла заявки (определяет имя файла на диске) |
| `JuryRoundStatus` | OPEN / CLOSED / DRAWN_BY_LOT | Статус раунда жюри по конкретному пулу |
| `JuryVoteValue` | YES / NO | Бинарная оценка судьи по работе |
| `JuryVoteState` | DRAFT / SUBMITTED | Черновик / отправленный голос (учитывается только SUBMITTED) |

### users

Базовая модель пользователя (на этом каркасе стоят health-чек и
проактивные сообщения; в конкурсной логике практически не нужна, но
не удалена сознательно).

| Колонка | Тип | Описание |
|---|---|---|
| huid | UUID, PK | Идентификатор пользователя из eXpress |
| chat_id | UUID, nullable, indexed | ID чата для проактивных сообщений |
| full_name | VARCHAR(255) | ФИО (заполняется из CTS, `public_name or username`) |
| username | VARCHAR(255), nullable | Имя пользователя из CTS |
| is_deleted | BOOLEAN, indexed | Soft-delete |
| deleted_at | TIMESTAMP, nullable | Дата удаления |
| last_activity | TIMESTAMP, nullable, indexed | Последняя активность |
| ad_login | VARCHAR(255), nullable, indexed | CTS-кэш: AD-логин |
| ad_domain | VARCHAR(255), nullable | CTS-кэш: AD-домен |
| email | VARCHAR(255), nullable, indexed | CTS-кэш: первый email из `emails[]` (lower-case) |
| ip_phone | VARCHAR(32), nullable | CTS-кэш: внутренний/IP-телефон |
| other_phone | VARCHAR(32), nullable | CTS-кэш: внешний/мобильный телефон |
| department | VARCHAR(255), nullable | CTS-кэш: подразделение |
| company | VARCHAR(255), nullable | CTS-кэш: компания |
| company_position | VARCHAR(255), nullable | CTS-кэш: должность |
| public_name | VARCHAR(255), nullable | CTS-кэш: публичное имя (часто полное ФИО) |
| cts_synced_at | TIMESTAMP, nullable | Момент последней синхронизации с CTS |
| created_at | TIMESTAMP | Дата создания |
| updated_at | TIMESTAMP | Дата обновления |

**Upsert:** `chat_id`, `ad_login`, `ad_domain`, `username` и `last_activity`
пишутся на каждом входящем из личного чата через
`handlers._user_sync_middleware.user_sync_middleware`
(`services.users.upsert_user_from_message`). Без этого таблица
оставалась бы пустой и проактивные DM (`notifications._send_to_user`,
`discovery._resolve_user_chat_id`) тихо падали в WARNING.

**CTS-кэш** (`ad_login`..`cts_synced_at`) заполняется отдельной
тяжёлой функцией `services.users.sync_user_from_cts`
(`bot.search_user_by_huid`):
- fire-and-forget на `on_chat_created` (PERSONAL_CHAT) и `cmd_start` —
  прогрев кэша к моменту первого `/apply`;
- blocking через `ensure_user_profile_loaded(max_age_sec=86400, timeout=5)`
  в `cmd_apply` — отдаёт горячий кэш либо синхронно тянет CTS с
  5-секундным таймаутом.

CTS-данные используются в анкете для автоподстановки ФИО
(`parent_full_name`) и подразделения (`parent_division`) — ручные шаги
из `UserIntake` удалены. Поля `email`, `ip_phone`, `other_phone` идут
как **подсказка** на шаге «Контакт» (но не подменяют ввод пользователя)
и параллельно сохраняются для аудита и проактивных DM.

### applications

Источник правды по всем колонкам реестра. Excel-выгрузка собирается из
этой таблицы on-demand (см. [`registry-spec.md`](registry-spec.md)).

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | Внутренний UUID |
| br_id | VARCHAR(20), UNIQUE, indexed | `BR-{YEAR}-NNNN`, монотонный счётчик, выдаётся через `assign_br_id` под advisory-lock |
| parent_huid | UUID, indexed | HUID родителя из eXpress |
| parent_full_name | VARCHAR(255) | Снимок ФИО на момент submit (из CTS-кэша `users.full_name`, либо ручной fallback-ввод) |
| parent_division | VARCHAR(255) | Снимок подразделения на момент submit (из CTS-кэша `users.department`, либо ручной fallback-ввод) |
| parent_ad_login | VARCHAR(255), nullable | AD-логин для записи `@login` в meta/Excel (fallback контакта) |
| parent_contact | VARCHAR(255), nullable | Контакт для связи, явно введённый родителем на шаге «Контакт» (email или телефон) |
| parent_contact_type | VARCHAR(16), nullable | `'email'` или `'phone'` — автоматически по наличию `@` в `parent_contact` |
| child_name | VARCHAR(255) | Имя ребёнка |
| child_age | INTEGER | Полных лет (0–18) |
| age_category | Enum `AgeCategory` | Вычисляется автоматически (`AgeCategory.from_age`) |
| track | Enum `Track` | Конкурсный трек |
| title | VARCHAR(500) | Название работы |
| description | TEXT | Описание работы |
| intake_mode | Enum `IntakeMode` | Режим, в котором подавалась заявка (FILES / LINKS) |
| cloud_link | TEXT, nullable | Ссылка на папку в облаке (для `LINKS`) |
| moderation_status | Enum `ModerationStatus` | По умолчанию `NA_MODERATSII` |
| moderator_comment | TEXT, nullable | Поле «Комментарий модератора» в реестре |
| jury_status | Enum `JuryStatus` | Автополе по итогам процесса голосования по пулу |
| voting_status | Enum `VotingStatus` | Заполняется модератором/организатором |
| merch_potential | VARCHAR(255), nullable | Поле «Потенциал для мерча» |
| is_possible_duplicate | BOOLEAN, indexed | Автопометка дубля: `parent_huid` + нормализованное имя ребёнка + `track` |
| related_application_br_id | VARCHAR(20), nullable | Ссылка на связанную заявку |
| is_actual_version | BOOLEAN | Признак актуальной версии (заполняется модератором) |
| jury_round1_yes / 2 / 3 | INTEGER | Голосов «Достоин» в раундах 1/2/3 |
| jury_final_round | INTEGER, nullable | Итоговый раунд (1/2/3) |
| jury_decided_by_lot | BOOLEAN | Определено жребием |
| pool_position | INTEGER, nullable | Позиция в пуле (1..N) |
| created_at, updated_at | TIMESTAMP | Аудиторские поля |

### application_files

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| application_id | UUID, FK→applications.id, CASCADE | |
| kind | Enum `FileKind` | ORIGINAL / ANGLE / AI_IMAGE / DIPTYCH |
| angle_no | INTEGER, nullable | 1..4 для `ANGLE` (ракурсы 3D-работ) |
| original_filename | VARCHAR(512) | Как прислал родитель |
| stored_filename | VARCHAR(512) | Переименованное по шаблону `BR-{YEAR}-NNNN_{kind}[N].{ext}` |
| relative_path | VARCHAR(1024) | Путь от `ATTACHMENTS_DIR` |
| size_bytes | INTEGER | |
| mime_type | VARCHAR(100) | |
| uploaded_at | TIMESTAMP | |

### moderators / jury_members

Простые справочники с PK по `huid` и полями `full_name`, `username`,
`added_by_huid`, `is_active`, `added_at`. **Источник правды для ролей
в рантайме** — таблицы БД (через in-memory кэш в `services/access.py`).
`MODERATOR_HUIDS` / `JURY_HUIDS` из env — только bootstrap при первом
запуске (`seed_access_from_config_if_empty`), дальше состав меняется
кнопками админа (discovery-карточки + `/admin_roles`,
`/admin_role_revoke`). Проверка ролей (`is_moderator` / `is_jury`) идёт
по in-memory кэшу — O(1), без походов в БД.

### app_settings

Универсальный KV-стор настроек, переживающих рестарт. Ключи:

| Ключ | Назначение |
|---|---|
| `moderation_chat_id` | UUID группового чата «Безопасные рисунки — модерация». Меняется через `/admin_chat_approve`. Bootstrap из `MODERATION_CHAT_ID`. |
| `intake_mode` | Текущий режим приёма заявок (FILES / LINKS). |

### jury_pool_assignments

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| jury_huid | UUID, FK→jury_members.huid, CASCADE | |
| track | Enum `Track` | |
| age_category | Enum `AgeCategory` | |
| created_at | TIMESTAMP | |

Уникальный индекс `(jury_huid, track, age_category)`. Если в таблице
нет ни одной записи — все активные `JuryMember` участвуют во всех
пулах (`len(Track) × len(AgeCategory)` = 9 пулов = 3 трека × 3
возрастные категории).

### jury_rounds

| Колонка | Тип | Описание |
|---|---|---|
| id | UUID, PK | |
| track | Enum `Track` | |
| age_category | Enum `AgeCategory` | |
| round_no | INTEGER | 1..`JURY_ROUNDS` (по умолчанию 3) |
| status | Enum `JuryRoundStatus` | OPEN / CLOSED / DRAWN_BY_LOT |
| opened_at | TIMESTAMP | |
| deadline_at | TIMESTAMP | `opened_at + JURY_ROUND_DEADLINE_HOURS` |
| closed_at | TIMESTAMP, nullable | |

Уникальный индекс `(track, age_category, round_no)`.

### jury_votes

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

### disk_alerts

Журнал автопредупреждений `(threshold_pct, created_at)` — нужен
для дедупликации, чтобы не слать сообщение в чат модерации каждые
30 минут после срабатывания порога. Один алёрт на порог в сутки.

> При расширении схемы обновляй и эту таблицу, и скилл
> `.cursor/skills/query-server-db/SKILL.md` → «Схема базы данных».

---

## 5. FSM-система

### Хранилище

Только **Redis** (`RedisFSMStorage`):
- Ключ: `fsm:{user_huid}` (Redis hash, поля `state`, `data`), TTL = `FSM_TTL_DAYS`.
- Контейнер `redis:7-alpine` из `docker-compose.yml`, AOF (`--appendonly yes --appendfsync everysec`) на named volume `redisdata` — состояния анкет переживают рестарт бота, Redis и хоста.
- Переменная `REDIS_URL` обязательна; при пустой `app/config.py` бросает `RuntimeError` на старте.

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

Заведены три класса состояний — по одному на ветку сценария:

| Класс | Состояния | Назначение |
|---|---|---|
| `UserIntake` | parent_contact → child_name → child_age → track → title → description → files_collect → consents → review (горячий путь). Fallback: parent_full_name_fb / parent_division_fb — включаются, только если CTS не дал соответствующее поле | Поэтапная анкета участника. ФИО и подразделение тянутся из CTS-кэша в `cmd_apply` (`services.users.ensure_user_profile_loaded`), в анкете остаётся один шаг — «Контакт» (email/телефон с автоопределением) |
| `ModeratorAction` | status_change, comment_input, reject_reason, fix_note | Диалоговые подсказки модератора |
| `JuryTaskFlow` | jury_task_voting, jury_task_confirm_submit | Прохождение задачи жюри |

Каждый класс — `class XYZ(str, Enum)` со значениями вида
`{раздел}:{подраздел}:{состояние}`; значения регистрируются в
диспетчере `default_message_handler` (см. раздел 11 «Диспетчер»).

---

## 5а. Discovery ролей, in-memory кэш доступа и chat-gate

### Источник правды по ролям

Список модераторов и жюри хранится в БД (`moderators` / `jury_members`),
UUID чата модерации — в `app_settings.moderation_chat_id`. Env-переменные
`MODERATOR_HUIDS` / `JURY_HUIDS` / `MODERATION_CHAT_ID` используются
**только** как одноразовый bootstrap при первом запуске
(`services.access.seed_access_from_config_if_empty`). После этого
управление списками — через discovery-кнопки админа и команды
`/admin_roles`, `/admin_role_revoke`.

### In-memory кэш (`services/access.py`)

Чтобы hot path (chat-gate middleware и проверки ролей в каждом хендлере)
не дёргал PostgreSQL, состав ролей хранится в module-level set'ах:

- `_moderator_huids: set[str]`,
- `_jury_huids: set[str]`,
- `_moderation_chat_id: UUID | None`.

`reload_access_cache(session)` вызывается:

1. На старте бота (lifespan, после seed).
2. После каждой операции `add_moderator` / `add_jury_member` /
   `revoke_*` / `set_moderation_chat`. Подмена set'ов атомарна под
   `asyncio.Lock`.

Проверки `is_moderator(huid)` / `is_jury(huid)` / `get_moderation_chat_id()`
— чистый sync lookup, без `async`, без БД.

### Discovery (`services/discovery.py`)

Когда юзер без роли дёргает `/moderator` или `/jury_menu`, а также
когда бота добавляют в новый групповой чат, админу шлётся карточка
с профилем (из `bot.search_user_by_huid`) и двумя кнопками
(«Назначить / Отклонить» либо «Сделать чатом модерации / Отклонить»).
Кнопки несут payload в `data={"role": ..., "huid": ...}` или
`data={"chat_id": ...}`. Обработка — в `handlers/admin_roles.py`.

Дедуп: in-memory словарь `_notified_at[(kind, ...)]` с TTL 1 час —
повторные попытки не флудят админа.

После одобрения модератора:

- `discovery.add_moderator_to_chat(bot, huid)` пытается добавить юзера
  в текущий `moderation_chat_id` через `bot.add_users_to_chat`
  (предварительно проверив `bot.chat_info` на «уже состоит»);
- `discovery.send_welcome_dm_to_moderator(bot, huid)` шлёт короткое
  приветственное DM (если у модератора есть `users.chat_id` после
  предыдущего `/start`).

Оба шага не критичны — результат подмешивается в admin reply
(«добавлен в чат модерации» / «не удалось — добавьте вручную»;
«welcome-DM отправлен» / «не отправлен — пусть напишет боту /start»).

### Chat-gate middleware (`fsm/chat_gate.py`)

Глобальный middleware, подключённый через `Bot(middlewares=[…])`
в `main.create_bot()`. Пропускает входящие **только** в личных чатах
(`ChatTypes.PERSONAL_CHAT`); всё остальное (включая чат модерации)
молча игнорируется. Outbound (`bot.send_message(chat_id=…)`) не gated —
бот по-прежнему свободно пушит уведомления в чат модерации.

Это сознательное решение: модерация ведётся в DM модератора с ботом,
чат модерации — только outbound (с возможностью открыть DM по
deeplink-кнопке, см. ниже).

`ChatCreatedEvent` идёт отдельно (system event, не проходит через
middlewares) — обрабатывается в `handlers/common.py::on_chat_created`,
который сам разветвляет PERSONAL_CHAT vs группа.

### Deeplink в чате модерации (`utils/deeplink.py`)

Шаблон `EXPRESS_DEEPLINK_TEMPLATE` (env, optional) рендерится в URL
для кнопки «🔎 Открыть в боте». Плейсхолдеры: `{bot_id}`, `{cts_url}`.
Если переменная пуста — кнопка не добавляется (graceful degradation),
текст команды в теле сообщения (`/find BR-XXXX` / `/files BR-XXXX`)
остаётся. У eXpress нет аналога Telegram `?start=payload` —
deeplink только открывает DM, команду модератор вводит вручную.

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
- Без Redis модуль не работает — `REDIS_URL` валидируется на старте.

При следующем `source_sync_id` `cleanup_middleware` удаляет все трекаемые сообщения и очищает список.

Подробности — в правиле `.cursor/rules/message-navigation.mdc`.

---

## 7. Scheduler и фоновые задачи

В проекте сейчас **одна** фоновая asyncio-задача — мониторинг диска
(`services.storage._disk_monitor_loop`). Стартует из `main.py` при
`ENABLE_SCHEDULER=true` через `start_disk_monitor_task(bot, interval)`;
интервал — `DISK_CHECK_INTERVAL_SEC` (по умолчанию 1800 с). Сам алёрт
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

### Файлы заявок

Файлы конкурса хранятся в именованном Docker-томе `attachments_volume`,
смонтированном в `/app/data/attachments`. Корневой путь в коде —
`config.ATTACHMENTS_DIR`. Структура папок — `<дата-подачи>/<трек>/
<возрастная-категория>/<папка-заявки>/`; имена файлов формируются по
шаблону `BR-{YEAR}-NNNN_{kind}[N].{ext}` (`original`, `angle-N`,
`ai-image`, `diptych` — см. перечисление `FileKind` в
`app/database/models.py`).

При отклонении заявки физические файлы работы удаляются (`rm`), а в
`99_Отклонено/<дата_модерации>/<папка-заявки>/` остаются только
метаданные — `description.txt`, `meta.txt`, `reason.txt`.

### Реестр

Источник правды — БД. Файл `registry.xlsx` **не хранится на диске** и
**не пересобирается** на каждое событие; он собирается из БД по
запросам `/export` и `/export_shortlist`, отдаётся в чат attachment'ом
и забывается. См. `app/services/registry.py` — функции возвращают
`bytes`. Полная спецификация состава колонок, имени файла, форматирования
и open-questions design-фазы — в [`registry-spec.md`](registry-spec.md).

### Мониторинг диска

`services/storage.py` экспонирует `get_disk_usage_bytes()` и
`should_block_intake()`. Пороги — `config.DISK_WARN_PCT` (по умолчанию
80 %) и `config.DISK_BLOCK_PCT` (95 %). При достижении блокирующего
порога бот автоматически переключает `intake_mode` в `LINKS`
(`services/intake_mode.maybe_auto_switch_to_links`). История
автопредупреждений — в таблице `disk_alerts` (дедупликация: одно
сообщение на порог в сутки, а не раз в 30 минут).

---

## 10. Контракты сервисов (`app/utils/contracts.py`)

Это **единственная точка**, из которой ветки бота могут импортировать
друг у друга DTO и сигнатуры. Прямой импорт реализаций между ветками
запрещён — он приводит к циклическим зависимостям и разламывает
параллельную разработку.

### DTO

| Тип | Назначение |
|---|---|
| `PoolKey(track, age_category)` | Ключ пула жюри, frozen — используется ключом в словарях агрегации jury-event'ов |
| `ApplicationDTO` | Лёгкий слепок заявки для листингов (`/queue`, `/find`) |
| `ApplicationFileDTO` | Файл заявки без зависимостей на ORM |
| `JuryTaskDTO` | Задача жюри (превью или ссылка, локальный номер, черновик голоса) |
| `RoundResult` | Итог раунда (top_ids, tie_ids, decided_by_lot, needs_next_round) |

Все DTO — `@dataclass(frozen=True)`. Без pydantic — DTO лёгкие и
хешируемые; при необходимости валидации хендлер оборачивает их в
pydantic локально, не меняя контракт.

### Protocols

`ApplicationsService`, `StorageService`, `RegistryService`,
`NotificationsService`, `JuryService`, `PoolsService`,
`IntakeModeService`, `AccessService` — `runtime_checkable` Protocols
ровно по публичному API соответствующих модулей `services/*`. Это даёт
хендлерам type-чекинг и возможность подменять реализации фейками в
тестах.

---

## 11. Диспетчер `default_message_handler`

По правилам pybotx на приложение может быть **только один**
`default_message_handler`. Он живёт в `app/handlers/common.py` и
реализован как диспетчер по FSM-состоянию.

### Контракт

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

### Пример регистрации

```python
# app/handlers/user_intake.py
from states import UserIntake
from handlers.common import register_state_handler

async def on_parent_contact(message, bot):
    ...

register_state_handler(
    UserIntake.user_intake_parent_contact.value, on_parent_contact
)
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
- Тестовые модули:
  - `test_application_flow.py` — `services.applications` (normalize,
    BR-ID, AgeCategory bounds, валидация `intake_mode`).
  - `test_moderation_flow.py` — `services.moderation` (parse_status_group,
    enum-by-value, фильтры `/queue`).
  - `test_jury_flow.py` — инварианты top_n, пулов, детерминизм
    сортировки + `_compute_outcome_from_data`.
  - `test_jury_algorithm.py` — 3 классических кейса алгоритма раунда.
  - `test_registry.py` — `registry_export_filename`, `transliterate`,
    `jury_column_header`, smoke-рендер XLSX без БД.
  - `test_validation.py` — валидаторы пользовательского ввода (есть
    1 пре-existing fail в `test_sanitize_input`, не блокирующий).

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
