#!/usr/bin/env python3
"""
Скрипт для запуска статического анализа безопасности кода (SAST) с использованием Bandit.

Использование:
    python3 scripts/run_security_scan.py [--html] [--output FILENAME] [--config CONFIG_FILE]

Аргументы:
    --html: Сгенерировать отчет в формате HTML вместо текстового
    --output FILENAME: Имя файла для сохранения отчета
    --config CONFIG_FILE: Путь к файлу конфигурации Bandit
"""

import os
import sys
import subprocess
import argparse
import logging
from datetime import datetime
from pathlib import Path

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("security_scan")

# Путь к корневой директории проекта
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

def parse_arguments():
    """Парсинг аргументов командной строки"""
    parser = argparse.ArgumentParser(description="Запуск статического анализа безопасности кода")
    parser.add_argument("--html", action="store_true", help="Сгенерировать отчет в формате HTML")
    parser.add_argument("--output", type=str, help="Имя файла для сохранения отчета")
    parser.add_argument("--config", type=str, help="Путь к файлу конфигурации Bandit")
    return parser.parse_args()

def run_bandit_scan(html_format=False, output_file=None, config_file=None):
    """
    Запускает сканирование с помощью Bandit
    
    Args:
        html_format (bool): Генерировать отчет в формате HTML
        output_file (str): Имя файла для сохранения отчета
        config_file (str): Путь к файлу конфигурации Bandit
    
    Returns:
        bool: True если сканирование успешно, False в случае ошибок
    """
    # Базовая команда для запуска Bandit
    command = ["bandit", "-r", str(PROJECT_ROOT)]
    
    # Используем переданный файл конфигурации, если указан
    if config_file and Path(config_file).exists():
        command.extend(["-c", config_file])
        logger.info(f"Используется указанный конфигурационный файл: {config_file}")
    else:
        # Ищем конфигурационный файл .bandit.yaml
        yaml_config_file = PROJECT_ROOT / ".bandit.yaml"
        if yaml_config_file.exists():
            command.extend(["-c", str(yaml_config_file)])
            logger.info(f"Используется конфигурационный файл: {yaml_config_file}")
        else:
            # Ищем альтернативный конфигурационный файл .bandit
            alt_config_file = PROJECT_ROOT / ".bandit"
            if alt_config_file.exists():
                command.extend(["-c", str(alt_config_file)])
                logger.info(f"Используется альтернативный конфигурационный файл: {alt_config_file}")
            else:
                # Если конфигурационный файл не найден, используем параметры командной строки
                logger.warning(f"Конфигурационный файл не найден")
                logger.warning("Используются параметры командной строки для исключения директорий")
                command.extend(["-x", "env,venv,.venv,.git,test_logs,__pycache__,logs,security_reports"])
    
    # Установка формата вывода
    if html_format:
        command.extend(["-f", "html"])
    else:
        command.extend(["-f", "txt"])
    
    # Настройка вывода
    if output_file:
        # Создаем директорию для отчетов, если её нет
        reports_dir = PROJECT_ROOT / "security_reports"
        reports_dir.mkdir(exist_ok=True)
        
        # Полный путь к файлу отчета
        output_path = reports_dir / output_file
        command.extend(["-o", str(output_path)])
    
    # Запуск команды
    try:
        logger.info(f"Запуск сканирования: {' '.join(command)}")
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        
        # Выводим результаты
        if result.returncode == 0:
            logger.info("Сканирование завершено успешно. Уязвимостей не обнаружено.")
            if output_file:
                logger.info(f"Отчет сохранен в: {output_path}")
            return True
        else:
            # Если код возврата равен 1, это означает, что были найдены уязвимости
            if result.returncode == 1:
                logger.warning("Обнаружены потенциальные уязвимости!")
                if output_file:
                    logger.info(f"Подробный отчет сохранен в: {output_path}")
                else:
                    # Выводим результат, если он не сохранен в файл
                    print(result.stdout)
                return False
            else:
                # Любой другой код возврата означает ошибку выполнения
                logger.error(f"Ошибка при выполнении сканирования: {result.stderr}")
                logger.error(f"Вывод команды: {result.stdout}")
                return False
    
    except Exception as e:
        logger.error(f"Ошибка при запуске сканирования: {e}")
        return False

def main():
    """Основная функция скрипта"""
    args = parse_arguments()
    
    # Генерируем имя файла отчета, если не указано
    if args.output is None and args.html:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"security_report_{timestamp}.html"
    elif args.output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"security_report_{timestamp}.txt"
    
    # Запускаем сканирование
    success = run_bandit_scan(
        html_format=args.html, 
        output_file=args.output,
        config_file=args.config
    )
    
    # Устанавливаем код возврата
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main()) 