"""Схемы запросов и ответов — единый источник формы данных на границе API.

Ответы собираются только отсюда: ни один роут не конструирует словарь вручную.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from .constants import Direction, MessageStatus

_ORM = ConfigDict(from_attributes=True)

ItemT = TypeVar("ItemT")


@dataclass(slots=True)
class PageResult:
    """Страница, как её возвращает сервис: модели, а не схемы.

    Сервис не должен знать о форме ответа — за неё отвечает `Page`.
    """

    items: list
    total: int
    limit: int
    offset: int


class Page(BaseModel, Generic[ItemT]):
    """Страница коллекции в ответе.

    Голый массив не годится: по нему нельзя понять, есть ли ещё записи, и
    клиенту приходится гадать по длине ответа.
    """

    model_config = _ORM

    items: list[ItemT]
    total: int = Field(..., description="Всего записей, удовлетворяющих фильтру")
    limit: int
    offset: int


class AttachmentOut(BaseModel):
    model_config = _ORM

    id: int
    filename: str
    content_type: str | None = None
    size: int


class MessageOut(BaseModel):
    model_config = _ORM

    id: int
    direction: Direction
    status: MessageStatus
    sender: str
    recipient: str
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    message_id: str | None = None
    in_reply_to: str | None = None
    thread_key: str | None = None
    external_id: str | None = None
    is_read: bool
    attempts: int
    last_error: str | None = None
    bounced_at: datetime | None = Field(
        None, description="Когда пришёл отчёт о невозможности доставки"
    )
    bounce_status: str | None = Field(
        None, description="Код из отчёта, например `5.1.1`; 5.x.x — адресата не существует"
    )
    bounce_diagnostic: str | None = None
    spf_result: str | None = Field(None, description="Результат проверки SPF для входящего письма")
    dkim_result: str | None = None
    dmarc_result: str | None = None
    created_at: datetime
    sent_at: datetime | None = None
    attachments: list[AttachmentOut] = []


class MessageFilter(BaseModel):
    """Параметры выборки писем (F-32). Валидация — здесь, не в роуте."""

    direction: Direction | None = None
    status: MessageStatus | None = None
    recipient: str | None = None
    mailbox: str | None = Field(None, description="Фильтр по локальному адресу")
    unread_only: bool = False
    dmarc_result: str | None = Field(
        None, description="Фильтр по результату DMARC, например `fail`"
    )
    search: str | None = Field(
        None,
        max_length=200,
        description="Подстрока в теме, адресе отправителя или получателя",
    )
    since_id: int | None = Field(
        None,
        ge=0,
        description="Курсор опроса: вернуть письма с id строго больше, по возрастанию. "
                    "`0` — с самого начала; без параметра — последние письма по убыванию",
    )
    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)


class PageParams(BaseModel):
    """Постраничный вывод для коллекций без собственных фильтров."""

    limit: int = Field(50, ge=1, le=200)
    offset: int = Field(0, ge=0)


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")  # принимаем только известные поля

    sender: EmailStr = Field(..., description="Адрес отправителя (должен принадлежать домену шлюза)")
    recipient: EmailStr
    subject: str = Field("", max_length=998)
    body_text: str = ""
    body_html: str | None = None
    in_reply_to: str | None = Field(None, max_length=255, description="Message-ID письма, на которое отвечаем")
    external_id: str | None = Field(None, max_length=64, description="Идентификатор объекта во внешней системе")
    attachment_ids: list[int] = Field(
        default_factory=list, max_length=50, description="ID ранее загруженных файлов"
    )

    @field_validator("subject")
    @classmethod
    def _no_header_injection(cls, value: str) -> str:
        """Перевод строки в теме позволил бы дописать произвольные заголовки письма."""
        return value.replace("\r", " ").replace("\n", " ")

    @field_validator("in_reply_to")
    @classmethod
    def _clean_reference(cls, value: str | None) -> str | None:
        """То же для `In-Reply-To`, но отказом, а не заменой.

        Значение уходит в заголовок как есть. Перевод строки в нём Python
        и так не пропустит, но сорвётся это уже в воркере — письмо примется
        с `202`, пять раз попробует отправиться и осядет в `failed`. Ошибка
        ввода должна называться ошибкой ввода сразу, на границе API.
        """
        if value is None:
            return None
        if "\r" in value or "\n" in value:
            raise ValueError("не может содержать перевод строки")
        return value.strip() or None


class SendResponse(BaseModel):
    id: int
    status: MessageStatus
    message_id: str


class MessagePatch(BaseModel):
    """Изменяемая часть письма. Всё остальное задаётся при постановке в очередь."""

    model_config = ConfigDict(extra="forbid")

    is_read: bool | None = None


class MailboxIn(BaseModel):
    """Тело PUT /mailboxes/{address}: адрес задаётся путём, а не телом."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(None, max_length=255)
    external_id: str | None = Field(None, max_length=64)
    is_active: bool = True


class MailboxOut(MailboxIn):
    model_config = _ORM

    id: int
    address: EmailStr
    created_at: datetime


class UploadOut(BaseModel):
    model_config = _ORM

    id: int
    filename: str
    size: int


class HealthOut(BaseModel):
    status: str
    version: str
    domain: str
    outbound_mode: str
    queued: int
    sending: int
    failed: int
    last_message_id: int = Field(
        ..., description="Наибольший id письма — опорная точка для опроса по since_id"
    )


class ReadyOut(BaseModel):
    ready: bool
    database: bool
    smtp: bool
