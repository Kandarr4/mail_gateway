"""Веб-панель управления ящиками.

Роуты тонкие: разбор формы → сервис → страница. Вся логика ящиков живёт в
`services/mailboxes.py` и переиспользуется вместе с HTTP API — второго набора
правил здесь нет.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError as SchemaError
from sqlalchemy.orm import Session as DbSession

from ..api.deps import get_db
from ..api.errors import ApiError, NotFound
from ..api.loaders import attachment_response, load_attachment_file, load_message
from ..config import settings
from ..constants import Direction, MessageStatus
from ..schemas import MailboxIn, MessageFilter, PageParams, SendRequest
from ..services import admin_users, app_settings, backup, mailboxes, messages, uploads
from ..services.rate_limit import RateLimiter
from . import session as web_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Панель управления"], include_in_schema=False)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# В собранном приложении шаблоны зашифрованы (`build.py`: .html → .enc) —
# загрузчик подменяется на расшифровывающий. В дев-режиме .enc-файлов нет
# и работает обычный поиск по каталогу.
if any(_TEMPLATES_DIR.glob("*.enc")):
    from .encrypted_loader import EncryptedTemplateLoader

    templates.env.loader = EncryptedTemplateLoader(_TEMPLATES_DIR)

_login_limiter = RateLimiter(settings.web_login_attempts_per_minute)

LOGIN_URL = "/admin/login"
HOME_URL = "/admin/"
MESSAGES_URL = "/admin/messages"

#: Писем на странице списка.
PAGE_SIZE = 50


# --- Вспомогательное ---------------------------------------------------------- #


def require_session(request: Request) -> web_session.Session:
    """Зависимость доступа. Неавторизованного разворачиваем на страницу входа."""
    current = web_session.current(request)
    if current is None:
        raise _Redirect(LOGIN_URL)
    return current


class _Redirect(Exception):
    """Не ошибка, а управление потоком: браузеру нужен редирект, а не JSON."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(url)


def page(request: Request, name: str, session: web_session.Session | None = None, **context):
    return templates.TemplateResponse(
        request,
        name,
        {
            "session": session,
            "csrf_token": session.csrf if session else "",
            "domain": settings.domain,
            **context,
        },
    )


def _verify_csrf(session: web_session.Session, token: str | None) -> None:
    if not web_session.check_csrf(session, token):
        logger.warning("Отклонён запрос без действительного токена CSRF от %s", session.username)
        raise ApiError("Форма устарела, обновите страницу и повторите")


def require_csrf(
    csrf_token: str = Form(""),
    session: web_session.Session = Depends(require_session),
) -> web_session.Session:
    """Сессия, проверенная вместе с токеном CSRF, — для изменяющих POST.

    Зависимость вместо вызова первой строкой в каждом обработчике: «где-то
    проверили, где-то забыли» не должно быть возможным по построению.
    """
    _verify_csrf(session, csrf_token)
    return session


# --- Вход и выход ------------------------------------------------------------- #


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if web_session.current(request) is not None:
        return RedirectResponse(HOME_URL, status_code=303)
    return page(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: DbSession = Depends(get_db),
):
    client = request.client.host if request.client else "unknown"
    if not _login_limiter.allow(client):
        logger.warning("Превышен лимит попыток входа с адреса %s", client)
        return page(request, "login.html", error="Слишком много попыток. Подождите минуту.")

    user = admin_users.authenticate(db, username, password)
    if user is None:
        # Одно и то же сообщение на оба случая: иначе перебором выясняются имена.
        return page(request, "login.html", error="Неверное имя пользователя или пароль")

    response = RedirectResponse(HOME_URL, status_code=303)
    web_session.create(response, user.id, user.username)
    logger.info("Вход в панель: %s с адреса %s", user.username, client)
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse(LOGIN_URL, status_code=303)
    web_session.destroy(request, response)
    return response


# --- Ящики --------------------------------------------------------------------- #


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    result = mailboxes.get_page(db, PageParams(limit=200, offset=0))
    return page(request, "mailboxes.html", session, mailboxes=result.items, total=result.total)


@router.post("/mailboxes")
def create_mailbox(
    request: Request,
    address: str = Form(...),
    display_name: str = Form(""),
    external_id: str = Form(""),
    is_active: bool = Form(False),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    payload = MailboxIn(
        display_name=display_name or None,
        external_id=external_id or None,
        is_active=is_active,
    )
    mailboxes.upsert(db, address, payload)
    logger.info("%s изменил ящик %s", session.username, address)
    return RedirectResponse(HOME_URL, status_code=303)


@router.post("/mailboxes/{address}/delete")
def delete_mailbox(
    request: Request,
    address: str,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    mailbox = mailboxes.by_address(db, address)
    if mailbox is not None:
        mailboxes.delete(db, mailbox)
        logger.info("%s удалил ящик %s", session.username, address)
    return RedirectResponse(HOME_URL, status_code=303)


# --- Почта: просмотр -------------------------------------------------------------- #


def _one_of(value: str, allowed: type[Direction] | type[MessageStatus]) -> str | None:
    """Значение фильтра из строки запроса, если оно вообще существует.

    Строку запроса правит кто угодно; неизвестное значение — это «фильтра нет»,
    а не ошибка сервера от несработавшей валидации схемы.
    """
    return value if value in set(allowed) else None


@router.get("/messages", response_class=HTMLResponse)
def message_list(
    request: Request,
    direction: str = "",
    status: str = "",
    mailbox: str = "",
    search: str = "",
    unread_only: bool = False,
    offset: int = 0,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    filters = MessageFilter(
        direction=_one_of(direction, Direction),
        status=_one_of(status, MessageStatus),
        mailbox=mailbox or None,
        search=search[:200] or None,
        unread_only=unread_only,
        limit=PAGE_SIZE,
        offset=max(offset, 0),
    )
    result = messages.get_page(db, filters)
    boxes = mailboxes.get_page(db, PageParams(limit=200, offset=0))
    return page(
        request,
        "messages.html",
        session,
        messages=result.items,
        total=result.total,
        offset=result.offset,
        page_size=PAGE_SIZE,
        mailboxes=boxes.items,
        statuses=list(MessageStatus),
        current={
            "direction": filters.direction or "",
            "status": filters.status or "",
            "mailbox": filters.mailbox or "",
            "search": filters.search or "",
            "unread_only": filters.unread_only,
        },
    )


@router.get("/messages/{message_id}", response_class=HTMLResponse)
def message_detail(
    request: Request,
    message_id: int,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    return page(request, "message.html", session, message=load_message(db, message_id))


@router.get("/messages/{message_id}/body", response_class=HTMLResponse)
def message_body(
    message_id: int,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
) -> HTMLResponse:
    """HTML-тело письма для показа в изолированном фрейме.

    Тело письма пишет отправитель, то есть кто угодно из интернета. Вставлять
    его в страницу панели нельзя ни в каком виде: один `<script>` внутри — и
    чужой код выполняется с правами администратора. Поэтому тело отдаётся
    отдельным документом, а страница подключает его через `<iframe sandbox>`:
    фрейм получает изолированное происхождение, скрипты в нём не выполняются,
    формы не отправляются, до cookie панели он не дотягивается.

    Собственная политика тут — второй рубеж на случай, если атрибут `sandbox`
    когда-нибудь потеряется при правке шаблона: внешние загрузки запрещены,
    поэтому картинка-маячок не подтвердит отправителю, что письмо прочитали.
    """
    message = load_message(db, message_id)
    return HTMLResponse(
        message.body_html or "",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                "form-action 'none'; base-uri 'none'; sandbox"
            ),
            # Иначе общий X-Frame-Options: DENY запретит показ и в своём же фрейме.
            "X-Frame-Options": "SAMEORIGIN",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/attachments/{attachment_id}")
def download_attachment(
    attachment_id: int,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
) -> Response:
    """Вложение под сессией панели: у оператора нет токена API.

    Всегда как файл на скачивание, никогда не в открытие браузером: HTML или
    SVG из письма, отданный с нашего происхождения, — это тот же XSS.
    """
    attachment, path = load_attachment_file(db, attachment_id)
    return attachment_response(path, attachment.filename, "application/octet-stream")


# --- Почта: отправка --------------------------------------------------------------- #


def _compose_page(request, session, db, *, form: dict, error: str | None = None):
    boxes = mailboxes.get_page(db, PageParams(limit=200, offset=0))
    senders = [box for box in boxes.items if box.is_active]
    return page(request, "compose.html", session, senders=senders, form=form, error=error)


@router.get("/compose", response_class=HTMLResponse)
def compose_form(
    request: Request,
    reply_to: int | None = None,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    form = {"sender": "", "recipient": "", "subject": "", "body_text": "", "in_reply_to": ""}
    if reply_to is not None:
        original = load_message(db, reply_to)
        subject = original.subject or ""
        form |= {
            # Отвечаем с того адреса, на который письмо пришло.
            "sender": original.recipient if not original.is_outgoing else original.sender,
            "recipient": original.sender if not original.is_outgoing else original.recipient,
            "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}".strip(),
            "in_reply_to": original.message_id or "",
            "body_text": _quote(original),
        }
    return _compose_page(request, session, db, form=form)


def _quote(message) -> str:
    """Цитата исходного письма под курсором ответа."""
    body = message.body_text or ""
    if not body:
        return ""
    quoted = "\n".join(f"> {line}" for line in body.splitlines())
    return f"\n\n{message.sender} писал:\n{quoted}\n"


@router.post("/compose", response_class=HTMLResponse)
async def send_from_panel(
    request: Request,
    sender: str = Form(...),
    recipient: str = Form(...),
    subject: str = Form(""),
    body_text: str = Form(""),
    in_reply_to: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    form = {
        "sender": sender, "recipient": recipient, "subject": subject,
        "body_text": body_text, "in_reply_to": in_reply_to,
    }

    # Пустой input type=file всё равно приезжает — но без имени файла.
    attached = [item for item in files if item.filename]

    sender_mailbox = mailboxes.by_address(db, sender.strip())
    if sender_mailbox is None:
        return _compose_page(request, session, db, form=form, error="Отправитель не зарегистрирован")
    client_id = sender_mailbox.client_id

    try:
        upload_ids = [(await uploads.create(db, client_id, item)).id for item in attached]
        payload = SendRequest(
            sender=sender.strip(),
            recipient=recipient.strip(),
            subject=subject,
            body_text=body_text,
            in_reply_to=in_reply_to or None,
            attachment_ids=upload_ids,
        )
        message = messages.queue_outgoing(db, client_id, payload)
    except ApiError as exc:
        return _compose_page(request, session, db, form=form, error=exc.detail)
    except SchemaError as exc:
        return _compose_page(request, session, db, form=form, error=_first_error(exc))

    logger.info("%s отправил письмо id=%s на %s", session.username, message.id, message.recipient)
    return RedirectResponse(f"{MESSAGES_URL}/{message.id}", status_code=303)


def _first_error(exc: SchemaError) -> str:
    """Первая претензия схемы человеческим языком, без дампа pydantic в вёрстку."""
    error = exc.errors()[0]
    field = str(error["loc"][0]) if error["loc"] else ""
    names = {"sender": "отправителя", "recipient": "получателя"}
    if error["type"].startswith("value_error") and field in names:
        return f"Неверный адрес {names[field]}"
    return f"Поле «{field}»: {error['msg']}" if field else error["msg"]


# --- Резервные копии --------------------------------------------------------------- #

BACKUP_URL = "/admin/backup"


def _backup_page(request, session, db, *, error=None, message=None):
    limit_mb = app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB)
    return page(
        request,
        "backup.html",
        session,
        error=error,
        message=message,
        definitions=app_settings.DEFINITIONS,
        values=app_settings.all_values(db),
        snapshots=backup.existing(),
        used=backup.total_size(),
        limit=int(limit_mb) * 1024 * 1024,
        free=backup.free_space(),
        directory=settings.backup_path,
    )


@router.get("/backup", response_class=HTMLResponse)
def backup_form(
    request: Request,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    return _backup_page(request, session, db)


@router.post("/backup/settings", response_class=HTMLResponse)
async def save_backup_settings(
    request: Request,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    # Форму читаем целиком: снятый флажок в теле не приходит вовсе, и по
    # именованным параметрам его было бы не отличить от «поле не менялось».
    form = dict((await request.form()).multi_items())

    try:
        app_settings.apply_form(db, form)
    except ApiError as exc:
        return _backup_page(request, session, db, error=exc.detail)

    logger.info("%s изменил настройки резервного копирования", session.username)
    return _backup_page(request, session, db, message="Настройки сохранены")


@router.post("/backup/run", response_class=HTMLResponse)
def run_backup_now(
    request: Request,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    try:
        snapshot = backup.create()
        limit_mb = app_settings.get(db, app_settings.BACKUP_MAX_TOTAL_MB)
        backup.prune(int(limit_mb) * 1024 * 1024)
    except backup.BackupError as exc:
        return _backup_page(request, session, db, error=str(exc))

    logger.info("%s создал снимок базы %s", session.username, snapshot.name)
    return _backup_page(request, session, db, message=f"Снимок создан: {snapshot.name}")


@router.get("/backup/{name}")
def download_backup(
    name: str,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
) -> FileResponse:
    """Отдаёт снимок на скачивание.

    Имя сверяется со списком существующих снимков, а не подставляется в путь:
    иначе `..%2F..%2F.env` вынес бы наружу любой файл с диска.
    """
    for snapshot in backup.existing():
        if snapshot.name == name:
            return FileResponse(
                snapshot.path,
                filename=snapshot.name,
                media_type="application/octet-stream",
                content_disposition_type="attachment",
            )
    raise NotFound("Снимок не найден")


@router.post("/backup/{name}/delete")
def delete_backup(
    name: str,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    for snapshot in backup.existing():
        if snapshot.name == name:
            snapshot.path.unlink(missing_ok=True)
            logger.info("%s удалил снимок %s", session.username, name)
            break
    return RedirectResponse(BACKUP_URL, status_code=303)


# --- Свой пароль ---------------------------------------------------------------- #


@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request, session: web_session.Session = Depends(require_session)):
    return page(request, "password.html", session)


@router.post("/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    repeat_password: str = Form(...),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):

    if admin_users.authenticate(db, session.username, current_password) is None:
        return page(request, "password.html", session, error="Текущий пароль неверен")
    if new_password != repeat_password:
        return page(request, "password.html", session, error="Пароли не совпадают")

    try:
        admin_users.set_password(db, session.username, new_password)
    except ApiError as exc:
        return page(request, "password.html", session, error=exc.detail)

    return page(request, "password.html", session, message="Пароль изменён")


def register(app) -> None:
    """Подключает панель и обработчик редиректов неавторизованных."""
    # Импорт здесь, а не наверху: routes_tenants сам импортирует этот модуль
    # (page, require_csrf), и импорт на уровне модуля был бы циклическим.
    from . import routes_tenants

    @app.exception_handler(_Redirect)
    async def _handle_redirect(_request: Request, exc: _Redirect) -> Response:
        return RedirectResponse(exc.url, status_code=303)

    app.include_router(router)
    app.include_router(routes_tenants.router)
