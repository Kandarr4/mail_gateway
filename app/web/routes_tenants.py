"""Веб-панель: клиенты, их домены и API-ключи.

Отдельный модуль, чтобы не раздувать `routes.py`; стиль тот же — тонкие
роуты, вся логика в сервисах. Ключ показывается в открытом виде ровно один
раз, на странице результата создания: в базе остаётся только хеш, и другой
возможности увидеть значение не существует.
"""

import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from ..api.deps import get_db
from ..api.errors import ApiError, NotFound
from ..models import ApiKey, Domain, Mailbox
from ..schemas import PageParams
from ..services import api_keys, clients, domains
from . import session as web_session
from .routes import page, require_csrf, require_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Панель управления"], include_in_schema=False)

CLIENTS_URL = "/admin/clients"


def _client_or_404(db: DbSession, client_id: int):
    client = clients.by_id(db, client_id)
    if client is None:
        raise NotFound("Клиент не найден")
    return client


def _counts(db: DbSession, client_id: int) -> dict:
    def count(model) -> int:
        return db.scalar(
            select(func.count()).select_from(model).where(model.client_id == client_id)
        ) or 0

    return {"domains": count(Domain), "keys": count(ApiKey), "mailboxes": count(Mailbox)}


def _clients_page(request, session, db, *, error=None, message=None):
    result = clients.get_page(db, PageParams(limit=200, offset=0))
    rows = [(client, _counts(db, client.id)) for client in result.items]
    return page(
        request, "clients.html", session,
        clients=rows, total=result.total, error=error, message=message,
    )


def _detail_page(request, session, db, client, *, error=None, message=None):
    return page(
        request, "client_detail.html", session,
        client=client,
        domains=domains.for_client(db, client.id),
        keys=api_keys.list_for_client(db, client.id),
        counts=_counts(db, client.id),
        error=error, message=message,
    )


# --- Клиенты -------------------------------------------------------------------- #


@router.get("/clients", response_class=HTMLResponse)
def clients_list(
    request: Request,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    return _clients_page(request, session, db)


@router.post("/clients", response_class=HTMLResponse)
def create_client(
    request: Request,
    name: str = Form(...),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    try:
        client = clients.create(db, name)
    except ApiError as exc:
        return _clients_page(request, session, db, error=exc.detail)
    logger.info("%s создал клиента «%s»", session.username, client.name)
    return RedirectResponse(f"{CLIENTS_URL}/{client.id}", status_code=303)


@router.get("/clients/{client_id}", response_class=HTMLResponse)
def client_detail(
    request: Request,
    client_id: int,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    return _detail_page(request, session, db, _client_or_404(db, client_id))


@router.post("/clients/{client_id}/toggle")
def toggle_client(
    request: Request,
    client_id: int,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    client = _client_or_404(db, client_id)
    clients.set_active(db, client.id, not client.is_active)
    state = "включил" if client.is_active else "отключил"
    logger.info("%s %s клиента «%s»", session.username, state, client.name)
    return RedirectResponse(f"{CLIENTS_URL}/{client_id}", status_code=303)


@router.post("/clients/{client_id}/delete")
def delete_client(
    request: Request,
    client_id: int,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    client = _client_or_404(db, client_id)
    clients.delete(db, client)
    logger.info("%s удалил клиента «%s»", session.username, client.name)
    return RedirectResponse(CLIENTS_URL, status_code=303)


# --- Домены --------------------------------------------------------------------- #


@router.post("/clients/{client_id}/domains", response_class=HTMLResponse)
def add_domain(
    request: Request,
    client_id: int,
    name: str = Form(...),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    client = _client_or_404(db, client_id)

    from ..services import licensing

    allowed, reason = licensing.check_domain_limit(db)
    if not allowed:
        return _detail_page(request, session, db, client, error=reason)

    try:
        domain = domains.create(db, client.id, name)
    except ApiError as exc:
        return _detail_page(request, session, db, client, error=exc.detail)
    logger.info("%s добавил домен %s клиенту «%s»", session.username, domain.name, client.name)
    return RedirectResponse(f"{CLIENTS_URL}/{client_id}", status_code=303)


@router.post("/domains/{domain_id}/dkim", response_class=HTMLResponse)
def set_domain_dkim(
    request: Request,
    domain_id: int,
    private_key_file: str = Form(""),
    selector: str = Form("mail"),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    domain = domains.by_id(db, domain_id)
    if domain is None:
        raise NotFound("Домен не найден")
    client = _client_or_404(db, domain.client_id)

    if private_key_file.strip():
        from ..services.dkim_signer import DkimError, check_key_file

        try:
            check_key_file(private_key_file.strip())
        except DkimError as exc:
            return _detail_page(request, session, db, client, error=str(exc))

    domains.set_dkim(
        db, domain.id,
        private_key_file=private_key_file.strip() or None,
        selector=selector.strip() or "mail",
    )
    logger.info("%s изменил DKIM домена %s", session.username, domain.name)
    return _detail_page(request, session, db, client, message=f"DKIM домена {domain.name} сохранён")


@router.post("/domains/{domain_id}/delete")
def delete_domain(
    request: Request,
    domain_id: int,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    domain = domains.by_id(db, domain_id)
    if domain is None:
        raise NotFound("Домен не найден")
    client_id = domain.client_id
    domains.delete(db, domain)
    logger.info("%s удалил домен %s", session.username, domain.name)
    return RedirectResponse(f"{CLIENTS_URL}/{client_id}", status_code=303)


# --- API-ключи ------------------------------------------------------------------- #


@router.post("/clients/{client_id}/keys", response_class=HTMLResponse)
def create_key(
    request: Request,
    client_id: int,
    name: str = Form(""),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    client = _client_or_404(db, client_id)
    api_key, raw = api_keys.create(db, client.id, name.strip() or "Без названия")
    logger.info("%s выпустил ключ «%s» клиента «%s»", session.username, api_key.name, client.name)
    # Значение ключа существует только в этом ответе. Ни в журнал, ни в базу
    # оно не попадает.
    return page(request, "key_created.html", session, client=client, api_key=api_key, raw_token=raw)


@router.post("/keys/{key_id}/revoke")
def revoke_key(
    request: Request,
    key_id: int,
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    api_key = db.get(ApiKey, key_id)
    if api_key is None:
        raise NotFound("Ключ не найден")
    api_keys.revoke(db, api_key)
    logger.info("%s отозвал ключ «%s» (id=%s)", session.username, api_key.name, api_key.id)
    return RedirectResponse(f"{CLIENTS_URL}/{api_key.client_id}", status_code=303)


# --- Лицензия -------------------------------------------------------------------- #


def _license_page(request, session, db, *, error=None, message=None):
    from ..services import licensing

    current = licensing.status(use_cache=False)
    return page(
        request, "license.html", session,
        license=current,
        domains_used=domains.count_all(db),
        licensing_url=licensing.LICENSING_URL,
        error=error, message=message,
    )


@router.get("/license", response_class=HTMLResponse)
def license_status(
    request: Request,
    session: web_session.Session = Depends(require_session),
    db: DbSession = Depends(get_db),
):
    return _license_page(request, session, db)


@router.post("/license", response_class=HTMLResponse)
async def upload_license(
    request: Request,
    file: UploadFile = File(...),
    session: web_session.Session = Depends(require_csrf),
    db: DbSession = Depends(get_db),
):
    """Установка нового файла `.lic`. Битый файл не затирает работающий."""
    import tempfile
    from pathlib import Path as FsPath

    from ..services import licensing

    content = await file.read()
    if len(content) > 64 * 1024:  # лицензия — килобайты; мегабайты — не лицензия
        return _license_page(request, session, db, error="Файл слишком велик для лицензии")

    with tempfile.NamedTemporaryFile(suffix=".lic", delete=False) as handle:
        handle.write(content)
        temp_path = FsPath(handle.name)
    try:
        result = licensing.install_license_file(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)  # noqa: ASYNC240 — одно удаление файла на редком пути установки лицензии

    if not result.valid:
        return _license_page(request, session, db, error=result.reason)
    logger.info("%s установил лицензию: %s", session.username, result.company)
    return _license_page(request, session, db, message=f"Лицензия установлена: {result.company}")
