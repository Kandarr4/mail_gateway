"""Значок Mail Gateway в системном трее: состояние и пульт службы Windows.

Архитектурное отличие от Quick-Queue, откуда взят внешний вид: там трей сам
запускает сервер в фоновом потоке, потому что настоящей службы у Quick-Queue
нет. У Mail Gateway служба есть (service.py), и запускать второй сервер из
трея означало бы конфликт портов и почту, живущую до выхода из сеанса RDP.
Поэтому значок — отдельный лёгкий процесс: показывает состояние (опросом
`GET /ready` и диспетчера служб), открывает панель и управляет службой через
`win32serviceutil`. Секретов в нём нет: `/ready` намеренно без токена.

Опрос состояния живёт в фоновом потоке (`StatusPoller`), а не в обработчиках
меню: `QueryServiceStatus` и HTTP-запрос с таймаутом 1.5 с, выполненные в
UI-потоке при каждой перестройке меню, замораживали его открытие до ~3 с —
хуже всего при остановленной службе, когда таймаут выбирался целиком. Меню
строится один раз, дальше обновляются только тексты строк по готовому кешу.

Управление службой (пуск/стоп/перезапуск) требует прав администратора:
недостающие запрашиваются через UAC на месте (`_run_elevated`), а не
требуют перезапуска всей программы от имени администратора.
"""

import json
import logging
import os
import sys
import threading
import urllib.request
import webbrowser
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QAction, QIcon  # QAction в Qt6 живёт в QtGui, не в QtWidgets
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QWidget,
    QWidgetAction,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "MailGateway"

#: Коды Windows, на которые есть отдельная реакция. Числами, а не импортом
#: `winerror`: модуль намеренно переживает отсутствие pywin32 — все обращения
#: к нему обёрнуты, и обязательный импорт ради двух констант это бы сломал.
_ACCESS_DENIED = 5
_UAC_CANCELLED = 1223

#: Пауза между опросами состояния в фоновом потоке.
POLL_INTERVAL_SECONDS = 3.0

#: Фирменная палитра Somnium — та же, что в Quick-Queue.
COLORS = {
    "bg": "#2b3c4e",
    "panel": "#34495e",
    "border": "#506880",
    "teal": "#1abc9c",
    "teal_dark": "#16a085",
    "text": "#ecf0f1",
    "muted": "#bdc3c7",
    "danger": "#e74c3c",
    "ok": "#2ecc71",
    "warn": "#f39c12",
}

MENU_STYLESHEET = f"""
    QMenu {{
        background-color: {COLORS['bg']};
        border: 1px solid {COLORS['border']};
        border-radius: 6px;
        padding: 5px;
        font-family: 'Segoe UI', sans-serif;
    }}
    QMenu::item {{
        color: {COLORS['text']};
        padding: 8px 20px;
        border-radius: 4px;
        margin: 2px 0px;
        font-size: 10pt;
    }}
    QMenu::item:selected {{
        background-color: {COLORS['teal']};
        color: white;
    }}
    QMenu::item:disabled {{
        color: {COLORS['muted']};
    }}
    QMenu::separator {{
        height: 1px;
        background-color: {COLORS['border']};
        margin: 5px 10px;
    }}
"""


def icon_path() -> str:
    # Под PyInstaller ресурсы распаковываются во временную папку _MEIPASS.
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "icon.ico")


@dataclass(frozen=True)
class StatusSnapshot:
    """Результат одного прохода опроса — всё, что показывает меню."""

    service: tuple[str, str]
    api: tuple[str, str]
    license: tuple[str, str]
    running: bool


class ServiceProbe:
    """Опрос состояния: служба Windows + готовность HTTP API.

    Методы блокирующие (таймауты до 1.5 с) — вызывать только из фонового
    потока, не из UI.
    """

    def __init__(self, api_port: int) -> None:
        self.api_port = api_port

    def snapshot(self) -> StatusSnapshot:
        service_text, service_color = self.service_state()
        return StatusSnapshot(
            service=(service_text, service_color),
            api=self.api_ready(),
            license=self.license_state(),
            running=service_text == "работает",
        )

    def service_state(self) -> tuple[str, str]:
        """(текст, цвет). Диспетчер служб — источник правды о процессе."""
        try:
            import win32service
            import win32serviceutil

            state = win32serviceutil.QueryServiceStatus(SERVICE_NAME)[1]
        except Exception as exc:  # noqa: BLE001 — служба не установлена или нет прав
            if getattr(exc, "winerror", None) == 1060:
                return "не установлена", COLORS["warn"]
            return "недоступна", COLORS["warn"]

        mapping = {
            win32service.SERVICE_RUNNING: ("работает", COLORS["ok"]),
            win32service.SERVICE_STOPPED: ("остановлена", COLORS["danger"]),
            win32service.SERVICE_START_PENDING: ("запускается…", COLORS["warn"]),
            win32service.SERVICE_STOP_PENDING: ("останавливается…", COLORS["warn"]),
            win32service.SERVICE_PAUSED: ("приостановлена", COLORS["warn"]),
        }
        return mapping.get(state, (f"состояние {state}", COLORS["warn"]))

    def api_ready(self) -> tuple[str, str]:
        """`/ready` отвечает без токена — трею не нужны секреты."""
        url = f"http://127.0.0.1:{self.api_port}/api/v1/ready"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:  # noqa: S310 — адрес собран выше: http на 127.0.0.1
                data = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — любой сбой опроса означает «не отвечает»
            return "не отвечает", COLORS["danger"]

        if data.get("ready"):
            return "принимает почту", COLORS["ok"]
        parts = []
        if not data.get("database"):
            parts.append("БД")
        if not data.get("smtp"):
            parts.append("SMTP")
        return f"сбой: {', '.join(parts) or '?'}", COLORS["danger"]

    def license_state(self) -> tuple[str, str]:
        try:
            from app.services import licensing

            status = licensing.status()
        except Exception:  # noqa: BLE001 — трей живёт даже без доступа к файлам приложения
            return "недоступна", COLORS["muted"]
        if not status.valid:
            # Красным, а не жёлтым: почта не ходит, пока это не исправят.
            return status.reason, COLORS["danger"]
        days = status.days_remaining
        if days is not None and days <= 30:
            return f"истекает через {days} дн.", COLORS["warn"]
        return f"до {status.data['expiry_date'][:10]}", COLORS["ok"]


class StatusPoller(QThread):
    """Фоновый опрос: раз в несколько секунд снимает состояние и шлёт его в
    UI-поток сигналом. UI никогда не ждёт ни диспетчера служб, ни HTTP."""

    updated = Signal(object)

    def __init__(self, probe: ServiceProbe) -> None:
        super().__init__()
        self.probe = probe
        self._stop = threading.Event()

    def run(self) -> None:  # контракт QThread
        while not self._stop.is_set():
            try:
                self.updated.emit(self.probe.snapshot())
            except Exception:  # опрос не должен ронять поток
                logger.exception("Сбой опроса состояния")
            self._stop.wait(POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._stop.set()
        self.wait(3000)


class TrayIconManager:
    """Значок, меню и действия. Скелет — из Quick-Queue V1.

    Меню собирается один раз; дальше по сигналу опроса обновляются только
    тексты `QLabel` и видимость пунктов пуск/стоп — `menu.clear()` на каждый
    тик заставлял Qt пересоздавать виджеты и вместе с синхронным опросом
    подвешивал открытие меню.
    """

    def __init__(self, qt_app, api_port: int, version: str) -> None:
        self.app = qt_app
        self.version = version
        self.probe = ServiceProbe(api_port)
        self.api_port = api_port
        self._snapshot: StatusSnapshot | None = None

        self.icon = QSystemTrayIcon(QIcon(icon_path()), qt_app)
        self.icon.setToolTip("Mail Gateway")
        self.menu = QMenu()
        self.menu.setStyleSheet(MENU_STYLESHEET)

        self.poller = StatusPoller(self.probe)
        self.poller.updated.connect(self._apply_snapshot)

    # --- Сборка меню ----------------------------------------------------------- #

    def setup(self) -> None:
        self._build_menu()
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        # Перед показом — привести пуск/стоп к последнему снятому состоянию:
        # пока меню открыто, их видимость не трогается (пункты пропали бы
        # из-под курсора), и aboutToShow догоняет отложенное.
        self.menu.aboutToShow.connect(self._apply_service_actions)
        self.icon.show()
        self.poller.start()

    def _build_menu(self) -> None:
        """Один раз: каркас меню со ссылками на обновляемые подписи."""
        pending = ("опрашивается…", COLORS["muted"])

        self._add_widget(self._header("MAIL GATEWAY"))
        self._service_label = self._add_info_row("Служба:", *pending)
        self._api_label = self._add_info_row("API:", *pending)
        self._add_info_row("Порт:", str(self.api_port), COLORS["text"])
        self._add_info_row("Версия:", f"v{self.version}", COLORS["text"])
        self._license_label = self._add_info_row("Лицензия:", *pending)

        self.menu.addSeparator()

        self._action("🌐  Открыть панель управления", self.open_admin_panel)
        self._action("📁  Открыть папку журналов", self.open_logs)
        self._action("⚙️  Настройки…", self.show_settings_dialog)
        self._action("🛡️  Лицензия…", self.show_license_dialog)

        self.menu.addSeparator()

        self._start_action = self._action("▶️  Запустить службу", self.start_service)
        self._stop_action = self._action("⏸  Остановить службу", self.stop_service)
        self._restart_action = self._action("🔄  Перезапустить службу", self.restart_service)
        # Пока состояние не снято, пульт не показывается — кнопки появятся с
        # первым результатом опроса (сразу после старта).
        for action in (self._start_action, self._stop_action, self._restart_action):
            action.setVisible(False)

        self.menu.addSeparator()
        self._action("❌  Выход (почта продолжит работать)", self.exit_tray)

    def _apply_snapshot(self, snapshot: StatusSnapshot) -> None:
        """Слот сигнала опроса — выполняется в UI-потоке, но мгновенно:
        только тексты и стили, никакого ввода-вывода."""
        self._snapshot = snapshot
        self._set_row(self._service_label, *snapshot.service)
        self._set_row(self._api_label, *snapshot.api)
        self._set_row(self._license_label, *snapshot.license)
        if not self.menu.isVisible():
            self._apply_service_actions()

    def _apply_service_actions(self) -> None:
        if self._snapshot is None:
            return
        running = self._snapshot.running
        self._start_action.setVisible(not running)
        self._stop_action.setVisible(running)
        self._restart_action.setVisible(running)

    # --- Действия --------------------------------------------------------------- #

    def open_admin_panel(self) -> None:
        webbrowser.open(f"http://localhost:{self.api_port}/admin/")

    def open_logs(self) -> None:
        try:
            from app.config import settings

            os.startfile(str(settings.log_path))  # noqa: S606 — путь из настроек
        except Exception as exc:  # noqa: BLE001
            self._balloon("Журналы", f"Не удалось открыть папку: {exc}", error=True)

    def show_settings_dialog(self) -> None:
        from .settings_dialog import SettingsDialog

        SettingsDialog(self.api_port).exec()

    def show_license_dialog(self) -> None:
        from .license_dialog import LicenseDialog

        LicenseDialog().exec()

    def start_service(self) -> None:
        self._service_command("запуск", "start", lambda util: util.StartService(SERVICE_NAME))

    def stop_service(self) -> None:
        self._service_command("остановка", "stop", lambda util: util.StopService(SERVICE_NAME))

    def restart_service(self) -> None:
        self._service_command("перезапуск", "restart", lambda util: util.RestartService(SERVICE_NAME))

    def _service_command(self, title: str, verb: str, command) -> None:
        """Пуск/стоп/перезапуск службы, при необходимости — через UAC.

        Управление службами требует прав администратора, а программа живёт в
        автозагрузке пользователя и запускается без них. Раньше это упиралось
        в «Отказано в доступе» и совет перезапустить всё от имени
        администратора — теперь недостающие права запрашиваются на месте: тот
        же exe перезапускается с одной командой (`start`), делает своё дело и
        завершается. Постоянно работать с правами администратора программе
        незачем, а автозагрузка с ними и вовсе не работает.
        """
        try:
            import win32serviceutil

            command(win32serviceutil)
        except Exception as exc:  # noqa: BLE001 — чаще всего отказ в доступе
            if getattr(exc, "winerror", None) != _ACCESS_DENIED:
                self._balloon("Служба Mail Gateway", str(exc), error=True)
                return
            if not self._run_elevated(verb):
                return
        self._balloon("Служба Mail Gateway", f"Выполняется {title} службы")

    def _run_elevated(self, verb: str) -> bool:
        """Перезапускает себя с правами администратора ради одной команды.

        Возврата результата нет: ShellExecute отдаёт управление сразу, а
        итог покажет ближайший опрос состояния — через пару секунд.
        """
        import ctypes

        if getattr(sys, "frozen", False):
            program, arguments = sys.executable, verb
        else:
            # В разработке запускается не exe, а интерпретатор: команду ему
            # передаёт единая точка входа mailgateway.py.
            entry = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                 "mailgateway.py")
            program, arguments = sys.executable, f'"{entry}" {verb}'

        # 32 — граница успеха в API ShellExecute: меньшие значения это коды
        # ошибок, и среди них 1223 — «пользователь отменил запрос UAC».
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", program, arguments, None, 0)
        if result > 32:
            return True
        if result == _UAC_CANCELLED:
            logger.info("Пользователь отказал в правах администратора (%s)", verb)
            return False
        self._balloon(
            "Служба Mail Gateway",
            f"Не удалось получить права администратора (код {result})",
            error=True,
        )
        return False

    def exit_tray(self) -> None:
        """Убирает значок. Служба продолжает работать — почта не зависит от
        того, открыта программа или нет."""
        self.poller.stop()
        self.icon.hide()
        self.app.quit()

    # --- Служебное --------------------------------------------------------------- #

    def _on_activated(self, reason) -> None:
        # Меню открывается сразу: данные уже лежат в кеше опроса.
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.Context):
            from PySide6.QtGui import QCursor

            self.menu.popup(QCursor.pos())

    def _balloon(self, title: str, text: str, *, error: bool = False) -> None:
        kind = QSystemTrayIcon.Critical if error else QSystemTrayIcon.Information
        self.icon.showMessage(title, text, kind, 6000)

    def _action(self, text: str, handler) -> QAction:
        action = QAction(text, self.menu)
        action.triggered.connect(handler)
        self.menu.addAction(action)
        return action

    def _add_widget(self, widget: QWidget) -> None:
        holder = QWidgetAction(self.menu)
        holder.setDefaultWidget(widget)
        self.menu.addAction(holder)

    def _add_info_row(self, key: str, value: str, color: str) -> QLabel:
        row, label = self._info_row(key, value, color)
        self._add_widget(row)
        return label

    @staticmethod
    def _set_row(label: QLabel, text: str, color: str) -> None:
        label.setText(text)
        label.setStyleSheet(f"color: {color}; font-size: 9pt; font-weight: 600;")

    @staticmethod
    def _header(text: str) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"background-color: {COLORS['panel']}; border-radius: 4px; margin: 2px;"
        )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {COLORS['teal']}; font-weight: bold; font-size: 11pt; background: none;"
        )
        layout.addWidget(label, alignment=Qt.AlignCenter)
        return frame

    @staticmethod
    def _info_row(key: str, value: str, color: str) -> tuple[QWidget, QLabel]:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(16, 3, 16, 3)
        name = QLabel(key)
        name.setStyleSheet(f"color: {COLORS['muted']}; font-size: 9pt;")
        data = QLabel(value)
        data.setStyleSheet(f"color: {color}; font-size: 9pt; font-weight: 600;")
        layout.addWidget(name)
        layout.addStretch()
        layout.addWidget(data)
        return row, data
