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

### 3.1. Базовые поля (обязательно)

| Переменная | Описание |
|---|---|
| `BOT_ID` | UUID бота (из админки eXpress) |
| `CTS_URL` | URL CTS сервера |
| `BOT_SECRET_KEY` | Секретный ключ бота |
| `ADMIN_HUID` | UUID администратора (несколько — через запятую; первый получает алёрты) |
| `DB_HOST` | Адрес PostgreSQL (см. ниже) |
| `DB_PASSWORD` | Пароль PostgreSQL |
| `REDIS_URL` | URL Redis для FSM (по умолчанию `redis://172.20.0.4:6379/0`) |

### 3.2. Переменные конкурса «Безопасные рисунки» (Wave 2)

Эти переменные появились в Wave 2 — без них функциональные ветки модератора, жюри и хранилища не работают полноценно. Полный пример — в `.env-example`.

| Переменная | Обязательно | Описание | ТЗ |
|---|---|---|---|
| `MODERATOR_HUIDS` | да | UUID модераторов через запятую — прошиваются на старте, синхронизируются с БД (`§5.2`, `§27.2`). | §5.2, §27.2 |
| `JURY_HUIDS` | да | UUID членов жюри через запятую (`§5.4`, `§35.4`). | §5.4, §35.4 |
| `MODERATION_CHAT_ID` | да | UUID группового чата «Безопасные рисунки — модерация» (`§19`). Создаётся модератором, бот добавляется участником. | §19 |
| `MODERATION_CHAT_ID` пустой | — | Бот не упадёт, но `notifications` будут писать в лог `WARNING` без отправки в чат. | — |
| `ATTACHMENTS_DIR` | нет | Путь до каталога файлов внутри контейнера. По умолчанию `/app/data/attachments` смонтирован на named-volume `attachments_volume` (см. `docker-compose.yml`). | §21, §33.1 |
| `MAX_FILE_SIZE_MB` | нет | Лимит размера одного файла, по умолчанию `10`. | §11.4, §16 |
| `DISK_WARN_PCT` | нет | Порог предупреждения в чат модерации, по умолчанию `80`. | §28.1 |
| `DISK_BLOCK_PCT` | нет | Порог блокировки и авто-перехода в LINKS, по умолчанию `95`. | §28.1, §33.6 |
| `DISK_CHECK_INTERVAL_SEC` | нет | Период мониторинга диска, секунды; по умолчанию `1800` (30 мин); алёрт сам дедуплицируется (раз в 24 ч на порог). | §28.1 |
| `INTAKE_MODE_DEFAULT` | нет | Режим приёма заявок при первом старте: `files` (основной) или `links`. Дальше переключается через БД (`/intake_mode`). | §33.6 |
| `TOP_N` | нет | Размер шорт-листа на пул, по умолчанию `10`. | §35.1 |
| `JURY_ROUNDS` | нет | Максимум раундов до автоматического жребия, по умолчанию `3`. | §35.2 |
| `JURY_ROUND_DEADLINE_HOURS` | нет | Дедлайн одного раунда жюри в часах, по умолчанию `48`. | §35.6 |
| `JURY_POOLS_CONFIG` | нет | JSON-конфиг распределения судей по пулам. Пустая строка = все судьи во всех 9 пулах (3 трека × 3 возрастные категории). | §35.6 |
| `COMPETITION_YEAR` | нет | Год конкурса, используется в `BR-{YEAR}-NNNN`. По умолчанию `2026`. | §20 |
| `ENABLE_SCHEDULER` | нет | `true`/`false`, включать periodic jobs в этом процессе. При multi-worker деплое — `false` у бота, scheduler выносится в отдельный контейнер. | архитектура |
| `CONTACTS_TEXT` | нет | Переопределяет текст экрана «Контакты организаторов» (`§7`); многострочный — через `\n`. Пусто → дефолт из `app/config.py`. | §7 |

> **Совет.** Перед стартом откройте `.env-example` — там для каждой переменной короткий комментарий с дефолтом и примером значения. После заполнения сравните `diff .env-example .env`, чтобы не пропустить новые переменные при будущих обновлениях бота.

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
