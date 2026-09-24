"""Лицензионное соглашение установщика.

Соглашение генерируется, а не лежит готовым текстом: реквизиты берутся из
`docs/generate/config.py`, версия — из `app/__init__.py`. Проверяется именно
эта связь. Разошедшийся ИИН в договоре и в установщике заметит заказчик, а
переименованный `EXECUTOR` уронит сборку установщика молча — на шаге, до
которого доходят раз в релиз.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import __version__  # noqa: E402
from docs.generate.config import EXECUTOR, PRODUCT  # noqa: E402
from installer.make_license import license_text, version_defines  # noqa: E402


@pytest.fixture(scope="module")
def text() -> str:
    return license_text()


def test_requisites_come_from_single_source(text):
    assert EXECUTOR["name"] in text
    assert EXECUTOR["iin"] in text
    assert PRODUCT["name"] in text


def test_states_what_happens_without_license(text):
    """Главное расхождение, которого нельзя допустить: соглашение обязано
    описывать то, что программа действительно делает (см. licensing.py)."""
    assert "не принимает и не отправляет" in text
    assert "возобновляются автоматически" in text


def test_covers_terms_a_dispute_would_turn_on(text):
    for required in (
        "неисключительное",       # вид предоставляемого права
        "Республики Казахстан",   # территория и применимое право
        "техническим средством защиты",
        "как есть",               # отказ от расширенных гарантий
        "РЕКВИЗИТЫ",
    ):
        assert required in text, required


def test_version_defines_match_the_program(text):
    defines = version_defines()
    assert f'#define AppVersion "{__version__}"' in defines
    assert EXECUTOR["name"] in defines
