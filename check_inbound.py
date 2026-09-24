"""Диагностика приёма почты: DNS, порт, SMTP-диалог, доставка до API.

Запуск (шлюз должен быть уже запущен):

    .venv\\Scripts\\python.exe check_inbound.py                 проверка изнутри
    .venv\\Scripts\\python.exe check_inbound.py --host mail.somnium.kz   снаружи

Ничего не меняет: только подключается, отправляет одно письмо и читает API.
"""

import argparse
import smtplib
import socket
import ssl
import sys
import uuid
from email.message import EmailMessage

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from app.config import settings

OK, FAIL, WARN, INFO = "  OK  ", " FAIL ", " WARN ", "      "
problems: list[str] = []


def say(mark: str, text: str, detail: str = "") -> None:
    print(f"[{mark}] {text}" + (f"\n{INFO}   {detail}" if detail else ""))
    if mark is FAIL:
        problems.append(text)


def check_dns(domain: str, host: str) -> None:
    print(f"\n== DNS для {domain} ==")
    try:
        import dns.resolver
    except ImportError:
        say(WARN, "dnspython не установлен, проверка DNS пропущена")
        return

    try:
        mx = sorted(dns.resolver.resolve(domain, "MX"), key=lambda r: r.preference)
        targets = [r.exchange.to_text().rstrip(".") for r in mx]
        say(OK, f"MX найдена: {', '.join(targets)}")
    except Exception as exc:
        say(FAIL, f"MX-запись для {domain} не получена", str(exc))
        targets = []

    for target in targets[:1]:
        try:
            addresses = [r.to_text() for r in dns.resolver.resolve(target, "A")]
            say(OK, f"A для {target}: {', '.join(addresses)}")
            _check_ptr(addresses[0], target)
        except Exception as exc:
            say(FAIL, f"A-запись для {target} не получена", str(exc))

    _check_txt(domain, "SPF", "v=spf1")
    _check_txt(f"_dmarc.{domain}", "DMARC", "v=DMARC1")


def _check_ptr(ip: str, expected: str) -> None:
    import dns.resolver
    import dns.reversename

    try:
        name = dns.resolver.resolve(dns.reversename.from_address(ip), "PTR")[0].to_text().rstrip(".")
    except Exception as exc:
        say(FAIL, f"PTR для {ip} отсутствует — многие серверы отвергнут почту", str(exc))
        return
    if name.lower() == expected.lower():
        say(OK, f"PTR для {ip}: {name}")
    else:
        say(WARN, f"PTR для {ip} = {name}, ожидался {expected}")


def _check_txt(name: str, label: str, prefix: str) -> None:
    import dns.resolver

    try:
        records = [b"".join(r.strings).decode() for r in dns.resolver.resolve(name, "TXT")]
    except Exception:
        say(FAIL, f"{label}-запись для {name} не найдена")
        return
    match = next((r for r in records if r.startswith(prefix)), None)
    say(OK, f"{label}: {match}") if match else say(FAIL, f"{label}-запись для {name} не найдена")


def check_port(host: str, port: int) -> bool:
    print(f"\n== Доступность {host}:{port} ==")
    try:
        with socket.create_connection((host, port), timeout=10):
            say(OK, f"порт {port} принимает соединения")
            return True
    except TimeoutError:
        say(FAIL, f"{host}:{port} — таймаут",
            "Проброс на маршрутизаторе, брандмауэр Windows или провайдер закрыл порт 25")
    except OSError as exc:
        say(FAIL, f"{host}:{port} недоступен", str(exc))
    return False


def check_smtp(host: str, port: int) -> None:
    print(f"\n== SMTP-диалог с {host}:{port} ==")
    try:
        with smtplib.SMTP(host, port, timeout=15) as client:
            client.ehlo("check-inbound.local")
            greeting = client.ehlo_resp.splitlines()[0].decode(errors="replace").strip()
            if "." in greeting:
                say(OK, f"EHLO принят, сервер представился: {greeting}")
            else:
                say(WARN, f"сервер представился как {greeting} — это не доменное имя",
                    "Задайте MG_HELO_NAME: принимающие серверы считают такое имя признаком спама")

            if client.has_extn("starttls"):
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                client.starttls(context=context)
                client.ehlo("check-inbound.local")
                say(OK, "STARTTLS работает")
            else:
                say(WARN, "STARTTLS не объявлен",
                    "Задайте MG_TLS_CERT_FILE и MG_TLS_KEY_FILE — почта идёт открытым текстом")

            if client.has_extn("size"):
                say(OK, f"SIZE объявлен: {int(client.esmtp_features['size']) // 1024 // 1024} МБ")
            if client.has_extn("smtputf8"):
                say(OK, "SMTPUTF8 объявлен")
    except Exception as exc:
        say(FAIL, "SMTP-диалог не состоялся", f"{type(exc).__name__}: {exc}")


def send_probe(host: str, port: int, mailbox: str) -> str | None:
    print(f"\n== Отправка тестового письма на {mailbox} ==")
    message_id = f"<probe-{uuid.uuid4().hex}@check-inbound.local>"

    msg = EmailMessage()
    msg["From"] = "probe@check-inbound.local"
    msg["To"] = mailbox
    msg["Subject"] = "Проверка приёма Mail Gateway"
    msg["Message-ID"] = message_id
    msg.set_content("Служебное письмо диагностики. Можно удалить.")

    try:
        with smtplib.SMTP(host, port, timeout=20) as client:
            client.send_message(msg)
        say(OK, "письмо принято сервером")
        return message_id
    except smtplib.SMTPRecipientsRefused:
        say(FAIL, f"ящик {mailbox} не зарегистрирован",
            f"PUT /api/v1/mailboxes/{mailbox} — либо MG_REQUIRE_KNOWN_MAILBOX=false")
    except Exception as exc:
        say(FAIL, "письмо не отправлено", f"{type(exc).__name__}: {exc}")
    return None


def check_stored(message_id: str) -> None:
    print("\n== Письмо в базе шлюза ==")
    try:
        import httpx
    except ImportError:
        say(WARN, "httpx не установлен, проверка через API пропущена")
        return

    url = f"http://127.0.0.1:{settings.api_port}/api/v1/messages"
    token = next(iter(settings.tokens))
    try:
        page = httpx.get(url, headers={"Authorization": f"Bearer {token}"},
                         params={"direction": "incoming", "limit": 20}, timeout=10).json()
    except Exception as exc:
        say(FAIL, "API шлюза недоступен", str(exc))
        return

    found = next((m for m in page.get("items", []) if m.get("message_id") == message_id), None)
    if found is None:
        say(FAIL, "письмо принято по SMTP, но в базе не найдено", "Смотрите журнал data/logs/")
        return

    say(OK, f"письмо сохранено, id={found['id']}")
    spf, dmarc = found.get("spf_result"), found.get("dmarc_result")
    if spf in (None, "none"):
        say(WARN, f"проверки не отработали (spf={spf}, dmarc={dmarc})",
            "Если шлюз за прокси — потерян адрес отправителя, нужен MG_PROXY_PROTOCOL=true. "
            "При проверке с локальной машины это нормально.")
    else:
        say(OK, f"проверки отработали: spf={spf}, dkim={found.get('dkim_result')}, dmarc={dmarc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Диагностика приёма почты")
    parser.add_argument("--host", default="127.0.0.1",
                        help="куда подключаться (по умолчанию локально)")
    parser.add_argument("--port", type=int, default=settings.smtp_port)
    parser.add_argument("--mailbox", default=None, help="ящик-получатель тестового письма")
    parser.add_argument("--skip-dns", action="store_true")
    args = parser.parse_args()

    mailbox = args.mailbox or f"postmaster@{settings.domain}"
    print(f"Домен шлюза: {settings.domain}")
    print(f"Проверяем: {args.host}:{args.port}, ящик {mailbox}")

    if not args.skip_dns:
        check_dns(settings.domain, args.host)

    if check_port(args.host, args.port):
        check_smtp(args.host, args.port)
        if message_id := send_probe(args.host, args.port, mailbox):
            check_stored(message_id)

    print("\n" + ("=" * 60))
    if problems:
        print("ЕСТЬ ПРОБЛЕМЫ:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("ПРИЁМ РАБОТАЕТ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
