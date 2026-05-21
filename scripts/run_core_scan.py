#!/usr/bin/env python3
"""
Скрипт для запуска статического анализа безопасности кода (SAST) только основных файлов проекта,
игнорируя зависимости и внешние библиотеки.

Использование:
    python3 scripts/run_core_scan.py [--html] [--output FILENAME]

Аргументы:
    --html: Сгенерировать отчет в формате HTML вместо текстового
    --output FILENAME: Имя файла для сохранения отчета
"""

import os
import sys
import subprocess
import argparse
import logging
from datetime import datetime
from pathlib import Path
import tempfile
import json
import re
import html

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("security_scan")

# Путь к корневой директории проекта
PROJECT_ROOT = Path(__file__).parent.parent.absolute()

def parse_arguments():
    """Парсинг аргументов командной строки"""
    parser = argparse.ArgumentParser(description="Запуск статического анализа безопасности основного кода")
    parser.add_argument("--html", action="store_true", help="Сгенерировать отчет в формате HTML")
    parser.add_argument("--output", type=str, help="Имя файла для сохранения отчета")
    return parser.parse_args()

def run_bandit_scan_for_target(target, output_format='json'):
    """
    Запускает Bandit для одной директории или файла
    
    Args:
        target (Path): Путь к директории или файлу для сканирования
        output_format (str): Формат вывода (json, txt, html)
        
    Returns:
        dict: Результаты сканирования в формате JSON или None в случае ошибки
    """
    if not target.exists():
        logger.warning(f"Цель не существует: {target}")
        return None
    
    # Если это директория, добавляем опцию -r для рекурсивного сканирования
    is_recursive = ["-r"] if target.is_dir() else []
    
    # Команда для запуска Bandit
    command = ["bandit"] + is_recursive + [str(target), "-f", output_format]
    
    try:
        logger.debug(f"Запуск сканирования для {target}: {' '.join(command)}")
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        
        # Проверяем результат
        if result.returncode in [0, 1]:  # 0 = без уязвимостей, 1 = найдены уязвимости
            if output_format == 'json':
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    logger.error(f"Не удалось декодировать JSON-вывод для {target}")
                    return None
            else:
                return result.stdout
        else:
            logger.error(f"Ошибка при сканировании {target}: {result.stderr}")
            return None
    
    except Exception as e:
        logger.error(f"Исключение при сканировании {target}: {e}")
        return None

def merge_json_results(results_list):
    """
    Объединяет результаты сканирования из нескольких запусков Bandit в формате JSON
    
    Args:
        results_list (list): Список результатов сканирования
        
    Returns:
        dict: Объединенные результаты
    """
    if not results_list:
        return None
    
    # Фильтруем None значения
    valid_results = [r for r in results_list if r is not None]
    if not valid_results:
        return None
    
    # Инициализируем объединенный результат первым элементом
    merged = valid_results[0].copy()
    
    # Счетчики для статистики
    total_issues = 0
    
    # Объединяем остальные результаты
    for result in valid_results[1:]:
        # Объединяем результаты для каждого файла
        merged["results"].extend(result.get("results", []))
        
        # Объединяем метрики
        metrics = result.get("metrics", {})
        for file_path, file_metrics in metrics.items():
            merged["metrics"][file_path] = file_metrics
    
    # Считаем общее количество проблем
    for result in merged.get("results", []):
        if result.get("issue_severity") in ["LOW", "MEDIUM", "HIGH"]:
            total_issues += 1
    
    # Обновляем сводную информацию
    merged["metrics"]["_totals"] = {
        "CONFIDENCE.HIGH": sum(r["metrics"].get("_totals", {}).get("CONFIDENCE.HIGH", 0) for r in valid_results),
        "CONFIDENCE.LOW": sum(r["metrics"].get("_totals", {}).get("CONFIDENCE.LOW", 0) for r in valid_results),
        "CONFIDENCE.MEDIUM": sum(r["metrics"].get("_totals", {}).get("CONFIDENCE.MEDIUM", 0) for r in valid_results),
        "CONFIDENCE.UNDEFINED": sum(r["metrics"].get("_totals", {}).get("CONFIDENCE.UNDEFINED", 0) for r in valid_results),
        "SEVERITY.HIGH": sum(r["metrics"].get("_totals", {}).get("SEVERITY.HIGH", 0) for r in valid_results),
        "SEVERITY.LOW": sum(r["metrics"].get("_totals", {}).get("SEVERITY.LOW", 0) for r in valid_results),
        "SEVERITY.MEDIUM": sum(r["metrics"].get("_totals", {}).get("SEVERITY.MEDIUM", 0) for r in valid_results),
        "SEVERITY.UNDEFINED": sum(r["metrics"].get("_totals", {}).get("SEVERITY.UNDEFINED", 0) for r in valid_results),
        "loc": sum(r["metrics"].get("_totals", {}).get("loc", 0) for r in valid_results),
        "nosec": sum(r["metrics"].get("_totals", {}).get("nosec", 0) for r in valid_results),
        "skipped_tests": sum(r["metrics"].get("_totals", {}).get("skipped_tests", 0) for r in valid_results),
    }
    
    # Обновляем информацию о сканировании
    merged["generated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    merged["stats"] = {
        "total_issues": total_issues,
        "total_files_analyzed": sum(len(r.get("metrics", {})) - 1 for r in valid_results),  # вычитаем _totals
        "total_lines_of_code": merged["metrics"]["_totals"]["loc"],
        "total_issues_by_severity": {
            "HIGH": merged["metrics"]["_totals"]["SEVERITY.HIGH"],
            "MEDIUM": merged["metrics"]["_totals"]["SEVERITY.MEDIUM"],
            "LOW": merged["metrics"]["_totals"]["SEVERITY.LOW"],
        }
    }
    
    return merged

def generate_txt_report(json_result):
    """
    Генерирует текстовый отчет на основе объединенных результатов JSON
    
    Args:
        json_result (dict): Результаты сканирования в формате JSON
        
    Returns:
        str: Текстовый отчет
    """
    if not json_result:
        return "Нет данных для отчета."
    
    lines = []
    lines.append(f"Run started:{datetime.now()}")
    lines.append("=" * 80)
    
    # Добавляем статистику
    stats = json_result.get("stats", {})
    lines.append(f"Количество проверенных файлов: {stats.get('total_files_analyzed', 0)}")
    lines.append(f"Общее количество строк кода: {stats.get('total_lines_of_code', 0)}")
    lines.append(f"Найдено уязвимостей: {stats.get('total_issues', 0)}")
    lines.append(f"  HIGH: {stats.get('total_issues_by_severity', {}).get('HIGH', 0)}")
    lines.append(f"  MEDIUM: {stats.get('total_issues_by_severity', {}).get('MEDIUM', 0)}")
    lines.append(f"  LOW: {stats.get('total_issues_by_severity', {}).get('LOW', 0)}")
    lines.append("=" * 80)
    
    # Добавляем найденные проблемы
    for issue in json_result.get("results", []):
        lines.append(f">> Файл: {issue.get('filename')}")
        lines.append(f"   Строка: {issue.get('line_number')}")
        lines.append(f"   Критичность: {issue.get('issue_severity')}")
        lines.append(f"   Уверенность: {issue.get('issue_confidence')}")
        lines.append(f"   Код: {issue.get('issue_text')}")
        lines.append(f"   Код: {issue.get('code')}")
        lines.append("-" * 80)
    
    lines.append(f"Run completed:{datetime.now()}")
    return "\n".join(lines)

def generate_html_report(json_result):
    """
    Генерирует HTML отчет на основе объединенных результатов JSON
    
    Args:
        json_result (dict): Результаты сканирования в формате JSON
        
    Returns:
        str: HTML отчет
    """
    if not json_result:
        return "<html><body><h1>Нет данных для отчета</h1></body></html>"
    
    # Генерируем простой HTML отчет
    html_parts = []
    html_parts.append("""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Отчет по безопасности кода</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .high { background-color: #ffdddd; border-left: 5px solid #ff0000; }
        .medium { background-color: #ffffcc; border-left: 5px solid #ffcc00; }
        .low { background-color: #e6f3ff; border-left: 5px solid #0066cc; }
        .issue { margin: 10px 0; padding: 10px; border-radius: 5px; }
        .code { font-family: monospace; white-space: pre; overflow-x: auto; background-color: #f5f5f5; padding: 10px; }
        .stats { display: flex; justify-content: space-around; margin: 20px 0; }
        .stat-box { text-align: center; padding: 10px; border: 1px solid #ddd; border-radius: 5px; flex: 1; margin: 0 5px; }
        h1, h2 { color: #333; }
        table { border-collapse: collapse; width: 100%; }
        th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
        th { background-color: #f2f2f2; }
    </style>
</head>
<body>
    <h1>Отчет по безопасности кода</h1>
    <p>Дата создания: """)
    html_parts.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    html_parts.append("</p>")
    
    # Добавляем блок статистики
    stats = json_result.get("stats", {})
    html_parts.append('<div class="stats">')
    html_parts.append(f'<div class="stat-box"><h3>Файлов проверено</h3><p>{stats.get("total_files_analyzed", 0)}</p></div>')
    html_parts.append(f'<div class="stat-box"><h3>Строк кода</h3><p>{stats.get("total_lines_of_code", 0)}</p></div>')
    html_parts.append(f'<div class="stat-box"><h3>Всего уязвимостей</h3><p>{stats.get("total_issues", 0)}</p></div>')
    html_parts.append('</div>')
    
    # Таблица с разбивкой по уровням критичности
    html_parts.append('<table><tr><th>Критичность</th><th>Количество</th></tr>')
    severity_counts = stats.get("total_issues_by_severity", {})
    html_parts.append(f'<tr><td>HIGH</td><td>{severity_counts.get("HIGH", 0)}</td></tr>')
    html_parts.append(f'<tr><td>MEDIUM</td><td>{severity_counts.get("MEDIUM", 0)}</td></tr>')
    html_parts.append(f'<tr><td>LOW</td><td>{severity_counts.get("LOW", 0)}</td></tr>')
    html_parts.append('</table>')
    
    # Если есть уязвимости, выводим их
    if json_result.get("results"):
        html_parts.append('<h2>Найденные уязвимости</h2>')
        
        # Сортируем уязвимости по критичности
        sorted_issues = sorted(
            json_result.get("results", []),
            key=lambda x: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(x.get("issue_severity"), 3)
        )
        
        for issue in sorted_issues:
            severity = issue.get("issue_severity", "").lower()
            html_parts.append(f'<div class="issue {severity}">')
            html_parts.append(f'<h3>{issue.get("test_id")}: {html.escape(issue.get("issue_text", ""))}</h3>')
            html_parts.append(f'<p><strong>Файл:</strong> {html.escape(issue.get("filename", ""))}</p>')
            html_parts.append(f'<p><strong>Строка:</strong> {issue.get("line_number", "")}</p>')
            html_parts.append(f'<p><strong>Критичность:</strong> {issue.get("issue_severity", "")}</p>')
            html_parts.append(f'<p><strong>Уверенность:</strong> {issue.get("issue_confidence", "")}</p>')
            
            # Выводим код с подсветкой проблемной строки
            code = issue.get("code", "").strip()
            if code:
                html_parts.append(f'<div class="code">{html.escape(code)}</div>')
            
            html_parts.append('</div>')
    else:
        html_parts.append('<h2>Уязвимостей не обнаружено</h2>')
    
    html_parts.append("""
</body>
</html>""")
    
    return "".join(html_parts)

def run_bandit_scan(html_format=False, output_file=None):
    """
    Запускает сканирование с помощью Bandit только основных директорий проекта
    
    Args:
        html_format (bool): Генерировать отчет в формате HTML
        output_file (str): Имя файла для сохранения отчета
    
    Returns:
        bool: True если сканирование успешно, False в случае ошибок
    """
    # Основные директории и файлы проекта для сканирования
    core_dirs = [
        "handlers",
        "services",
        "utils",
        "database",
        "middlewares"
    ]
    
    core_files = [
        "main.py",
        "scheduler.py",
        "state_utils.py",
        "config.py",
        "keyboards.py"
    ]
    
    # Конвертируем пути в Path объекты
    core_dir_paths = [PROJECT_ROOT / dir_name for dir_name in core_dirs]
    core_file_paths = [PROJECT_ROOT / file_name for file_name in core_files]
    
    # Отфильтровываем несуществующие пути
    target_dirs = [path for path in core_dir_paths if path.exists() and path.is_dir()]
    target_files = [path for path in core_file_paths if path.exists() and path.is_file()]
    
    # Запускаем сканирование для каждой директории и файла
    logger.info(f"Запуск сканирования для {len(target_dirs)} директорий и {len(target_files)} файлов")
    
    # Собираем результаты
    results = []
    
    # Сканируем директории
    for dir_path in target_dirs:
        logger.info(f"Сканирование директории: {dir_path}")
        result = run_bandit_scan_for_target(dir_path)
        if result:
            results.append(result)
    
    # Сканируем отдельные файлы
    for file_path in target_files:
        logger.info(f"Сканирование файла: {file_path}")
        result = run_bandit_scan_for_target(file_path)
        if result:
            results.append(result)
    
    # Объединяем результаты
    merged_results = merge_json_results(results)
    
    # Если нет результатов, возвращаем ошибку
    if not merged_results:
        logger.error("Не удалось получить результаты сканирования")
        return False
    
    # Генерируем отчет в нужном формате
    if html_format:
        report_content = generate_html_report(merged_results)
    else:
        report_content = generate_txt_report(merged_results)
    
    # Сохраняем отчет, если указано имя файла
    if output_file:
        # Создаем директорию для отчетов, если её нет
        reports_dir = PROJECT_ROOT / "security_reports"
        reports_dir.mkdir(exist_ok=True)
        
        # Полный путь к файлу отчета
        output_path = reports_dir / output_file
        
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(report_content)
            logger.info(f"Отчет сохранен в: {output_path}")
        except Exception as e:
            logger.error(f"Ошибка при сохранении отчета: {e}")
            return False
    else:
        # Если имя файла не указано, выводим отчет в консоль
        print(report_content)
    
    # Проверяем, есть ли уязвимости
    has_issues = merged_results.get("stats", {}).get("total_issues", 0) > 0
    if has_issues:
        logger.warning("Обнаружены потенциальные уязвимости!")
        return False
    else:
        logger.info("Сканирование завершено успешно. Уязвимостей не обнаружено.")
        return True

def main():
    """Основная функция скрипта"""
    args = parse_arguments()
    
    # Генерируем имя файла отчета, если не указано
    if args.output is None and args.html:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"core_security_report_{timestamp}.html"
    elif args.output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = f"core_security_report_{timestamp}.txt"
    
    # Запускаем сканирование
    success = run_bandit_scan(html_format=args.html, output_file=args.output)
    
    # Устанавливаем код возврата
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main()) 