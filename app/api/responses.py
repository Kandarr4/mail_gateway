"""Единая форма ответа об ошибке по RFC 9457 — собирается только здесь.

Успешные ответы — это представления ресурсов, описанные схемами в
`app/schemas.py`; конверта над ними нет, признак успеха несёт код состояния.

Ошибка любого происхождения выглядит одинаково:

    Content-Type: application/problem+json
    {"type": "/errors/not-found", "title": "Ресурс не найден", "status": 404,
     "detail": "Письмо не найдено", "instance": "/api/v1/messages/999",
     "request_id": "3a1f60d3b343"}
"""

from typing import Any

from fastapi.responses import JSONResponse

from .errors import TYPE_PREFIX, ApiError

#: Медиатип из RFC 9457. Клиент по нему отличает описание проблемы от данных.
PROBLEM_JSON = "application/problem+json"


def problem(
    status: int,
    code: str,
    title: str,
    detail: str,
    *,
    instance: str | None = None,
    request_id: str | None = None,
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": TYPE_PREFIX + code.replace("_", "-"),
        "title": title,
        "status": status,
        "detail": detail,
    }
    if instance:
        body["instance"] = instance
    if request_id:
        body["request_id"] = request_id
    # Расширения RFC 9457 живут верхним уровнем, а не вложенным объектом.
    body.update({key: value for key, value in extra.items() if value is not None})

    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_JSON, headers=headers)


def from_api_error(exc: ApiError, *, instance: str | None = None, request_id: str | None = None) -> JSONResponse:
    return problem(
        exc.http_status,
        exc.code,
        exc.title,
        exc.detail,
        instance=instance,
        request_id=request_id,
        **exc.extra,
    )
