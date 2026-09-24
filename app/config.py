"""Конфигурация из окружения/.env. Секретов в коде нет — только имена переменных."""

import sys
from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .constants import InboundAuthPolicy, OutboundMode

#: Корень развёртывания: рядом лежат `.env`, `data/`, БД и `license.lic`.
#: В собранном приложении (PyInstaller) это каталог с exe, а не каталог
#: модуля — тот указывает внутрь `_internal`, где операторские файлы жить
#: не должны (их снёс бы любой апдейт сборки).
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent.parent

#: Значение токена по умолчанию — заведомо небезопасное, служит маркером
#: незавершённой настройки: приложение откажется стартовать с ним в бою.
INSECURE_TOKEN = "change-me-please"  # noqa: S105 — маркер ненастроенности, а не секрет


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MG_",
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Домен и режим работы ---
    domain: str = "localhost"
    debug: bool = False

    #: Имя, которым шлюз представляется в SMTP (HELO/EHLO). Пусто — `mail.<домен>`.
    #: Обязано быть разрешимым полным доменным именем с PTR-записью на этот же
    #: адрес: имя машины вроде `WIN-SERVER` принимающие серверы считают спамом,
    #: а часть отвечает "need fully-qualified hostname".
    helo_name: str = ""

    # --- Приём ---
    smtp_host: str = "0.0.0.0"  # noqa: S104 — принимать почту извне — назначение шлюза
    smtp_port: int = Field(25, ge=1, le=65535)
    require_known_mailbox: bool = True
    max_message_mb: int = Field(35, ge=1, le=1024, description="Потолок размера письма целиком")
    smtp_rate_limit_per_minute: int = Field(
        60, ge=0, description="Писем в минуту с одного адреса, 0 — без ограничения"
    )

    #: Файлы сертификата для STARTTLS. Без них приём идёт открытым текстом.
    tls_cert_file: str = ""
    tls_key_file: str = ""

    #: Читать заголовок PROXY protocol. Включать ТОЛЬКО если перед шлюзом стоит
    #: доверенный прокси, который его отправляет: иначе любой подключившийся
    #: сможет назваться чужим адресом и обойти проверку SPF.
    proxy_protocol: bool = False

    inbound_auth: InboundAuthPolicy = InboundAuthPolicy.ANNOTATE

    # --- HTTP API ---
    api_host: str = "0.0.0.0"  # noqa: S104 — API слушает локальную сеть; доступ закрывает токен
    api_port: int = Field(8025, ge=1, le=65535)
    api_tokens: str = INSECURE_TOKEN
    rate_limit_per_minute: int = Field(600, ge=0, description="0 — без ограничения")

    # --- Веб-панель управления ---
    web_enabled: bool = True
    #: Ставить флаг Secure на cookie сессии. Обязательно при доступе по HTTPS;
    #: при доступе по обычному HTTP вход перестанет работать.
    web_secure_cookies: bool = False
    web_session_hours: int = Field(8, ge=1, le=168)
    web_login_attempts_per_minute: int = Field(10, ge=1, le=100)

    # --- Хранилище ---
    database_url: str = "sqlite:///mail_gateway.db"
    attachment_dir: str = "data/attachments"
    #: Куда складывать снимки базы. Путь — настройка развёртывания и остаётся
    #: в окружении; периодичность и предельный объём оператор задаёт в панели.
    backup_dir: str = "data/backups"
    #: Путь к ключу Fernet: тела писем в БД и файлы вложений хранятся
    #: зашифрованными. Пусто — открытым текстом. Создание ключа:
    #: `python manage.py encryption-keygen`. Ключ хранить отдельно от
    #: резервных копий — без него содержимое копий невосстановимо.
    encryption_key_file: str = ""
    max_attachment_mb: int = Field(25, ge=1, le=1024)
    upload_ttl_hours: int = Field(24, ge=1, description="Через сколько чистить неиспользованные загрузки")

    # --- Журналирование ---
    log_level: str = "INFO"
    log_dir: str = "data/logs"
    log_json: bool = False
    #: Сколько суточных архивов держать помимо текущего файла (N-08).
    log_retention_days: int = Field(10, ge=1)

    # --- Исходящая почта ---
    outbound_mode: OutboundMode = OutboundMode.DIRECT
    relay_host: str = ""
    relay_port: int = Field(587, ge=1, le=65535)
    relay_username: str = ""
    relay_password: str = ""
    relay_use_tls: bool = True
    #: Проверять сертификат релея. Выключать только для внутреннего релея с
    #: самоподписанным сертификатом: без проверки перехвативший соединение
    #: получает логин и пароль от релея.
    relay_verify_tls: bool = True

    # --- Подпись DKIM ---
    #: Путь к закрытому ключу. Пусто — письма уходят без подписи.
    dkim_private_key_file: str = ""
    #: Селектор: часть имени TXT-записи `<селектор>._domainkey.<домен>`.
    #: Позволяет держать несколько ключей и менять их без перерыва.
    dkim_selector: str = "mail"

    send_max_attempts: int = Field(5, ge=1, le=50)
    send_retry_base_seconds: int = Field(60, ge=1)
    #: Потолок паузы между попытками — экспонента без него уводит последние
    #: попытки за пределы разумного срока жизни письма.
    send_retry_max_seconds: int = Field(3600, ge=1)
    #: Сколько писем отправляется одновременно. Одна линия означает, что
    #: очередь целиком стоит за первым же недоступным адресатом.
    send_concurrency: int = Field(4, ge=1, le=32)

    # ---------------------------------------------------------------- #

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        level = v.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"MG_LOG_LEVEL: недопустимый уровень {v!r}")
        return level

    @field_validator("domain")
    @classmethod
    def _lower_domain(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def _check_consistency(self) -> "Settings":
        if not self.tokens:
            raise ValueError("MG_API_TOKENS пуст — API остался бы без аутентификации")
        if self.outbound_mode is OutboundMode.RELAY and not self.relay_host:
            raise ValueError("MG_OUTBOUND_MODE=relay требует непустой MG_RELAY_HOST")
        if self.send_retry_max_seconds < self.send_retry_base_seconds:
            raise ValueError(
                "MG_SEND_RETRY_MAX_SECONDS меньше MG_SEND_RETRY_BASE_SECONDS: "
                "потолок паузы обрезал бы уже первую попытку"
            )
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("MG_TLS_CERT_FILE и MG_TLS_KEY_FILE задаются только вместе")
        if self.max_message_mb < self.max_attachment_mb:
            raise ValueError(
                "MG_MAX_MESSAGE_MB меньше MG_MAX_ATTACHMENT_MB: письмо с допустимым "
                "вложением не пролезло бы через SMTP"
            )
        return self

    @cached_property
    def smtp_name(self) -> str:
        """Полное доменное имя шлюза для HELO/EHLO и приветствия."""
        return (self.helo_name or f"mail.{self.domain}").strip().lower()

    @cached_property
    def tokens(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.api_tokens.split(",") if t.strip())

    @cached_property
    def attachment_path(self) -> Path:
        return self._resolve(self.attachment_dir)

    @cached_property
    def log_path(self) -> Path:
        return self._resolve(self.log_dir)

    @cached_property
    def backup_path(self) -> Path:
        return self._resolve(self.backup_dir)

    @cached_property
    def max_attachment_bytes(self) -> int:
        return self.max_attachment_mb * 1024 * 1024

    @cached_property
    def max_message_bytes(self) -> int:
        return self.max_message_mb * 1024 * 1024

    @cached_property
    def dkim_key_path(self) -> Path | None:
        if not self.dkim_private_key_file:
            return None
        return self._resolve(self.dkim_private_key_file)

    @cached_property
    def encryption_key_path(self) -> Path | None:
        if not self.encryption_key_file:
            return None
        return self._resolve(self.encryption_key_file)

    @cached_property
    def tls_paths(self) -> tuple[Path, Path] | None:
        if not self.tls_cert_file:
            return None
        return self._resolve(self.tls_cert_file), self._resolve(self.tls_key_file)

    @cached_property
    def sqlalchemy_url(self) -> str:
        """Относительный путь к sqlite приводим к абсолютному, чтобы БД не
        зависела от текущего рабочего каталога процесса."""
        prefix = "sqlite:///"
        if self.database_url.startswith(prefix):
            path = Path(self.database_url[len(prefix):])
            if not path.is_absolute():
                return prefix + str(BASE_DIR / path)
        return self.database_url

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.startswith("sqlite")

    def insecure_defaults(self) -> list[str]:
        """Настройки, с которыми нельзя выпускать сервис в бой."""
        problems = []
        if INSECURE_TOKEN in self.tokens:
            problems.append("MG_API_TOKENS содержит значение по умолчанию")
        if self.domain in {"localhost", ""}:
            problems.append("MG_DOMAIN не задан")
        if not self.tls_cert_file:
            problems.append("MG_TLS_CERT_FILE не задан — входящая почта принимается без шифрования")
        if self.outbound_mode is OutboundMode.RELAY and self.relay_username and (
            not self.relay_use_tls or not self.relay_verify_tls
        ):
            problems.append(
                "Учётные данные релея уходят по непроверенному каналу — "
                "включите MG_RELAY_USE_TLS и MG_RELAY_VERIFY_TLS"
            )
        if self.outbound_mode is OutboundMode.DIRECT and not self.dkim_private_key_file:
            problems.append(
                "MG_DKIM_PRIVATE_KEY_FILE не задан — при прямой доставке письма уходят "
                "без подписи и почти наверняка попадут в спам"
            )
        if not self.encryption_key_file:
            problems.append(
                "MG_ENCRYPTION_KEY_FILE не задан — тела писем и вложения "
                "хранятся открытым текстом"
            )
        if self.inbound_auth is InboundAuthPolicy.OFF:
            problems.append("MG_INBOUND_AUTH=off — подлинность отправителя не проверяется")
        if self.web_enabled and not self.web_secure_cookies:
            problems.append(
                "MG_WEB_SECURE_COOKIES=false — cookie сессии уйдёт по открытому HTTP; "
                "включите при доступе к панели через HTTPS"
            )
        return problems

    @staticmethod
    def _resolve(raw: str) -> Path:
        p = Path(raw)
        return p if p.is_absolute() else BASE_DIR / p


settings = Settings()
