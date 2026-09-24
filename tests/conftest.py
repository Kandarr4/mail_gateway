"""Общая обвязка тестов.

Окружение задаётся до импорта приложения: `Settings` читается один раз при
импорте модуля конфигурации, поэтому позже переопределить его уже нельзя.
"""

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

TMP = Path(tempfile.mkdtemp(prefix="mailgw_tests_"))

# Весь прогон работает с включённым шифрованием содержимого: так каждый тест,
# касающийся писем и вложений, проверяет и шифрованный контур. Открытый режим
# (без ключа) проверяется точечно в test_crypto.py.
_ENCRYPTION_KEY_FILE = TMP / "encryption.key"
_ENCRYPTION_KEY_FILE.write_bytes(Fernet.generate_key())

os.environ.update(
    MG_DOMAIN="test-gw.kz",
    MG_API_TOKENS="token-a,token-b",
    MG_DATABASE_URL=f"sqlite:///{TMP / 'test.db'}",
    MG_ATTACHMENT_DIR=str(TMP / "attachments"),
    MG_LOG_DIR=str(TMP / "logs"),
    MG_SMTP_PORT="8225",
    MG_RATE_LIMIT_PER_MINUTE="0",
    MG_MAX_ATTACHMENT_MB="1",
    MG_ENCRYPTION_KEY_FILE=str(_ENCRYPTION_KEY_FILE),
    # Явно гасим DKIM: иначе поле утечёт из .env рядом с репозиторием (мастер
    # первого запуска мог его туда записать), и тесты стали бы зависеть от
    # локальной машины. Тесты, которым нужен ключ, задают путь сами.
    MG_DKIM_PRIVATE_KEY_FILE="",
    # По той же причине гасим PROXY protocol. На площадке он бывает включён
    # (шлюз стоит за stream-прокси), а тесты SMTP подключаются напрямую и
    # заголовка не шлют — с включённой настройкой сервер закрывает соединение
    # до приветствия, и падает всё, что касается SMTP. Кому нужен разбор
    # заголовка, включает настройку у себя.
    MG_PROXY_PROTOCOL="false",
)

import base64  # noqa: E402
import json  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete, select  # noqa: E402

from app.bootstrap import ensure_default_client_from_env  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import (  # noqa: E402
    AdminUser,
    AppSetting,
    Attachment,
    Client,
    Mailbox,
    Message,
    Upload,
)
from app.schema_setup import prepare_database  # noqa: E402
from app.services import licensing  # noqa: E402

AUTH = {"Authorization": "Bearer token-a"}

# --- Лицензия на весь прогон ----------------------------------------------------- #
#
# Без действующей лицензии шлюз не принимает и не отправляет почту, и каждый
# второй тест проверял бы отказ вместо своего предмета. Поэтому весь прогон
# идёт с действующей лицензией — как в бою; отсутствие лицензии проверяется
# точечно в test_licensing.py, там же файл подменяется на свой.
#
# Ключ подписи — собственный, подставленный тем же механизмом, что
# предусмотрен для тестового сервера лицензирования. В собранном приложении
# переменная LICENSE_PUBLIC_KEY игнорируется (см. licensing._public_key).

LICENSE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

os.environ["LICENSE_PUBLIC_KEY"] = LICENSE_KEY.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
).decode().replace("\n", "\\n")


def make_license(path, *, days=365, max_domains=3, program_id="mail-gateway",
                 issue_shift_days=0, key=None, **extra) -> None:
    """Файл лицензии в том же формате, что выпускает somnium_licensing."""
    issue = datetime.now() + timedelta(days=issue_shift_days)
    data = {
        "license_id": "test",
        "program_id": program_id,
        "company_name": "ТОО Тест",
        "issue_date": issue.isoformat(),
        "expiry_date": (issue + timedelta(days=days)).isoformat(),
        "issued_by": "Somnium Systems",
        "max_domains": max_domains,
        **extra,
    }
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode()
    signature = (key or LICENSE_KEY).sign(
        payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    path.write_bytes(base64.b64encode(payload + signature))


# Боевой `license.lic` рядом с репозиторием тесты не читают: путь уводится в
# каталог прогона, иначе результат зависел бы от машины разработчика.
licensing.LICENSE_FILE = TMP / "license.lic"
make_license(licensing.LICENSE_FILE, max_domains=1000)
licensing.reset_cache()


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Схема + клиент «Default» с доменом и ключами из MG_API_TOKENS.

    Бутстрап здесь, а не в lifespan: `client` собирает `TestClient` без
    контекстного менеджера, то есть lifespan в тестах не выполняется.
    """
    prepare_database()
    ensure_default_client_from_env()


@pytest.fixture(autouse=True)
def clean_db(_schema):
    """Каждый тест начинается с пустой базы — порядок тестов ничего не решает.

    Клиент «Default», его домен и API-ключи — сессионное состояние наравне со
    схемой: они создаются один раз в `_schema` и не вычищаются. Стирается всё,
    кроме них, включая клиентов, заведённых самими тестами.
    """
    with SessionLocal() as db:
        for model in (Attachment, Message, Upload, Mailbox, AdminUser, AppSetting):
            db.execute(delete(model))
        # Дополнительные клиенты (и каскадом их домены/ключи) — тестовые.
        extra = db.scalars(select(Client).where(Client.name != "Default")).all()
        for client_row in extra:
            db.delete(client_row)
        db.commit()
    yield


@pytest.fixture
def default_client_id(db) -> int:
    """id клиента «Default» — для тестов, создающих модели напрямую."""
    return db.scalars(select(Client).where(Client.name == "Default")).one().id


def make_mailbox(db, **fields) -> Mailbox:
    """Ящик клиента «Default» — для тестов, создающих модели мимо API.

    `client_id` теперь обязателен (F-54), и каждому тесту повторять его
    выяснение незачем.
    """
    client_id = db.scalars(select(Client.id).where(Client.name == "Default")).one()
    box = Mailbox(client_id=client_id, **fields)
    db.add(box)
    return box


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture(scope="session")
def client(_schema):
    """HTTP-клиент без lifespan.

    `TestClient` запускает lifespan только внутри `with`; здесь он не нужен —
    поднимать SMTP-слушатель и фоновые воркеры ради проверки роутов ни к чему.
    """
    return TestClient(create_app(), raise_server_exceptions=False)
