"""Мелкие чистые помощники без зависимостей от слоёв приложения."""

import re
import uuid
from html import unescape
from pathlib import Path

from .config import settings
from .constants import WINDOWS_RESERVED_NAMES

_UNSAFE = re.compile(r"[^A-Za-z0-9А-Яа-яЁё @._-]")
_MAX_NAME_LEN = 120


def sanitize_filename(filename: str) -> str:
    """Безопасное имя файла: без разделителей путей, служебных и зарезервированных имён.

    Учитывает особенности Windows (сервис работает и под ним): имена вида
    `CON`, `NUL`, `COM1` недопустимы даже с расширением, а хвостовые точки и
    пробелы молча отбрасываются файловой системой.
    """
    name = Path(filename.replace("\\", "/")).name
    name = _UNSAFE.sub("_", name).strip(" .")
    if not name:
        return "unnamed"

    stem, dot, suffix = name.partition(".")
    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    # Оставляем место под уникальный префикс, не выходя за лимит пути.
    name = (stem[:_MAX_NAME_LEN] + dot + suffix[:_MAX_NAME_LEN]).strip(" .")
    return name or "unnamed"


def unique_filename(filename: str) -> str:
    """Имя с коротким уникальным префиксом — исключает перезапись одноимённых
    вложений внутри одного письма."""
    return f"{uuid.uuid4().hex[:12]}_{sanitize_filename(filename)}"


def new_message_id() -> str:
    return f"<{uuid.uuid4()}@{settings.domain}>"


def domain_of(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower()


def html_to_text(html: str) -> str:
    """Грубое приведение HTML к тексту — только для случая, когда plain-части нет."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", unescape(text)).strip()


def is_within(path: Path, root: Path) -> bool:
    """Проверка, что путь не выводит за пределы каталога (защита в глубину)."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False
