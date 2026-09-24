"""Сборка приложения: фабрика `create_app()` и точка входа процесса.

Один процесс поднимает SMTP-приём, HTTP API и фоновые воркеры.
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from . import __version__
from .api.handlers import register_error_handlers
from .api.routes import api_router
from .api.routes.health import set_smtp_ready
from .bootstrap import ensure_admin_exists, ensure_default_client_from_env
from .config import settings
from .db import session_scope
from .logging_setup import request_id_var, setup_logging
from .schema_setup import prepare_database
from .services import dkim_signer, messages
from .services.dkim_signer import DkimError
from .smtp_server import start_smtp
from .web import routes as web_routes
from .workers import start_all

logger = logging.getLogger("mail_gateway")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    _check_license()
    _warn_about_insecure_defaults()
    prepare_database()
    ensure_default_client_from_env()
    _check_dkim()

    with session_scope() as db:
        messages.requeue_stuck_sending(db)

    stop = asyncio.Event()
    controller = start_smtp()
    set_smtp_ready(True)
    tasks = start_all(stop)
    logger.info("Mail Gateway %s запущен, домен %s", __version__, settings.domain)

    try:
        yield
    finally:
        logger.info("Остановка Mail Gateway...")
        stop.set()
        set_smtp_ready(False)
        controller.stop()
        # Даём воркерам завершить текущую задачу, и только потом снимаем.
        _, pending = await asyncio.wait(tasks, timeout=10)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("Mail Gateway остановлен")


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="Mail Gateway",
        version=__version__,
        description="Приём и отправка электронной почты через HTTP API",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        """Сквозной идентификатор запроса: в журнале и в теле ошибки."""
        incoming = request.headers.get("X-Request-ID")
        token = request_id_var.set(incoming or uuid.uuid4().hex[:12])
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id_var.get()
            return response
        finally:
            request_id_var.reset(token)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        """Заголовки, снижающие ущерб от XSS и кликджекинга в панели."""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/admin"):
            response.headers.setdefault(
                "Content-Security-Policy",
                # frame-src 'self' — под изолированный фрейм с телом письма;
                # само тело приходит со своей, куда более строгой политикой.
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                "frame-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
        return response

    register_error_handlers(app)
    app.include_router(api_router)
    if settings.web_enabled:
        web_routes.register(app)
        logger.info("Панель управления доступна по адресу /admin")
    return app


def _warn_about_insecure_defaults() -> None:
    for problem in settings.insecure_defaults():
        logger.warning("НЕБЕЗОПАСНАЯ НАСТРОЙКА: %s", problem)


def _check_dkim() -> None:
    """Битый ключ должен обнаружиться на старте, а не на первом письме.

    Иначе о нём узнаёшь тогда, когда очередь уже полна писем в `failed`.
    Останавливать из-за этого приём почты не за что — он от подписи не зависит.
    """
    try:
        dkim_signer.check_ready()
    except DkimError as exc:
        logger.error("DKIM настроен, но не работает: %s", exc)


def _check_license() -> None:
    """Состояние лицензии в журнал — но не отказ от запуска.

    Служба обязана подниматься всегда: без работающего сервера некуда
    установить лицензию, а оператор видит лишь остановленную службу. Отказ
    точечный — почта (см. `app/services/licensing.py`), а панель, настройки и
    просмотр переписки доступны.
    """
    from .services import licensing

    licensing.log_status()


app = create_app()


def main() -> None:
    import uvicorn

    setup_logging()
    # До старта сервера, а не в lifespan: диалог блокирует поток, и внутри
    # цикла событий он остановил бы вместе с собой и приём почты.
    prepare_database()
    ensure_default_client_from_env()
    ensure_admin_exists()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_config=None)


if __name__ == "__main__":
    main()
