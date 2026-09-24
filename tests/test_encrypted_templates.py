"""Загрузчик зашифрованных шаблонов панели (используется только в сборке)."""

import pytest
from cryptography.fernet import Fernet
from jinja2 import Environment, TemplateNotFound

from app.web.encrypted_loader import (
    KEY_FILE_NAME,
    EncryptedTemplateLoader,
    deobfuscate_key,
    obfuscate_key,
    read_key,
)


def test_key_obfuscation_roundtrip():
    key = Fernet.generate_key()
    assert deobfuscate_key(obfuscate_key(key)) == key
    assert obfuscate_key(key) != key  # в файле не сам ключ


def test_loader_decrypts_and_renders(tmp_path):
    key = Fernet.generate_key()
    (tmp_path / "page.html.enc").write_bytes(
        Fernet(key).encrypt(b"<h1>{{ title }}</h1>")
    )

    loader = EncryptedTemplateLoader(tmp_path, key)
    env = Environment(loader=loader, autoescape=True)
    assert env.get_template("page.html").render(title="ок") == "<h1>ок</h1>"
    assert loader.list_templates() == ["page.html"]


def test_loader_reads_key_file_like_in_build(tmp_path):
    """Как в сборке: ключ лежит в encryption_key.dat рядом с шаблонами."""
    key = Fernet.generate_key()
    (tmp_path / KEY_FILE_NAME).write_bytes(obfuscate_key(key))
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "base.html.enc").write_bytes(Fernet(key).encrypt(b"ok"))

    loader = EncryptedTemplateLoader(templates, read_key(tmp_path))
    source, _, uptodate = loader.get_source(None, "base.html")
    assert source == "ok"
    assert uptodate()


def test_missing_and_corrupt_templates(tmp_path):
    key = Fernet.generate_key()
    loader = EncryptedTemplateLoader(tmp_path, key)
    with pytest.raises(TemplateNotFound):
        loader.get_source(None, "нет.html")

    (tmp_path / "битый.html.enc").write_bytes(b"not a fernet token")
    with pytest.raises(TemplateNotFound):
        loader.get_source(None, "битый.html")
