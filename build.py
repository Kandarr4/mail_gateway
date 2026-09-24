"""Сборка Mail Gateway в поставляемый каталог `dist/MailGateway`.

    .venv\\Scripts\\python.exe build.py              каталог + установщик
    .venv\\Scripts\\python.exe build.py --no-installer   только каталог

Шаги:
  1. PyInstaller по `mail_gateway.spec` (onedir, один exe на все режимы);
  2. шифрование шаблонов панели: каждый `.html` в сборке превращается в
     `.enc` (Fernet), оригинал удаляется, обфусцированный ключ кладётся в
     `encryption_key.dat` рядом с шаблонами — расшифровку на лету делает
     `app/web/encrypted_loader.py`;
  3. раскладка сопутствующих файлов (.env.example) и проверка результата;
  4. установщик Inno Setup (`installer/mail_gateway.iss`) — если в системе
     найден ISCC.exe. Не найден — шаг пропускается с подсказкой, каталог
     сборки от этого не страдает.

В сборку не входят и не должны попадать: `.env`, `license.lic`, `data/`,
`mail_gateway.db*` — операторские файлы живут рядом с exe (BASE_DIR).

Развёртывание собранного каталога:
    MailGateway.exe install && MailGateway.exe start     служба
    MailGateway.exe    — значок в трее; при первом запуске сам потребует
                         создать учётную запись панели (мастер настройки)
"""

import shutil
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist" / "MailGateway"
INTERNAL_DIR = DIST_DIR / "_internal"
TEMPLATES_DIR = INTERNAL_DIR / "app" / "web" / "templates"
INSTALLER_DIR = BASE_DIR / "installer"

#: Файлы, обязанные оказаться в готовой сборке.
EXPECTED = ("MailGateway.exe",)

#: Где искать компилятор Inno Setup. Версия в имени каталога («Inno Setup 6»,
#: «Inno Setup 7») меняется с каждым мажорным выпуском, поэтому перебираем
#: маской, а не списком: иначе установленный компилятор новой версии молча
#: не находится и установщик не собирается.
ISCC_SEARCH_DIRS = (
    Path(r"C:\Program Files"),
    Path(r"C:\Program Files (x86)"),
)


def run_pyinstaller() -> None:
    print("[1/4] PyInstaller…")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("  PyInstaller не установлен, ставлю…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "pyinstaller"], check=True
        )

    for stale in (BASE_DIR / "build", DIST_DIR):
        if stale.exists():
            shutil.rmtree(stale)

    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "mail_gateway.spec", "--noconfirm"],
        cwd=BASE_DIR,
        check=True,
    )


def encrypt_templates() -> int:
    """Шифрует шаблоны в сборке и кладёт рядом обфусцированный ключ.

    Ключ создаётся заново на каждую сборку — привязывать сборки друг к другу
    незачем, шаблоны шифруются и читаются внутри одного каталога.
    """
    from cryptography.fernet import Fernet

    from app.web.encrypted_loader import KEY_FILE_NAME, obfuscate_key

    print("[2/4] Шифрование шаблонов…")
    if not TEMPLATES_DIR.is_dir():
        raise SystemExit(f"Каталог шаблонов не найден в сборке: {TEMPLATES_DIR}")

    key = Fernet.generate_key()
    cipher = Fernet(key)
    count = 0
    for source in sorted(TEMPLATES_DIR.rglob("*.html")):
        source.with_suffix(source.suffix + ".enc").write_bytes(
            cipher.encrypt(source.read_bytes())
        )
        source.unlink()
        count += 1
        print(f"  {source.relative_to(TEMPLATES_DIR)} -> .enc")

    (TEMPLATES_DIR.parent / KEY_FILE_NAME).write_bytes(obfuscate_key(key))
    return count


def finalize() -> None:
    print("[3/4] Раскладка и проверка…")
    shutil.copy2(BASE_DIR / ".env.example", DIST_DIR / ".env.example")

    # Мусор от включения каталога миграций как данных.
    for pycache in INTERNAL_DIR.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)

    # Каталог data/ появляется, если собранный exe запускали прямо из dist:
    # служба и трей пишут туда журналы, а мастер настройки — ключи. Уехав к
    # заказчику внутри установщика, они и чужие данные раскрывают, и при
    # удалении программы стирают у него одноимённые файлы (Inno считает их
    # своими). Чистим молча: это след проверки сборки, а не ошибка.
    stray_data = DIST_DIR / "data"
    if stray_data.is_dir():
        shutil.rmtree(stray_data, ignore_errors=True)
        print("  удалён след проверки: data/")
    # Файл блокировки миграций (app/schema_setup.py) — того же рода след.
    for stray_lock in DIST_DIR.rglob("schema.lock"):
        stray_lock.unlink(missing_ok=True)
        print("  удалён след проверки: schema.lock")

    problems = [name for name in EXPECTED if not (DIST_DIR / name).is_file()]
    leftovers = list(TEMPLATES_DIR.rglob("*.html"))
    encrypted = list(TEMPLATES_DIR.rglob("*.enc"))
    for forbidden in (".env", "license.lic", "mail_gateway.db", "data"):
        if list(DIST_DIR.rglob(forbidden)):
            problems.append(f"в сборку попал операторский файл: {forbidden}")
    if leftovers:
        problems.append(f"остались незашифрованные шаблоны: {leftovers}")
    if not encrypted:
        problems.append("в сборке нет ни одного зашифрованного шаблона")
    if problems:
        raise SystemExit("Сборка неполная:\n  - " + "\n  - ".join(map(str, problems)))


def find_iscc() -> Path | None:
    """ISCC.exe: в PATH или в «Program Files\\Inno Setup N». Новейшая версия."""
    found = shutil.which("ISCC")
    if found:
        return Path(found)

    candidates = [
        path
        for directory in ISCC_SEARCH_DIRS
        if directory.is_dir()
        for path in directory.glob("Inno Setup*/ISCC.exe")
    ]
    # Сортировка по имени каталога: «Inno Setup 7» новее «Inno Setup 6».
    return max(candidates, key=lambda path: path.parent.name, default=None)


def build_installer() -> Path | None:
    """Текст соглашения + компиляция установщика. None — ISCC не найден."""
    # flush: следом пишет дочерний процесс напрямую в консоль, и без сброса
    # буфера заголовок шага оказался бы после его вывода.
    print("[4/4] Установщик Inno Setup…", flush=True)
    # Соглашение и version.iss пересобираются всегда: версия программы и
    # реквизиты правообладателя меняются в своих файлах, а установщик обязан
    # нести их свежими, а не те, что сгенерировались когда-то раньше.
    subprocess.run(
        [sys.executable, str(INSTALLER_DIR / "make_license.py")],
        cwd=BASE_DIR,
        check=True,
    )

    iscc = find_iscc()
    if iscc is None:
        print(
            "  ISCC.exe не найден — установщик не собран.\n"
            "  Поставьте Inno Setup 6 (https://jrsoftware.org/isdl.php) и\n"
            "  повторите, либо соберите вручную:\n"
            f'    ISCC.exe "{INSTALLER_DIR / "mail_gateway.iss"}"'
        )
        return None

    subprocess.run([str(iscc), str(INSTALLER_DIR / "mail_gateway.iss")], cwd=BASE_DIR, check=True)

    from app import __version__

    result = BASE_DIR / "dist" / f"MailGateway-Setup-{__version__}.exe"
    if not result.is_file():
        raise SystemExit(f"ISCC отработал, но установщика нет: {result}")
    return result


def main() -> int:
    with_installer = "--no-installer" not in sys.argv[1:]

    run_pyinstaller()
    count = encrypt_templates()
    finalize()
    installer = build_installer() if with_installer else None

    if installer is not None:
        print(
            f"\nГотово: {installer}\n"
            f"  каталог сборки: {DIST_DIR}\n"
            f"  зашифровано шаблонов: {count}\n\n"
            "Установщик спрашивает файл лицензии, регистрирует службу с\n"
            "автозапуском и правилами брандмауэра. Данные при удалении\n"
            "не стираются."
        )
        return 0

    print(
        f"\nГотово: {DIST_DIR}\n"
        f"  зашифровано шаблонов: {count}\n\n"
        "Развёртывание на целевой машине (всё — один MailGateway.exe):\n"
        "  1. скопировать каталог, рядом с exe создать .env по .env.example;\n"
        "  2. положить license.lic рядом с exe — без действующей лицензии\n"
        "     служба не запустится (файл можно установить и позже: значок в\n"
        "     трее, пункт «Лицензия...»);\n"
        "  3. MailGateway.exe encryption-keygen и dkim-keygen — ключи;\n"
        "  4. MailGateway.exe install и MailGateway.exe start — служба;\n"
        "  5. MailGateway.exe без аргументов — значок в трее (ярлык в\n"
        "     автозагрузку); при первом запуске мастер потребует создать\n"
        "     учётную запись панели."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
