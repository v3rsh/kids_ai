# kids_ai bot

Бот платформы eXpress на pybotx. Стартовый каркас, готовый к разработке функционала.

## Стек

- Python 3.10, async (asyncio)
- [pybotx](https://github.com/ExpressApp/pybotx) — фреймворк бота
- Starlette + uvicorn — веб-слой
- SQLAlchemy 2.0 (async) + asyncpg — PostgreSQL
- Redis 7 — FSM storage и трекинг transient-сообщений
- loguru — логирование
- Docker / Docker Compose — оркестрация
- Сборка под `linux/amd64`, деплой на сервер без интернета (см. [`DEPLOY.md`](DEPLOY.md))

## Структура

```
kids_ai/
├── app/           # Исходный код бота
├── docs/          # Документация (архитектура, деплой)
├── tests/         # pytest-тесты
├── scripts/       # Утилиты (security-сканы)
├── Dockerfile         # Образ бота
├── docker-compose.yml # Оркестрация: bot + redis + postgres (test profile)
├── build.sh           # Сборка офлайн-пакета
├── start.sh           # Запуск dev/test/prod
├── DEPLOY.md          # Инструкция для инженера по установке на сервер
└── requirements.txt
```

Подробное описание модулей — в [`docs/architecture.md`](docs/architecture.md).

## Быстрый старт

### Локальный запуск (test-режим: Docker + PostgreSQL + Redis)

```bash
cp .env-example .env
# Заполни BOT_ID, CTS_URL, BOT_SECRET_KEY, DB_PASSWORD

./start.sh test
```

Проверка:

```bash
curl http://localhost:8000/healthz
# {"status":"healthy","postgres":"ok","redis":"ok"}
```

### Сборка офлайн-пакета для сервера

```bash
./build.sh
# Появится dist/kids_ai-deploy.tar.gz
```

Передай архив инженеру вместе с инструкцией [`DEPLOY.md`](DEPLOY.md).

## Команды бота (на старте)

| Команда | Описание |
|---------|----------|
| `/start` | Начать работу |
| `/help` | Справка |

По мере развития проекта добавляй команды в `app/handlers/` и обновляй эту таблицу.

## Документация

- [`docs/architecture.md`](docs/architecture.md) — структура модулей, FSM, навигация, логирование
- [`docs/deployment.md`](docs/deployment.md) — Docker, env-переменные, offline-деплой, troubleshooting
- [`DEPLOY.md`](DEPLOY.md) — пошаговая установка на сервер инженером

## Cursor / Agent setup

В папке `.cursor/` лежит набор универсальных артефактов:

- `rules/` — 10 правил (логирование, performance, conventional commits, pybotx-специфика)
- `skills/` — `deploy-bot`, `project-docs`, `query-server-db`
- `agents/` — `api-explorer` (ресёрч pybotx через Context7), `test-runner`

Эти правила и скиллы Cursor подхватывает автоматически при работе в этой папке.

## Дальнейшие шаги

1. Определи доменные модели в `app/database/models.py`
2. Опиши FSM-состояния по веткам сценариев в `app/states.py`
3. Создай хендлеры в `app/handlers/` (по конвенции `admin_*.py` / `user_*.py`)
4. Бизнес-логику и работу с БД выноси в `app/services/`
5. По мере появления periodic jobs — добавь `app/scheduler.py` и `app/scheduler_worker.py`, раскомментируй блок в `docker-compose.yml`
6. Обновляй `docs/architecture.md` после значимых изменений (см. скилл `project-docs`)
