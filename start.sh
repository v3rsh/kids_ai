#!/bin/bash

# Скрипт запуска бота kids_ai
# Использование: ./start.sh [dev|prod|test]
#   dev  — локальный запуск без Docker (требуется venv и ИНТЕРНЕТ для pip)
#   prod — запуск через Docker Compose (без PostgreSQL-контейнера)
#   test — запуск через Docker Compose с PostgreSQL-контейнером
#
# ВНИМАНИЕ: Режим dev требует интернет для установки зависимостей (pip install).
# На сервере без интернета используйте только test или prod режимы.

set -e

MODE=${1:-prod}

echo "=== Запуск бота kids_ai в режиме: $MODE ==="

# Проверяем наличие .env файла
if [ ! -f .env ]; then
    echo "ОШИБКА: Файл .env не найден!"
    echo "Скопируйте .env-example в .env и заполните переменные:"
    echo "  cp .env-example .env"
    exit 1
fi

# Проверяем обязательные переменные
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
        echo "На сервере без интернета используйте: ./start.sh test или ./start.sh prod"
        echo ""
        echo "Установка зависимостей..."
        pip install -r requirements.txt

        echo "Запуск сервера с автоперезагрузкой..."
        cd app
        uvicorn main:app --host 0.0.0.0 --port ${SERVER_PORT:-8000} --reload
        ;;
    test)
        echo "Режим тестирования (Docker + PostgreSQL)"

        docker compose --profile test down 2>/dev/null || true
        docker compose --profile test up -d --build

        echo "Бот и PostgreSQL запущены!"
        echo "Логи: docker compose --profile test logs -f"
        echo "Остановка: docker compose --profile test down"
        ;;
    prod)
        echo "Режим production (Docker, внешний PostgreSQL)"

        docker compose down 2>/dev/null || true
        docker compose up -d --build

        echo "Бот запущен!"
        echo "Логи: docker compose logs -f bot"
        echo "Остановка: docker compose down"
        ;;
    *)
        echo "Использование: $0 [dev|prod|test]"
        exit 1
        ;;
esac
