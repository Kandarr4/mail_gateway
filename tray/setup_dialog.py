"""Мастер первого запуска: создание учётной записи панели управления.

Показывается из GUI-режима (`MailGateway.exe` без аргументов), когда в базе
нет ни одной учётной записи: панель в таком состоянии поднята, но войти в неё
не может никто. Внешний вид и каркас — из Quick-Queue (ui/setup_dialog.py).

Порт и прочие настройки сервера мастер не спрашивает намеренно: у Mail
Gateway они живут в `.env` и читаются службой при старте, а не выбираются в
GUI. Мастер решает ровно одну задачу — учётную запись.

Правила пароля не дублируются: проверяет `admin_users.create`, диалог только
показывает его ошибки.
"""

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIntValidator
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .tray_icon import COLORS

logger = logging.getLogger(__name__)

_INPUT_BG = "#3e5269"

STYLESHEET = f"""
    QFrame#MainFrame {{
        background-color: {COLORS['bg']};
        border-radius: 12px;
        border: 1px solid {COLORS['border']};
    }}
    QLabel#HeaderLabel {{
        color: {COLORS['teal']};
        font-family: 'Segoe UI', sans-serif;
        font-size: 16pt;
        font-weight: bold;
    }}
    QLabel#SectionTitle {{
        color: {COLORS['teal']};
        font-family: 'Segoe UI', sans-serif;
        font-size: 12pt;
        font-weight: 600;
    }}
    QLabel {{
        color: {COLORS['text']};
        font-family: 'Segoe UI', sans-serif;
        font-size: 11pt;
    }}
    QLabel#SubLabel {{
        color: {COLORS['muted']};
        font-size: 10pt;
    }}
    QLineEdit {{
        background-color: {_INPUT_BG};
        border: 1px solid {COLORS['border']};
        color: {COLORS['text']};
        padding: 5px 15px;
        border-radius: 6px;
        font-family: 'Segoe UI', sans-serif;
        font-size: 11pt;
        min-height: 45px;
    }}
    QLineEdit:focus {{
        border: 2px solid {COLORS['teal']};
        background-color: {COLORS['panel']};
    }}
    QFrame#InfoPanel {{
        background-color: rgba(26, 188, 156, 0.15);
        border: 1px solid {COLORS['teal']};
        border-radius: 8px;
    }}
    QPushButton {{
        border: none;
        border-radius: 6px;
        padding: 12px 25px;
        font-family: 'Segoe UI', sans-serif;
        font-size: 11pt;
        font-weight: bold;
        color: white;
        min-width: 120px;
    }}
    QPushButton#SaveBtn {{
        background-color: {COLORS['teal']};
    }}
    QPushButton#SaveBtn:hover {{
        background-color: {COLORS['teal_dark']};
    }}
    QPushButton#CancelBtn {{
        background-color: #546e7a;
    }}
    QPushButton#CancelBtn:hover {{
        background-color: #455a64;
    }}
"""


class SetupDialog(QDialog):
    """Обязательный диалог: закрывается успешно только с созданной записью."""

    def __init__(self, version: str) -> None:
        super().__init__()
        self.version = version

        # Полный `.env` мастер генерирует только при его полном отсутствии.
        # Частично заполненный файл не трогаем: раз он есть, домен и секреты
        # уже настроены, а мастер тогда решает единственную задачу — админа.
        from app.config import BASE_DIR

        self._env_path = BASE_DIR / ".env"
        self._env_missing = not self._env_path.exists()
        #: True после успешной генерации `.env` — трею нужно перезапуститься,
        #: чтобы синглтон настроек перечитал свежий файл (см. tray/app.py).
        self.env_generated = False
        #: Результат bootstrap_env — чтобы повторный клик «Сохранить» (когда
        #: админ не прошёл по паролю) не пересоздавал уже записанный .env.
        self._generated = None

        self._init_ui()

    def _init_ui(self) -> None:
        # Обычное окно с рамкой ОС — надёжно на Windows Server/RDP, в отличие
        # от безрамочного полупрозрачного (проверено в Quick-Queue).
        self.setWindowTitle("Mail Gateway — Первоначальная настройка")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(680)
        self.setStyleSheet(f"QDialog {{ background-color: {COLORS['bg']}; }}")

        dialog_layout = QVBoxLayout(self)
        dialog_layout.setContentsMargins(10, 10, 10, 10)

        frame = QFrame()
        frame.setObjectName("MainFrame")
        frame.setStyleSheet(STYLESHEET)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setXOffset(0)
        shadow.setYOffset(5)
        shadow.setColor(QColor(0, 0, 0, 120))
        frame.setGraphicsEffect(shadow)
        dialog_layout.addWidget(frame)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(40, 30, 40, 40)
        layout.setSpacing(20)

        header = QLabel("⚙️ ПЕРВОНАЧАЛЬНАЯ НАСТРОЙКА")
        header.setObjectName("HeaderLabel")
        layout.addWidget(header)

        if self._env_missing:
            desc_text = (
                "Файл настроек .env не найден — это первый запуск. Мастер создаст "
                "рабочий .env (токен API, ключи шифрования и подписи DKIM), задаст "
                "администратора панели и покажет запись для DNS."
            )
        else:
            desc_text = (
                "В базе нет ни одной учётной записи панели управления — войти в "
                "/admin сейчас не может никто. Задайте администратора почтового "
                "шлюза Mail Gateway."
            )
        desc = QLabel(desc_text)
        desc.setWordWrap(True)
        desc.setObjectName("SubLabel")
        layout.addWidget(desc)

        layout.addSpacing(10)

        if self._env_missing:
            self._add_network_section(layout)

        section = QLabel("👤 Администратор панели")
        section.setObjectName("SectionTitle")
        layout.addWidget(section)

        grid = QGridLayout()
        grid.setSpacing(20)

        self.login_entry = QLineEdit()
        self.login_entry.setPlaceholderText("Придумайте логин (admin)")
        self.login_entry.setText("admin")
        grid.addWidget(QLabel("Логин:"), 0, 0)
        grid.addWidget(self.login_entry, 1, 0, 1, 2)

        self.password_entry = QLineEdit()
        self.password_entry.setEchoMode(QLineEdit.Password)
        self.password_entry.setPlaceholderText("минимум 10 символов, буквы и цифры")
        grid.addWidget(QLabel("Пароль:"), 2, 0)
        grid.addWidget(self.password_entry, 3, 0)

        self.confirm_entry = QLineEdit()
        self.confirm_entry.setEchoMode(QLineEdit.Password)
        self.confirm_entry.setPlaceholderText("••••••••")
        grid.addWidget(QLabel("Повторите пароль:"), 2, 1)
        grid.addWidget(self.confirm_entry, 3, 1)

        layout.addLayout(grid)
        layout.addStretch()

        info = QFrame()
        info.setObjectName("InfoPanel")
        info_layout = QHBoxLayout(info)
        info_layout.setContentsMargins(15, 15, 15, 15)

        icon = QLabel("ℹ️")
        icon.setStyleSheet("font-size: 20px; border: none; background: transparent;")

        text_layout = QVBoxLayout()
        text_layout.setSpacing(5)
        info_header = QLabel("Важная информация")
        info_header.setStyleSheet(
            f"font-weight: bold; font-size: 10pt; color: {COLORS['text']}; "
            "border: none; background: transparent;"
        )
        info_body = QLabel(
            "Учётная запись сохраняется в локальную базу и используется для "
            "входа в панель управления. Приём и отправка почты работают "
            "независимо от неё. Пароль позже меняется в панели (/admin/password)."
        )
        info_body.setWordWrap(True)
        info_body.setStyleSheet(
            f"color: {COLORS['muted']}; font-size: 9pt; border: none; background: transparent;"
        )
        text_layout.addWidget(info_header)
        text_layout.addWidget(info_body)

        info_layout.addWidget(icon, alignment=Qt.AlignTop)
        info_layout.addLayout(text_layout)
        layout.addWidget(info)

        layout.addSpacing(10)

        buttons = QHBoxLayout()
        cancel_btn = QPushButton("Выход")
        cancel_btn.setObjectName("CancelBtn")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Сохранить и запустить")
        save_btn.setObjectName("SaveBtn")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.clicked.connect(self._save)

        buttons.addStretch()
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        layout.addLayout(buttons)

        version_label = QLabel(f"v{self.version}")
        version_label.setStyleSheet(f"color: {COLORS['muted']}; font-size: 8pt;")
        version_label.setAlignment(Qt.AlignRight)
        layout.addWidget(version_label)

    def _add_network_section(self, layout: QVBoxLayout) -> None:
        """Поля домена и порта API — нужны только при генерации нового .env.

        Домен обязателен: без него не сгенерировать ключ DKIM и запись для DNS.
        Порт API можно оставить по умолчанию.
        """
        section = QLabel("🌐 Домен и сеть")
        section.setObjectName("SectionTitle")
        layout.addWidget(section)

        hint = QLabel(
            "Домен, от имени которого шлюз отправляет и принимает почту — "
            "по нему создаётся подпись DKIM. Остальные настройки потом "
            "правятся в .env рядом с программой."
        )
        hint.setWordWrap(True)
        hint.setObjectName("SubLabel")
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(20)

        self.domain_entry = QLineEdit()
        self.domain_entry.setPlaceholderText("например, somnium.kz")
        grid.addWidget(QLabel("Домен:"), 0, 0)
        grid.addWidget(self.domain_entry, 1, 0)

        self.port_entry = QLineEdit()
        self.port_entry.setText("8025")
        self.port_entry.setValidator(QIntValidator(1, 65535, self))
        grid.addWidget(QLabel("Порт HTTP API:"), 0, 1)
        grid.addWidget(self.port_entry, 1, 1)

        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        layout.addSpacing(10)

    def showEvent(self, event) -> None:  # noqa: N802 — контракт Qt
        super().showEvent(event)
        # Активация и фокус ввода — надёжность на Windows Server/RDP.
        self.raise_()
        self.activateWindow()
        if self._env_missing:
            self.domain_entry.setFocus()
        else:
            self.login_entry.setFocus()

    def _save(self) -> None:
        login = self.login_entry.text().strip()
        password = self.password_entry.text()

        domain = ""
        api_port = 0
        if self._env_missing:
            domain = self.domain_entry.text().strip().lower()
            if not self._valid_domain(domain):
                QMessageBox.warning(
                    self, "Ошибка", "Укажите корректный домен, например somnium.kz"
                )
                return
            port_text = self.port_entry.text().strip()
            if not port_text or not 1 <= int(port_text) <= 65535:
                QMessageBox.warning(
                    self, "Ошибка", "Порт HTTP API должен быть числом от 1 до 65535."
                )
                return
            api_port = int(port_text)

        if not login:
            QMessageBox.warning(self, "Ошибка", "Заполните логин администратора!")
            return
        if password != self.confirm_entry.text():
            QMessageBox.warning(self, "Ошибка", "Пароли не совпадают!")
            return

        # Пароль проверяем ДО генерации .env — тем же правилом, что и сервис
        # (без дублирования). Иначе слабый пароль оставил бы .env и ключи
        # созданными, а учётку — нет: мастер всплыл бы второй раз, уже прося
        # только администратора.
        from app.api.errors import ApiError
        from app.services import admin_users

        try:
            admin_users.validate_password(password)
        except ApiError as exc:
            QMessageBox.warning(self, "Ошибка", exc.detail)
            return

        # Порядок важен: сначала .env с секретами, затем администратор. При
        # сбое записи .env учётка не создаётся, и следующий запуск снова
        # покажет полный мастер. Обратный порядок оставил бы админа без .env —
        # мастер больше не всплыл бы, а сервер поднялся с небезопасным токеном
        # по умолчанию. Повторный клик (админ не прошёл по паролю) .env не
        # пересоздаёт: результат уже сохранён в self._generated.
        if self._env_missing and self._generated is None:
            try:
                self._generated = self._generate_env(domain, api_port)
            except Exception as exc:  # крипта, файлы, права
                logger.exception("Не удалось сгенерировать .env из мастера")
                QMessageBox.critical(self, "Ошибка", f"Не удалось создать .env:\n{exc}")
                return

        # Уникальность логина проверяет сервис — тот же код, что у manage.py и
        # консольного диалога run.bat.
        from app.db import session_scope

        try:
            with session_scope() as db:
                user = admin_users.create(db, login, password)
                name = user.username
        except ApiError as exc:
            QMessageBox.warning(self, "Ошибка", exc.detail)
            return
        except Exception as exc:  # БД недоступна и т.п.
            logger.exception("Не удалось создать учётную запись из мастера")
            QMessageBox.critical(self, "Ошибка", f"Не удалось создать учётную запись:\n{exc}")
            return

        logger.info("Мастер первого запуска создал учётную запись %s", name)

        if self._generated is not None:
            self.env_generated = True
            self._show_dns_record(domain, self._generated)

        self.accept()

    @staticmethod
    def _valid_domain(value: str) -> bool:
        """Достаточная проверка для DKIM/DNS: непустой домен с точкой, без
        пробелов и '@'. Полная валидация FQDN здесь избыточна."""
        return bool(value) and "." in value and " " not in value and "@" not in value

    def _generate_env(self, domain: str, api_port: int):
        """Генерирует полный .env и ключи. Ядро — в app/services/keygen.py,
        тем же пользуется manage.py."""
        from app.config import BASE_DIR
        from app.services import keygen

        return keygen.bootstrap_env(BASE_DIR, domain=domain, api_port=api_port)

    def _show_dns_record(self, domain: str, result) -> None:
        """Сохраняет памятку по DNS рядом с программой и показывает запись DKIM.

        В файл идут ВСЕ нужные записи (A, MX, SPF, DKIM, DMARC, PTR): DKIM
        программа создаёт сама, а без остальных почта либо не придёт, либо
        уйдёт в спам — и раньше оператор о них не узнавал. На экране остаётся
        DKIM: это единственная запись, значение которой больше нигде не взять,
        а её длинную строку нужно скопировать сразу."""
        saved_path = None
        try:
            from app.services import dns_setup

            saved_path = self._env_path.parent / "dns_record.txt"
            saved_path.write_text(dns_setup.as_text(domain), encoding="utf-8")
        except Exception:  # не смогли сохранить файл, покажем на экране
            logger.exception("Не удалось сохранить памятку по DNS")
            saved_path = None

        DnsRecordDialog(result.dns_name, result.dns_value, saved_path, self).exec()


class DnsRecordDialog(QDialog):
    """Показывает TXT-запись DKIM с кнопками «копировать».

    Всплывает один раз после генерации .env. Оператор обязан внести запись в
    DNS домена — до этого исходящие письма уходят без подписи и попадают в
    спам, поэтому запись видна крупно и копируется в один клик."""

    def __init__(self, name: str, value: str, saved_path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Mail Gateway — запись DKIM для DNS")
        self.setWindowFlags(Qt.Dialog | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(640)
        self.setStyleSheet(f"QDialog {{ background-color: {COLORS['bg']}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)

        frame = QFrame()
        frame.setObjectName("MainFrame")
        frame.setStyleSheet(STYLESHEET)
        outer.addWidget(frame)

        layout = QVBoxLayout(frame)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(15)

        header = QLabel("🔑 Запись DKIM для DNS")
        header.setObjectName("HeaderLabel")
        layout.addWidget(header)

        desc = QLabel(
            "Добавьте эту TXT-запись в DNS домена. До её появления в DNS "
            "исходящие письма уходят без подписи DKIM и почти наверняка "
            "попадают в спам. Запись распространяется от нескольких минут до часа."
        )
        desc.setWordWrap(True)
        desc.setObjectName("SubLabel")
        layout.addWidget(desc)

        layout.addWidget(self._field_label("Имя записи (Host):"))
        layout.addLayout(self._copy_row(self._readonly_line(name), name))

        layout.addWidget(self._field_label("Значение (Value):"))
        value_edit = QPlainTextEdit(value)
        value_edit.setReadOnly(True)
        value_edit.setFixedHeight(90)
        value_edit.setStyleSheet(
            f"background-color: {_INPUT_BG}; border: 1px solid {COLORS['border']}; "
            f"color: {COLORS['text']}; border-radius: 6px; padding: 8px; "
            "font-family: 'Consolas', monospace; font-size: 10pt;"
        )
        layout.addLayout(self._copy_row(value_edit, value))

        if saved_path is not None:
            saved = QLabel(
                f"Эта и остальные нужные записи (MX, SPF, DMARC, обратная запись PTR) "
                f"сохранены в файл:\n{saved_path}\n"
                "Их же видно в окне программы: «Настройки» → «Записи DNS», там есть "
                "кнопка проверки того, что уже добавлено."
            )
            saved.setObjectName("SubLabel")
            saved.setWordWrap(True)
            layout.addWidget(saved)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("Готово")
        close_btn.setObjectName("SaveBtn")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("SectionTitle")
        label.setStyleSheet("font-size: 10pt; font-weight: 600;")
        return label

    @staticmethod
    def _readonly_line(text: str) -> QLineEdit:
        line = QLineEdit(text)
        line.setReadOnly(True)
        return line

    def _copy_row(self, widget, value: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(widget)
        copy_btn = QPushButton("Копировать")
        copy_btn.setObjectName("CancelBtn")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setMinimumWidth(120)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(value))
        row.addWidget(copy_btn, alignment=Qt.AlignTop)
        return row
