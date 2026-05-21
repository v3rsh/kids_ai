---
name: query-server-db
description: Generate ready-to-paste psql commands for querying the bot's PostgreSQL database on the server. Use when the user asks to check data, get statistics, find users, debug database state, or needs any SQL query for the server terminal.
---

# Query Server DB

Генерирует готовые однострочные команды для выполнения в терминале сервера из каталога проекта.

## Формат команды

Все команды — через `docker exec` к контейнеру PostgreSQL:

```bash
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SQL_QUERY"
```

Для табличного вывода с выравниванием (по умолчанию) — без дополнительных флагов.
Для компактного вывода без рамок — добавить `-t` (tuples only):

```bash
docker exec kids_ai_db psql -U postgres -d kids_ai -t -c "SELECT count(*) FROM users"
```

Для CSV-экспорта:

```bash
docker exec kids_ai_db psql -U postgres -d kids_ai -c "COPY (SQL_QUERY) TO STDOUT WITH CSV HEADER"
```

## Подключение

| Параметр | Тестовый режим | Продакшн |
|----------|---------------|----------|
| Контейнер | `kids_ai_db` | `kids_ai_db` (если PG в контейнере) |
| База | `kids_ai` | `kids_ai` |
| Пользователь | `postgres` | `postgres` |
| Пароль | из `.env` | из `.env` |

Если PostgreSQL внешний (продакшн без контейнера), вместо `docker exec` используй:

```bash
PGPASSWORD=$(grep DB_PASSWORD .env | cut -d= -f2) psql -h $(grep DB_HOST .env | cut -d= -f2) -U postgres -d kids_ai -c "SQL_QUERY"
```

## Схема базы данных

> ВАЖНО: схема описывается по мере появления моделей в `app/database/models.py`.
> На старте проекта таблиц практически нет. После добавления модели обнови этот раздел.

### Шаблонные колонки (рекомендуется наследовать в моделях)

| Колонка | Тип | Описание |
|---------|-----|----------|
| huid | UUID, PK | Идентификатор пользователя (если таблица users) |
| chat_id | UUID | ID чата для проактивных сообщений |
| full_name | VARCHAR(255) | ФИО пользователя |
| is_deleted | BOOLEAN | Soft-delete флаг |
| created_at | TIMESTAMP | Дата создания |
| updated_at | TIMESTAMP | Дата обновления |

## Правила построения запросов

1. **Soft-delete** — почти всегда фильтруй по `is_deleted = false`:
   ```sql
   WHERE is_deleted = false
   ```

2. **Если в модели появится `registration_complete`/`is_active`** — добавляй такие фильтры в зависимости от смысла запроса.

3. **Аналитика** — если в проекте появятся события и флаги тестов/админов:
   ```sql
   WHERE is_admin = false AND is_test = false
   ```

4. **UUID-ы** — оборачивай в одинарные кавычки:
   ```sql
   WHERE huid = 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
   ```

5. **Enum-значения** — строки в одинарных кавычках:
   ```sql
   WHERE status = 'active'
   ```

6. **SQLAlchemy enums хранятся в БД по имени атрибута (UPPERCASE)**, а не по `value`. Сравнивай как `WHERE field::text = 'NAME_UPPER'`. Проверить набор значений конкретного enum:
   ```sql
   SELECT unnest(enum_range(NULL::yourenum))::text ORDER BY 1;
   ```

## Базовая библиотека запросов

### Общая разведка

```bash
# Список всех таблиц
docker exec kids_ai_db psql -U postgres -d kids_ai -c "\dt"

# Структура таблицы
docker exec kids_ai_db psql -U postgres -d kids_ai -c "\d+ users"

# Все enum типы
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SELECT n.nspname, t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typtype = 'e'"
```

### Подсчёт записей

```bash
# Всего пользователей (когда появится таблица users)
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SELECT count(*) FROM users WHERE is_deleted = false"

# Активность за период
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SELECT count(*) FROM users WHERE created_at > now() - interval '7 days'"
```

### Поиск пользователя

```bash
# По имени (частичное совпадение)
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SELECT huid, full_name FROM users WHERE full_name ILIKE '%Иванов%' AND is_deleted = false"

# По UUID
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SELECT * FROM users WHERE huid = 'UUID_HERE'"
```

### Размер таблиц (для оптимизации)

```bash
# Топ-10 самых больших таблиц
docker exec kids_ai_db psql -U postgres -d kids_ai -c "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10"
```

## Инструкции для агента

При ответе на запрос пользователя:

1. **Определи**, какие таблицы и колонки нужны (по схеме выше)
2. **Если схема устарела** — попроси прочитать `app/database/models.py` и обнови раздел «Схема базы данных» в этом скилле
3. **Составь SQL**, учитывая фильтры (`is_deleted`, soft-delete, enum в UPPERCASE)
4. **Оберни** в формат `docker exec kids_ai_db psql -U postgres -d kids_ai -c "..."`
5. **Выдай одну готовую команду** — пользователь копирует и вставляет в терминал
6. Если нужна вариативность — дай 2-3 варианта с пояснениями
7. Используй `ILIKE` для нечувствительного к регистру поиска
8. Для дат используй `now() - interval 'N days/hours'`
9. Для сложных выводов добавляй алиасы (`AS`) для читаемости

Если пользователь спрашивает на естественном языке — переведи в SQL и сразу выдай команду.
