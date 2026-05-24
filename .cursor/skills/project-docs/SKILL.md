---
name: project-docs
description: Find and maintain project documentation. Use when the user asks about project architecture, how a system works, deployment procedures, or when code changes require documentation updates. Also use proactively after significant code changes to suggest doc updates.
---
# Project Documentation

## Documentation Index

Точка входа в актуальную документацию — `docs/README.md`. Архивные документы (включая исходное ТЗ) — в `docs/history/`, они **не правятся**.

| Document | Path | Content |
|----------|------|---------|
| Docs index | `docs/README.md` | Карта актуальной документации, правила её изменения |
| Architecture | `docs/architecture.md` | Модули, модель данных, FSM, навигация, scheduler, логирование |
| Deployment | `docs/deployment.md` | Docker, env vars, offline-деплой, мониторинг, troubleshooting, бэкапы |
| Registry spec | `docs/registry-spec.md` | Формат Excel-выгрузок реестра и шорт-листа |
| Testing | `docs/testing.md` | Что тестируется, как запускать pytest, ручной чек-лист |
| Backlog | `docs/backlog.md` | Отложенные задачи и улучшения |
| History | `docs/history/` | Архив (исходное `ТЗ.md` и ответы заказчика) — read-only |
| README | `README.md` | Обзор проекта, быстрый старт, команды бота |

> По мере развития проекта в `docs/` добавляются предметные документы (например, `docs/feature-name.md`). Любые требования и изменения поведения бота фиксируются прямо в актуальных файлах `docs/`, а не в архивном `ТЗ.md`.

## When to Consult Docs

### Architecture (`docs/architecture.md`)

Читай перед:
- Добавлением нового handler, service, модели
- Изменением periodic jobs (мониторинг диска, scheduler-задачи)
- Работой с FSM (состояния, middleware, storage)
- Изменением навигации (menu/transient сообщения)
- Введением новой бизнес-логики

### Deployment (`docs/deployment.md`)

Читай перед:
- Изменением Docker/docker-compose конфигурации
- Добавлением переменных окружения
- Работой с health checks или мониторингом
- Troubleshooting на сервере

### Registry / Testing / Backlog

- `docs/registry-spec.md` — единственный источник правды по формату Excel-выгрузок (реестр, шорт-лист, лист «Голосование жюри»). Любые правки `services/registry.py` обязаны соответствовать описанному формату.
- `docs/testing.md` — список тестовых модулей, инструкция по запуску `pytest`, ручной smoke-чек-лист.
- `docs/backlog.md` — отложенные задачи (LINKS-UX и т.п.). При появлении новой отложенной идеи дописывай сюда.

## When to Consult Rules

Rules (`.cursor/rules/`) содержат стандарты написания кода. Они подключаются автоматически при генерации кода. Не дублировать их содержимое в docs.

| Rule | Содержание |
|------|------------|
| `core-standards.mdc` | Архитектура модулей, async, SQLAlchemy, error handling |
| `performance.mdc` | N+1, batch, session management |
| `bot.mdc` | Handler types, FSM conventions, message sending |
| `message-navigation.mdc` | reply_to_user, transient, cleanup_middleware |
| `pybotx-bubbles.mdc` | BubbleMarkup, bubbles=None, wait_callback |
| `mentions.mdc` | MentionBuilder.contact vs .user |
| `logging.mdc` | loguru only, no stdlib logging |
| `infrastructure.mdc` | File structure, security |
| `docker-build.mdc` | linux/amd64, --build |
| `conventional-commits.mdc` | Commit message format |

## Documentation Update Checklist

После значительных изменений в коде проверь, нужно ли обновить документацию:

### Новые модели / таблицы в `database/models.py`
- [ ] Добавить таблицу в `docs/architecture.md` → «Модель данных»
- [ ] Обновить индексы если добавлены новые

### Новые handlers
- [ ] Добавить в список модулей `docs/architecture.md` → «Структура модулей»
- [ ] Обновить `README.md` → «Команды бота» если добавлена пользовательская команда

### Изменения в `docker-compose.yml` / `Dockerfile`
- [ ] Обновить таблицу сервисов в `docs/deployment.md` → «Docker Compose»
- [ ] Обновить переменные окружения если добавлены новые

### Новые переменные окружения
- [ ] Добавить в `.env-example`
- [ ] Добавить в `docs/deployment.md` → «Переменные окружения»
- [ ] Прочитать в `app/config.py`

### Изменения в FSM / навигации
- [ ] Обновить `docs/architecture.md` → «FSM-система» или «Навигация»

### Новые services
- [ ] Добавить в список модулей `docs/architecture.md` → «Структура модулей»

### Появление periodic jobs (scheduler)
- [ ] Создать секцию «Scheduler» в `docs/architecture.md`
- [ ] Описать расписание и логику задач
- [ ] При необходимости включить отдельный scheduler-контейнер в `docker-compose.yml`

## How to Update Docs

Главное правило: **`docs/` — единственный источник правды**. Все требования и изменения поведения бота фиксируются прямо в актуальных файлах. `docs/history/ТЗ.md` не правится — это снапшот стартовых требований.

### architecture.md

Документ разделён на секции (по мере появления функциональности):
1. Обзор и стек
2. Структура модулей
3. Точка входа
4. Модель данных
5. FSM-система
6. Навигация
7. Scheduler / фоновые задачи
8. Логирование

Обновляй только затронутые секции. Не переписывай весь документ.

### deployment.md

Секции:
1. Docker Compose (сервисы)
2. Переменные окружения
3. Offline-деплой
4. Health checks
5. Мониторинг
6. Бэкапы
7. Troubleshooting
8. Безопасность

### README.md

Краткий обзор. Обновляй только при:
- Добавлении новых пользовательских команд
- Изменении технологического стека
- Изменении процесса установки

## Когда создавать новый скилл

Если в проекте появляется устойчивая фича со своими правилами работы (например, аналитика событий, интеграция с внешним API, миграционный паттерн), — создай отдельный скилл в `.cursor/skills/<feature-name>/SKILL.md` и опиши там best practices.
