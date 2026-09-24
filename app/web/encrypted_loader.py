"""Jinja2-загрузчик зашифрованных шаблонов — для собранного приложения.

В дев-режиме шаблоны лежат обычными `.html` и этот модуль не используется.
В сборке (`build.py`) каждый `.html` зашифрован Fernet в `.enc`, оригинал
удалён; здесь `.enc` прозрачно расшифровывается при первом рендеринге и
кешируется в памяти.

Ключ лежит рядом, в `encryption_key.dat`, XOR-обфусцированным. Это защита
шаблонов от беглого просмотра и правки в поставленной сборке (охрана
внешнего вида продукта), а не криптография: у кого есть каталог сборки,
у того есть и ключ. Секреты (.env, БД, лицензия) этим механизмом не
защищаются и в сборку не входят.
"""

import base64
import os
from pathlib import Path

from jinja2 import BaseLoader, TemplateNotFound

KEY_FILE_NAME = "encryption_key.dat"
TEMPLATE_SUFFIX = ".enc"

#: Маска обфускации ключа. Меняется синхронно с `build.py` (он импортирует её
#: отсюда, так что рассинхронизация возможна только при правке сборки руками).
XOR_MASK = b"MgTemplateMask_Somnium_2026" * 3


def obfuscate_key(key: bytes) -> bytes:
    """key → содержимое `encryption_key.dat` (XOR + base64). Обратима сама себе."""
    masked = bytes(b ^ m for b, m in zip(key, XOR_MASK, strict=False))
    return base64.b64encode(masked)


def deobfuscate_key(data: bytes) -> bytes:
    masked = base64.b64decode(data.strip())
    return bytes(b ^ m for b, m in zip(masked, XOR_MASK, strict=False))


def read_key(directory: Path | None = None) -> bytes:
    """Ключ из `encryption_key.dat` рядом с этим модулем (кладётся сборкой)."""
    key_file = (directory or Path(__file__).parent) / KEY_FILE_NAME
    return deobfuscate_key(key_file.read_bytes())


class EncryptedTemplateLoader(BaseLoader):
    """Читает `<имя>.html.enc` вместо `<имя>.html` и расшифровывает на лету."""

    def __init__(self, searchpath: Path, key: bytes | None = None) -> None:
        from cryptography.fernet import Fernet

        self.searchpath = Path(searchpath)
        self._cipher = Fernet(key if key is not None else read_key())
        self._cache: dict[str, tuple[str, str, object]] = {}

    def get_source(self, environment, template: str):
        cached = self._cache.get(template)
        if cached is not None:
            return cached

        path = self.searchpath / (template + TEMPLATE_SUFFIX)
        if not path.is_file():
            raise TemplateNotFound(template)
        try:
            source = self._cipher.decrypt(path.read_bytes()).decode("utf-8")
        except Exception as exc:  # битый файл или чужой ключ
            raise TemplateNotFound(
                template, f"Не удалось расшифровать шаблон {path}: {exc}"
            ) from exc

        mtime = os.path.getmtime(path)

        def uptodate() -> bool:
            try:
                return os.path.getmtime(path) == mtime
            except OSError:
                return False

        result = (source, str(path), uptodate)
        self._cache[template] = result
        return result

    def list_templates(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.searchpath)).replace(os.sep, "/")[: -len(TEMPLATE_SUFFIX)]
            for p in self.searchpath.rglob(f"*{TEMPLATE_SUFFIX}")
        )
