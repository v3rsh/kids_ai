#!/bin/bash

# Скрипт запуска бота kids_ai
# Использование: ./start.sh [dev|prod]
#   dev  — локальный запуск без Docker (требуется venv и ИНТЕРНЕТ для pip)
#   prod — запуск через Docker Compose (PostgreSQL + Redis + bot)
#
# ВНИМАНИЕ: Режим dev требует интернет для установки зависимостей (pip install).
# На сервере без интернета используйте только prod режим.

set -e

MODE=${1:-prod}

echo "=== Запуск бота kids_ai в режиме: $MODE ==="

if [ ! -f .env ]; then
    echo "ОШИБКА: Файл .env не найден!"
    echo "Скопируйте .env-example в .env и заполните переменные:"
    echo "  cp .env-example .env"
    exit 1
fi

source .env

if [ -z "$BOT_ID" ] || [ -z "$CTS_URL" ] || [ -z "$BOT_SECRET_KEY" ]; then
    echo "ОШИБКА: Не заданы обязательные переменные для бота!"
    echo "  Убедитесь, что в .env указаны:"
    echo "  - BOT_ID"
    echo "  - CTS_URL"
    echo "  - BOT_SECRET_KEY"
    exit 1
fi

case "$MODE" in
    dev)
        echo "Режим разработки (локальный запуск)"
        echo ""
        echo "ВНИМАНИЕ: Этот режим требует интернет для pip install."
        echo "На сервере без интернета используйте: ./start.sh prod"
        echo ""
        echo "Установка зависимостей..."
        pip install -r requirements.txt

        echo "Запуск сервера с автоперезагрузкой..."
        cd app
        uvicorn main:app --host 0.0.0.0 --port ${SERVER_PORT:-8000} --reload
        ;;
    prod)
        echo "Режим production (Docker: PostgreSQL + Redis + bot)"

        docker compose down 2>/dev/null || true
        docker compose up -d --build

        echo "Стек запущен (postgres, redis, bot)."
        echo "Логи: docker compose logs -f bot"
        echo "Остановка: docker compose down"
        ;;
    *)
        echo "Использование: $0 [dev|prod]"
        exit 1
        ;;
esac
