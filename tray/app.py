"""Окно Mail Gateway: значок в трее, мастер первого запуска, заставка.

    .venv\\Scripts\\pythonw.exe -m tray.app      без окна консоли
    .venv\\Scripts\\python.exe -m tray.app       с консолью (отладка)

`run.bat` запускает её сам, вместе с сервером; в сборке то же делает
`MailGateway.exe` без аргументов. Иначе — ярлыком на рабочем столе или из
автозагрузки.

Запускается в единственном экземпляре: второй запуск показывает, где искать
значок, и выходит (`_claim_single_instance`). Иначе в трее набирался десяток
одинаковых значков, каждый со своим опросом службы.

При старте проверяется учётная запись панели: если её нет, работа не
продолжается, пока оператор не создаст её в мастере (`tray/setup_dialog.py`).
Затем показывается заставка (`tray/welcome_overlay.py`), гаснущая после
первого реального снятия состояния службы, а следом — диалог лицензии, если
действующей нет (`_offer_license_dialog`): почта в таком состоянии не
обрабатывается, и оператор должен узнать об этом сразу.

Сервер эта часть программы не запускает и секретов не хранит: состояние
читается из `GET /ready` (без токена) и диспетчера служб Windows. Закрытие
значка на почту не влияет — служба работает сама по себе.
"""

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# Трей импортирует app.config/app.services.licensing — корень проекта
# должен быть в пути и при запуске файла напрямую, не через -m.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from tray.tray_icon import TrayIconManager

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    """Консоль (если есть) + файл `data/logs/tray.log` с той же ротацией,
    что у сервера. Под `pythonw.exe` консоли нет — без файла этот журнал
    пропадал бы бесследно."""
    handlers: list[logging.Handler] = []
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    try:
        from app.config import settings

        settings.log_path.mkdir(parents=True, exist_ok=True)
        handlers.append(
            TimedRotatingFileHandler(
                settings.log_path / "tray.log",
                when="midnight",
                backupCount=settings.log_retention_days,
                encoding="utf-8",
                delay=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 — программа живёт и без файла журнала
        print(f"Файл журнала недоступен: {exc}", file=sys.stderr or sys.__stderr__)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def _ensure_admin(version: str) -> str:
    """Проверяет наличие учётной записи панели; при отсутствии требует
    создать её в мастере. Возвращает 'ok' | 'abort' | 'restart'.

    'abort' — оператор отказался, запускаться нельзя. 'restart' — мастер
    сгенерировал .env, и программу нужно перезапустить, чтобы синглтон настроек
    перечитал свежий файл (см. `_restart`).

    Сбой самой проверки (БД занята, миграция не прошла) запуск не
    останавливает: показывать состояние службы полезно и в этом случае.
    """
    try:
        from app.db import session_scope
        from app.schema_setup import prepare_database
        from app.services import admin_users

        prepare_database()
        with session_scope() as db:
            if admin_users.count(db) > 0:
                return "ok"
    except Exception:
        logger.exception("Не удалось проверить учётные записи панели")
        return "ok"

    from PySide6.QtWidgets import QDialog

    from tray.setup_dialog import SetupDialog

    dialog = SetupDialog(version)
    if dialog.exec() != QDialog.Accepted:
        logger.warning("Мастер первого запуска отклонён — выход без учётной записи")
        return "abort"
    return "restart" if dialog.env_generated else "ok"


def _offer_license_dialog() -> None:
    """Без действующей лицензии сразу показывает диалог установки.

    Не запрет, а подсказка: программа работает и без лицензии, но почту не
    обрабатывает, и оператор должен узнать об этом сразу, а не по молчащей
    очереди. Отказ от диалога ничего не ломает — лицензию можно установить
    позже отсюда же или в панели.

    Сбой самой проверки (нет доступа к файлам приложения) запуск не
    останавливает: состояние службы полезно видеть и в этом случае.
    """
    try:
        from app.services import licensing

        if licensing.status(use_cache=False).valid:
            return
    except Exception:
        logger.exception("Не удалось проверить лицензию")
        return

    from tray.license_dialog import LicenseDialog

    logger.warning("Лицензия недействительна — почта не обрабатывается")
    LicenseDialog(required=True).exec()


#: Мьютекс единственного экземпляра. Держится открытым всю жизнь процесса —
#: закрытый (или собранный сборщиком мусора) дескриптор снимает защиту.
_instance_mutex = None

#: Имя без префикса Global\ — защита в пределах сеанса. В другом сеансе RDP
#: свой значок в трее нужен и не мешает: это отдельный рабочий стол.
_MUTEX_NAME = "MailGateway.SingleInstance"


def _claim_single_instance() -> bool:
    """False — программа уже запущена в этом сеансе.

    Каждый запуск поднимал свой значок в трее, и их набиралось сколько угодно:
    десяток иконок, десяток опросов службы, и неясно, какая из копий чья.

    Сбой самой проверки (нет pywin32) запуск не отменяет: остаться без
    программы хуже, чем с двумя её копиями.
    """
    global _instance_mutex
    try:
        import win32api
        import win32event
        import winerror

        _instance_mutex = win32event.CreateMutex(None, False, _MUTEX_NAME)
        return win32api.GetLastError() != winerror.ERROR_ALREADY_EXISTS
    except Exception:
        logger.exception("Не удалось проверить, запущена ли программа")
        return True


def _restart() -> None:
    """Перезапускает программу, чтобы настройки перечитались из нового .env.

    Синглтон `settings` прочитан при импорте, ещё до появления .env; надёжнее
    всего стартовать процесс заново. Вызывается до создания значка в трее — на
    экране ничего не мигает. .env и учётная запись уже на месте, поэтому
    повторный запуск проходит мимо мастера, зацикливания нет.

    Если перезапустить не удалось — продолжаем со старыми настройками: служба
    всё равно прочитает .env свежим при своём старте.
    """
    import os

    logger.info("Перезапуск для применения сгенерированного .env")
    try:
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        os.execv(sys.executable, [sys.executable, *args])
    except OSError:
        logger.exception("Не удалось перезапустить — работаю со старыми настройками")


def _show_overlay(manager: TrayIconManager, version: str):
    """Заставка до первого реального снятия состояния (и минимум 3 секунды,
    чтобы не мигала на быстрой машине). Страховка: гаснет сама через 12 с."""
    from PySide6.QtCore import QTimer

    from tray.welcome_overlay import WelcomeOverlay

    overlay = WelcomeOverlay(version)
    overlay.show()

    state = {"elapsed": False, "snapshot": False}

    def maybe_finish() -> None:
        if state["elapsed"] and state["snapshot"]:
            overlay.start_fade_out()

    def on_elapsed() -> None:
        state["elapsed"] = True
        maybe_finish()

    def on_snapshot(_snapshot) -> None:
        state["snapshot"] = True
        maybe_finish()

    QTimer.singleShot(3000, on_elapsed)
    manager.poller.updated.connect(on_snapshot)
    QTimer.singleShot(12000, overlay.start_fade_out)
    return overlay


def main() -> int:
    _setup_logging()

    # `--setup-only` (из run.bat при отсутствии .env): прогнать мастер первого
    # запуска и выйти, не поднимая значок в трее. Так дев-лаунчер сначала
    # получает готовый .env, а уже потом отдельными процессами стартует трей и
    # сервер — они прочитают свежий файл сами. Собранный exe этот флаг не
    # передаёт: там первый запуск ведёт прямо в полноценный GUI-режим.
    setup_only = "--setup-only" in sys.argv[1:]

    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)  # закрытие диалога не убивает трей

    # Мастер первого запуска (--setup-only) идёт мимо проверки: его вызывает
    # run.bat, когда .env ещё нет, — работающей копии в этот момент быть не
    # может, а заблокированный мастер оставил бы дев-окружение без настроек.
    if not setup_only and not _claim_single_instance():
        logger.info("Программа уже запущена — второй экземпляр не нужен")
        QMessageBox.information(
            None, "Mail Gateway",
            "Mail Gateway уже запущен.\n\nЗначок программы — в области уведомлений, "
            "рядом с часами (может быть скрыт стрелкой «Отображать скрытые значки»).",
        )
        return 0

    from app import __version__

    status = _ensure_admin(__version__)
    if status == "abort":
        return 1

    if setup_only:
        # .env и учётка созданы (или уже были). Перезапускать нечего —
        # процесс всё равно завершается, свежие настройки прочитают
        # следующие процессы run.bat.
        return 0

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "Mail Gateway", "Системный трей недоступен в этом сеансе.")
        return 1

    if status == "restart":
        _restart()
        # execv не вернётся при успехе; если вернулся — идём дальше со старыми
        # настройками: работающая программа лучше никакой.

    # Настройки читаем после мастера: при перезапуске это уже свежий .env.
    from app.config import settings

    manager = TrayIconManager(qt_app, settings.api_port, __version__)
    overlay = _show_overlay(manager, __version__)  # noqa: F841 — живёт до fade-out
    manager.setup()
    logger.info("Mail Gateway запущен (порт API %s)", settings.api_port)

    # После значка, а не до: диалог лицензии не должен задерживать появление
    # программы в трее, иначе оператор не понимает, запустилась она или нет.
    _offer_license_dialog()
    return qt_app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
