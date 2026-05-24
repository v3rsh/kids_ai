"""
HTTP-эндпоинты приложения kids_ai.

Health checks и webhook-обработчики BotX API.
"""
from http import HTTPStatus

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from pybotx import build_command_accepted_response
from pybotx.bot.exceptions import BotXMethodCallbackNotFoundError


# ===== Health checks =====

async def root(request: Request) -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok", "service": "kids_ai"})


async def healthz(request: Request) -> JSONResponse:
    """
    Readiness health check.

    Проверяет доступность PostgreSQL и Redis.
    Возвращает 503 при недоступности любой из зависимостей.
    """
    checks: dict = {"status": "healthy"}

    try:
        from database.db import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"
        checks["status"] = "unhealthy"

    try:
        from fsm.storage import get_fsm_storage

        storage = get_fsm_storage()
        checks["redis"] = "ok" if await storage.ping() else "error: ping failed"
        if checks["redis"] != "ok":
            checks["status"] = "unhealthy"
    except Exception as e:
        checks["redis"] = f"error: {e}"
        checks["status"] = "unhealthy"

    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(checks, status_code=status_code)


async def livez(request: Request) -> JSONResponse:
    """Liveness check (легковесный, без обращения к зависимостям)."""
    return JSONResponse({"status": "alive"})


# ===== BotX webhook handlers =====

async def command_handler(request: Request) -> JSONResponse:
    """
    Обработчик входящих команд от BotX API.
    CTS отправляет сюда все сообщения пользователей.
    Webhook URL в админке: http://<ip>:8000 (без /command).
    """
    bot = request.app.state.bot

    if bot is None:
        return JSONResponse(
            {"error": "Bot not initialized"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    try:
        bot.async_execute_raw_bot_command(
            await request.json(),
            request_headers=request.headers,
        )
    except Exception:
        logger.exception("Ошибка обработки команды")
        return JSONResponse(
            {"error": "Internal error"},
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JSONResponse(
        build_command_accepted_response(),
        status_code=HTTPStatus.ACCEPTED,
    )


async def status_handler(request: Request) -> JSONResponse:
    """
    Статус бота и список доступных команд.
    CTS запрашивает этот эндпоинт для отображения меню команд.
    """
    bot = request.app.state.bot

    if bot is None:
        return JSONResponse(
            {"error": "Bot not initialized"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    status = await bot.raw_get_status(
        dict(request.query_params),
        request_headers=request.headers,
    )
    return JSONResponse(status)


async def callback_handler(request: Request) -> JSONResponse:
    """Обработчик коллбэков от BotX API."""
    bot = request.app.state.bot

    if bot is None:
        return JSONResponse(
            {"error": "Bot not initialized"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    try:
        await bot.set_raw_botx_method_result(
            await request.json(),
            request_headers=request.headers,
        )
    except BotXMethodCallbackNotFoundError:
        logger.debug("Получен callback для неизвестного или просроченного sync_id")
        return JSONResponse(
            build_command_accepted_response(),
            status_code=HTTPStatus.ACCEPTED,
        )
    except Exception:
        logger.exception("Ошибка обработки коллбэка")
        return JSONResponse(
            {"error": "Internal error"},
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        )

    return JSONResponse(
        build_command_accepted_response(),
        status_code=HTTPStatus.ACCEPTED,
    )


# ===== Маршруты =====

routes = [
    Route("/", root, methods=["GET"]),
    Route("/healthz", healthz, methods=["GET"]),
    Route("/livez", livez, methods=["GET"]),
    Route("/command", command_handler, methods=["POST"]),
    Route("/status", status_handler, methods=["GET"]),
    Route("/notification/callback", callback_handler, methods=["POST"]),
]
