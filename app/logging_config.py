"""
Конфигурация логирования через loguru.

Использование:
    from logging_config import logger

    logger.info("Сообщение")
    logger.info("С данными", user_id=123, action="login")
    logger.error("Ошибка", exc_info=True)
"""
import sys
import logging
from pathlib import Path
from loguru import logger


def setup_logging(
    log_level: str = "INFO",
    json_logs: bool = False,
    logs_dir: Path | None = None,
) -> None:
    """
    Настройка loguru для приложения.

    Args:
        log_level: Уровень логирования (DEBUG, INFO, WARNING, ERROR)
        json_logs: Использовать JSON-формат (для Docker/ELK)
        logs_dir: Директория для файловых логов (по умолчанию ./logs)
    """
    logger.remove()

    # diagnose=False — не печатать локальные переменные в traceback (утечка секретов)
    # backtrace=False — не раскручивать стек за пределы точки перехвата исключения
    safe_opts = dict(diagnose=False, backtrace=False)

    if json_logs:
        logger.add(
            sys.stderr,
            format="{message}",
            serialize=True,
            level=log_level,
            **safe_opts,
        )
    else:
        logger.add(
            sys.stderr,
            format=(
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            ),
            level=log_level,
            colorize=True,
            **safe_opts,
        )

    if logs_dir is None:
        logs_dir = Path(__file__).parent / "logs"

    logs_dir.mkdir(exist_ok=True)

    logger.add(
        logs_dir / "app.log",
        rotation="10 MB",
        retention="7 days",
        compression="gz",
        level=log_level,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}",
        **safe_opts,
    )

    _intercept_standard_logging()

    logger.info("Логирование настроено", level=log_level, json_logs=json_logs)


def _intercept_standard_logging() -> None:
    """Перехват стандартного logging и перенаправление в loguru."""

    class InterceptHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                level = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno

            frame, depth = sys._getframe(6), 6
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1

            logger.opt(depth=depth, exception=record.exc_info).log(
                level, record.getMessage()
            )

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)

    for name in ["uvicorn", "uvicorn.access", "uvicorn.error", "sqlalchemy.engine"]:
        logging.getLogger(name).handlers = [InterceptHandler()]
        logging.getLogger(name).propagate = False

    # httpx/httpcore логируют HTTP-запросы на INFO, включая URL с signature/токенами.
    # Поднимаем их порог до WARNING, чтобы секреты не попадали в лог.
    for name in ["httpx", "httpcore"]:
        _logger = logging.getLogger(name)
        _logger.handlers = [InterceptHandler()]
        _logger.setLevel(logging.WARNING)
        _logger.propagate = False


__all__ = ["logger", "setup_logging"]
