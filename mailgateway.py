"""Единая точка входа Mail Gateway — один exe на все режимы (сборка build.py).

    MailGateway.exe                        значок в трее + мастер первого запуска
    MailGateway.exe install|start|stop|
                    restart|remove         служба Windows (см. service.py)
    MailGateway.exe debug                  сервер в текущей консоли (без службы)
    MailGateway.exe create-admin <имя>     служебные команды (см. manage.py)
                    set-password, list-admins,
                    dkim-keygen, encryption-keygen

Без аргументов процесс сначала пробует отдаться диспетчеру служб — так его
запускает SCM после `MailGateway.exe install`. Если диспетчер недоступен
(ошибка 1063 — процесс запущен человеком, а не SCM), открывается GUI-режим.

Серверные режимы (служба и `debug`) требуют действующего `license.lic` и без
него не поднимаются. GUI-режим и служебные команды работают без лицензии —
иначе установить её было бы неоткуда.

Exe собирается без консоли (значок в трее не должен тащить за собой чёрное
окно), поэтому консольные команды сами подключаются к консоли родительского
процесса — вывод `install`/`create-admin` виден в том же cmd/powershell, из
которого их вызвали.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVICE_COMMANDS = frozenset({"install", "update", "remove", "start", "stop", "restart"})


def _attach_console() -> None:
    """Подключает окно-less процесс к консоли, из которой его запустили.

    В дев-режиме (обычный python.exe) консоль уже есть. В сборке без консоли
    stdout/stderr/stdin — None, и любой print уронил бы команду; после
    AttachConsole потоки открываются заново на унаследованную консоль. Если
    родительской консоли нет (запуск команды из планировщика), создаётся своя.
    """
    if not getattr(sys, "frozen", False):
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    if not kernel32.AttachConsole(-1) and not kernel32.AllocConsole():
        return
    try:
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stdin = open("CONIN$", encoding="utf-8")  # noqa: SIM115
    except OSError:
        pass


def _run_service_dispatcher() -> bool:
    """True — процесс был запущен SCM и отработал как служба до остановки."""
    import servicemanager
    import win32service
    import winerror

    from service import MailGatewayService

    try:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(MailGatewayService)
        servicemanager.StartServiceCtrlDispatcher()
        return True
    except win32service.error as exc:
        if exc.winerror == winerror.ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            return False
        raise


def main() -> int:
    args = sys.argv[1:]

    if not args:
        # Порядок важен: SCM даёт процессу ~30 с на подключение диспетчера,
        # а при ручном запуске попытка отваливается мгновенно (1063).
        if getattr(sys, "frozen", False) and _run_service_dispatcher():
            return 0
        from tray.app import main as tray_main

        return tray_main()

    command = args[0].lower()

    if command == "debug":
        # Свой debug, а не pywin32-овский: тот требует уже установленной
        # службы и перезапускает зарегистрированный в SCM exe — то есть в
        # дев-окружении запустил бы не эту сборку. Здесь сервер стартует
        # прямо в текущем процессе с выводом в консоль.
        _attach_console()
        from app.main import main as server_main

        server_main()
        return 0

    if command in SERVICE_COMMANDS:
        _attach_console()
        import win32serviceutil

        from service import MailGatewayService

        win32serviceutil.HandleCommandLine(MailGatewayService, argv=[sys.argv[0], *args])
        return 0

    _attach_console()
    from manage import main as manage_main

    return manage_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
