; Установщик Mail Gateway (Inno Setup 6.3 и новее, проверено на 7).
;
;   1. .venv\Scripts\python.exe build.py          собрать dist\MailGateway
;   2. ISCC.exe installer\mail_gateway.iss        собрать установщик
;
; Тихая установка (развёртывание скриптом):
;   MailGateway-Setup-1.0.0.exe /VERYSILENT /LICENSEFILE="C:\путь\license.lic"
;
; Шаг 1 сам вызывает installer\make_license.py (текст соглашения и
; version.iss) и, если ISCC найден, сразу выполняет шаг 2. Результат —
; dist\MailGateway-Setup-<версия>.exe.
;
; ВАЖНО: файл хранится в UTF-8 С СИГНАТУРОЙ (BOM). Без неё Inno Setup 6
; читает его как ANSI, и русские строки мастера превращаются в мусор.

; Двойная фигурная скобка — не опечатка: Inno Setup читает одиночную `{` как
; начало константы, `{{` означает литеральную скобку в значении AppId.
#define AppId "{{6B9DEA50-8270-4033-B20D-6717265FDC9A}"
#define ServiceName "MailGateway"
#define SourceDir "..\dist\MailGateway"

; Версия и реквизиты правообладателя — из installer\make_license.py.
; Файла нет? Значит, не выполнен шаг 1: он его и создаёт.
#include "version.iss"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppLicensingURL}
AppSupportURL={#AppLicensingURL}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} — почтовый шлюз с HTTP API

; Не в Program Files. Операторские файлы (.env, license.lic, data\, база)
; живут рядом с exe — так устроен BASE_DIR в app\config.py. В Program Files
; программа, запущенная от обычной учётной записи, не смогла бы записать
; ни лицензию, ни настройки, а служба — базу.
DefaultDirName={sd}\MailGateway
DefaultGroupName={#AppName}
DisableDirPage=no
DisableProgramGroupPage=yes

; Соглашение показывается до выбора каталога и кладётся в каталог программы.
LicenseFile=LICENSE.txt

OutputDir=..\dist
OutputBaseFilename=MailGateway-Setup-{#AppVersion}
SetupIconFile=..\tray\icon.ico
UninstallDisplayIcon={app}\MailGateway.exe
UninstallDisplayName={#AppName} {#AppVersion}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

; Служба и правила брандмауэра — только с правами администратора.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=6.1sp1

; Обновление поверх работающей копии: службу останавливаем сами (см. [Code]),
; а запущенная программа держит открытыми файлы сборки — её закрывает Inno Setup.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "service"; Description: "Зарегистрировать службу Windows и запустить её"; GroupDescription: "Служба"
Name: "traystartup"; Description: "Запускать при входе в систему (для всех пользователей)"; GroupDescription: "Автозапуск"
Name: "firewallsmtp"; Description: "Разрешить входящие подключения SMTP (TCP 25)"; GroupDescription: "Брандмауэр Windows"
Name: "firewallapi"; Description: "Разрешить входящие подключения к HTTP API (TCP 8025)"; GroupDescription: "Брандмауэр Windows"; Flags: unchecked

[Files]
; Excludes — не перестраховка. Стоит запустить собранный exe прямо из
; dist\MailGateway (обычное дело при проверке сборки), и рядом появляются
; data\ с журналами и ключами, .env и база. Без этого списка они уезжают
; заказчику внутри установщика, а при удалении программы Inno стирает у него
; одноимённые файлы — уже его собственные. Проверено на живом прогоне.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "data\*,.env,license.lic,schema.lock,*.db,*.db-shm,*.db-wal"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion

; Секции [Dirs] здесь намеренно нет. Каталог data\ (журналы, вложения, копии,
; ключи шифрования) создаёт сама программа при первом запуске, а объявленный
; в [Dirs] каталог удаление программы вычищает ВМЕСТЕ С СОДЕРЖИМЫМ — проверено
; на живом прогоне: журналы исчезали, и флаг uninsneveruninstall этого не
; менял. Цена ошибки — потерянный ключ шифрования, без которого резервные
; копии переписки невосстановимы. Не объявлено — не удалено.

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\MailGateway.exe"
Name: "{group}\Панель управления"; Filename: "http://localhost:8025/admin/"
Name: "{group}\Журналы"; Filename: "{app}\data\logs"
Name: "{group}\Лицензионное соглашение"; Filename: "{app}\LICENSE.txt"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{commondesktop}\{#AppName}"; Filename: "{app}\MailGateway.exe"
; Автозагрузка для всех пользователей, а не {userstartup}: установщик работает
; от администратора, и ярлык в его личной автозагрузке не появился бы у
; оператора, который потом входит на сервер под своей учётной записью.
Name: "{commonstartup}\{#AppName}"; Filename: "{app}\MailGateway.exe"; Tasks: traystartup

[Run]
; Правила брандмауэра — здесь: они ни от чего не зависят и их неудача не
; мешает работе. Регистрация службы, наоборот, идёт из [Code] (SetupService):
; порядок относительно копирования лицензии там задан явно, а результат
; каждого шага проверяется — молча «установить» неработающую службу нельзя.
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Mail Gateway SMTP"" dir=in action=allow protocol=TCP localport=25 program=""{app}\MailGateway.exe"""; StatusMsg: "Настройка брандмауэра..."; Flags: runhidden; Tasks: firewallsmtp
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Mail Gateway API"" dir=in action=allow protocol=TCP localport=8025 program=""{app}\MailGateway.exe"""; StatusMsg: "Настройка брандмауэра..."; Flags: runhidden; Tasks: firewallapi

Filename: "{app}\MailGateway.exe"; Description: "Запустить {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\MailGateway.exe"; Parameters: "stop"; RunOnceId: "StopService"; Flags: runhidden
Filename: "{app}\MailGateway.exe"; Parameters: "remove"; RunOnceId: "RemoveService"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Mail Gateway SMTP"""; RunOnceId: "DelRuleSmtp"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Mail Gateway API"""; RunOnceId: "DelRuleApi"; Flags: runhidden

[Code]
var
  LicensePage: TInputFileWizardPage;

function ServiceInstalled(): Boolean;
begin
  Result := RegKeyExists(HKEY_LOCAL_MACHINE,
                         'SYSTEM\CurrentControlSet\Services\{#ServiceName}');
end;

{ Работающая служба держит exe открытым: пока она не остановлена, файлы
  сборки не заменить. }
function StopService(): Boolean;
var
  ResultCode: Integer;
begin
  { `net stop` не возвращает управление, пока служба не остановится, —
    в отличие от `sc stop`, который лишь отправляет запрос и сразу выходит.
    Код 2 — «служба не запущена», для нас это тоже успех. }
  Result := Exec(ExpandConstant('{sys}\net.exe'), 'stop {#ServiceName}',
                 '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and ((ResultCode = 0) or (ResultCode = 2));
end;

procedure InitializeWizard();
begin
  LicensePage := CreateInputFilePage(wpSelectTasks,
    'Файл лицензии',
    'Укажите файл лицензии, полученный от правообладателя.',
    'Без действующего файла license.lic программа устанавливается и' + #13#10 +
    'работает, но не принимает и не отправляет почту.' + #13#10 + #13#10 +
    'Если файла ещё нет, оставьте поле пустым и продолжите установку —' + #13#10 +
    'лицензию можно установить позже: значок Mail Gateway в трее, пункт' + #13#10 +
    '«Лицензия...». Почта пойдёт сразу после этого, без перезапуска.');
  LicensePage.Add('Файл лицензии (.lic):', 'Файл лицензии|*.lic|Все файлы|*.*', '.lic');
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  { При обновлении поверх рабочей установки лицензия уже на месте; не
    спрашиваем и когда путь задан параметром /LICENSEFILE. }
  Result := (PageID = LicensePage.ID)
            and (FileExists(ExpandConstant('{app}\license.lic'))
                 or (ExpandConstant('{param:licensefile|}') <> ''));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = LicensePage.ID) and (LicensePage.Values[0] <> '')
     and (not FileExists(LicensePage.Values[0])) then
  begin
    MsgBox('Файл не найден:' + #13#10 + LicensePage.Values[0], mbError, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if ServiceInstalled() and (not StopService()) then
    Result := 'Не удалось остановить службу «{#ServiceName}».' + #13#10 +
              'Остановите её вручную (services.msc) и повторите установку.';
end;

{ Путь к .lic: со страницы мастера, а в тихой установке — из параметра
  командной строки /LICENSEFILE="C:\путь\license.lic". }
function ChosenLicenseFile(): String;
begin
  Result := '';
  if Assigned(LicensePage) then
    Result := LicensePage.Values[0];
  if Result = '' then
    Result := ExpandConstant('{param:licensefile|}');
end;

function RunHidden(const FileName, Params: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(FileName, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
            and (ResultCode = 0);
end;

procedure InstallLicenseFile();
var
  Source, Target: String;
begin
  Source := ChosenLicenseFile();
  if Source = '' then
    Exit;
  Target := ExpandConstant('{app}\license.lic');
  { FileCopy, а не CopyFile: в Inno Setup 7 функцию переименовали, но старое
    имя работает в обеих версиях, а новое — только в 7. Подсказка компилятора
    об устаревшем имени здесь ожидаема. }
  if not FileCopy(Source, Target, False) then
    SuppressibleMsgBox('Не удалось скопировать файл лицензии в' + #13#10 + Target + #13#10 +
                       'Установите его позже: значок в трее, пункт «Лицензия...».',
                       mbError, MB_OK, IDOK);
end;

procedure SetupService();
var
  Exe, ServiceControl, Command: String;
begin
  Exe := ExpandConstant('{app}\MailGateway.exe');
  ServiceControl := ExpandConstant('{sys}\sc.exe');

  { `install` на уже зарегистрированной службе завершается ошибкой, а
    `update` переписывает путь к exe — он мог измениться вместе с каталогом. }
  if ServiceInstalled() then
    Command := 'update'
  else
    Command := 'install';

  WizardForm.StatusLabel.Caption := 'Регистрация службы Windows...';
  RunHidden(Exe, Command);

  { Проверяем не код возврата, а результат: служба либо появилась в системе,
    либо нет. Молчаливое «установлено» при незарегистрированной службе —
    худший исход: почта не работает, а установщик отрапортовал успех. }
  if not ServiceInstalled() then
  begin
    SuppressibleMsgBox('Не удалось зарегистрировать службу Windows «{#ServiceName}».' + #13#10 + #13#10 +
                       'Программа установлена, но почта работать не будет. Выполните от' + #13#10 +
                       'имени администратора и посмотрите на сообщение об ошибке:' + #13#10 + #13#10 +
                       '    "' + Exe + '" install',
                       mbError, MB_OK, IDOK);
    Exit;
  end;

  { Автозапуск при загрузке сервера: у pywin32 по умолчанию ручной, а
    почтовый приёмник обязан подниматься сам после перезагрузки. }
  RunHidden(ServiceControl, 'config {#ServiceName} start= auto');
  { Перезапуск после сбоя. Штатная остановка (в том числе по окончании срока
    лицензии) сбоем не считается и перезапуска не вызывает. }
  RunHidden(ServiceControl, 'failure {#ServiceName} reset= 86400 actions= restart/60000/restart/60000/restart/300000');

  { Служба запускается всегда, даже без лицензии: она поднимается, но не
    принимает и не отправляет почту. Так оператор получает рабочую панель
    (через неё же ставится лицензия) вместо остановленной службы, которую
    потом нельзя запустить без прав администратора. }
  WizardForm.StatusLabel.Caption := 'Запуск службы...';
  if not RunHidden(Exe, 'start') then
  begin
    SuppressibleMsgBox('Служба зарегистрирована, но не запустилась.' + #13#10 + #13#10 +
                       'Причина записана в журнал событий Windows (Просмотр событий,' + #13#10 +
                       'Журналы Windows, Приложение — источник «{#ServiceName}») и в' + #13#10 +
                       'подкаталоге data\logs.',
                       mbError, MB_OK, IDOK);
    Exit;
  end;

  if not FileExists(ExpandConstant('{app}\license.lic')) then
    SuppressibleMsgBox('Установка завершена, служба запущена.' + #13#10 + #13#10 +
                       'Лицензия не установлена, поэтому приём и отправка почты пока' + #13#10 +
                       'не работают. Установите файл .lic — значок Mail Gateway в трее,' + #13#10 +
                       'пункт «Лицензия...» — и почта пойдёт сразу, без перезапуска.',
                       mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep <> ssPostInstall then
    Exit;

  { Порядок задан здесь явно, а не распределён между [Run] и [Code]: лицензия
    обязана лечь на место до первого старта службы, иначе та откажется
    подниматься и оператор увидит остановленную службу без объяснений. }
  InstallLicenseFile();
  if WizardIsTaskSelected('service') then
    SetupService();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    SuppressibleMsgBox('Программа удалена.' + #13#10 + #13#10 +
           'Данные остались в каталоге установки и удаляются только вручную:' + #13#10 +
           '  .env (настройки), license.lic (лицензия),' + #13#10 +
           '  mail_gateway.db (переписка), data\ (вложения, журналы, ключи).' + #13#10 + #13#10 +
           'Ключ шифрования из data\keys нужен для чтения резервных копий —' + #13#10 +
           'не удаляйте его, пока копии могут понадобиться.',
           mbInformation, MB_OK, IDOK);
end;
