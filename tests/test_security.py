"""Безопасность: имена файлов, границы хранилища, лимиты, подпись вебхука."""

import pytest

from app.constants import WINDOWS_RESERVED_NAMES
from app.services import storage
from app.services.rate_limit import RateLimiter
from app.utils import is_within, sanitize_filename, unique_filename
from tests.conftest import AUTH


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("../../etc/passwd", "/"),
        (r"..\..\windows\system32\cmd.exe", "\\"),
        ("C:/Windows/win.ini", ":"),
        ("отчёт/../../secret.txt", "/"),
    ],
)
def test_sanitize_strips_path_separators(raw, forbidden):
    result = sanitize_filename(raw)
    assert forbidden not in result
    assert ".." not in result


@pytest.mark.parametrize("reserved", sorted(WINDOWS_RESERVED_NAMES)[:6])
def test_windows_reserved_names_are_escaped(reserved):
    """`CON.txt` под Windows — устройство, а не файл: запись повисает или падает."""
    for candidate in (reserved, f"{reserved}.txt", reserved.lower()):
        assert sanitize_filename(candidate).upper().partition(".")[0] not in WINDOWS_RESERVED_NAMES


def test_sanitize_never_returns_empty():
    for raw in ("", "...", "   ", "/", "\\", "..", "***"):
        assert sanitize_filename(raw)


def test_unique_filename_differs_each_call():
    assert unique_filename("a.txt") != unique_filename("a.txt")


def test_storage_refuses_paths_outside_root(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    assert storage.readable(str(outside)) is None
    assert not is_within(outside, storage.root())


def test_storage_keeps_files_inside_root():
    path = storage.store(b"data", "файл.txt", "тест")
    assert is_within(path, storage.root())
    assert storage.readable(str(path)) == path


def test_upload_over_limit_rejected_and_leaves_no_file(client):
    """Лимит в тестах — 1 МБ. Недописанный файл не должен оставаться на диске."""
    before = {p for p in storage.root().rglob("*") if p.is_file()}

    payload = b"x" * (2 * 1024 * 1024)
    response = client.post(
        "/api/v1/uploads", headers=AUTH, files={"file": ("big.bin", payload, "application/octet-stream")}
    )

    assert response.status_code == 413
    assert response.json()["type"] == "/errors/payload-too-large"
    assert {p for p in storage.root().rglob("*") if p.is_file()} == before


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(3)
    assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]
    assert limiter.allow("другой ключ") is True


def test_rate_limiter_disabled_when_zero():
    limiter = RateLimiter(0)
    assert all(limiter.allow("k") for _ in range(1000))


def test_rate_limiter_evicts_keys_under_pressure():
    limiter = RateLimiter(5)
    limiter.MAX_KEYS = 10
    for index in range(50):
        limiter.allow(f"key-{index}")
    assert len(limiter._hits) <= limiter.MAX_KEYS
