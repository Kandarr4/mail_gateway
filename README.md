# Mail Gateway

[![CI](https://github.com/Kandarr4/mail_gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/Kandarr4/mail_gateway/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Tests](https://img.shields.io/badge/tests-346-brightgreen)
![License](https://img.shields.io/badge/license-proprietary-lightgrey)

🇷🇺 [Русская версия](README.ru.md)

A self-contained mail gateway for Windows. It receives mail over SMTP, stores it
in its own database, and hands it to your application over an HTTP API. Sending
is a `POST`; receiving is a cursor-based poll. No external mail provider is
involved, and the gateway knows nothing about the system that consumes it.

It ships as a Windows service with a tray application and a web panel, installed
by a single Inno Setup installer.

```
                    ┌──────────────────────┐
  internet ──SMTP──▶│  aiosmtpd listener   │──▶ SPF / DKIM / DMARC
                    │       :25            │        verdict
                    └──────────┬───────────┘
                               ▼
                    ┌──────────────────────┐
                    │  SQLite + SQLAlchemy │  bodies and attachments
                    │  bodies encrypted    │  encrypted at rest
                    └──────────┬───────────┘
                               ▼
  your app ◀──HTTP──│  FastAPI :8025  │──▶ outbox worker ──DKIM──▶ internet
       poll by cursor   /admin panel        retry with backoff
```

## What is interesting in here

- **Encryption at rest with a deliberate exception.** Message bodies and
  attachments are encrypted with Fernet; envelope addresses and subjects are
  left in clear text on purpose, because the panel and the API search on them.
  The tradeoff is documented rather than hidden.
- **Inbound authentication as a policy, not a switch.** SPF, DKIM and DMARC are
  verified on every message, and the result drives one of three policies:
  `annotate` (record the verdict), `enforce` (reject on failure) or `off`. The
  verdict is stored with the message, so a delivery dispute can be reconstructed
  afterwards.
- **Per-domain DKIM signing.** Each tenant domain carries its own selector and
  private key, so one gateway signs for several domains correctly.
- **Multi-tenancy with hashed keys.** API keys are stored as SHA-256 digests,
  never in clear text; every message carries the client it belongs to, and
  tenants cannot see each other's mail.
- **Delivery that survives the network.** The outbox worker retries with
  exponential backoff and jitter, caps the exponent instead of computing
  `2**49`, parses bounces, and gives up only after a configured number of
  attempts.
- **Licensing that does not hold the service hostage.** An RSA-PSS signed
  licence file gates the *mail path* only: without a valid licence the service
  still starts, the panel still opens and the queue simply pauses, while SMTP
  answers `451`. Losing a licence should not lose a customer's configuration.
- **Schema migrations under a cross-process lock.** The service and the tray
  application start at the same time and both run Alembic; without a file lock
  the second process died on `duplicate column name`. The lock is the fix, and
  the reason is written down next to it.
- **Templates encrypted in the shipped build.** `build.py` encrypts the Jinja
  templates into `.enc` files and installs a custom loader, so the delivered
  package does not carry readable HTML.
- **Strict linting for a security-adjacent product.** `ruff` runs with
  `bandit` (`S`), blind-except (`BLE`), naming (`N`) and async (`ASYNC`) rules
  enabled. Every suppression names the rule and the reason.

## Quick start

```bat
copy .env.example .env
:: edit .env — at minimum MG_DOMAIN and MG_API_TOKENS
run.bat
```

Or by hand:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe run.py
```

The schema is applied automatically on start. Interactive API docs live at
`http://<host>:8025/docs`, the admin panel at `http://<host>:8025/admin`.

## Layout

| Path | Contents |
|------|----------|
| `app/api/` | HTTP API: routes, dependencies, error mapping |
| `app/web/` | Admin panel, templates and their encrypted loader |
| `app/services/` | Domain logic: receiving, delivery, DKIM, licensing, keys, mailboxes |
| `app/workers/` | Background tasks: outbox, maintenance, backups |
| `app/models.py`, `app/schemas.py` | SQLAlchemy tables and Pydantic contracts |
| `app/config.py` | Every setting, read from `.env` — the single source of values |
| `tray/` | Tray icon, settings, first-run and licence dialogs (PySide6) |
| `installer/` | Inno Setup script and the EULA generator |
| `migrations/` | Alembic migrations |
| `tests/` | 346 tests (`pytest`) |
| `docs/` | Full manual, specification and deployment guide (Russian) |

Entry points: `run.py` (console), `service.py` (Windows service),
`mailgateway.py` (single application with the tray icon), `manage.py`
(maintenance CLI), `build.py` (build the delivery package).

## Configuration

Settings are environment variables prefixed `MG_`, read from `.env` in the
project root. `.env.example` lists every key with comments; missing keys and
secrets are generated on first run.

These never enter the repository and stay on the machine:

- `.env` — working settings, including API tokens;
- `data/keys/encryption.key` — the key for message bodies and attachments;
- `data/dkim/*.private` — DKIM private keys;
- `data/` as a whole — attachments, logs, backups;
- `*.lic` — issued licences;
- `mail_gateway.db` — the mail database.

## Tests

```bat
test.bat
```

Runs the unit tests (`pytest`) and the end-to-end `smoke_test.py`, which starts
the gateway on a temporary database, sends a message over SMTP and exercises the
API.

```bat
.venv\Scripts\python.exe -m ruff check .
```

## Build

```bat
.venv\Scripts\python.exe build.py
```

Produces a PyInstaller onedir package, encrypts the HTML templates into `.enc`,
and prepares the Inno Setup installer. The EULA and installer version are
generated by `installer/make_license.py`; the `.docx` documents by
`make_docs.bat`. None of the generated artefacts are stored in the repository.

## Documentation

The detailed documentation is in Russian, since that is the language of the
product and its customers.

- [docs/README.md](docs/README.md) — full manual: API, internals, operations,
  admin panel, tray icon, licensing, build, inbound security, production
  deployment.
- [docs/ТЗ.md](docs/ТЗ.md) — specification and requirement list.

## License

Proprietary, source-available: published to demonstrate the author's work. You
may read it and run it to evaluate it; production use, modification and
redistribution require written permission. See [LICENSE](LICENSE) for the full
terms in English and Russian.

Copyright © 2026 Valentin Bortnikov (ИП Somnium) — https://somnium.kz
