"""Настройки, которые оператор меняет из панели без перезапуска сервиса.

Каждая настройка описана здесь одной записью: тип, значение по умолчанию,
допустимый диапазон. Разбор и проверка живут рядом с описанием, поэтому
«где-то забыли проверить» невозможно, а панель не знает про типы вовсе.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..api.errors import ValidationError
from ..models import AppSetting

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Definition:
    """Описание одной настройки."""

    key: str
    title: str
    default: int | bool
    kind: type
    minimum: int = 0
    maximum: int = 0
    unit: str = ""
    hint: str = ""

    def parse(self, raw: str) -> int | bool:
        if self.kind is bool:
            return raw.strip().lower() in {"1", "true", "on", "yes", "да"}

        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{self.title}: нужно целое число") from exc

        if not (self.minimum <= value <= self.maximum):
            raise ValidationError(
                f"{self.title}: допустимо от {self.minimum} до {self.maximum} {self.unit}".strip()
            )
        return value


BACKUP_ENABLED = Definition(
    key="backup.enabled",
    title="Резервное копирование",
    default=True,
    kind=bool,
    hint="Снимок базы данных по расписанию.",
)

BACKUP_INTERVAL_HOURS = Definition(
    key="backup.interval_hours",
    title="Периодичность",
    default=24,
    kind=int,
    minimum=1,
    maximum=24 * 30,
    unit="ч",
    hint="Через сколько часов делать очередной снимок.",
)

BACKUP_MAX_TOTAL_MB = Definition(
    key="backup.max_total_mb",
    title="Размер резервной копии",
    default=1024,
    kind=int,
    minimum=1,
    maximum=1024 * 1024,
    unit="МБ",
    hint=(
        "Сколько места суммарно занимают снимки. При превышении удаляются самые "
        "старые — свежий снимок никогда не вытесняется, иначе лимит оставил бы "
        "систему вообще без копий."
    ),
)

#: Всё, что показывает панель. Порядок соблюдается при выводе.
DEFINITIONS: tuple[Definition, ...] = (
    BACKUP_ENABLED,
    BACKUP_INTERVAL_HOURS,
    BACKUP_MAX_TOTAL_MB,
)

_BY_KEY = {item.key: item for item in DEFINITIONS}


def get(db: Session, definition: Definition) -> int | bool:
    """Текущее значение либо значение по умолчанию.

    Испорченную запись не считаем фатальной: настройка возвращается к
    умолчанию, а сервис продолжает работать. Уронить резервное копирование
    из-за нечитаемого числа было бы хуже, чем сделать копию по расписанию по
    умолчанию.
    """
    row = db.get(AppSetting, definition.key)
    if row is None:
        return definition.default
    try:
        return definition.parse(row.value)
    except ValidationError:
        logger.warning(
            "Настройка %s содержит недопустимое значение %r — взято умолчание %r",
            definition.key, row.value, definition.default,
        )
        return definition.default


def set_value(db: Session, definition: Definition, raw: str) -> int | bool:
    """Проверяет и сохраняет значение. Бросает ValidationError при негодном."""
    value = definition.parse(raw)
    row = db.get(AppSetting, definition.key)
    if row is None:
        row = AppSetting(key=definition.key, value=str(value))
        db.add(row)
    else:
        row.value = str(value)
    db.flush()
    return value


def all_values(db: Session) -> dict[str, int | bool]:
    """Значения всех известных настроек — для вывода формы одним запросом."""
    stored = {row.key: row for row in db.scalars(select(AppSetting))}
    result: dict[str, int | bool] = {}
    for definition in DEFINITIONS:
        row = stored.get(definition.key)
        try:
            result[definition.key] = definition.parse(row.value) if row else definition.default
        except ValidationError:
            result[definition.key] = definition.default
    return result


def apply_form(db: Session, form: dict[str, str]) -> None:
    """Сохраняет присланную панелью форму целиком.

    Флажок, который сняли, в теле формы не приходит вовсе — поэтому булевы
    настройки берутся из самого факта присутствия ключа, а не из значения.
    """
    for definition in DEFINITIONS:
        if definition.kind is bool:
            set_value(db, definition, "true" if definition.key in form else "false")
        elif definition.key in form:
            set_value(db, definition, form[definition.key])


def by_key(key: str) -> Definition | None:
    return _BY_KEY.get(key)
