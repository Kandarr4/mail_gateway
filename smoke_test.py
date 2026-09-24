"""Сквозная проверка шлюза: приём письма по SMTP + основные вызовы API.

Запуск:  .venv\\Scripts\\python.exe smoke_test.py
Использует временный каталог, боевую БД не трогает.
"""

import os
import smtplib
import tempfile
import threading
import time
from email.message import EmailMessage
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="mailgw_smoke_"))
os.environ.update(
    MG_DOMAIN="mailgw-demo.kz",
    MG_SMTP_HOST="127.0.0.1",
    MG_SMTP_PORT="8125",
    MG_API_HOST="127.0.0.1",
    MG_API_PORT="8126",
    MG_API_TOKENS="test-token",
    MG_DATABASE_URL=f"sqlite:///{TMP / 'smoke.db'}",
    MG_ATTACHMENT_DIR=str(TMP / "attachments"),
    # Как в tests/conftest.py: на площадке PROXY protocol бывает включён в .env,
    # а проверка подключается к SMTP напрямую, без заголовка.
    MG_PROXY_PROTOCOL="false",
)


def _issue_temporary_license() -> None:
    """Свежая лицензия на время проверки — во временном каталоге.

    Без действующей лицензии сервер не поднимается вовсе, а проверять сборку
    боевым файлом заказчика незачем: подменяем открытый ключ в модуле
    лицензирования на свой (так же, как тесты) и выписываем
    однодневную лицензию рядом с временной базой. Настоящий `license.lic` в
    корне развёртывания при этом не читается и не трогается.
    """
    import base64
    import json
    from datetime import datetime, timedelta

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    from app.services import licensing

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    licensing._PUBLIC_KEY_PEM = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    now = datetime.now()
    payload = json.dumps({
        "license_id": "smoke",
        "program_id": "mail-gateway",
        "company_name": "Сквозная проверка",
        "issue_date": now.isoformat(),
        "expiry_date": (now + timedelta(days=1)).isoformat(),
        "issued_by": "smoke_test.py",
        "max_domains": 10,
    }, ensure_ascii=False).encode()
    signature = key.sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )

    licensing.LICENSE_FILE = TMP / "license.lic"
    licensing.LICENSE_FILE.write_bytes(base64.b64encode(payload + signature))
    licensing.reset_cache()


_issue_temporary_license()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

BASE = "http://127.0.0.1:8126/api/v1"
AUTH = {"Authorization": "Bearer test-token"}
failures = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if condition else 'FAIL'}  {name}{'' if condition else f'  -> {detail}'}")
    if not condition:
        failures.append(name)


def main() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=8126, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    for _ in range(100):
        try:
            httpx.get(f"{BASE}/health", headers=AUTH, timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise SystemExit("Сервер не поднялся")

    print("\n== Аутентификация ==")
    check("без токена -> 401/403", httpx.get(f"{BASE}/messages").status_code in (401, 403))
    check("чужой токен -> 401", httpx.get(f"{BASE}/messages", headers={"Authorization": "Bearer nope"}).status_code == 401)

    print("\n== Регистрация ящика ==")
    r = httpx.put(f"{BASE}/mailboxes/Sales@mailgw-demo.kz", headers=AUTH, json={"external_id": "42"})
    check("ящик создан (201)", r.status_code == 201, r.text)
    check("отдан Location", r.headers.get("Location") == "/api/v1/mailboxes/sales@mailgw-demo.kz", str(r.headers))
    check("адрес приведён к нижнему регистру", r.json()["address"] == "sales@mailgw-demo.kz", r.text)
    again = httpx.put(f"{BASE}/mailboxes/sales@mailgw-demo.kz", headers=AUTH, json={"external_id": "42"})
    check("повторный PUT идемпотентен (200)", again.status_code == 200, again.text)

    print("\n== Готовность ==")
    ready = httpx.get(f"{BASE}/ready", timeout=5)
    check("/ready доступен без токена", ready.status_code == 200, ready.text)
    check("сервис готов", ready.json() == {"ready": True, "database": True, "smtp": True}, ready.text)

    cursor = httpx.get(f"{BASE}/health", headers=AUTH).json()["last_message_id"]

    print("\n== Приём почты по SMTP ==")
    msg = EmailMessage()
    msg["From"] = "client@outside-demo.kz"
    msg["To"] = "sales@mailgw-demo.kz"
    msg["Subject"] = "Заявка на кровать"
    msg["Message-ID"] = "<smoke-1@outside-demo.kz>"
    msg.set_content("Здравствуйте, интересует каталог.")
    msg.add_attachment(b"file-body", maintype="text", subtype="plain", filename="заявка.txt")

    with smtplib.SMTP("127.0.0.1", 8125, timeout=10) as s:
        s.send_message(msg)
    time.sleep(0.6)

    r = httpx.get(f"{BASE}/messages", headers=AUTH, params={"direction": "incoming"})
    items = r.json()["items"]
    check("письмо сохранено", len(items) == 1, r.text)
    check("страница сообщает total", r.json()["total"] == 1, r.text)
    if items:
        m = items[0]
        check("тело разобрано", "каталог" in (m["body_text"] or ""), str(m["body_text"]))
        check("тема разобрана", m["subject"] == "Заявка на кровать", str(m["subject"]))
        check("external_id проброшен из ящика", m["external_id"] == "42", str(m["external_id"]))
        check("вложение сохранено", len(m["attachments"]) == 1, str(m["attachments"]))
        if m["attachments"]:
            att = m["attachments"][0]
            d = httpx.get(f"{BASE}/attachments/{att['id']}", headers=AUTH)
            check("вложение скачивается", d.status_code == 200 and d.content == b"file-body", d.text[:200])
        r2 = httpx.patch(f"{BASE}/messages/{m['id']}", headers=AUTH, json={"is_read": True})
        check("отметка о прочтении через PATCH", r2.status_code == 200 and r2.json()["is_read"] is True, r2.text)

    print("\n== Защита от дублей и чужих адресов ==")
    with smtplib.SMTP("127.0.0.1", 8125, timeout=10) as s:
        s.send_message(msg)
    time.sleep(0.4)
    check("дубль не создал второе письмо",
          httpx.get(f"{BASE}/messages", headers=AUTH, params={"direction": "incoming"}).json()["total"] == 1)

    unknown = EmailMessage()
    unknown["From"] = "client@outside-demo.kz"
    unknown["To"] = "nobody@mailgw-demo.kz"
    unknown["Subject"] = "spam"
    unknown.set_content("...")
    refused = False
    try:
        with smtplib.SMTP("127.0.0.1", 8125, timeout=10) as s:
            s.send_message(unknown)
    except smtplib.SMTPRecipientsRefused:
        refused = True
    check("неизвестный ящик отклонён на RCPT", refused)

    print("\n== Постановка письма в очередь отправки ==")
    up = httpx.post(f"{BASE}/uploads", headers=AUTH, files={"file": ("прайс.txt", b"price-list", "text/plain")})
    check("файл загружен (201)", up.status_code == 201, up.text)

    r = httpx.post(f"{BASE}/messages", headers=AUTH, json={
        "sender": "sales@mailgw-demo.kz",
        "recipient": "client@outside-demo.kz",
        "subject": "Ответ",
        "body_text": "Каталог во вложении.",
        "attachment_ids": [up.json()["id"]],
    })
    check("письмо принято в очередь (202)", r.status_code == 202, r.text)
    check("присвоен Message-ID", r.json().get("message_id", "").endswith("@mailgw-demo.kz>"), r.text)

    bad = httpx.post(f"{BASE}/messages", headers=AUTH, json={
        "sender": "someone@gmail.com", "recipient": "a@outside-demo.kz", "body_text": "x"})
    check("чужой домен отправителя отклонён", bad.status_code == 422, bad.text)
    check("ошибка в формате RFC 9457",
          bad.headers.get("content-type", "").startswith("application/problem+json")
          and bad.json()["type"] == "/errors/validation-error", bad.text)

    empty = httpx.post(f"{BASE}/messages", headers=AUTH, json={
        "sender": "sales@mailgw-demo.kz", "recipient": "a@outside-demo.kz"})
    check("пустое письмо отклонено", empty.status_code == 422, empty.text)

    reuse = httpx.post(f"{BASE}/messages", headers=AUTH, json={
        "sender": "sales@mailgw-demo.kz", "recipient": "a@outside-demo.kz",
        "body_text": "x", "attachment_ids": [up.json()["id"]]})
    check("повторное использование файла отклонено", reuse.status_code == 422, reuse.text)

    print("\n== Опрос новых писем ==")
    poll = httpx.get(f"{BASE}/messages", headers=AUTH,
                     params={"direction": "incoming", "since_id": cursor}).json()
    check("опрос по курсору вернул новое письмо", poll["total"] == 1, str(poll))
    caught_up = poll["items"][-1]["id"] if poll["items"] else cursor
    check("после сдвига курсора новых нет",
          httpx.get(f"{BASE}/messages", headers=AUTH,
                    params={"direction": "incoming", "since_id": caught_up}).json()["total"] == 0)

    print("\n== Health ==")
    h = httpx.get(f"{BASE}/health", headers=AUTH).json()
    check("health отвечает", h.get("status") == "ok", str(h))
    check("health отдаёт опорный курсор", isinstance(h.get("last_message_id"), int), str(h))

    server.should_exit = True
    time.sleep(0.5)

    print("\n" + ("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ" if not failures else f"ПРОВАЛЕНО: {failures}"))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
