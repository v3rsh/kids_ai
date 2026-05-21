# Деплой и эксплуатация

## Среда разработки

- Разработка локально в Cursor на macOS (Apple Silicon)
- Сборка Docker-образов локально (машина с интернетом)
- Целевой сервер — Linux amd64, **без интернета** (корпоративный периметр)
- На сервере предустановлены Docker и Docker Compose, нет git/pip/docker pull

---

## Docker Compose

### Сервисы

| Сервис | Образ | IP | Описание |
|--------|-------|----|----------|
| `bot` | `kids_ai_bot:latest` | 172.20.0.2 | Основной бот (Starlette + pybotx + uvicorn) |
| `redis` | `redis:7-alpine` | 172.20.0.4 | FSM storage + message tracking |
| `postgres` | `postgres:15-alpine` | 172.20.0.3 | БД (только с профилем `test`) |
| `scheduler` | `kids_ai_bot:latest` | 172.20.0.5 | Отдельный scheduler (закомментирован, активируется при появлении periodic jobs) |

### Профили

- **Без профиля** — `docker compose up -d` — только bot + redis (БД внешняя)
- **test** — `docker compose --profile test up -d` — bot + redis + postgres в контейнере

### Сеть

Все сервисы в сети `app_network` (subnet `172.20.0.0/16`).

---

## Переменные окружения

Шаблон: `.env-example`. Скопировать в `.env` и заполнить.

### Bot (обязательные)

| Переменная | Описание |
|-----------|----------|
| `BOT_ID` | UUID бота (из админки eXpress) |
| `CTS_URL` | URL CTS-сервера |
| `BOT_SECRET_KEY` | Секретный ключ бота |
| `ADMIN_HUID` | UUID администраторов через запятую (первый получает системные уведомления) |

### PostgreSQL

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `DB_HOST` | Хост БД (`172.20.0.3` для контейнера, IP сервера для внешней) | `172.20.0.3` |
| `DB_PORT` | Порт | `5432` |
| `DB_NAME` | Имя БД | `kids_ai` |
| `DB_USER` | Пользователь | `postgres` |
| `DB_PASSWORD` | Пароль | — |

Пул соединений SQLAlchemy: `pool_size=20`, `max_overflow=30` (макс. 50 соединений). Настраивается в `app/database/db.py`. PostgreSQL должен быть сконфигурирован с `max_connections` >= 50 (по умолчанию 100).

### Redis

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `REDIS_URL` | URL Redis (если не задан — fallback на SQLite) | `redis://172.20.0.4:6379/0` |
| `FSM_TTL_DAYS` | Время жизни FSM-записей (дни) | `30` |

### Scheduler / Workers

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `ENABLE_SCHEDULER` | Запускать scheduler в процессе бота (требует `app/scheduler.py`) | `false` |
| `UVICORN_WORKERS` | Количество uvicorn workers | `1` |

**Правило**: `UVICORN_WORKERS > 1` требует `ENABLE_SCHEDULER=false` + отдельный scheduler-контейнер.

### Resource limits (Docker)

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `BOT_MEM_LIMIT` | Лимит памяти для bot | `2G` |
| `REDIS_MEM_LIMIT` | Лимит памяти для redis-контейнера | `512M` |
| `REDIS_MAXMEMORY` | maxmemory для Redis | `256mb` |
| `PG_MEM_LIMIT` | Лимит памяти для postgres | `1G` |

### Logging

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `LOG_LEVEL` | Уровень: DEBUG, INFO, WARNING, ERROR | `INFO` |
| `JSON_LOGS` | JSON-формат для ELK | `False` |

### Прочее

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `SERVER_PORT` | Порт webhook-сервера | `8000` |
| `DEBUG` | Режим отладки | `False` |

---

## Offline-деплой

### Workflow

1. Сделать изменения, закоммитить
2. Запустить `./build.sh` — собирает образы под `linux/amd64`, экспортирует в `.tar`, создаёт архив
3. Передать `dist/kids_ai-deploy.tar.gz` инженеру
4. Инженер распаковывает, загружает образы, запускает

### Сборка

```bash
./build.sh
# или без образа PostgreSQL:
./build.sh --no-postgres
# или без образа Redis:
./build.sh --no-redis
```

Создаёт `dist/kids_ai-deploy.tar.gz`:
- `kids_ai_bot.tar` — Docker-образ бота
- `postgres.tar` — образ PostgreSQL (опционально)
- `redis.tar` — образ Redis (опционально)
- `docker-compose.yml`
- `.env-example`
- `DEPLOY.md`

### Установка на сервере

```bash
mkdir -p /opt/kids_ai && cd /opt/kids_ai
tar xzf kids_ai-deploy.tar.gz

docker load -i dist/kids_ai_bot.tar
docker load -i dist/redis.tar
docker load -i dist/postgres.tar  # если есть

cp .env-example .env
nano .env  # заполнить

# Тестовый режим (PostgreSQL в контейнере)
docker compose --profile test up -d

# Продакшн (внешний PostgreSQL)
docker compose up -d
```

### Обновление

```bash
docker compose down
docker load -i dist/kids_ai_bot.tar
docker compose up -d
```

---

## Scheduler-контейнер

При появлении периодических задач (notifications, бэкапы, очистка) и multi-worker деплое scheduler запускается как отдельный контейнер.

### Когда нужен

- `UVICORN_WORKERS > 1` — scheduler не должен дублироваться в каждом worker
- Для изоляции: фоновые задачи не конкурируют с обработкой webhook

### Как подключить

1. Создать `app/scheduler.py` (определение jobs) и `app/scheduler_worker.py` (точка входа)
2. В `main.py` импортировать `setup_scheduler` и вызывать его при `ENABLE_SCHEDULER=true`
3. Раскомментировать блок `scheduler` в `docker-compose.yml`
4. Установить `ENABLE_SCHEDULER=false` в `.env`
5. Установить `UVICORN_WORKERS=2` (или больше)

### Ручной запуск (без docker-compose)

```bash
docker run -d --name kids_ai_scheduler \
  --network <project>_app_network \
  --env-file .env \
  -e DB_HOST=172.20.0.3 \
  -e REDIS_URL=redis://172.20.0.4:6379/0 \
  -v /opt/kids_ai/data:/app/data \
  -v /opt/kids_ai/logs:/app/logs \
  -v /opt/kids_ai/backup:/app/backup \
  kids_ai_bot:latest \
  python scheduler_worker.py
```

Имя сети зависит от имени проекта Docker Compose (проверить: `docker network ls | grep app_network`).

---

## Health Checks

### Эндпоинты

| Эндпоинт | Проверяет | Использование |
|-----------|-----------|--------------|
| `GET /healthz` | PostgreSQL + Redis | Readiness probe, Docker healthcheck |
| `GET /livez` | Ничего (процесс жив) | Liveness probe |
| `GET /` | Ничего | Быстрая проверка |

### Docker healthcheck

Bot-контейнер проверяет `/healthz` каждые 30 секунд:
```
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3
    CMD curl -f http://localhost:8000/healthz || exit 1
```

Scheduler-контейнер: healthcheck от Dockerfile (проверяет `/healthz`, но HTTP-сервер не запущен — будет `unhealthy`). Это не влияет на работу; проверять через `docker logs`.

---

## Мониторинг

### Логи

```bash
# Bot
docker compose logs -f bot
docker compose logs --tail=100 bot

# Scheduler (если отдельный контейнер)
docker logs --tail=100 kids_ai_scheduler

# Файловые логи (внутри контейнера и на хосте)
# Хост: ./logs/app.log
```

### Проверка состояния

```bash
# Контейнеры запущены?
docker compose ps

# Health check
curl http://localhost:8000/healthz

# Если есть scheduler-контейнер
docker inspect -f 'status={{.State.Status}} restart_count={{.RestartCount}}' kids_ai_scheduler
```

---

## Бэкапы

### Ручной бэкап (через docker exec)

```bash
# Из хоста через docker exec
docker exec kids_ai_db pg_dump -U postgres kids_ai > backup.sql

# Через psql из соседнего контейнера / хоста
pg_dump -h 172.20.0.3 -U postgres -d kids_ai > backup.sql
```

### Восстановление

```bash
psql -h 172.20.0.3 -U postgres -d kids_ai < backup.sql
```

### Автоматические бэкапы

При появлении scheduler-контейнера добавь `backup_job` (ежедневно в 03:00 МСК), который:
- Выполняет `pg_dump` через `subprocess`
- Сохраняет в `backup/pg_backup_YYYYMMDD_HHMMSS.sql`
- Удаляет файлы старше 7 дней

---

## Troubleshooting

### Container name conflict

```
Error: container name "/kids_ai_scheduler" is already in use
```

Решение: `docker rm -f kids_ai_scheduler`

### Network not found

```
Error: network kids_ai_app_network not found
```

Docker Compose добавляет префикс проекта к имени сети. Найти правильное имя:
```bash
docker network ls | grep app_network
```

### Image conflict (unable to remove)

```
Error: unable to remove repository reference (container is using image)
```

Решение: остановить и удалить контейнер, затем удалить образ:
```bash
docker stop <container_id>
docker rm <container_id>
docker rmi <image_name>
```

### Duplicate index при старте с несколькими workers

```
duplicate key value violates unique constraint "pg_class_relname_nsp_index"
```

Гонка при создании индексов двумя workers одновременно. Безопасно — индекс уже создан первым worker. Повторный запуск не выдаст ошибку.

### Redis warning: Memory overcommit

```
WARNING Memory overcommit must be enabled!
```

На сервере (требует root):
```bash
sysctl vm.overcommit_memory=1
# Для сохранения: echo "vm.overcommit_memory = 1" >> /etc/sysctl.conf
```

### Bot не отвечает на команды

1. Проверить логи: `docker compose logs -f bot`
2. Проверить контейнер: `docker compose ps`
3. Проверить webhook URL в админке eXpress
4. Проверить health: `curl http://localhost:8000/healthz`

### FSM-состояния не сохраняются

1. Проверить Redis: `docker exec kids_ai_redis redis-cli ping` → `PONG`
2. Если Redis недоступен и `REDIS_URL` задан — FSM не работает
3. Без `REDIS_URL` — проверить файл `./data/states.sqlite3` и права доступа

---

## Безопасность

- **НИКОГДА** не коммитить `.env` файлы
- Использовать `.env-example` как шаблон
- Хранить секреты только в переменных окружения
- Собирать образы только на доверенных машинах с интернетом
- Ограничить доступ к PostgreSQL подсетью Docker (`172.20.0.0/16`)
- Использовать `scram-sha-256` для аутентификации PostgreSQL
