---
name: project-docs
description: Find and maintain project documentation. Use when the user asks about project architecture, how a system works, deployment procedures, or when code changes require documentation updates. Also use proactively after significant code changes to suggest doc updates.
---
# Project Documentation

## Documentation Index

| Document | Path | Content |
|----------|------|---------|
| Architecture | `docs/architecture.md` | Модули, модель данных, FSM, навигация, scheduler, логирование |
| Deployment | `docs/deployment.md` | Docker, env vars, offline-деплой, мониторинг, troubleshooting, бэкапы |
| README | `README.md` | Обзор проекта, быстрый старт, команды бота |

> По мере развития проекта сюда добавляются предметные документы (например, `docs/feature-name.md`).

## When to Consult Docs

### Architecture (`docs/architecture.md`)

Читай перед:
- Добавлением нового handler, service, модели
- Изменением periodic jobs (когда они появятся)
- Работой с FSM (состояния, middleware, storage)
- Изменением навигации (menu/transient сообщения)
- Введением новой бизнес-логики

### Deployment (`docs/deployment.md`)

Читай перед:
- Изменением Docker/docker-compose конфигурации
- Добавлением переменных окружения
- Работой с health checks или мониторингом
- Troubleshooting на сервере

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

### architecture.md

Документ разделён на секции (по мере появления функциональности):
1. Обзор и стек
2. Структура модулей
3. Точка входа
4. Модель данных
5. FSM-система
6. Навигация
7. Scheduler (когда появится)
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
