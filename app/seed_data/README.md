# Seed data: импорт 100 тестовых заявок

Данные упаковываются **в Docker-образ бота** — отдельно на сервер ничего класть не нужно.

## 1. Положить картинки локально

```
app/seed_data/images/
  001.jpg
  002.jpg
  ...
  100.jpg
```

Имена файлов = колонка `image_file` в [`applications.csv`](applications.csv) (строки 001–100).

JPG/PNG **не коммитятся** в git, но попадают в образ при `./build.sh`.

## 2. Собрать деплой-пакет

```bash
./build.sh
```

Передайте `dist/kids_ai-deploy.tar.gz` инженеру — как обычно.

## 3. На prod после `docker compose up -d`

Бэкап (рекомендуется):

```bash
docker exec kids_ai_db pg_dump -U postgres kids_ai > backup_before_seed.sql
tar czf attachments_backup.tgz data/attachments/
```

Проверка (dry-run — файлы должны быть в образе):

```bash
docker exec kids_ai_bot python3 scripts/import_applications.py
```

Импорт:

```bash
docker exec kids_ai_bot python3 scripts/import_applications.py --apply
```

`parent_huid` берётся из `ADMIN_HUID` в `.env`. Явно:

```bash
docker exec kids_ai_bot python3 scripts/import_applications.py \
  --apply --parent-huid 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'
```

## 4. Проверка

```bash
docker exec kids_ai_db psql -U postgres -d kids_ai -c \
  "SELECT track, age_category, count(*) FROM applications WHERE parent_contact='mvershkov@beeline.ru' GROUP BY 1,2 ORDER BY 1,2;"
```

Ожидание: **100** заявок, пул `AI` / `AGE_7_12` = **32**.

## 5. Закрыть приём

Админ → Система → «Закрыть приём».

## Распределение

| Трек | 0–6 | 7–12 | 13–18 | Итого |
|------|-----|------|-------|-------|
| TRADITIONAL | 8 | 10 | 8 | 26 |
| AI | 8 | **32** | 8 | 48 |
| HANDMADE_TO_AI | 8 | 10 | 8 | 26 |

Все заявки: родитель = `ADMIN_HUID`, контакт `mvershkov@beeline.ru`, статус **ДОПУЩЕНО**.

Повторный `--apply` безопасен: уже импортированные строки пропускаются.
