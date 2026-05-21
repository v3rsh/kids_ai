#!/bin/bash

# Скрипт для запуска SAST-анализа основного кода проекта с использованием Bandit
# Автоматически устанавливает Bandit, если он отсутствует

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# Проверяем, установлен ли Bandit
if ! command -v bandit &> /dev/null; then
    echo -e "${YELLOW}Bandit не найден. Устанавливаем...${NC}"
    pip install bandit
    
    # Повторная проверка
    if ! command -v bandit &> /dev/null; then
        echo -e "${RED}Не удалось установить Bandit. Убедитесь, что pip установлен и работает.${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}Bandit успешно установлен!${NC}"
fi

# Получаем путь к директории скрипта
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Создаем директорию для отчетов, если она не существует
REPORTS_DIR="${PROJECT_ROOT}/security_reports"
mkdir -p "$REPORTS_DIR"

# Генерируем имя файла отчета с текущей датой и временем
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
HTML_REPORT="${REPORTS_DIR}/core_security_report_${TIMESTAMP}.html"

echo -e "${YELLOW}Запуск сканирования безопасности основного кода проекта...${NC}"
echo -e "${YELLOW}Сканирование затронет только основные компоненты проекта, игнорируя зависимости.${NC}"

# Запускаем Python-скрипт для анализа
python3 "${SCRIPT_DIR}/run_core_scan.py" --html --output "core_security_report_${TIMESTAMP}.html"

# Получаем статус выполнения
STATUS=$?

if [ $STATUS -eq 0 ]; then
    echo -e "${GREEN}Сканирование завершено успешно! Уязвимостей не обнаружено.${NC}"
    echo -e "Отчет сохранен в: ${HTML_REPORT}"
    exit 0
else
    echo -e "${RED}Сканирование выявило потенциальные уязвимости!${NC}"
    echo -e "Подробный отчет сохранен в: ${HTML_REPORT}"
    echo -e "${YELLOW}Рекомендуется просмотреть отчет и устранить найденные проблемы.${NC}"
    exit 1
fi 