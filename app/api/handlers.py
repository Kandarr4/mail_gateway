"""Централизованные обработчики ошибок — единственное место, где исключение
превращается в HTTP-ответ. Роуты не пишут ни try/except, ни ручных тел ошибок.
"""

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..logging_setup import request_id_var
from .errors import ApiError
from .responses import from_api_error, problem

logger = logging.getLogger(__name__)

#: Ответы, которые формирует не наш код (405 от роутера, 401 от схемы
#: безопасности), приводим к тому же виду, что и доменные.
_HTTP_PROBLEMS = {
    400: ("bad_request", "Некорректный запрос"),
    401: ("unauthorized", "Требуется действительный токен"),
    403: ("access_denied", "Доступ запрещён"),
    404: ("not_found", "Ресурс не найден"),
    405: ("method_not_allowed", "Метод не поддерживается"),
    409: ("conflict", "Конфликт состояния"),
    413: ("payload_too_large", "Слишком большой файл"),
    415: ("unsupported_media_type", "Неподдерживаемый тип содержимого"),
    422: ("validation_error", "Данные не прошли проверку"),
    429: ("rate_limited", "Превышена частота запросов"),
    503: ("service_unavailable", "Сервис временно недоступен"),
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        logger.info("Отклонено: %s (%s)", exc.detail, exc.code)
        return from_api_error(exc, instance=request.url.path, request_id=request_id_var.get())

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return problem(
            422,
            "validation_error",
            "Данные не прошли проверку",
            "Одно или несколько полей запроса недопустимы",
            instance=request.url.path,
            request_id=request_id_var.get(),
            errors=_fields(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        code, title = _HTTP_PROBLEMS.get(exc.status_code, ("error", "Ошибка запроса"))
        detail = exc.detail if isinstance(exc.detail, str) else title
        return problem(
            exc.status_code,
            code,
            title,
            detail,
            instance=request.url.path,
            request_id=request_id_var.get(),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Стектрейс — в журнал, клиенту — только идентификатор обращения.
        logger.exception("Необработанная ошибка: %s", exc)
        return problem(
            500,
            "internal_error",
            "Внутренняя ошибка сервиса",
            "Обратитесь к администратору, указав request_id",
            instance=request.url.path,
            request_id=request_id_var.get(),
        )

    # HTTPException, поднятый внутри зависимостей FastAPI, — тот же путь.
    app.add_exception_handler(HTTPException, _http)


def _fields(exc: RequestValidationError) -> dict[str, str]:
    """Плоская карта «поле → причина» вместо сырого списка pydantic."""
    result: dict[str, str] = {}
    for err in exc.errors():
        location = ".".join(str(p) for p in err.get("loc", ()) if p != "body") or "body"
        result[location] = err.get("msg", "недопустимое значение")
    return result
