"""Модели предметной области.

Модель знает только про данные, связи и собственные инварианты — ни про HTTP,
ни про сервисы. Переходы состояний очереди отправки описаны здесь же, рядом с
данными, которыми они управляют.
"""

import random
from datetime import datetime, timedelta
from typing import ClassVar

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .constants import Direction, MessageStatus
from .db import Base, EncryptedText, utcnow


class RetryableMixin:
    """Поведение сущности, которая доставляется с повторными попытками.

    Конкретные значения статусов задаёт наследник, поэтому алгоритм backoff
    не зашит в модель письма и переиспользуем.
    """

    #: Состояния конечного автомата очереди — переопределяются в наследнике.
    PENDING: ClassVar[str]
    IN_PROGRESS: ClassVar[str]
    DONE: ClassVar[str]
    FAILED: ClassVar[str]

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(index=True)
    last_error: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str]

    @classmethod
    def claim_values(cls) -> dict:
        """Поля перехода «взял в работу» для атомарного UPDATE.

        Именно значениями, а не присваиванием в Python: захват задачи обязан
        быть одним оператором `UPDATE ... WHERE status = PENDING`, иначе два
        воркера успевают прочитать одну строку до того, как первый её пометит,
        и письмо уходит получателю дважды. `attempts + 1` считается сервером
        БД по той же причине — прочитанное в Python значение уже устарело.
        """
        return {"status": cls.IN_PROGRESS, "attempts": cls.attempts + 1}

    def mark_done(self) -> None:
        self.status = self.DONE
        self.last_error = None
        self.next_attempt_at = None

    def mark_failed(self, error: str) -> None:
        self.status = self.FAILED
        self.last_error = error
        self.next_attempt_at = None

    def schedule_retry(self, error: str, base_seconds: int, max_seconds: int) -> int:
        """Планирует повтор и возвращает выбранную паузу в секундах.

        База удваивается с каждой попыткой, но упирается в потолок: при
        `MG_SEND_MAX_ATTEMPTS=50` чистая экспонента отложила бы последнюю
        попытку на миллионы лет, то есть письмо не ушло бы никогда.

        Разброс ±20% не украшение: письма, отложенные одной и той же аварией
        принимающей стороны, без него дозревают одновременно и обрушиваются на
        только что поднявшийся сервер единой пачкой — той самой, от которой он
        и упал.
        """
        # Показатель ограничиваем отдельно, чтобы не считать 2**49 ради числа,
        # которое всё равно будет срезано потолком.
        exponent = min(max(self.attempts - 1, 0), 30)
        delay = min(base_seconds * (2**exponent), max_seconds)
        delay = max(1, round(delay * random.uniform(0.8, 1.2)))  # noqa: S311 — джиттер повторов, не криптография

        self.status = self.PENDING
        self.last_error = error
        self.next_attempt_at = utcnow() + timedelta(seconds=delay)
        return delay

    def attempts_exhausted(self, max_attempts: int) -> bool:
        return self.attempts >= max_attempts


class Client(Base):
    """Клиент шлюза: владелец доменов, ящиков и API-ключей.

    Один инстанс может обслуживать нескольких клиентов одновременно — ключ
    одного не должен видеть данные другого (F-54).
    """

    __tablename__ = "client"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Domain(Base):
    """Домен, закреплённый за клиентом. Один домен — один владелец: DKIM/SPF
    домена может контролировать только одна сторона."""

    __tablename__ = "domain"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Путь к закрытому ключу DKIM этого домена. Пусто — письма уходят без
    #: подписи (как и раньше на уровне всего инстанса), но теперь по-доменно.
    dkim_private_key_file: Mapped[str | None] = mapped_column(String(1024))
    dkim_selector: Mapped[str] = mapped_column(String(64), default="mail", nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)

    client: Mapped[Client] = relationship()


class ApiKey(Base):
    """API-ключ клиента. Хранится только хеш — сам токен показывается один раз,
    в момент создания, и больше нигде не восстановим.

    SHA-256, не scrypt: ключ — случайная строка высокой энтропии, проверяемая
    на каждый запрос, а не пароль, подобранный человеком. Стоимость scrypt
    защищает от перебора низкоэнтропийных паролей и здесь была бы чистым
    замедлением каждого API-вызова без всякой пользы.
    """

    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    #: Первые символы токена — только чтобы отличить ключи в списке панели.
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column()

    client: Mapped[Client] = relationship()


class Mailbox(Base):
    """Локальный адрес, для которого шлюз принимает входящую почту."""

    __tablename__ = "mailbox"
    __table_args__ = (
        Index("ix_mailbox_client_id_address", "client_id", "address"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False, index=True
    )
    address: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Message(Base, RetryableMixin):
    """Письмо любого направления."""

    __tablename__ = "message"
    __table_args__ = (
        # Под запрос воркера отправки: WHERE direction/status ORDER BY next_attempt_at.
        Index("ix_message_outbox", "direction", "status", "next_attempt_at"),
        Index("ix_message_mailbox_id_id", "mailbox_id", "id"),
    )

    PENDING = MessageStatus.QUEUED
    IN_PROGRESS = MessageStatus.SENDING
    DONE = MessageStatus.SENT
    FAILED = MessageStatus.FAILED

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    direction: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=MessageStatus.RECEIVED, index=True
    )

    sender: Mapped[str] = mapped_column(String(320), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    subject: Mapped[str | None] = mapped_column(String(998))
    # Содержимое — зашифровано в БД (при заданном ключе). Адреса и тема
    # намеренно открыты: по ним работают поиск и фильтры (_apply_filters).
    body_text: Mapped[str | None] = mapped_column(EncryptedText)
    body_html: Mapped[str | None] = mapped_column(EncryptedText)

    message_id: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(255))
    thread_key: Mapped[str | None] = mapped_column(String(255), index=True)

    #: Владелец письма (F-54). Прямо на письме, а не через ящик: исходящее
    #: письмо законно существует и без записи `Mailbox` об отправителе, а
    #: удаление ящика (`mailbox_id` → NULL) не должно прятать переписку от
    #: её собственного клиента. NULL — письмо, принятое до регистрации
    #: получателя: его не видит ни один ключ, только веб-панель.
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("client.id", ondelete="SET NULL"), index=True
    )
    mailbox_id: Mapped[int | None] = mapped_column(ForeignKey("mailbox.id", ondelete="SET NULL"))
    external_id: Mapped[str | None] = mapped_column(String(64), index=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)
    sent_at: Mapped[datetime | None] = mapped_column()
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Отчёт о невозможности доставки (RFC 3464), пришедший после успешной
    # отправки. Заполняется у исходящего письма, к которому относится отчёт.
    bounced_at: Mapped[datetime | None] = mapped_column()
    #: Код состояния из отчёта, например `5.1.1`. Первая цифра решает всё:
    #: 5 — адресата не существует, 4 — временная помеха.
    bounce_status: Mapped[str | None] = mapped_column(String(16))
    bounce_diagnostic: Mapped[str | None] = mapped_column(EncryptedText)

    # Подлинность отправителя входящего письма (RFC 8601). Решение о доверии
    # принимает потребитель — шлюз только фиксирует результат проверки.
    spf_result: Mapped[str | None] = mapped_column(String(16))
    dkim_result: Mapped[str | None] = mapped_column(String(16))
    dmarc_result: Mapped[str | None] = mapped_column(String(16), index=True)
    auth_results: Mapped[str | None] = mapped_column(Text)

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="message", cascade="all, delete-orphan", lazy="selectin"
    )
    @property
    def is_outgoing(self) -> bool:
        return self.direction == Direction.OUTGOING


class Attachment(Base):
    __tablename__ = "attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id_fk: Mapped[int] = mapped_column(
        ForeignKey("message.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    filepath: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    message: Mapped[Message] = relationship(back_populates="attachments")


class Upload(Base):
    """Файл, загруженный заранее и ожидающий привязки к исходящему письму."""

    __tablename__ = "upload"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    filepath: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False, index=True)


class AppSetting(Base):
    """Настройка, которую меняет оператор из панели, а не администратор сервера.

    Отдельно от `.env` намеренно: переменные окружения читаются один раз при
    старте, и правка требует перезапуска процесса — то есть обрыва приёма
    почты. Здесь живёт только то, что человек осмысленно меняет на ходу;
    адреса, порты и секреты остаются в окружении.
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class AdminUser(Base):
    """Учётная запись для веб-панели управления.

    Отдельная сущность от токенов API: токен принадлежит программе, учётная
    запись — человеку, и отзываются они независимо.
    """

    __tablename__ = "admin_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    #: Только хеш scrypt — пароль в открытом виде не хранится нигде.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column()
