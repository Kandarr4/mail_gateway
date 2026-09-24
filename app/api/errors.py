"""Иерархия доменных ошибок.

Бизнес-слой их *бросает* и ничего не знает про HTTP; перевод в статус и тело
ответа делает единственный обработчик в `handlers.py`.

Поля класса соответствуют RFC 9457 (Problem Details for HTTP APIs):
`title` — постоянное название проблемы, одинаковое для всех её проявлений;
`detail` — пояснение конкретного случая.
"""

from typing import Any

#: Пространство имён для поля `type`. Значение — идентификатор вида проблемы,
#: а не адрес: клиент сравнивает строку, ходить по ней не обязан.
TYPE_PREFIX = "/errors/"


class ApiError(Exception):
    http_status: int = 400
    code: str = "error"
    title: str = "Некорректный запрос"

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.title
        #: Дополнительные члены проблемы — уходят в ответ верхним уровнем,
        #: как предписывает RFC 9457 для расширений.
        self.extra = extra
        super().__init__(self.detail)

    @property
    def type_uri(self) -> str:
        return TYPE_PREFIX + self.code.replace("_", "-")


class BadRequest(ApiError):
    http_status = 400
    code = "bad_request"
    title = "Некорректный запрос"


class Unauthorized(ApiError):
    http_status = 401
    code = "unauthorized"
    title = "Требуется действительный токен"


class AccessDenied(ApiError):
    http_status = 403
    code = "access_denied"
    title = "Доступ запрещён"


class NotFound(ApiError):
    http_status = 404
    code = "not_found"
    title = "Ресурс не найден"


class Conflict(ApiError):
    http_status = 409
    code = "conflict"
    title = "Конфликт состояния"


class PayloadTooLarge(ApiError):
    http_status = 413
    code = "payload_too_large"
    title = "Слишком большой файл"


class ValidationError(ApiError):
    http_status = 422
    code = "validation_error"
    title = "Данные не прошли проверку"


class RateLimited(ApiError):
    http_status = 429
    code = "rate_limited"
    title = "Превышена частота запросов"


class ServiceUnavailable(ApiError):
    http_status = 503
    code = "service_unavailable"
    title = "Сервис временно недоступен"
