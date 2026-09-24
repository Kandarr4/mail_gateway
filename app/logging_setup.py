"""Журналирование: один вызов настраивает stdout и файл с ротацией (N-08).

Формат переключается на JSON через `MG_LOG_JSON=true` — для сборщиков логов.
Каждая запись несёт `request_id`, что позволяет связать строки одного запроса.
"""

import json
import logging
import sys
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler

from .config import settings

#: Идентификатор текущего запроса/задачи; проставляется middleware и воркерами.
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_TEXT_FORMAT = "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"

#: Поля LogRecord, которые не являются пользовательскими extra-полями.
_STANDARD_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
    "request_id",
}


#: Пути, которые трей опрашивает раз в несколько секунд: в журнале это сотни
#: строк в час при полном отсутствии событий.
_QUIET_ACCESS_PATHS = frozenset({"/api/v1/ready", "/api/v1/health"})


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class AccessNoiseFilter(logging.Filter):
    """Прячет из `uvicorn.access` удачные опросы состояния.

    Формат uvicorn — `'%s - "%s %s HTTP/%s" %d'`, то есть путь и код лежат в
    `record.args`. Ответы 4xx/5xx пропускаем: если опрос вдруг начнёт падать,
    это должно быть видно.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(status, int) or status >= 400:
            return True
        return str(path).split("?")[0] not in _QUIET_ACCESS_PATHS


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD_FIELDS}
        if extras:
            payload["extra"] = {k: str(v) for k, v in extras.items()}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    """Идемпотентная настройка корневого логгера."""
    root = logging.getLogger()
    if any(getattr(h, "_mail_gateway", False) for h in root.handlers):
        return

    formatter: logging.Formatter = JsonFormatter() if settings.log_json else logging.Formatter(_TEXT_FORMAT)
    id_filter = RequestIdFilter()

    # Консоль Windows по умолчанию в cp866: без этого кириллица в журнале — крокозябры.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        settings.log_path.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            settings.log_path / "mail_gateway.log",
            when="midnight",
            backupCount=settings.log_retention_days,
            encoding="utf-8",
            delay=True,
        )
        handlers.append(file_handler)
    except OSError as exc:  # каталог недоступен — не роняем сервис из-за логов
        print(f"Не удалось открыть файл журнала: {exc}", file=sys.stderr)

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(id_filter)
        handler._mail_gateway = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    root.setLevel(settings.log_level)
    # uvicorn заводит собственные обработчики — снимаем их, чтобы не двоилось.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    # Трей опрашивает /ready каждые 3 секунды — без фильтра журнал состоит
    # только из этих строк, и настоящие события в нём не найти.
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessNoiseFilter) for f in access_logger.filters):
        access_logger.addFilter(AccessNoiseFilter())
    # aiosmtpd пишет в логгер `mail.log` покомандный протокол SMTP — на INFO это
    # десятки строк на каждое письмо и адреса в открытом виде.
    for noisy in ("aiosmtpd", "mail.log", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
