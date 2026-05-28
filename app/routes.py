"""
HTTP-эндпоинты приложения kids_ai.

Health checks и webhook-обработчики BotX API.
"""
from http import HTTPStatus
from typing import Any

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from pybotx import build_command_accepted_response
from pybotx.bot.exceptions import (
    BotXMethodCallbackNotFoundError,
    UnknownBotAccountError,
    UnverifiedRequestError,
)

from config import BOT_ID


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


# ===== Helpers =====

# Тело «выключенного» статуса для чужого/старого bot_id.
# Структура совпадает с `pybotx.build_bot_status_response`: CTS получает
# валидный JSON и рисует бота без команд, без «красной» ошибки.
_DISABLED_STATUS_RESPONSE: dict = {
    "status": "ok",
    "result": {
        "enabled": False,
        "status_message": "Bot is not configured for this CTS",
        "commands": [],
    },
}


def _is_known_bot(bot_id: Any) -> bool:
    """True, если ``bot_id`` соответствует ``config.BOT_ID``.

    Сравнение через ``str(...).lower()`` — устойчиво к ``UUID``-объектам,
    регистру и пробелам. ``None``/пустая строка → False.

    Используется в webhook-хендлерах как дешёвая проверка до вызова
    pybotx: чужие/устаревшие регистрации (старый бот на том же URL)
    отсекаются мягким ответом без 500 ASGI.
    """
    if not bot_id or not BOT_ID:
        return False
    return str(bot_id).strip().lower() == BOT_ID.strip().lower()


def _log_unknown_bot(path: str, bot_id: Any, **extra: Any) -> None:
    """Один WARNING на чужой/старый bot_id (без traceback)."""
    logger.warning(
        "Webhook от неизвестного/старого bot_id",
        path=path,
        bot_id=str(bot_id) if bot_id else None,
        **extra,
    )


# ===== BotX webhook handlers =====

async def command_handler(request: Request) -> JSONResponse:
    """
    Обработчик входящих команд от BotX API.
    CTS отправляет сюда все сообщения пользователей.
    Webhook URL в админке: http://<ip>:8000 (без /command).

    Если в payload приходит чужой ``bot_id`` (старая/чужая регистрация
    в админке CTS, указывающая на наш URL) — молча отдаём 202 + WARNING
    без выполнения команды. Иначе ``pybotx`` бросает
    ``UnknownBotAccountError`` и uvicorn пишет 500 + traceback.
    """
    bot = request.app.state.bot

    if bot is None:
        return JSONResponse(
            {"error": "Bot not initialized"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    try:
        payload = await request.json()
    except Exception:
        logger.exception("Ошибка парсинга JSON в /command")
        return JSONResponse(
            {"error": "Invalid JSON"},
            status_code=HTTPStatus.BAD_REQUEST,
        )

    incoming_bot_id = payload.get("bot_id") if isinstance(payload, dict) else None
    if not _is_known_bot(incoming_bot_id):
        _log_unknown_bot("/command", incoming_bot_id)
        return JSONResponse(
            build_command_accepted_response(),
            status_code=HTTPStatus.ACCEPTED,
        )

    try:
        bot.async_execute_raw_bot_command(
            payload,
            request_headers=request.headers,
        )
    except (UnknownBotAccountError, UnverifiedRequestError) as exc:
        _log_unknown_bot("/command", incoming_bot_id, reason=repr(exc))
        return JSONResponse(
            build_command_accepted_response(),
            status_code=HTTPStatus.ACCEPTED,
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

    Если ``bot_id`` в query-параметрах не наш — отдаём 200 с
    «выключенным» статусом (CTS просто не покажет команд) + WARNING.
    Это лечит регулярные 500 ASGI от ``UnverifiedRequestError`` для
    старого бота, который раньше работал на этом же webhook URL.
    """
    bot = request.app.state.bot

    if bot is None:
        return JSONResponse(
            {"error": "Bot not initialized"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    query_params = dict(request.query_params)
    incoming_bot_id = query_params.get("bot_id")

    if not _is_known_bot(incoming_bot_id):
        _log_unknown_bot(
            "/status",
            incoming_bot_id,
            user_huid=query_params.get("user_huid"),
            ad_login=query_params.get("ad_login"),
        )
        return JSONResponse(_DISABLED_STATUS_RESPONSE)

    try:
        status = await bot.raw_get_status(
            query_params,
            request_headers=request.headers,
        )
    except (UnverifiedRequestError, UnknownBotAccountError) as exc:
        _log_unknown_bot("/status", incoming_bot_id, reason=repr(exc))
        return JSONResponse(_DISABLED_STATUS_RESPONSE)

    return JSONResponse(status)


async def callback_handler(request: Request) -> JSONResponse:
    """Обработчик коллбэков от BotX API.

    Чужой ``bot_id`` (старая регистрация в админке CTS) → 202 + WARNING,
    тело callback'а игнорируется.
    """
    bot = request.app.state.bot

    if bot is None:
        return JSONResponse(
            {"error": "Bot not initialized"},
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        )

    try:
        payload = await request.json()
    except Exception:
        logger.exception("Ошибка парсинга JSON в /notification/callback")
        return JSONResponse(
            {"error": "Invalid JSON"},
            status_code=HTTPStatus.BAD_REQUEST,
        )

    incoming_bot_id = payload.get("bot_id") if isinstance(payload, dict) else None
    if incoming_bot_id and not _is_known_bot(incoming_bot_id):
        _log_unknown_bot("/notification/callback", incoming_bot_id)
        return JSONResponse(
            build_command_accepted_response(),
            status_code=HTTPStatus.ACCEPTED,
        )

    try:
        await bot.set_raw_botx_method_result(
            payload,
            request_headers=request.headers,
        )
    except BotXMethodCallbackNotFoundError:
        logger.debug("Получен callback для неизвестного или просроченного sync_id")
        return JSONResponse(
            build_command_accepted_response(),
            status_code=HTTPStatus.ACCEPTED,
        )
    except (UnknownBotAccountError, UnverifiedRequestError) as exc:
        _log_unknown_bot(
            "/notification/callback", incoming_bot_id, reason=repr(exc)
        )
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
