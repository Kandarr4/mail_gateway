"""Диалог настроек шлюза (только чтение).

Первым делом — записи DNS: без них почта не ходит, а оператор о них не
догадывается. Показано, что вписать в панель регистратора, и (по кнопке
«Проверить») что из этого уже настроено. Дальше идут параметры `.env`,
разложенные по смысловым разделам, ключи и лицензия.

Параметры перечисляются программно, а не списком: новая настройка появляется
в окне сама. Раздел ей назначает `_SECTIONS`; не попавшие ни в один раздел
собираются в «Прочее» — так забытая настройка видна, а не потеряна.

Секрет открывается после проверки логина и пароля администратора панели — той
же `admin_users.authenticate`, что и у панели (scrypt). Одной успешной
проверки хватает на весь сеанс диалога; при закрытии окна всё снова под
маской.

Редактирование `.env` из GUI — отдельная задача; здесь только просмотр и
копирование. Значения берутся из загруженного процессом синглтона настроек
(после мастера первого запуска он уже соответствует .env, см. tray/app.py).
"""

import logging
from enum import Enum

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .tray_icon import COLORS

logger = logging.getLogger(__name__)

_INPUT_BG = "#3e5269"
_MASK = "••••••••"

#: Поля Settings, значение которых — секрет и показывается только после входа.
_SECRET_FIELDS = frozenset({"api_tokens", "relay_password"})

#: Разделы параметров и их состав. Порядок разделов — порядок в окне: сверху
#: то, что настраивают при развёртывании, снизу то, что трогают редко.
_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Домен и приём почты", (
        "domain", "helo_name", "smtp_host", "smtp_port", "require_known_mailbox",
        "max_message_mb", "smtp_rate_limit_per_minute", "inbound_auth",
        "tls_cert_file", "tls_key_file", "proxy_protocol",
    )),
    ("Отправка", (
        "outbound_mode", "relay_host", "relay_port", "relay_username", "relay_password",
        "relay_use_tls", "relay_verify_tls", "dkim_private_key_file", "dkim_selector",
        "send_max_attempts", "send_retry_base_seconds", "send_retry_max_seconds",
        "send_concurrency",
    )),
    ("API и панель управления", (
        "api_host", "api_port", "api_tokens", "rate_limit_per_minute",
        "web_enabled", "web_secure_cookies", "web_session_hours",
        "web_login_attempts_per_minute",
    )),
    ("Хранение", (
        "database_url", "attachment_dir", "backup_dir", "encryption_key_file",
        "max_attachment_mb", "upload_ttl_hours",
    )),
    ("Журналы", ("log_level", "log_dir", "log_json", "log_retention_days", "debug")),
)

#: Состояния проверки DNS: подпись и цвет строки.
_DNS_STATES = {
    "ok": ("✓ настроено", "ok"),
    "missing": ("✗ не добавлено", "danger"),
    "different": ("⚠ отличается", "warn"),
    "unknown": ("? не проверено", "muted"),
}

#: Порог неудачных попыток на сеанс диалога: дальше просмотр секретов
#: блокируется до повторного открытия окна. Панель имеет свой rate-limiter,
#: здесь достаточно простого счётчика.
_MAX_FAILURES = 5

_STYLESHEET = f"""
    QDialog {{ background-color: {COLORS['bg']}; }}
    QScrollArea {{ border: none; background: transparent; }}
    QWidget#Body {{ background-color: {COLORS['bg']}; }}
    QLabel#Header {{
        color: {COLORS['teal']};
        font-family: 'Segoe UI', sans-serif;
        font-size: 14pt;
        font-weight: bold;
    }}
    QLabel#Section {{
        color: {COLORS['teal']};
        font-family: 'Segoe UI', sans-serif;
        font-size: 11pt;
        font-weight: 600;
    }}
    QLabel#Key {{ color: {COLORS['muted']}; font-size: 9pt; }}
    QLabel#Value {{ color: {COLORS['text']}; font-size: 9pt; font-weight: 600; }}
    QLabel#Note {{ color: {COLORS['muted']}; font-size: 9pt; }}
    QLineEdit {{
        background-color: {_INPUT_BG};
        border: 1px solid {COLORS['border']};
        color: {COLORS['text']};
        border-radius: 5px;
        padding: 4px 8px;
        font-family: 'Consolas', monospace;
        font-size: 9pt;
    }}
    QPushButton#Tool {{
        background-color: transparent;
        border: 1px solid {COLORS['border']};
        border-radius: 5px;
        color: {COLORS['muted']};
        padding: 4px 10px;
        font-size: 9pt;
    }}
    QPushButton#Tool:hover {{ color: {COLORS['text']}; border-color: {COLORS['muted']}; }}
    QPushButton#Tool:disabled {{ color: {COLORS['border']}; }}
    QPushButton#Close {{
        background-color: {COLORS['teal']};
        border: none; border-radius: 5px; color: white;
        padding: 8px 22px; font-size: 10pt; font-weight: 600;
    }}
    QPushButton#Close:hover {{ background-color: {COLORS['teal_dark']}; }}
"""


def _format(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _is_secret(name: str, value) -> bool:
    if name in _SECRET_FIELDS:
        return True
    # URL БД секретен только когда несёт логин и пароль (postgresql://user:pass@…).
    return name == "database_url" and "@" in str(value)


class SettingsDialog(QDialog):
    def __init__(self, api_port: int, parent=None) -> None:
        super().__init__(parent)
        self.api_port = api_port
        self._authed = False
        self._failures = 0

        self.setWindowTitle("Mail Gateway — Настройки")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(_STYLESHEET)
        self.setMinimumSize(620, 640)

        self._build()

    # --- Каркас --------------------------------------------------------------- #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        header = QLabel("⚙️ Настройки шлюза")
        header.setObjectName("Header")
        outer.addWidget(header)

        note = QLabel(
            "Просмотр только для чтения. Секреты открываются после входа "
            "администратора панели. Правка — в файле .env рядом с программой."
        )
        note.setObjectName("Note")
        note.setWordWrap(True)
        outer.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName("Body")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(4, 4, 12, 4)
        self._body.setSpacing(4)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        self._fill_dns()
        self._fill_settings()
        self._fill_keys()
        self._fill_license()
        self._body.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("Закрыть")
        close_btn.setObjectName("Close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        outer.addLayout(buttons)

    def _section(self, title: str) -> None:
        label = QLabel(title)
        label.setObjectName("Section")
        self._body.addSpacing(8)
        self._body.addWidget(label)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {COLORS['border']};")
        self._body.addWidget(line)

    # --- Наполнение ----------------------------------------------------------- #

    def _fill_settings(self) -> None:
        from app.config import settings

        fields = list(type(settings).model_fields)
        shown: set[str] = set()

        for title, names in _SECTIONS:
            present = [name for name in names if name in fields]
            if not present:
                continue
            self._section(title)
            for name in present:
                self._add_setting(name, getattr(settings, name))
                shown.add(name)

        # Настройка, не попавшая ни в один раздел (новая — раздел ей ещё не
        # назначили), обязана быть видна: иначе окно молча врёт о составе .env.
        forgotten = [name for name in fields if name not in shown]
        if forgotten:
            self._section("Прочее")
            for name in forgotten:
                self._add_setting(name, getattr(settings, name))

    def _add_setting(self, name: str, value) -> None:
        env_name = "MG_" + name.upper()
        if _is_secret(name, value):
            self._body.addWidget(_SecretRow(self, env_name, lambda v=value: _format(v)))
        else:
            self._body.addWidget(_plain_row(env_name, _format(value)))

    # --- Записи DNS ------------------------------------------------------------ #

    def _fill_dns(self) -> None:
        """Что вписать в панель регистратора — первым делом в окне.

        Значения считаются локально и появляются сразу; сверка с DNS идёт по
        кнопке и в отдельном потоке: шесть сетевых запросов в UI-потоке
        подвесили бы окно на несколько секунд.
        """
        from app.config import settings
        from app.services import dns_setup

        self._section("Записи DNS")

        note = QLabel(
            f"Домен: {settings.domain}. Поле «Хост» в панели регистратора заполняется "
            "относительно зоны: «@» — сам домен. Без этих записей почта не ходит или "
            "уходит в спам."
        )
        note.setObjectName("Note")
        note.setWordWrap(True)
        self._body.addWidget(note)

        self._dns_rows: list[_DnsRow] = []
        try:
            records = dns_setup.required_records(settings.domain)
        except Exception:  # окно живёт и без справки по DNS
            logger.exception("Не удалось собрать список записей DNS")
            self._body.addWidget(_plain_row("Записи", "недоступны"))
            return

        for record in records:
            row = _DnsRow(record)
            self._dns_rows.append(row)
            self._body.addWidget(row)

        self._dns_check_button = QPushButton("Проверить записи в DNS")
        self._dns_check_button.setObjectName("Tool")
        self._dns_check_button.setCursor(Qt.PointingHandCursor)
        self._dns_check_button.clicked.connect(self._check_dns)
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(8, 2, 8, 6)
        layout.addWidget(self._dns_check_button)
        layout.addStretch()
        self._body.addWidget(holder)

    def _check_dns(self) -> None:
        self._dns_check_button.setEnabled(False)
        self._dns_check_button.setText("Проверяю…")
        self._dns_worker = _DnsCheckWorker([row.record for row in self._dns_rows])
        self._dns_worker.done.connect(self._apply_dns_check)
        self._dns_worker.start()

    def _apply_dns_check(self, results: object) -> None:
        for row, checked in zip(self._dns_rows, results, strict=False):
            row.apply(checked)
        self._dns_check_button.setEnabled(True)
        self._dns_check_button.setText("Проверить записи в DNS")

    def _fill_keys(self) -> None:
        from app.config import settings

        self._section("Ключи (содержимое файлов)")
        added = False
        if settings.dkim_key_path is not None:
            self._body.addWidget(
                _SecretRow(self, "Ключ DKIM", lambda: _read_file(settings.dkim_key_path))
            )
            added = True
        if settings.encryption_key_path is not None:
            self._body.addWidget(
                _SecretRow(
                    self, "Ключ шифрования", lambda: _read_file(settings.encryption_key_path)
                )
            )
            added = True
        if not added:
            self._body.addWidget(_plain_row("Ключи", "не настроены"))

    def _fill_license(self) -> None:
        self._section("Лицензия")
        try:
            from app.services import licensing

            status = licensing.status(use_cache=False)
        except Exception:  # трей живёт и без доступа к файлам
            logger.exception("Не удалось получить статус лицензии")
            self._body.addWidget(_plain_row("Состояние", "недоступно"))
            return

        state = "действительна" if status.valid else status.reason
        self._body.addWidget(_plain_row("Состояние", state))
        if status.data:
            self._body.addWidget(_plain_row("Лицензиат", status.company))
            self._body.addWidget(
                _plain_row("Действует до", str(status.data.get("expiry_date", ""))[:10])
            )
            days = status.days_remaining
            if days is not None:
                self._body.addWidget(_plain_row("Осталось", f"{days} дн."))
            if status.max_domains is not None:
                self._body.addWidget(_plain_row("Доменов по лицензии", str(status.max_domains)))

    # --- Авторизация ---------------------------------------------------------- #

    def ensure_authed(self) -> bool:
        """True — секреты можно показывать. Одна успешная проверка на сеанс."""
        if self._authed:
            return True
        if self._failures >= _MAX_FAILURES:
            QMessageBox.warning(
                self, "Доступ",
                "Слишком много неудачных попыток. Закройте и откройте окно снова.",
            )
            return False

        dialog = _AuthDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return False
        login, password = dialog.credentials()

        from app.db import session_scope
        from app.services import admin_users

        try:
            with session_scope() as db:
                # authenticate обновляет last_login_at и при необходимости
                # пересчитывает хеш — это штатно и не открывает сессию панели.
                user = admin_users.authenticate(db, login, password)
        except Exception:  # БД недоступна и т.п.
            logger.exception("Проверка учётных данных из диалога настроек не удалась")
            QMessageBox.critical(self, "Доступ", "Не удалось проверить учётные данные.")
            return False

        if user is None:
            self._failures += 1
            QMessageBox.warning(self, "Доступ", "Неверный логин или пароль.")
            return False

        self._authed = True
        return True


def _read_file(path) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _plain_row(key: str, value: str) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(8, 3, 8, 3)
    name = QLabel(key)
    name.setObjectName("Key")
    data = QLabel(value)
    data.setObjectName("Value")
    data.setTextInteractionFlags(Qt.TextSelectableByMouse)
    data.setWordWrap(True)
    layout.addWidget(name)
    layout.addStretch()
    layout.addWidget(data)
    return row


class _DnsCheckWorker(QThread):
    """Сверка записей с DNS в фоне: шесть запросов с таймаутом — не в UI."""

    done = Signal(object)

    def __init__(self, records: list) -> None:
        super().__init__()
        self._records = records

    def run(self) -> None:  # контракт QThread
        from app.services import dns_setup

        try:
            self.done.emit(dns_setup.check(self._records))
        except Exception:  # нет сети и т.п.
            logger.exception("Проверка записей DNS не удалась")
            self.done.emit([])


class _DnsRow(QWidget):
    """Одна запись: что добавить, зачем и (после проверки) как обстоит дело."""

    def __init__(self, record) -> None:
        super().__init__()
        self.record = record

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(3)

        head = QHBoxLayout()
        title = QLabel(f"{record.type} · {record.kind}")
        title.setObjectName("Value")
        head.addWidget(title)
        head.addStretch()
        self._state = QLabel("")
        self._state.setObjectName("Key")
        head.addWidget(self._state)
        layout.addLayout(head)

        layout.addWidget(_thin_label(f"хост: {record.host}    (полное имя: {record.fqdn})"))

        value = QHBoxLayout()
        field = QLineEdit(record.value or "(ключ ещё не создан)")
        field.setReadOnly(True)
        field.setCursorPosition(0)
        value.addWidget(field, stretch=1)
        copy = QPushButton("Копировать")
        copy.setObjectName("Tool")
        copy.setCursor(Qt.PointingHandCursor)
        copy.setEnabled(bool(record.value))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(record.value))
        value.addWidget(copy)
        layout.addLayout(value)

        layout.addWidget(_thin_label(record.note))
        self._actual = _thin_label("")
        self._actual.setVisible(False)
        layout.addWidget(self._actual)

    def apply(self, checked) -> None:
        text, color = _DNS_STATES.get(checked.status, _DNS_STATES["unknown"])
        self._state.setText(text)
        self._state.setStyleSheet(f"color: {COLORS[color]}; font-size: 9pt; font-weight: 600;")
        if checked.actual and checked.status != "ok":
            self._actual.setText(f"сейчас в DNS: {checked.actual}")
            self._actual.setVisible(True)
        else:
            self._actual.setVisible(False)


def _thin_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Note")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


class _SecretRow(QWidget):
    """Строка секрета: маска, «глазик» и «копировать».

    Значение получаем лениво через `getter` — файл ключа читается только в
    момент показа, и то после успешной проверки пароля."""

    def __init__(self, dialog: SettingsDialog, key: str, getter) -> None:
        super().__init__()
        self._dialog = dialog
        self._getter = getter
        self._value: str | None = None
        self._revealed = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(8)

        name = QLabel(key)
        name.setObjectName("Key")
        layout.addWidget(name)

        self._field = QLineEdit(_MASK)
        self._field.setReadOnly(True)
        layout.addWidget(self._field, stretch=1)

        self._eye = QPushButton("👁")
        self._eye.setObjectName("Tool")
        self._eye.setCursor(Qt.PointingHandCursor)
        self._eye.clicked.connect(self._toggle)
        layout.addWidget(self._eye)

        self._copy = QPushButton("Копировать")
        self._copy.setObjectName("Tool")
        self._copy.setCursor(Qt.PointingHandCursor)
        self._copy.setEnabled(False)
        self._copy.clicked.connect(self._do_copy)
        layout.addWidget(self._copy)

    def _toggle(self) -> None:
        if self._revealed:
            self._mask()
            return
        if not self._dialog.ensure_authed():
            return
        try:
            value = self._getter()
        except Exception:  # файл мог исчезнуть
            logger.exception("Не удалось прочитать значение секрета")
            value = ""
        if not value:
            self._field.setText("(нет значения)")
            return
        self._value = value
        self._field.setText(value)
        self._field.setCursorPosition(0)
        self._revealed = True
        self._eye.setText("🙈")
        self._copy.setEnabled(True)

    def _mask(self) -> None:
        self._value = None
        self._revealed = False
        self._field.setText(_MASK)
        self._eye.setText("👁")
        self._copy.setEnabled(False)

    def _do_copy(self) -> None:
        if self._revealed and self._value:
            QApplication.clipboard().setText(self._value)


class _AuthDialog(QDialog):
    """Мини-вход: логин и пароль администратора панели для показа секрета."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Подтверждение доступа")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setStyleSheet(_STYLESHEET)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        title = QLabel("🔒 Показать секрет")
        title.setObjectName("Section")
        layout.addWidget(title)

        hint = QLabel("Введите логин и пароль администратора панели.")
        hint.setObjectName("Note")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._login = QLineEdit()
        self._login.setPlaceholderText("Логин")
        layout.addWidget(self._login)

        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.Password)
        self._password.setPlaceholderText("Пароль")
        self._password.returnPressed.connect(self.accept)
        layout.addWidget(self._password)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("Tool")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        ok = QPushButton("Показать")
        ok.setObjectName("Close")
        ok.setCursor(Qt.PointingHandCursor)
        ok.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def credentials(self) -> tuple[str, str]:
        return self._login.text().strip(), self._password.text()

    def showEvent(self, event) -> None:  # noqa: N802 — контракт Qt
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self._login.setFocus()
