# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec — Mail Gateway. Сборка onedir, ОДИН exe на все режимы
# (точка входа mailgateway.py):
#
#   MailGateway.exe                       значок в трее + мастер первого запуска
#   MailGateway.exe install|start|...     служба Windows (service.py)
#   MailGateway.exe create-admin|...      служебные команды (manage.py)
#
# console=False: основной сценарий — значок в трее, чёрное окно ему ни к
# чему. Консольные команды подключаются к родительской консоли сами
# (mailgateway.py::_attach_console), служба в консоли не нуждается.
#
# Запускать не напрямую, а через `python build.py`: после PyInstaller там
# шифруются шаблоны панели и раскладываются сопутствующие файлы.
#
# В сборку намеренно НЕ входят: .env (секреты оператора), license.lic
# (выпускается сервером лицензирования), data/ (создаётся при работе).
# Они живут рядом с exe — см. BASE_DIR в app/config.py.

import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ICON = 'tray/icon.ico'

# pywin32 кладёт модули не в site-packages, а в подкаталоги `win32` и
# `win32/lib`, и добавляет их в sys.path файлом `pywin32.pth` при старте
# интерпретатора. Анализ PyInstaller эти пути не наследует и не находит ни
# одного модуля pywin32: сборка получается без win32serviceutil, а
# `MailGateway.exe install` падает с ModuleNotFoundError уже у заказчика.
# Отдаём каталоги явным путём поиска.
_SITE_PACKAGES = Path(sysconfig.get_paths()['purelib'])
pathex = [
    str(path)
    for path in (
        _SITE_PACKAGES / 'win32',
        _SITE_PACKAGES / 'win32' / 'lib',
        _SITE_PACKAGES / 'Pythonwin',
    )
    if path.is_dir()
]

# Пакеты `app` и `tray` собираются целиком: сервисы, воркеры и диалоги
# подключаются импортами внутри функций, которые статический анализ может
# не увидеть.
hiddenimports = (
    collect_submodules('app')
    + collect_submodules('tray')
    + collect_submodules('uvicorn')
    + [
        # Служба Windows целиком держится на pywin32. Перечислены явно, а не
        # понадеявшись на анализ импортов: половина из них подтягивается
        # динамически (win32timezone — самим фреймворком службы, известная
        # причина ошибки 1053), а цена промаха — неработающая служба у
        # заказчика вместо ошибки сборки.
        'servicemanager',
        'win32serviceutil',
        'win32service',
        'win32event',
        'win32timezone',
        'winerror',
        'pywintypes',
        'win32api',
    ]
)

datas = [
    # Шаблоны панели: после сборки build.py шифрует их в .enc.
    ('app/web/templates', 'app/web/templates'),
    # Миграции Alembic исполняются из файлов на диске (env.py + versions).
    ('migrations/env.py', 'migrations'),
    ('migrations/script.py.mako', 'migrations'),
    ('migrations/versions', 'migrations/versions'),
    # Значок трея: icon_path() ищет его в корне _internal.
    ('tray/icon.ico', '.'),
]
# Список публичных суффиксов authheaders — читается с диска при проверке DMARC.
datas += collect_data_files('authheaders')
datas += collect_data_files('cryptography')

# Чужие привязки к Qt. PyInstaller тянет в сборку всё, что найдёт в venv, а
# PyQt5/PyQt6 распространяются под GPL: случайно оставшийся в окружении пакет
# добавил бы в проприетарную поставку GPL-код. Трей работает на PySide6 (LGPL).
excludes = ['PyQt5', 'PyQt6', 'PySide2', 'tkinter']

a = Analysis(
    ['mailgateway.py'],
    pathex=pathex,
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='MailGateway',
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='MailGateway',
)
