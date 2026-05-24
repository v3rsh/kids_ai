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
    └── postgres.tar       (база данных)
```

## Шаг 2. Загрузка Docker-образов

```bash
docker load -i dist/kids_ai_bot.tar
docker load -i dist/redis.tar
docker load -i dist/postgres.tar
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
| `DB_HOST` | Адрес PostgreSQL внутри docker-сети — `172.20.0.3` |
| `DB_PASSWORD` | Пароль PostgreSQL |
| `REDIS_URL` | URL Redis для FSM (по умолчанию `redis://172.20.0.4:6379/0`); **обязателен** |

### 3.2. Переменные конкурса «Безопасные рисунки»

Эти переменные нужны функциональным веткам модератора, жюри и хранилища. Полный пример — в `.env-example`, подробное описание поведения — в [`docs/deployment.md`](docs/deployment.md).

| Переменная | Обязательно | Описание |
|---|---|---|
| `MODERATOR_HUIDS` | да | UUID модераторов через запятую — прошиваются на старте, синхронизируются с БД. |
| `JURY_HUIDS` | да | UUID членов жюри через запятую. |
| `MODERATION_CHAT_ID` | да | UUID группового чата «Безопасные рисунки — модерация». Создаётся модератором, бот добавляется участником. |
| `MODERATION_CHAT_ID` пустой | — | Бот не упадёт, но `notifications` будут писать в лог `WARNING` без отправки в чат. |
| `ATTACHMENTS_DIR` | нет | Путь до каталога файлов внутри контейнера. По умолчанию `/app/data/attachments` смонтирован на named-volume `attachments_volume` (см. `docker-compose.yml`). |
| `MAX_FILE_SIZE_MB` | нет | Лимит размера одного файла, по умолчанию `10`. |
| `DISK_WARN_PCT` | нет | Порог предупреждения в чат модерации, по умолчанию `80`. |
| `DISK_BLOCK_PCT` | нет | Порог блокировки и авто-перехода в LINKS, по умолчанию `95`. |
| `DISK_CHECK_INTERVAL_SEC` | нет | Период мониторинга диска, секунды; по умолчанию `1800` (30 мин); алёрт сам дедуплицируется (раз в 24 ч на порог). |
| `INTAKE_MODE_DEFAULT` | нет | Режим приёма заявок при первом старте: `files` (основной) или `links`. Дальше переключается через БД (`/intake_mode`). |
| `TOP_N` | нет | Размер шорт-листа на пул, по умолчанию `10`. |
| `JURY_ROUNDS` | нет | Максимум раундов до автоматического жребия, по умолчанию `3`. |
| `JURY_ROUND_DEADLINE_HOURS` | нет | Дедлайн одного раунда жюри в часах, по умолчанию `48`. |
| `JURY_POOLS_CONFIG` | нет | JSON-конфиг распределения судей по пулам. Пустая строка = все судьи во всех 9 пулах (3 трека × 3 возрастные категории). |
| `COMPETITION_YEAR` | нет | Год конкурса, используется в `BR-{YEAR}-NNNN`. По умолчанию `2026`. |
| `ENABLE_SCHEDULER` | нет | `true`/`false`, включать periodic jobs в этом процессе. При multi-worker деплое — `false` у бота, scheduler выносится в отдельный контейнер. |
| `CONTACTS_TEXT` | нет | Переопределяет текст экрана «Контакты организаторов»; многострочный — через `\n`. Пусто → дефолт из `app/config.py`. |

> **Совет.** Перед стартом откройте `.env-example` — там для каждой переменной короткий комментарий с дефолтом и примером значения. После заполнения сравните `diff .env-example .env`, чтобы не пропустить новые переменные при будущих обновлениях бота.

## Шаг 4. Запуск

Стек поднимается одной командой — три контейнера: `postgres`, `redis`, `bot`.
Параметры подключения по умолчанию совпадают с docker-сетью compose:

```env
DB_HOST=172.20.0.3
DB_PORT=5432
REDIS_URL=redis://172.20.0.4:6379/0
```

```bash
docker compose up -d
```

## Persistence (что и где хранится)

| Volume | Что хранит | Как переживает рестарт |
|---|---|---|
| `pgdata` | Данные PostgreSQL: заявки, статусы, голоса жюри | named volume; переживает `docker compose down/up` и перезагрузку хоста |
| `redisdata` | AOF-файл Redis: FSM-состояния анкет, трекинг transient-сообщений | named volume + `--appendonly yes --appendfsync everysec`; данные сохраняются раз в секунду на диск |
| `attachments_volume` | Файлы заявок в `/app/data/attachments` (§21, §33.1) | named volume; не пересоздаётся при обновлении бота |

Безопасные команды (данные сохраняются):

```bash
docker compose down
docker compose up -d
docker compose restart
```

Опасные команды (стирают данные):

```bash
docker compose down -v          # удаляет ВСЕ named volumes этого compose-проекта
docker volume rm kids_ai_pgdata # точечное удаление конкретного тома
docker system prune --volumes   # удаляет все неиспользуемые volumes
```

## Redis (FSM storage)

Redis хранит состояния диалогов (FSM). Контейнер Redis запускается автоматически вместе с ботом, AOF на named volume `redisdata` обеспечивает сохранность данных при рестарте.

**Настройки в `.env`:**

| Переменная | По умолчанию | Описание |
|---|---|---|
| `REDIS_URL` | `redis://172.20.0.4:6379/0` | URL подключения к Redis (обязателен) |
| `FSM_TTL_DAYS` | `30` | Время жизни FSM-записей (дни) |
| `REDIS_MAXMEMORY` | `512mb` | maxmemory Redis (политика `noeviction`) |
| `REDIS_MEM_LIMIT` | `1G` | Docker mem limit Redis-контейнера |

Если `REDIS_URL` не задан, бот **падает на старте** с понятным сообщением — fallback на SQLite убран.

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
docker compose down              # остановит bot + redis + postgres (данные на volumes сохранятся)
docker compose down -v           # ОПАСНО: удалит pgdata/redisdata/attachments_volume
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

## Чек-лист ручной приёмки

После первого запуска бота на сервере пройдите шаги ниже. Если хоть один пункт не пройден — собирайте логи (`docker compose logs --tail=200 bot`) и фиксируйте инцидент до того, как давать доступ заявителям.

> **Где смотреть.** Все ключевые этапы lifespan пишут структурный лог через loguru: фильтруйте по полю `event` или ключевым словам в нижеследующих пунктах. На health-check `curl http://localhost:8000/healthz` ожидается `200 OK`.

### Шаг 1. Lifespan и стартовые задачи

```bash
docker compose logs --tail=400 bot | grep -E "scheduler|disk monitor|роли|collector"
```

Ожидаем:

- [ ] `Логирование настроено` — конфигурация loguru применена.
- [ ] Сообщение про синхронизацию ролей (`MODERATOR_HUIDS` / `JURY_HUIDS`).
- [ ] Старт disk-monitor с интервалом `DISK_CHECK_INTERVAL_SEC`.
- [ ] Старт scheduler (только если `ENABLE_SCHEDULER=true`).
- [ ] Все коллекторы зарегистрированы (admin, moderator, jury, intake, registry, common).
- [ ] `curl http://localhost:8000/healthz` → `200 OK`.

### Шаг 2. Подача заявки (FILES)

С тестового HUID:

- [ ] Команда `/menu_about`/`/menu_rules`/`/menu_examples`/`/menu_dates`/`/menu_contacts` отвечают непустыми текстами; «Контакты» содержат содержимое `CONTACTS_TEXT` (или дефолт).
- [ ] `/apply` запускает анкету; шаги 1..7 проходятся через FSM (после рестарта контейнера состояние не теряется — Redis FSM).
- [ ] Файл вложением валидируется по расширению и размеру (`MAX_FILE_SIZE_MB`); дубль второго файла в треках AI / Handmade-to-AI отвергается.
- [ ] После submit пользователь получает «Заявка принята» и `BR-2026-NNNN`.
- [ ] В БД (`SELECT * FROM applications ORDER BY created_at DESC LIMIT 1`) есть запись с актуальным `intake_mode`.
- [ ] В `attachments_volume` появилась папка `BR-2026-NNNN_<…>` с файлами + `meta.txt` + `description.txt`.

### Шаг 3. Модератор

С HUID из `MODERATOR_HUIDS`:

- [ ] `/queue` показывает поданную заявку с фильтрами по статусу/трекам.
- [ ] `/accept BR-2026-NNNN` переводит заявку в `допущено`; в чат модерации улетает уведомление; участник получает сообщение «принято».
- [ ] `/reject BR-2026-NNNN <причина>` переводит в `отклонено`; участник получает текст с причиной.
- [ ] `/intake_mode files|links` корректно переключает режим; следующая поданная заявка получает новый режим.
- [ ] `/files BR-2026-NNNN` отдаёт ссылки/команды для просмотра вложений.

### Шаг 4. Жюри

С HUID из `JURY_HUIDS`:

- [ ] При первом раунде жюри получает уведомление с пулом и кнопками голосования.
- [ ] Голос `Достоин`/`Не достоин` сохраняется как `JuryVote` (`SUBMITTED`).
- [ ] После закрытия раунда (все судьи проголосовали ИЛИ дедлайн `JURY_ROUND_DEADLINE_HOURS`) — пересчёт топа: либо консенсус, либо открытие следующего раунда, либо жребий после `JURY_ROUNDS`.
- [ ] `JURY_POOLS_CONFIG` (если задан) уважается: судья видит только свой пул.

### Шаг 5. Реестр и шорт-лист

С HUID модератора:

- [ ] `/export` возвращает файл `registry_BR-2026_<dt>.xlsx` с двумя листами `Реестр` / `Голосование жюри`; в `Реестр` 29 колонок, freeze/autofilter — по `docs/registry-spec.md`.
- [ ] `/export_shortlist` возвращает `shortlist_BR-2026_<dt>.xlsx`: один лист `Шорт-лист`, разделители пулов, 13 колонок.
- [ ] Колонка №13 «Команда/ссылка просмотра файлов» корректно переключается между `/files BR-XXXX` и URL по `intake_mode`.

### Шаг 6. Диск-монитор

- [ ] При искусственном заполнении тома `attachments_volume` свыше `DISK_WARN_PCT` — в чат модерации приходит первое предупреждение, дедуп `1×24h` на порог.
- [ ] При превышении `DISK_BLOCK_PCT` — авто-переход `INTAKE_MODE = LINKS` (`app_settings.intake_mode = 'links'`); в чат модерации приходит уведомление об автопереключении.

> Если все 6 шагов зелёные — бот готов к открытию приёма заявок. Перед открытием не забудьте сообщить участникам контакты модераторов и URL чата.

## Решение проблем

| Проблема | Решение |
|---|---|
| Контейнер не запускается | `docker compose logs bot` |
| Ошибка подключения к БД | Проверить `DB_HOST`, `DB_PASSWORD` в `.env` |
| Бот не отвечает | Проверить `BOT_ID`, `CTS_URL`, `BOT_SECRET_KEY` |
| PostgreSQL не стартует | `docker compose logs postgres` |
| Redis не стартует | `docker compose logs redis` |
| Ошибка подключения к Redis | Проверить `REDIS_URL` в `.env` |
| Нет доступа к порту | Проверить `SERVER_PORT` и firewall |
| Уведомления модераторам не приходят | Проверить `MODERATION_CHAT_ID`; бот должен быть участником чата |
| Жюри не видит работ | Проверить `JURY_HUIDS`, `JURY_POOLS_CONFIG`; статус заявки `допущено` |
| `/export` падает | Проверить, что в `requirements.txt` установлены `openpyxl` и `Pillow` |
