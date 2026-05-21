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
├── states.py            # FSM-состояния (Enum-классы)
├── keyboards.py         # Переиспользуемые BubbleMarkup
├── handlers/            # Хендлеры команд бота
│   ├── __init__.py      # get_all_collectors()
│   └── common.py        # /start, /help, on_chat_created, default_message_handler
├── services/            # Бизнес-логика и работа с БД (CRUD)
├── database/            # SQLAlchemy
│   ├── db.py            # engine, session_maker, init_db
│   ├── models.py        # Base, User
│   └── migrations.py    # run_auto_migrations() — add columns/indexes/enums
├── fsm/                 # FSM (Redis + SQLite fallback)
│   ├── storage.py       # FSMStorage (SQLite), get_fsm_storage()
│   ├── redis_storage.py # RedisFSMStorage
│   ├── middleware.py    # fsm_middleware, personal_chat_only, FSMContext
│   └── cleanup_middleware.py  # Удаление transient-сообщений при навигации
└── utils/
    ├── bot_utils.py     # reply_to_user, safe_answer_transient, send_photo_transient
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

> Заполняется по мере добавления моделей в `app/database/models.py`.

### users

Базовая модель пользователя:

| Колонка | Тип | Описание |
|---------|-----|----------|
| huid | UUID, PK | Идентификатор пользователя из eXpress |
| chat_id | UUID, nullable, indexed | ID чата для проактивных сообщений |
| full_name | VARCHAR(255) | ФИО |
| username | VARCHAR(255), nullable | Имя пользователя |
| is_deleted | BOOLEAN, indexed | Soft-delete |
| deleted_at | TIMESTAMP, nullable | Дата удаления |
| last_activity | TIMESTAMP, nullable, indexed | Последняя активность |
| created_at | TIMESTAMP | Дата создания |
| updated_at | TIMESTAMP | Дата обновления |

> При расширении модели обновляй эту таблицу и одновременно скилл `.cursor/skills/query-server-db/SKILL.md` → «Схема базы данных».

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

## 9. Будущие разделы

По мере роста проекта здесь появятся:
- **Кэширование** — справочники в памяти (если потребуется)
- **Аналитика** — события и метрики
- **Интеграции** — внешние API (если потребуются)
- **Бизнес-логика** — основные сценарии

После добавления функциональности — обновляй этот документ.
