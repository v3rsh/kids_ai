# Установка бота kids_ai на сервер

## Требования

- Docker и Docker Compose установлены на сервере
- Архив `kids_ai-deploy.tar.gz` передан на сервер

## Шаг 1. Распаковка архива

```bash
mkdir -p /opt/kids_ai
cd /opt/kids_ai
tar xzf /путь/к/kids_ai-deploy.tar.gz
```

После распаковки в директории будут:

```
├── docker-compose.yml
├── .env-example
├── DEPLOY.md
└── dist/
    ├── kids_ai_bot.tar
    ├── redis.tar          (FSM storage)
    └── postgres.tar       (если включен)
```

## Шаг 2. Загрузка Docker-образов

```bash
docker load -i dist/kids_ai_bot.tar
docker load -i dist/redis.tar
docker load -i dist/postgres.tar    # если включен
```

Проверка:

```bash
docker images | grep -E "kids_ai|redis|postgres"
```

## Шаг 3. Настройка переменных окружения

```bash
cp .env-example .env
nano .env   # или vi .env
```

Заполните обязательные поля:

| Переменная | Описание |
|---|---|
| `BOT_ID` | UUID бота (из админки eXpress) |
| `CTS_URL` | URL CTS сервера |
| `BOT_SECRET_KEY` | Секретный ключ бота |
| `ADMIN_HUID` | UUID администратора |
| `DB_HOST` | Адрес PostgreSQL (см. ниже) |
| `DB_PASSWORD` | Пароль PostgreSQL |
| `REDIS_URL` | URL Redis для FSM (по умолчанию `redis://172.20.0.4:6379/0`) |

## Шаг 4. Запуск

### Вариант А: Тестовый режим (со встроенным PostgreSQL)

Используется встроенный контейнер PostgreSQL. Установите в `.env`:

```env
DB_HOST=172.20.0.3
DB_PORT=5432
```

Запуск:

```bash
docker compose --profile test up -d
```

### Вариант Б: Продакшн (внешний PostgreSQL)

Укажите в `.env` адрес корпоративного PostgreSQL:

```env
DB_HOST=10.x.x.x
DB_PORT=5432
```

Запуск (без контейнера PostgreSQL):

```bash
docker compose up -d
```

## Redis (FSM storage)

Redis используется для хранения состояний диалогов (FSM). Контейнер Redis запускается автоматически вместе с ботом.

Данные Redis сохраняются на диск (AOF-персистентность) и переживают рестарт контейнера.

**Настройки в `.env`:**

| Переменная | По умолчанию | Описание |
|---|---|---|
| `REDIS_URL` | `redis://172.20.0.4:6379/0` | URL подключения к Redis |
| `FSM_TTL_DAYS` | `30` | Время жизни FSM-записей (дни) |

Если используется внешний Redis (продакшн), измените `REDIS_URL`:

```env
REDIS_URL=redis://10.x.x.x:6379/0
```

Если `REDIS_URL` не задан, бот автоматически использует SQLite (файл `data/states.sqlite3`).

## Проверка работы

```bash
# Статус контейнеров
docker compose ps

# Логи бота
docker compose logs -f bot

# Health check
curl http://localhost:8000/healthz
```

## Остановка

```bash
docker compose down               # бот + Redis
docker compose --profile test down # бот + Redis + PostgreSQL
```

## Обновление

При получении нового архива:

```bash
docker compose down
docker load -i dist/kids_ai_bot.tar
docker compose up -d
```

## Откат к предыдущей версии

Сохраняйте предыдущие `.tar` файлы с номером версии:

```bash
# Перед обновлением
cp dist/kids_ai_bot.tar dist/kids_ai_bot_backup.tar

# Откат
docker compose down
docker load -i dist/kids_ai_bot_backup.tar
docker compose up -d
```

## Решение проблем

| Проблема | Решение |
|---|---|
| Контейнер не запускается | `docker compose logs bot` |
| Ошибка подключения к БД | Проверить `DB_HOST`, `DB_PASSWORD` в `.env` |
| Бот не отвечает | Проверить `BOT_ID`, `CTS_URL`, `BOT_SECRET_KEY` |
| PostgreSQL не стартует | `docker compose --profile test logs postgres` |
| Redis не стартует | `docker compose logs redis` |
| Ошибка подключения к Redis | Проверить `REDIS_URL` в `.env` |
| Нет доступа к порту | Проверить `SERVER_PORT` и firewall |
