#!/bin/bash

# Скрипт сборки и экспорта Docker-образов для офлайн-деплоя
# Использование: ./build.sh [--no-postgres] [--no-redis]
#
# Собирает образ бота, скачивает postgres:15-alpine и redis:7-alpine,
# экспортирует все в .tar и пакует деплой-архив.
# Результат: dist/kids_ai-deploy.tar.gz

set -e

DIST_DIR="dist"
BOT_IMAGE="kids_ai_bot:latest"
PG_IMAGE="postgres:15-alpine"
REDIS_IMAGE="redis:7-alpine"
INCLUDE_POSTGRES=true
INCLUDE_REDIS=true

# Разбор аргументов
for arg in "$@"; do
    case $arg in
        --no-postgres)
            INCLUDE_POSTGRES=false
            shift
            ;;
        --no-redis)
            INCLUDE_REDIS=false
            shift
            ;;
    esac
done

echo "=== Сборка деплой-пакета kids_ai ==="
echo ""

# Создаем директорию для артефактов
rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"

# 1. Собираем образ бота (для amd64 — целевой сервер)
echo "[1/5] Сборка Docker-образа бота (платформа: linux/amd64)..."
docker build --platform linux/amd64 --no-cache -t "$BOT_IMAGE" .
echo "      Готово."

# 2. Скачиваем PostgreSQL (если нужен)
if [ "$INCLUDE_POSTGRES" = true ]; then
    echo "[2/5] Подготовка образа PostgreSQL (платформа: linux/amd64)..."
    docker rmi "$PG_IMAGE" 2>/dev/null || true
    docker buildx create --name multiarch --use 2>/dev/null || docker buildx use multiarch
    docker buildx build --platform linux/amd64 --tag "$PG_IMAGE" --load - <<EOF
FROM postgres:15-alpine
EOF
    echo "      Готово."
else
    echo "[2/5] PostgreSQL пропущен (--no-postgres)."
fi

# 3. Скачиваем Redis (если нужен)
if [ "$INCLUDE_REDIS" = true ]; then
    echo "[3/5] Подготовка образа Redis (платформа: linux/amd64)..."
    docker rmi "$REDIS_IMAGE" 2>/dev/null || true
    docker buildx create --name multiarch --use 2>/dev/null || docker buildx use multiarch
    docker buildx build --platform linux/amd64 --tag "$REDIS_IMAGE" --load - <<EOF
FROM redis:7-alpine
EOF
    echo "      Готово."
else
    echo "[3/5] Redis пропущен (--no-redis)."
fi

# 4. Экспортируем образы в .tar
echo "[4/5] Экспорт образов в .tar..."
docker save -o "$DIST_DIR/kids_ai_bot.tar" "$BOT_IMAGE"
echo "      -> $DIST_DIR/kids_ai_bot.tar"

if [ "$INCLUDE_POSTGRES" = true ]; then
    docker save -o "$DIST_DIR/postgres.tar" "$PG_IMAGE"
    echo "      -> $DIST_DIR/postgres.tar"
fi

if [ "$INCLUDE_REDIS" = true ]; then
    docker save -o "$DIST_DIR/redis.tar" "$REDIS_IMAGE"
    echo "      -> $DIST_DIR/redis.tar"
fi

# 5. Пакуем деплой-архив
echo "[5/5] Создание деплой-архива..."

# Собираем список файлов для архива
ARCHIVE_FILES=(
    "$DIST_DIR/kids_ai_bot.tar"
    "docker-compose.yml"
    ".env-example"
    "DEPLOY.md"
)

if [ "$INCLUDE_POSTGRES" = true ]; then
    ARCHIVE_FILES+=("$DIST_DIR/postgres.tar")
fi

if [ "$INCLUDE_REDIS" = true ]; then
    ARCHIVE_FILES+=("$DIST_DIR/redis.tar")
fi

tar czf "$DIST_DIR/kids_ai-deploy.tar.gz" "${ARCHIVE_FILES[@]}"

# Размеры файлов
echo ""
echo "=== Готово ==="
echo "Файлы в $DIST_DIR/:"
ls -lh "$DIST_DIR/"
echo ""
echo "Передайте файл $DIST_DIR/kids_ai-deploy.tar.gz инженеру."
echo "Инструкция по установке: DEPLOY.md"
