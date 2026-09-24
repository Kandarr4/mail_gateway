"""Опрос — единственный канал уведомлений, поэтому его протокол закреплён тестами.

Потребитель хранит курсор (последний виденный id) и повторяет:
    GET /api/v1/messages?direction=incoming&since_id=<курсор>
Ни одно письмо не должно проскочить мимо курсора ни при каком `limit`.
"""

from email.message import EmailMessage

import pytest

from app.services import receiving
from tests.conftest import AUTH, make_mailbox

MAILBOX = "sales@test-gw.kz"


@pytest.fixture
def mailbox(db):
    make_mailbox(db, address=MAILBOX, external_id="42", is_active=True)
    db.commit()


def receive(index: int) -> None:
    msg = EmailMessage()
    msg["From"] = "client@outside.example"
    msg["To"] = MAILBOX
    msg["Subject"] = f"письмо {index}"
    msg["Message-ID"] = f"<poll-{index}@outside.example>"
    msg.set_content(f"тело {index}")
    receiving.accept(msg.as_bytes(), [MAILBOX], "client@outside.example")


def poll(client, cursor: int, **params) -> dict:
    return client.get(
        "/api/v1/messages",
        headers=AUTH,
        params={"direction": "incoming", "since_id": cursor, **params},
    ).json()


def test_since_id_returns_oldest_first(mailbox, client):
    """Догоняющий опрос обязан идти по возрастанию.

    При убывающем порядке `limit` отрезал бы *старые* письма, а курсор
    прыгал бы на самое новое — пропущенные не вернулись бы уже никогда.
    """
    for index in range(5):
        receive(index)

    page = poll(client, 0, limit=2)
    subjects = [m["subject"] for m in page["items"]]
    assert subjects == ["письмо 0", "письмо 1"]


def test_cursor_walk_loses_nothing(mailbox, client):
    """Полный проход маленькими страницами обязан отдать все письма ровно по разу."""
    for index in range(7):
        receive(index)

    seen: list[str] = []
    cursor = 0
    for _ in range(10):  # ограничитель, чтобы тест не завис
        page = poll(client, cursor, limit=2)
        if not page["items"]:
            break
        seen.extend(m["subject"] for m in page["items"])
        cursor = page["items"][-1]["id"]

    assert seen == [f"письмо {i}" for i in range(7)]
    assert len(seen) == len(set(seen))


def test_total_tells_consumer_to_keep_polling(mailbox, client):
    """По `total` видно, что за курсором осталось ещё — опрос не гадает."""
    for index in range(5):
        receive(index)

    page = poll(client, 0, limit=2)
    assert page["total"] == 5
    assert len(page["items"]) == 2

    cursor = page["items"][-1]["id"]
    assert poll(client, cursor)["total"] == 3


def test_empty_poll_when_nothing_new(mailbox, client):
    receive(0)
    cursor = poll(client, 0)["items"][0]["id"]

    page = poll(client, cursor)
    assert page["items"] == []
    assert page["total"] == 0


def test_browsing_without_cursor_shows_newest_first(mailbox, client):
    """Без `since_id` запрос означает «покажи последние»."""
    for index in range(3):
        receive(index)

    page = client.get("/api/v1/messages", headers=AUTH).json()
    assert [m["subject"] for m in page["items"]] == ["письмо 2", "письмо 1", "письмо 0"]


def test_health_exposes_cursor_start(mailbox, client):
    """`last_message_id` даёт стартовую точку тому, кто не хочет читать историю."""
    for index in range(3):
        receive(index)

    last = client.get("/api/v1/health", headers=AUTH).json()["last_message_id"]
    assert poll(client, last)["items"] == []


def test_unread_filter_survives_cursor(mailbox, client):
    """Фильтры совместимы с опросом: непрочитанные за курсором."""
    for index in range(3):
        receive(index)

    first = poll(client, 0)["items"][0]
    client.patch(f"/api/v1/messages/{first['id']}", headers=AUTH, json={"is_read": True})

    page = poll(client, 0, unread_only=True)
    assert first["id"] not in [m["id"] for m in page["items"]]
    assert page["total"] == 2
