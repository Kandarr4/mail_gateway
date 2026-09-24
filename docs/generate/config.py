"""Единственное место, где живут цены, реквизиты и названия.

Меняете здесь — пересобираете документы командой `make_docs.bat`, и оба
файла становятся согласованными. Правка цены прямо в .docx рано или поздно
разойдётся между инструкцией и коммерческим предложением.

Здесь лежит обезличенный образец. Реальные реквизиты и согласованные цены —
в `config_local.py` рядом (он в `.gitignore`): ИИН, телефон и прайс в
открытом репозитории никому не нужны, а генератор документов должен
работать у любого, кто склонировал проект.
"""

# --- Исполнитель ------------------------------------------------------------- #

EXECUTOR = {
    "name": "ИП «Наименование»",
    "iin": "000000000000",
    "oked": "Разработка программного обеспечения",
    # Заполните перед отправкой заказчику — или задайте в config_local.py.
    "phone": "___________________",
    "email": "___________________",
    "address": "___________________",
    "bank": "___________________",
    "iik": "___________________",
    "signatory": "___________________",
}

PRODUCT = {
    "name": "Mail Gateway",
    "tagline": "Собственный почтовый шлюз: приём и отправка писем из вашей системы",
    "version": "1.0",
}

# --- Цены (тенге) ------------------------------------------------------------- #
#
# Образец пропорций, а не прайс. Рабочие суммы — в config_local.py.

PRICES = {
    "license_year": 100_000,
    "setup_package": 60_000,
    "license_renewal": 50_000,
    "support_month": 18_000,
    "hour_rate": 5_000,
}

CURRENCY = "₸"

# --- Условия ------------------------------------------------------------------ #

TERMS = {
    "license_period": "12 месяцев с даты подписания акта",
    "warranty_months": 3,
    "setup_days": "до 5 рабочих дней с момента предоставления доступов",
    "offer_valid_days": 30,
    "prepayment_percent": 50,
    # НДС: ИП на упрощёнке НДС не облагается. Проверьте свой режим.
    "vat_note": "Без НДС (специальный налоговый режим).",
}

# --- Примеры для инструкции ---------------------------------------------------- #
#
# IP из диапазона RFC 5737, отведённого под документацию.

EXAMPLE = {
    "domain": "example.kz",
    "host": "mail.example.kz",
    "ip": "203.0.113.10",
    "api_port": 8025,
    "selector": "mail",
}


def money(amount: int) -> str:
    """100000 -> '100 000 ₸' с неразрывными пробелами."""
    return f"{amount:,}".replace(",", " ") + " " + CURRENCY


def _apply_local_overrides() -> None:
    """Подмешивает `config_local.py`, если он есть рядом.

    Файл грузится по пути, а не импортом пакета, потому что этот модуль
    подключают двумя способами: как `config` (генераторы правят sys.path) и
    как `docs.generate.config` (installer/make_license.py и тесты).
    Словари сливаются по ключам — локально можно переопределить одну цену.
    """
    import importlib.util
    from pathlib import Path

    local = Path(__file__).with_name("config_local.py")
    if not local.exists():
        return

    spec = importlib.util.spec_from_file_location("_config_local", local)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name, value in vars(module).items():
        if name.startswith("_"):
            continue
        current = globals().get(name)
        if isinstance(current, dict) and isinstance(value, dict):
            current.update(value)
        else:
            globals()[name] = value


_apply_local_overrides()
