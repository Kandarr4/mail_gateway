"""Старт сервиса: привязка SMTP и проверка настроек."""

import pytest

from app.config import INSECURE_TOKEN, Settings
from app.smtp_server import _bind_host


@pytest.mark.parametrize("configured", ["0.0.0.0", "::", "*", " 0.0.0.0 "])
def test_bind_all_addresses_normalized(configured, monkeypatch):
    """aiosmtpd после старта подключается к серверу сам.

    Подключение к `0.0.0.0` под Windows недопустимо (WinError 10049) — на
    конфигурации по умолчанию сервис не поднимался вовсе.
    """
    monkeypatch.setattr("app.smtp_server.settings.smtp_host", configured)
    assert _bind_host() == ""


def test_explicit_host_is_kept(monkeypatch):
    monkeypatch.setattr("app.smtp_server.settings.smtp_host", "10.0.0.2")
    assert _bind_host() == "10.0.0.2"


def test_smtp_actually_binds_all_interfaces(monkeypatch):
    import smtplib

    from app.smtp_server import start_smtp

    monkeypatch.setattr("app.smtp_server.settings.smtp_host", "0.0.0.0")
    monkeypatch.setattr("app.smtp_server.settings.smtp_port", 8725)

    controller = start_smtp()
    try:
        with smtplib.SMTP("127.0.0.1", 8725, timeout=5) as client:
            assert client.ehlo()[0] == 250
    finally:
        controller.stop()


# --- Проверка связности настроек -------------------------------------------- #


def test_relay_mode_without_host_is_rejected():
    with pytest.raises(ValueError, match="MG_RELAY_HOST"):
        Settings(domain="a.kz", api_tokens="t", outbound_mode="relay", relay_host="")


def test_empty_token_list_is_rejected():
    with pytest.raises(ValueError, match="MG_API_TOKENS"):
        Settings(domain="a.kz", api_tokens="  ,  ")


def test_insecure_defaults_are_reported():
    problems = " | ".join(Settings(domain="localhost", api_tokens=INSECURE_TOKEN).insecure_defaults())
    assert "MG_API_TOKENS" in problems
    assert "MG_DOMAIN" in problems


def test_missing_tls_is_reported(tmp_path):
    """Приём без шифрования — рабочее, но небезопасное состояние: о нём предупреждаем."""
    settings = Settings(domain="somnium.kz", api_tokens="a-real-token")
    assert any("MG_TLS_CERT_FILE" in p for p in settings.insecure_defaults())


def test_auth_off_is_reported():
    settings = Settings(domain="somnium.kz", api_tokens="a-real-token", inbound_auth="off")
    assert any("MG_INBOUND_AUTH" in p for p in settings.insecure_defaults())


def test_fully_configured_service_reports_no_problems(tmp_path):
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.touch()
    key.touch()
    settings = Settings(
        domain="somnium.kz", api_tokens="a-real-token",
        tls_cert_file=str(cert), tls_key_file=str(key),
        web_secure_cookies=True,
        dkim_private_key_file=str(tmp_path / "dkim.private"),
    )
    assert settings.insecure_defaults() == []


def test_direct_mode_without_dkim_is_reported():
    """При прямой доставке неподписанное письмо почти наверняка уйдёт в спам."""
    settings = Settings(domain="somnium.kz", api_tokens="a-real-token", outbound_mode="direct")
    assert any("MG_DKIM" in p for p in settings.insecure_defaults())


def test_insecure_cookies_are_reported():
    """Cookie сессии по открытому HTTP перехватывается вместе с доступом в панель."""
    settings = Settings(domain="a.kz", api_tokens="t", web_enabled=True, web_secure_cookies=False)
    assert any("MG_WEB_SECURE_COOKIES" in p for p in settings.insecure_defaults())


def test_helo_name_defaults_to_mail_subdomain():
    """`smtplib` иначе подставит имя машины (WIN-SERVER) — для принимающей
    стороны это признак спама, а часть серверов прямо отказывает."""
    assert Settings(domain="somnium.kz", api_tokens="t").smtp_name == "mail.somnium.kz"


def test_helo_name_can_be_overridden():
    settings = Settings(domain="somnium.kz", api_tokens="t", helo_name="MX1.Somnium.KZ")
    assert settings.smtp_name == "mx1.somnium.kz"


def test_cert_without_key_is_rejected():
    with pytest.raises(ValueError, match="MG_TLS_KEY_FILE"):
        Settings(domain="a.kz", api_tokens="t", tls_cert_file="/tmp/c.pem")


def test_message_limit_below_attachment_limit_is_rejected():
    """Иначе письмо с допустимым вложением не пролезло бы через SMTP."""
    with pytest.raises(ValueError, match="MG_MAX_MESSAGE_MB"):
        Settings(domain="a.kz", api_tokens="t", max_message_mb=5, max_attachment_mb=25)


def test_tokens_parsed_from_comma_list():
    assert Settings(domain="a.kz", api_tokens=" one , two ,, three ").tokens == {"one", "two", "three"}
