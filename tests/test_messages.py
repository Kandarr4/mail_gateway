"""Постановка писем в очередь и выборка (F-11…F-15, F-32…F-34)."""

from tests.conftest import AUTH

VALID = {
    "sender": "sales@test-gw.kz",
    "recipient": "client@outside.example",
    "subject": "Тема",
    "body_text": "Текст",
}


def _send(client, **overrides):
    return client.post("/api/v1/messages", headers=AUTH, json={**VALID, **overrides})


def test_queued_returns_202_with_location(client):
    """202, а не 201: ресурс создан, но обработка ещё не завершена."""
    response = _send(client)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["message_id"].endswith("@test-gw.kz>")
    assert response.headers["Location"] == f"/api/v1/messages/{body['id']}"


def test_foreign_sender_domain_rejected(client):
    """Это же правило закрывает шлюз от использования как открытого релея."""
    response = _send(client, sender="someone@gmail.com")
    assert response.status_code == 422
    assert response.json()["type"] == "/errors/validation-error"


def test_empty_body_rejected(client):
    assert _send(client, body_text="", body_html=None).status_code == 422


def test_subject_header_injection_is_neutralized(client):
    """Перевод строки в теме позволил бы дописать произвольные заголовки."""
    message_id = _send(client, subject="Тема\r\nBcc: victim@example.org").json()["id"]
    subject = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()["subject"]
    assert "\n" not in subject and "\r" not in subject


def test_reference_header_injection_rejected_at_the_border(client):
    """Отказ здесь, а не срыв в воркере.

    Значение уходит в заголовок `In-Reply-To` как есть. Python перевод строки
    в заголовок не пропустит, но сорвётся это уже при отправке: письмо примут
    с `202`, оно пять раз попробует уйти и осядет в `failed` — вместо того
    чтобы сразу назвать ошибку ввода ошибкой ввода.
    """
    assert _send(client, in_reply_to="<x@y>\r\nBcc: victim@example.org").status_code == 422


def test_thread_key_defaults_to_own_message_id(client):
    message_id = _send(client).json()["id"]
    message = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()
    assert message["thread_key"] == message["message_id"]


def test_reply_keeps_thread_key(client):
    root = "<root@outside.example>"
    message_id = _send(client, in_reply_to=root).json()["id"]
    message = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()
    assert message["thread_key"] == root


def test_external_id_inherited_from_mailbox(client):
    client.put("/api/v1/mailboxes/sales@test-gw.kz", headers=AUTH, json={"external_id": "42"})
    message_id = _send(client).json()["id"]
    message = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()
    assert message["external_id"] == "42"


def test_created_at_carries_timezone(client):
    """Без пометки зоны потребитель прочитает UTC как локальное время."""
    message_id = _send(client).json()["id"]
    created_at = client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()["created_at"]
    assert created_at.endswith("Z") or "+" in created_at[10:]


def test_filters_and_paging(client):
    for index in range(3):
        _send(client, subject=f"письмо {index}")

    page = client.get("/api/v1/messages", headers=AUTH).json()
    all_ids = [m["id"] for m in page["items"]]
    assert len(all_ids) == 3
    assert page["total"] == 3

    # since_id включает режим опроса: по возрастанию, см. tests/test_polling.py
    since = client.get("/api/v1/messages", headers=AUTH, params={"since_id": all_ids[-1]}).json()
    assert [m["id"] for m in since["items"]] == sorted(all_ids[:-1])
    assert since["total"] == 2

    limited = client.get("/api/v1/messages", headers=AUTH, params={"limit": 1}).json()
    assert len(limited["items"]) == 1
    assert limited["total"] == 3  # total считает всё, а не только страницу
    assert limited["limit"] == 1 and limited["offset"] == 0

    incoming = client.get("/api/v1/messages", headers=AUTH, params={"direction": "incoming"}).json()
    assert incoming["items"] == [] and incoming["total"] == 0


def test_bad_filter_value_rejected(client):
    assert client.get("/api/v1/messages", headers=AUTH, params={"direction": "sideways"}).status_code == 422
    assert client.get("/api/v1/messages", headers=AUTH, params={"limit": 9999}).status_code == 422


def test_patch_marks_read(client):
    message_id = _send(client).json()["id"]
    patched = client.patch(f"/api/v1/messages/{message_id}", headers=AUTH, json={"is_read": True})
    assert patched.json()["is_read"] is True
    assert client.get(f"/api/v1/messages/{message_id}", headers=AUTH).json()["is_read"] is True


def test_patch_ignores_omitted_fields(client):
    """PATCH меняет только переданное; пустое тело — не сброс полей."""
    message_id = _send(client).json()["id"]
    client.patch(f"/api/v1/messages/{message_id}", headers=AUTH, json={"is_read": True})

    unchanged = client.patch(f"/api/v1/messages/{message_id}", headers=AUTH, json={})
    assert unchanged.json()["is_read"] is True


def test_patch_rejects_readonly_fields(client):
    message_id = _send(client).json()["id"]
    response = client.patch(f"/api/v1/messages/{message_id}", headers=AUTH, json={"status": "sent"})
    assert response.status_code == 422


def test_missing_message_is_404(client):
    assert client.get("/api/v1/messages/424242", headers=AUTH).status_code == 404
    assert client.patch("/api/v1/messages/424242", headers=AUTH, json={"is_read": True}).status_code == 404
