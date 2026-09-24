"""Диалог состояния лицензии в стиле Quick-Queue: тёмная карточка без рамки.

Работает с файлами напрямую через `app.services.licensing` — трей стоит на
той же машине, что и служба, HTTP для этого не нужен. Установка нового файла
идёт через тот же `install_license_file`, что и веб-панель: одна проверка,
один путь установки.

Режим `required=True` — единственная дверь обратно в работу: сервер без
лицензии не стартует, панель вместе с ним недоступна, и установить файл
больше неоткуда. Диалог в этом режиме открывается сам при запуске трея и
закрывается «принятым» только после установки действующей лицензии.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .tray_icon import COLORS

_BUTTON = f"""
    QPushButton {{
        background-color: {COLORS['teal']}; color: white; border: none;
        border-radius: 5px; padding: 8px 18px; font-size: 10pt; font-weight: 600;
    }}
    QPushButton:hover {{ background-color: {COLORS['teal_dark']}; }}
"""

_BUTTON_FLAT = f"""
    QPushButton {{
        background-color: transparent; color: {COLORS['muted']};
        border: 1px solid {COLORS['border']}; border-radius: 5px;
        padding: 8px 18px; font-size: 10pt;
    }}
    QPushButton:hover {{ color: {COLORS['text']}; border-color: {COLORS['muted']}; }}
"""


class LicenseDialog(QDialog):
    def __init__(self, parent=None, *, required: bool = False) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._drag_offset = None
        self._required = required
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)

        card = QFrame()
        card.setObjectName("Card")
        card.setStyleSheet(
            f"QFrame#Card {{ background-color: {COLORS['bg']}; border-radius: 12px; "
            f"border: 1px solid {COLORS['border']}; }}"
        )
        shadow = QGraphicsDropShadowEffect(blurRadius=20, xOffset=0, yOffset=5)
        shadow.setColor(QColor(0, 0, 0, 120))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 18, 24, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("🛡️ ТРЕБУЕТСЯ ЛИЦЕНЗИЯ" if self._required else "🛡️ СОСТОЯНИЕ ЛИЦЕНЗИИ")
        title.setStyleSheet(
            f"color: {COLORS['teal']}; font-size: 12pt; font-weight: bold; background: none;"
        )
        close = QPushButton("×")
        close.setFixedSize(28, 28)
        close.setStyleSheet(
            f"QPushButton {{ background: none; color: {COLORS['muted']}; border: none; "
            f"font-size: 16pt; }} QPushButton:hover {{ color: {COLORS['danger']}; }}"
        )
        close.clicked.connect(self.reject)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(close)
        layout.addLayout(header)

        self.grid_holder = QVBoxLayout()
        layout.addLayout(self.grid_holder)
        self._fill_status()

        buttons = QHBoxLayout()
        pick = QPushButton("📁 Установить файл .lic")
        pick.setStyleSheet(_BUTTON)
        pick.clicked.connect(self._pick_file)
        # В обязательном режиме «принято» означает «лицензия установлена»,
        # поэтому кнопка отказа ведёт в reject: вызывающий по коду возврата
        # отличит установленную лицензию от выхода без неё.
        ok = QPushButton("Выйти" if self._required else "Закрыть")
        ok.setStyleSheet(_BUTTON_FLAT)
        ok.clicked.connect(self.reject if self._required else self.accept)
        buttons.addWidget(pick)
        buttons.addStretch()
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def _fill_status(self) -> None:
        while self.grid_holder.count():
            item = self.grid_holder.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        from app.services import licensing

        status = licensing.status(use_cache=False)

        state = QLabel("✓ Действительна" if status.valid else f"⚠ {status.reason}")
        state.setStyleSheet(
            f"color: {COLORS['ok'] if status.valid else COLORS['warn']}; "
            "font-size: 11pt; font-weight: bold; background: none;"
        )
        self.grid_holder.addWidget(state)

        if not status.valid:
            note = QLabel(
                "Без действующей лицензии шлюз не запускается: приём и\n"
                "отправка почты остановлены. Установите файл .lic —\n"
                "и запустите службу.\n"
                "Получить или продлить лицензию: " + licensing.LICENSING_URL
            )
            note.setStyleSheet(f"color: {COLORS['muted']}; font-size: 9pt; background: none;")
            self.grid_holder.addWidget(note)

        if status.data:
            grid_frame = QFrame()
            grid_frame.setStyleSheet(
                f"background-color: {COLORS['panel']}; border-radius: 8px;"
            )
            grid = QGridLayout(grid_frame)
            grid.setContentsMargins(14, 10, 14, 10)
            rows = [
                ("Лицензиат", status.company),
                ("Выдана", str(status.data.get("issue_date", ""))[:10]),
                ("Действует до", str(status.data.get("expiry_date", ""))[:10]),
            ]
            days = status.days_remaining
            if days is not None:
                color = COLORS["ok"] if days > 30 else (COLORS["warn"] if days > 7 else COLORS["danger"])
                rows.append(("Осталось", f"{days} дн."))
            limit = status.data.get("max_domains")
            if limit is not None:
                rows.append(("Доменов по лицензии", str(limit)))

            for index, (key, value) in enumerate(rows):
                name = QLabel(key)
                name.setStyleSheet(f"color: {COLORS['muted']}; font-size: 9pt; background: none;")
                data = QLabel(value)
                value_color = COLORS["text"]
                if key == "Осталось" and days is not None:
                    value_color = color
                data.setStyleSheet(
                    f"color: {value_color}; font-size: 9pt; font-weight: 600; background: none;"
                )
                grid.addWidget(name, index, 0)
                grid.addWidget(data, index, 1, alignment=Qt.AlignRight)
            self.grid_holder.addWidget(grid_frame)

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Файл лицензии", "", "Лицензия (*.lic);;Все файлы (*)"
        )
        if not path:
            return

        from pathlib import Path

        from app.services import licensing

        result = licensing.install_license_file(Path(path))
        if result.valid:
            tail = (
                "\n\nТеперь запустите службу: значок в трее → «Запустить службу»."
                if self._required
                else ""
            )
            QMessageBox.information(
                self, "Лицензия",
                f"Лицензия установлена: {result.company}\n"
                f"Действует до {result.data['expiry_date'][:10]}." + tail,
            )
            if self._required:
                self.accept()
                return
            self._fill_status()
        else:
            QMessageBox.critical(
                self, "Лицензия",
                f"Файл не принят: {result.reason}\nТекущая лицензия не изменена.",
            )

    # --- Перетаскивание безрамочного окна --------------------------------------- #

    def mousePressEvent(self, event):  # noqa: N802 — API Qt
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):  # noqa: N802 — API Qt
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPos() - self._drag_offset)

    def mouseReleaseEvent(self, _event):  # noqa: N802 — API Qt
        self._drag_offset = None
