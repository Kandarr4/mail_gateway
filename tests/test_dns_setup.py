"""Справка по записям DNS: состав набора и сверка с тем, что в зоне.

Сеть не трогается: подменяется `_lookup`. Иначе тесты зависели бы от чужой
зоны и падали бы в самолёте.
"""

import pytest

from app.services import dns_setup

DOMAIN = "example.kz"


@pytest.fixture
def records():
    return dns_setup.required_records(DOMAIN, dkim_public_key="PUBLICKEY")


def find(records, fqdn: str):
    return next(item for item in records if item.fqdn == fqdn)


# --- Состав набора ----------------------------------------------------------------- #


def test_covers_every_record_mail_needs(records):
    """Пропущенная запись — это либо непришедшая почта, либо спам-папка."""
    assert [item.type for item in records] == ["A", "MX", "TXT", "TXT", "TXT", "PTR"]
    assert {item.fqdn for item in records} >= {
        f"mail.{DOMAIN}",
        DOMAIN,
        f"mail._domainkey.{DOMAIN}",
        f"_dmarc.{DOMAIN}",
    }


def test_host_is_relative_to_the_zone(records):
    """Панели регистраторов дописывают зону сами; полное имя в поле «Хост»
    превращается в mail._domainkey.example.kz.example.kz."""
    assert find(records, f"mail._domainkey.{DOMAIN}").host == "mail._domainkey"
    assert find(records, f"_dmarc.{DOMAIN}").host == "_dmarc"
    assert find(records, DOMAIN).host == "@"
    assert find(records, f"mail.{DOMAIN}").host == "mail"


def test_dmarc_starts_with_p_none(records):
    """Сразу reject — это отбитые собственные письма до того, как оператор
    увидит хоть один отчёт."""
    dmarc = find(records, f"_dmarc.{DOMAIN}")
    assert "v=DMARC1" in dmarc.value
    assert "p=none" in dmarc.value


def test_dkim_value_carries_the_public_key(records):
    dkim = find(records, f"mail._domainkey.{DOMAIN}")
    assert dkim.value == "v=DKIM1; k=rsa; p=PUBLICKEY"


def test_missing_dkim_key_says_how_to_create_it():
    records = dns_setup.required_records(DOMAIN, dkim_public_key="")
    dkim = find(records, f"mail._domainkey.{DOMAIN}")
    assert dkim.value == ""
    assert "dkim-keygen" in dkim.note


def test_ptr_is_marked_as_not_a_zone_record(records):
    ptr = next(item for item in records if item.type == "PTR")
    assert "не в зоне" in ptr.note.lower() or "НЕ в зоне" in ptr.note
    assert ptr.value == f"mail.{DOMAIN}"


def test_text_is_ready_to_paste_into_a_registrar_panel(monkeypatch):
    monkeypatch.setattr(dns_setup, "public_key_of", lambda domain: "PUBLICKEY")
    text = dns_setup.as_text(DOMAIN)
    for needed in ("v=DMARC1", "v=DKIM1", "v=spf1", "MX", "PTR", "Хост"):
        assert needed in text, needed


def test_text_without_a_key_tells_how_to_create_it(monkeypatch):
    """Ключа ещё нет — вместо пустого места должно стоять указание."""
    monkeypatch.setattr(dns_setup, "public_key_of", lambda domain: "")
    text = dns_setup.as_text(DOMAIN)
    assert "(ещё не создано)" in text
    assert "dkim-keygen" in text


# --- Сверка с зоной ----------------------------------------------------------------- #


def check_with(monkeypatch, answers: dict[str, list[str]], records):
    def fake_lookup(record):
        if record.fqdn not in answers and record.type != "PTR":
            import dns.resolver

            raise dns.resolver.NXDOMAIN
        return answers.get(record.fqdn if record.type != "PTR" else "PTR", [])

    monkeypatch.setattr(dns_setup, "_lookup", fake_lookup)
    return {item.record.type + ":" + item.record.fqdn: item for item in dns_setup.check(records)}


def test_absent_record_is_reported_as_missing_not_as_failure(monkeypatch, records):
    """«Такого имени нет» — это ответ DNS, а не обрыв связи: оператор должен
    видеть «не добавлено», а не «не удалось проверить»."""
    result = check_with(monkeypatch, {}, records)
    assert result[f"TXT:_dmarc.{DOMAIN}"].status == dns_setup.MISSING


def test_network_failure_is_not_called_missing(monkeypatch, records):
    def broken(record):
        raise TimeoutError("нет сети")

    monkeypatch.setattr(dns_setup, "_lookup", broken)
    assert all(item.status == dns_setup.UNKNOWN for item in dns_setup.check(records))


@pytest.mark.parametrize(
    "spf",
    [
        "v=spf1 a:mail.example.kz -all",     # ровно как советуем
        "v=spf1 +a +mx include:_spf.ps.kz -all",  # как пишут регистраторы
        "v=spf1 mx -all",
    ],
)
def test_spf_written_differently_still_counts(monkeypatch, records, spf):
    """Ругаться на рабочую запись из-за формы — гонять человека по кругу."""
    result = check_with(monkeypatch, {DOMAIN: [spf]}, records)
    assert result[f"TXT:{DOMAIN}"].status == dns_setup.OK


def test_spf_without_our_server_is_flagged(monkeypatch, records):
    result = check_with(monkeypatch, {DOMAIN: ["v=spf1 include:spf.google.com -all"]}, records)
    assert result[f"TXT:{DOMAIN}"].status == dns_setup.DIFFERENT


def test_dkim_of_another_key_is_flagged(monkeypatch, records):
    answers = {f"mail._domainkey.{DOMAIN}": ["v=DKIM1; k=rsa; p=OTHERKEY"]}
    result = check_with(monkeypatch, answers, records)
    assert result[f"TXT:mail._domainkey.{DOMAIN}"].status == dns_setup.DIFFERENT


def test_dkim_split_into_chunks_is_recognised(monkeypatch, records):
    """Длинный ключ панели режут на части и склеивают пробелами."""
    answers = {f"mail._domainkey.{DOMAIN}": ["v=DKIM1; k=rsa; p=PUBLIC KEY"]}
    result = check_with(monkeypatch, answers, records)
    assert result[f"TXT:mail._domainkey.{DOMAIN}"].status == dns_setup.OK


def test_foreign_ptr_is_flagged(monkeypatch, records):
    """Чужой PTR (адрес провайдера) — главная причина попадания в спам."""
    result = check_with(monkeypatch, {"PTR": ["client.fttb.example.net"]}, records)
    checked = result["PTR:<IP-адрес сервера>"]
    assert checked.status == dns_setup.DIFFERENT
    assert "client.fttb" in checked.actual


def test_matching_ptr_passes(monkeypatch, records):
    result = check_with(monkeypatch, {"PTR": [f"mail.{DOMAIN}"]}, records)
    assert result["PTR:<IP-адрес сервера>"].status == dns_setup.OK


def test_mx_with_another_priority_still_counts(monkeypatch, records):
    """Приоритет — дело вкуса, важен адресат."""
    result = check_with(monkeypatch, {DOMAIN: [f"5 mail.{DOMAIN}"]}, records)
    assert result[f"MX:{DOMAIN}"].status == dns_setup.OK


def test_mx_pointing_elsewhere_is_flagged(monkeypatch, records):
    result = check_with(monkeypatch, {DOMAIN: ["10 mx.yandex.net"]}, records)
    assert result[f"MX:{DOMAIN}"].status == dns_setup.DIFFERENT
