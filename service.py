"""Служба Windows для Mail Gateway.

    python service.py install     зарегистрировать службу
    python service.py start       запустить
    python service.py stop        остановить
    python service.py remove      удалить регистрацию

Регистрация и удаление требуют прав администратора.

Зачем это нужно: запущенный из `run.bat` шлюз живёт ровно до закрытия окна
консоли и умирает вместе с сеансом RDP. Для приёмника почты это означает не
«сервис недоступен», а потерянные письма — отправитель получает отказ и через
несколько попыток возвращает письмо автору.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import servicemanager
import win32event
import win32service
import win32serviceutil


class MailGatewayService(win32serviceutil.ServiceFramework):
    _svc_name_ = "MailGateway"
    _svc_display_name_ = "Mail Gateway"
    _svc_description_ = (
        "Почтовый шлюз: приём по SMTP и отдача через HTTP API."
    )

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):  # noqa: N802 — имя задано pywin32
        """Диспетчер служб просит остановиться."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server is not None:
            # Оповещаем uvicorn так же, как это делает Ctrl+C: он доводит до
            # конца текущие запросы и корректно закрывает SMTP-слушатель.
            # Убивать процесс нельзя — письмо, принятое, но не записанное в
            # базу, потерялось бы после подтверждения приёма отправителю.
            self.server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):  # noqa: N802 — имя задано pywin32
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._run()

    def _run(self):
        import uvicorn

        from app.config import settings
        from app.logging_setup import setup_logging
        from app.main import app
        from app.schema_setup import prepare_database

        setup_logging()
        prepare_database()

        # Учётную запись панели здесь не спрашиваем: консоли нет, и диалог
        # либо повис бы, либо сорвался. `ensure_admin_exists()` это понимает и
        # пишет в журнал, что делать, — служба стартует в любом случае.
        from app.bootstrap import ensure_admin_exists, ensure_default_client_from_env

        ensure_default_client_from_env()
        ensure_admin_exists()

        config = uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.server.run()


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Запуск без аргументов означает, что нас поднял диспетчер служб.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MailGatewayService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(MailGatewayService)
