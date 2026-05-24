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
| `redis` | `redis:7-alpine` | 172.20.0.4 | FSM storage + message tracking; AOF на named volume |
| `postgres` | `postgres:15-alpine` | 172.20.0.3 | БД конкурса; данные на named volume `pgdata` |
| `scheduler` | `kids_ai_bot:latest` | 172.20.0.5 | Отдельный scheduler (закомментирован, активируется при появлении periodic jobs) |

### Запуск

`docker compose up -d` поднимает три контейнера (`postgres`, `redis`, `bot`); `bot` стартует только когда `postgres` и `redis` отдают `service_healthy`.

### Persistent volumes

| Volume | Что хранит | Поведение |
|---|---|---|
| `pgdata` | Данные PostgreSQL | переживает `docker compose down/up`, перезагрузку хоста |
| `redisdata` | AOF Redis (FSM-анкеты, трекинг transient-сообщений) | то же; `--appendfsync everysec` гарантирует потерю не более 1 сек данных при kill -9 |
| `attachments_volume` | Файлы заявок (`/app/data/attachments`) | то же |

Тома стираются только при `docker compose down -v` или явном `docker volume rm`.

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
| `ADMIN_HUID` / `ADMIN_HUIDS` | UUID администраторов через запятую (тех. роль — `/disk`, `/intake_mode`, discovery-кнопки; первый получает системные уведомления). Меняется только через рестарт. |
| `EXPRESS_DEEPLINK_TEMPLATE` | Опционально. Шаблон URL-deeplink для кнопки «🔎 Открыть в боте» в чате модерации. Плейсхолдеры: `{bot_id}`, `{cts_url}`. Если пусто — кнопка не добавляется. Пример: `express://chat?bot_id={bot_id}`. |

> Модераторы, жюри и чат модерации в env **не задаются** — только через
> discovery в боте (см. ниже «Управление ролями через бот»).

### Управление ролями через бот (единственный путь)

После того как бот поднят с заполненным `ADMIN_HUID`, конфигурация
ролей и чата модерации делается **только внутри бота**:

0. **Активируйте DM-канал админа.** Сам админ должен один раз написать
   боту `/start` в личном чате (или хотя бы открыть чат — `on_chat_created`
   делает `set_user_chat_id`). Без этого карточки discovery до админа
   просто не дойдут: бот не знает его `chat_id`. `/admin_state` покажет
   статус DM-канала.
1. **Назначить модератора или жюри.** Попросите кандидата написать
   боту `/moderator` (или `/jury`). Админу прилетит карточка
   с техническим профилем (HUID, AD-логин, должность, подразделение)
   и кнопками «Назначить / Отклонить». При нажатии «Назначить»:
   - запись попадает в `moderators` / `jury_members`;
   - кэш ролей обновляется атомарно;
   - для модератора — попытка автодобавления в чат модерации
     (нужно, чтобы бот был участником чата);
   - попытка отправить welcome-DM назначенному.
2. **Настроить чат модерации.** Создайте групповой чат и добавьте
   туда бота (прав админа в чате **не нужно**, достаточно участника).
   На `ChatCreatedEvent` админу всегда прилетает карточка с `chat_id`
   и кнопкой «Сделать чатом модерации». При нажатии бот запишет UUID
   в `app_settings.moderation_chat_id`, перечитает кэш и пошлёт в сам
   чат короткое подтверждение. На старте `_validate_moderation_chat`
   проверяет фактическое членство и сбрасывает настройку при ошибке.
3. **Диагностика.** Команды в личном чате с админом:
   - `/admin_roles` — активные роли и текущий `moderation_chat_id`
     с кнопками «🗑 Отозвать»;
   - `/admin_state` — состояние диска, intake_mode, валидность
     чата модерации, счётчики ролей, статус DM-канала самого админа.
4. **Отзыв роли.** Через кнопку из `/admin_roles` или вручную:
   `/admin_role_revoke <huid> <moderator|jury>`. Запись помечается
   `is_active=False`, история (голоса, комментарии) сохраняется.
5. **Мульти-роли.** Один HUID может быть админом + модератором + жюри +
   подавать заявки одновременно — проверки независимы, уведомления
   адресуются по разным каналам (DM участнику / DM админу / чат
   модерации).

Бот **молча игнорирует** входящие во всех групповых чатах — включая
чат модерации (см. `app/fsm/chat_gate.py`). Чат модерации служит
только для outbound-уведомлений с deeplink-кнопкой «🔎 Открыть в боте»
(если задан `EXPRESS_DEEPLINK_TEMPLATE`).

### Конкурс и реестр

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `COMPETITION_YEAR` | Год конкурса — используется в `BR-{YEAR}-NNNN` и имени файла реестра (см. [`registry-spec.md`](registry-spec.md) → «Имя отправляемого файла») | `2026` |
| `JURY_POOLS_CONFIG` | JSON `[{"huid": "...", "pools": [...] \| "all"}]`. Пустая строка = все активные судьи во всех 9 пулах (3 трека × 3 возрастные категории) | `""` |
| `INTAKE_MODE_DEFAULT` | `files` (основной) или `links` (резервный — присылается ссылка на облачную папку) | `files` |
| `TOP_N` | Размер шорт-листа на пул | `10` |
| `JURY_ROUNDS` | Максимум раундов до автоматического жребия | `3` |
| `JURY_ROUND_DEADLINE_HOURS` | Дедлайн одного раунда жюри | `48` |

### Хранилище и диск

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `ATTACHMENTS_DIR` | Корень файлового хранилища заявок; в контейнере — том `attachments_volume` (`/app/data/attachments`) | `data/attachments` |
| `MAX_FILE_SIZE_MB` | Лимит размера одного файла, присылаемого участником | `10` |
| `DISK_WARN_PCT` | Порог предупреждения в чат модерации | `80` |
| `DISK_BLOCK_PCT` | Порог блокировки приёма + автопереключение в режим LINKS | `95` |
| `DISK_CHECK_INTERVAL_SEC` | Интервал фонового монитора диска (запускается только при `ENABLE_SCHEDULER=true`). Сам алёрт дедуплицируется в БД на 24 ч | `1800` |

### PostgreSQL

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `DB_HOST` | Хост БД (контейнер `postgres` в docker-сети) | `172.20.0.3` |
| `DB_PORT` | Порт | `5432` |
| `DB_NAME` | Имя БД | `kids_ai` |
| `DB_USER` | Пользователь | `postgres` |
| `DB_PASSWORD` | Пароль | — |

Пул соединений SQLAlchemy: `pool_size=20`, `max_overflow=30` (макс. 50 соединений). Настраивается в `app/database/db.py`. PostgreSQL должен быть сконфигурирован с `max_connections` >= 50 (по умолчанию 100).

### Redis

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `REDIS_URL` | URL Redis (контейнер `redis`); **обязателен**, без неё бот падает на старте | `redis://172.20.0.4:6379/0` |
| `FSM_TTL_DAYS` | Время жизни FSM-записей (дни) | `30` |

### Scheduler / Workers

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `ENABLE_SCHEDULER` | Запускать фоновые задачи в процессе бота — сейчас это только **мониторинг диска** (`services.storage._disk_monitor_loop`). При `false` фоновый монитор НЕ работает; модератор должен пользоваться ручной командой `/disk` | `false` |
| `UVICORN_WORKERS` | Количество uvicorn workers | `1` |

**Правило**: `UVICORN_WORKERS > 1` требует `ENABLE_SCHEDULER=false`,
иначе lifespan бросит `RuntimeError` (дублирование фоновых задач
в нескольких web-процессах запрещено). Когда понадобится несколько
worker'ов — выноси scheduler в отдельный сервис из `docker-compose.yml`.

### Resource limits (Docker)

| Переменная | Описание | По умолчанию |
|-----------|----------|-------------|
| `BOT_MEM_LIMIT` | Лимит памяти для bot | `2G` |
| `REDIS_MEM_LIMIT` | Лимит памяти для redis-контейнера | `1G` |
| `REDIS_MAXMEMORY` | maxmemory для Redis (политика `noeviction`) | `512mb` |
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
```

Создаёт `dist/kids_ai-deploy.tar.gz`:
- `kids_ai_bot.tar` — Docker-образ бота
- `postgres.tar` — образ PostgreSQL
- `redis.tar` — образ Redis
- `docker-compose.yml`
- `.env-example`
- `DEPLOY.md`

### Установка на сервере

```bash
mkdir -p /opt/kids_ai && cd /opt/kids_ai
tar xzf kids_ai-deploy.tar.gz

docker load -i dist/kids_ai_bot.tar
docker load -i dist/redis.tar
docker load -i dist/postgres.tar

cp .env-example .env
nano .env  # заполнить

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
2. Проверить, что `REDIS_URL` задан в `.env`; без него бот не стартует.
3. Проверить, что `redisdata` volume не пересоздаётся при `docker compose up -d` (`docker volume ls | grep redisdata`); содержимое внутри: `docker run --rm -v kids_ai_redisdata:/data alpine ls -la /data`.

---

## Безопасность

- **НИКОГДА** не коммитить `.env` файлы
- Использовать `.env-example` как шаблон
- Хранить секреты только в переменных окружения
- Собирать образы только на доверенных машинах с интернетом
- Ограничить доступ к PostgreSQL подсетью Docker (`172.20.0.0/16`)
- Использовать `scram-sha-256` для аутентификации PostgreSQL
