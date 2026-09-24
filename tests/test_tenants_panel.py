"""Панель клиентов: домены, ключи, страница лицензии.

Проверяется цепочка, которой пойдёт оператор при продаже: создать клиента →
добавить домен → выпустить ключ → ключ работает в API и виден только своему
клиенту. Значение ключа при этом нигде, кроме страницы выпуска, не всплывает.
"""

import pytest

from app.models import AdminUser
from app.schemas import PageParams
from app.services import api_keys, clients, domains
from app.services.passwords import hash_password
from app.web import session as web_session
from app.web.routes import _login_limiter

USERNAME = "operator"
PASSWORD = "Pa55word-Test"


@pytest.fixture(autouse=True)
def _clean_sessions(client):
    web_session.clear_all()
    _login_limiter.reset()
    client.cookies.clear()
    yield
    web_session.clear_all()
    _login_limiter.reset()
    client.cookies.clear()


@pytest.fixture
def logged_in(client, db):
    db.add(AdminUser(username=USERNAME, password_hash=hash_password(PASSWORD)))
    db.commit()
    response = client.post("/admin/login", data={"username": USERNAME, "password": PASSWORD},
                           follow_redirects=False)
    assert response.status_code == 303
    return client


def csrf_of(client) -> str:
    html = client.get("/admin/clients").text
    marker = 'name="csrf_token" value="'
    start = html.index(marker) + len(marker)
    return html[start:html.index('"', start)]


def test_clients_page_requires_login(client):
    response = client.get("/admin/clients", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_full_flow_client_domain_key(logged_in, db):
    """Создание клиента, домена и ключа через формы панели."""
    csrf = csrf_of(logged_in)

    created = logged_in.post("/admin/clients", data={"name": "ТОО Панель", "csrf_token": csrf},
                             follow_redirects=False)
    assert created.status_code == 303
    row = next(c for c in clients.get_page(db, PageParams()).items if c.name == "ТОО Панель")

    added = logged_in.post(f"/admin/clients/{row.id}/domains",
                           data={"name": "panel.example", "csrf_token": csrf},
                           follow_redirects=False)
    assert added.status_code == 303
    assert domains.by_name(db, "panel.example").client_id == row.id

    issued = logged_in.post(f"/admin/clients/{row.id}/keys",
                            data={"name": "CRM", "csrf_token": csrf})
    assert issued.status_code == 200
    assert "Скопируйте значение сейчас" in issued.text

    # Выпущенный ключ действительно работает в API — и видит только своё.
    import re

    raw = re.search(r'id="token"[^>]*>([^<]+)</code>', issued.text).group(1)
    page = logged_in.get("/api/v1/mailboxes", headers={"Authorization": f"Bearer {raw}"})
    assert page.status_code == 200
    assert page.json()["total"] == 0


def test_duplicate_domain_shows_error_in_page(logged_in, db):
    csrf = csrf_of(logged_in)
    row = clients.create(db, "Первый")
    domains.create(db, row.id, "taken.example")
    other = clients.create(db, "Второй")
    db.commit()

    response = logged_in.post(f"/admin/clients/{other.id}/domains",
                              data={"name": "taken.example", "csrf_token": csrf})
    assert response.status_code == 200
    assert "уже зарегистрирован" in response.text


def test_revoke_key_from_panel(logged_in, db):
    csrf = csrf_of(logged_in)
    row = clients.create(db, "Отзыв")
    key, raw = api_keys.create(db, row.id, "устаревший")
    db.commit()

    response = logged_in.post(f"/admin/keys/{key.id}/revoke", data={"csrf_token": csrf},
                              follow_redirects=False)
    assert response.status_code == 303

    rejected = logged_in.get("/api/v1/mailboxes", headers={"Authorization": f"Bearer {raw}"})
    assert rejected.status_code == 401


def test_raw_key_absent_from_client_page(logged_in, db):
    """После выпуска значение ключа нигде не показывается повторно."""
    row = clients.create(db, "Секретность")
    _, raw = api_keys.create(db, row.id, "ключ")
    db.commit()

    page = logged_in.get(f"/admin/clients/{row.id}")
    assert raw not in page.text
    assert raw[:8] in page.text  # префикс для узнавания — показывается


def test_license_page_renders_without_license(logged_in, monkeypatch, tmp_path):
    """Страница обязана открываться и без файла: панель остаётся рабочей, и
    именно через неё лицензию ставят."""
    from app.services import licensing

    monkeypatch.setattr(licensing, "LICENSE_FILE", tmp_path / "нет.lic")
    licensing.reset_cache()
    try:
        response = logged_in.get("/admin/license")
    finally:
        licensing.reset_cache()

    assert response.status_code == 200
    assert "Приём и отправка почты приостановлены" in response.text
