"""Характеристические тесты HTTP-контракта: коды состояния, доступ, форма ошибки."""

import pytest

from tests.conftest import AUTH

PROTECTED = [
    ("get", "/api/v1/messages"),
    ("get", "/api/v1/messages/1"),
    ("patch", "/api/v1/messages/1"),
    ("get", "/api/v1/mailboxes"),
    ("get", "/api/v1/attachments/1"),
    ("get", "/api/v1/health"),
]


@pytest.mark.parametrize(("method", "url"), PROTECTED)
def test_requires_token(client, method, url):
    assert getattr(client, method)(url).status_code == 401


@pytest.mark.parametrize(("method", "url"), PROTECTED)
def test_rejects_wrong_token(client, method, url):
    response = getattr(client, method)(url, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_non_ascii_token_is_rejected_not_crashed(client):
    """`compare_digest` не принимает строки с не-ASCII — это давало 500 вместо 401.

    Заголовок передаётся байтами, как его отправил бы curl: HTTP-клиенты вроде
    httpx такое собрать откажутся, а сервер обязан ответить отказом, а не упасть.
    Starlette декодирует заголовки как latin-1, поэтому байты выше 127 доходят
    до проверки токена как символы вне ASCII.
    """
    response = client.get(
        "/api/v1/health", headers={"Authorization": "Bearer токен".encode()}
    )
    assert response.status_code == 401


def test_second_token_also_works(client):
    """Несколько токенов нужны, чтобы отзывать доступ по одному (F-38)."""
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer token-b"}).status_code == 200


def test_problem_details_shape(client):
    """RFC 9457: свой медиатип и постоянный `title` при переменном `detail`."""
    response = client.get("/api/v1/messages/999999", headers=AUTH)

    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["type"] == "/errors/not-found"
    assert body["title"] == "Ресурс не найден"
    assert body["status"] == 404
    assert body["detail"] == "Письмо не найдено"
    assert body["instance"] == "/api/v1/messages/999999"
    assert body["request_id"]


def test_validation_problem_lists_fields(client):
    """Расширения RFC 9457 лежат верхним уровнем, а не во вложенном объекте."""
    response = client.put(
        "/api/v1/mailboxes/a@test-gw.kz", headers=AUTH, json={"external_id": "x" * 100}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["type"] == "/errors/validation-error"
    assert "external_id" in body["errors"]


def test_unknown_field_rejected(client):
    """Принимаем только whitelisted-поля — лишнее не должно доезжать до модели."""
    payload = {"display_name": "Отдел продаж", "is_admin": True}
    assert client.put("/api/v1/mailboxes/a@test-gw.kz", headers=AUTH, json=payload).status_code == 422


def test_internal_error_hides_details(client, monkeypatch):
    """Клиенту — код обращения, стектрейс — только в журнал."""
    from app.services import mailboxes

    def boom(*_args, **_kwargs):
        raise RuntimeError("пароль от базы: hunter2")

    monkeypatch.setattr(mailboxes, "get_page", boom)
    response = client.get("/api/v1/mailboxes", headers=AUTH)
    assert response.status_code == 500
    assert "hunter2" not in response.text
    assert response.json()["type"] == "/errors/internal-error"


def test_ready_needs_no_token(client):
    body = client.get("/api/v1/ready").json()
    assert body["database"] is True
