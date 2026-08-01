; sdp の Windows per-machine インストーラー定義（Inno Setup 6.3 以降）。
;
; この .iss が installer 仕様の source of truth である。Inno Setup の GUI で
; 設定したローカル状態には依存しない。compile は scripts\build-installer.ps1 から行い、
; version と入力配布物は必ず外部から注入する。
;
;   ISCC.exe /DAppVersion=0.0.1 /DVersionInfoVersion=0.0.1.0 /DSourceDir=<dist\sdp> ...
;
; 重要な契約:
;   - per-machine（%ProgramFiles%\sdp）。UAC 昇格を要求して HKLM へ登録する。
;   - VC++ v14 Redistributable x64は同梱せず、HKLMを読み取って導入済みか確認する。
;   - 「プログラムから開く」候補として登録するだけで、既定アプリは変更しない。
;     UserChoice には一切触れない。
;   - アンインストールしてもユーザーデータ（%LOCALAPPDATA%\sdp）は削除しない。
;   - ライセンスの未解決事項が残るため、生成物は技術検証用であり公開配布物ではない。

#ifndef AppVersion
  #error AppVersion が未定義です。scripts\build-installer.ps1 から compile してください。
#endif
#ifndef VersionInfoVersion
  #error VersionInfoVersion が未定義です。scripts\build-installer.ps1 から compile してください。
#endif
#ifndef SourceDir
  #error SourceDir が未定義です。検証済みの配布物 dist\sdp を指定してください。
#endif

#define AppIdGuid "8F3B7C21-5D4E-4A96-9C2F-1E7A6B0D3F58"
#define AppName "sdp"
#define AppPublisher "sdp contributors"
#define ProgId "sdp.AudioFile"
#define Architecture "x64"
#define IconFile "..\assets\sdp.ico"

[Setup]
; AppId は version を含めない固定値。upgrade で同じ登録を引き継ぐために変更しない。
AppId={{{#AppIdGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#VersionInfoVersion}
VersionInfoProductVersion={#VersionInfoVersion}
VersionInfoProductTextVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\sdp.exe
; per-machine インストール。Program Files と HKLM へ書くため UAC 昇格を要求する。
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=sdp-{#AppVersion}-windows-{#Architecture}-setup
SetupIconFile={#IconFile}
LicenseFile={#SourceDir}\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 起動中の sdp を Restart Manager で検出して終了を依頼する。silent 実行時は
; /FORCECLOSEAPPLICATIONS を明示しない限り強制終了しない（Inno Setup の既定）。
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=yes
; 関連付けの登録をシェルへ通知する（既定アプリの変更は行わない）。
ChangesAssociations=yes

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Messages]
japanese.WelcomeLabel2=%1 をこのコンピューターへインストールします。%n%nこのインストーラーは技術検証用です。同梱ライブラリのライセンス条件が未解決のため、公開配布物ではありません。%n%n続行する前に他のアプリケーションをすべて終了してください。
japanese.ConfirmUninstall=%1 をアンインストールしますか？%n%n設定・プレイリスト・キャッシュは保持されます（%%LOCALAPPDATA%%\sdp）。

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成する"; GroupDescription: "追加のショートカット:"; Flags: unchecked

[Files]
; 入力は scripts\build-installer.ps1 が検証した onedir 配布物だけ。
; ユーザーデータと開発物は除外する（配布物側でも layout 検査で拒否している）。
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: "settings.json,playlist.json,ui-state.json,*.py,*.pyc,*.log,__pycache__\*,logs\*,cache\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\sdp.exe"; IconFilename: "{app}\sdp.exe"; Comment: "sdp を起動する"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\sdp.exe"; IconFilename: "{app}\sdp.exe"; Tasks: desktopicon

[Registry]
; --- ProgID（形式別に分けず 1 つへまとめる。sdp は全形式を同じ扱いで開くため） ---
Root: HKLM; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: ""; ValueData: "sdp 音声ファイル"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "sdp 音声ファイル"
Root: HKLM; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\sdp.exe,0"
Root: HKLM; Subkey: "Software\Classes\{#ProgId}\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"
Root: HKLM; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\sdp.exe"" ""%1"""

; --- 「プログラムから開く」候補（HKLM\Software\Classes\Applications\sdp.exe） ---
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\sdp.exe,0"
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\sdp.exe"" ""%1"""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".wav"; ValueData: ""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".mp3"; ValueData: ""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".flac"; ValueData: ""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".ogg"; ValueData: ""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".opus"; ValueData: ""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".m4a"; ValueData: ""
Root: HKLM; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".aac"; ValueData: ""

; --- 拡張子ごとの OpenWithProgids（自分の値だけを足し、自分の値だけを消す） ---
Root: HKLM; Subkey: "Software\Classes\.wav\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Classes\.mp3\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Classes\.flac\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Classes\.ogg\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Classes\.opus\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Classes\.m4a\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKLM; Subkey: "Software\Classes\.aac\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue

; --- Capabilities（Windows の「既定のアプリ」画面へ sdp を列挙させるため） ---
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Windows 11 向け個人用ローカル音声プレイヤー"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".wav"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mp3"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".flac"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".ogg"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".opus"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m4a"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".aac"; ValueData: "{#ProgId}"
Root: HKLM; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\{#AppName}\Capabilities"; Flags: uninsdeletevalue

[UninstallDelete]
; インストール先に残った実行時生成物だけを消す。%LOCALAPPDATA%\sdp（ユーザーデータ）は消さない。
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\.upgrade-backup"

[Run]
Filename: "{app}\sdp.exe"; Description: "sdp を起動する"; Flags: nowait postinstall skipifsilent

[Code]
const
  GENERIC_WRITE = $40000000;
  OPEN_EXISTING = 3;
  INVALID_HANDLE_VALUE = $FFFFFFFF;
  FILE_USE_AVAILABLE = 0;
  FILE_USE_IN_USE = 1;
  FILE_USE_UNAVAILABLE = 2;
  ERROR_SHARING_VIOLATION = 32;
  ERROR_LOCK_VIOLATION = 33;
  VC_RUNTIME_KEY = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';
  VC_RUNTIME_URL = 'https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist';

var
  UpgradeBackupActive: Boolean;
  UpgradeCommitted: Boolean;

function CreateFileW(lpFileName: String; dwDesiredAccess: LongWord; dwShareMode: LongWord;
  lpSecurityAttributes: LongWord; dwCreationDisposition: LongWord;
  dwFlagsAndAttributes: LongWord; hTemplateFile: LongWord): LongWord;
  external 'CreateFileW@kernel32.dll stdcall';

function CloseHandle(hObject: LongWord): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function GetLastError(): LongWord;
  external 'GetLastError@kernel32.dll stdcall';

function InstalledExecutable(): String;
begin
  Result := ExpandConstant('{app}\sdp.exe');
end;

function UninstallKey(): String;
begin
  Result := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    '{' + '{#AppIdGuid}' + '}_is1';
end;

function RegisteredInstallDirectory(var Directory: String): Boolean;
begin
  Result := RegQueryStringValue(
    HKEY_LOCAL_MACHINE,
    UninstallKey(),
    'Inno Setup: App Path',
    Directory
  ) and (Directory <> '');
end;

function IsVCRuntimeInstalled(): Boolean;
var
  Installed: Cardinal;
begin
  Result := RegQueryDWordValue(
    HKEY_LOCAL_MACHINE,
    VC_RUNTIME_KEY,
    'Installed',
    Installed
  ) and (Installed = 1);
end;

function EnsureVCRuntime(): Boolean;
var
  ErrorCode: Integer;
  MessageText: String;
begin
  Result := IsVCRuntimeInstalled();
  if Result then
    Exit;

  MessageText :=
    'Microsoft Visual C++ v14 Redistributable x64が必要です。' + #13#10 + #13#10 +
    'Microsoft公式の再頒布可能パッケージをインストールしてから、' +
    'sdpのセットアップを再実行してください。' + #13#10 + #13#10 +
    VC_RUNTIME_URL;
  Log(MessageText);

  if not WizardSilent then
  begin
    if MsgBox(MessageText + #13#10 + #13#10 +
      'Microsoft公式ページを開きますか？', mbError, MB_YESNO) = IDYES then
      if not ShellExec('open', VC_RUNTIME_URL, '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode) then
        MsgBox('Microsoft公式ページを開けませんでした。' + #13#10 + VC_RUNTIME_URL,
          mbError, MB_OK);
  end;
end;

function SameDirectory(const Left: String; const Right: String): Boolean;
begin
  Result := CompareText(
    RemoveBackslashUnlessRoot(Left),
    RemoveBackslashUnlessRoot(Right)
  ) = 0;
end;

function IsRegisteredPreviousInstall(const Target: String): Boolean;
var
  Registered: String;
begin
  Result := False;
  if not RegisteredInstallDirectory(Registered) then
    Exit;
  Result := SameDirectory(Registered, Target) and FileExists(Target + '\sdp.exe');
end;

function UpgradeBackupDirectory(const Target: String): String;
begin
  Result := Target + '\.upgrade-backup';
end;

function IsUninstallerFile(const Name: String): Boolean;
begin
  Result := Lowercase(Copy(Name, 1, 5)) = 'unins';
end;

function IsDotDirectory(const Name: String): Boolean;
begin
  Result := (Name = '.') or (Name = '..');
end;

{ 実行中（image として map 済み）かどうかを判定する。

  書き込みアクセスを要求して開く。実行中の exe / DLL は image section が張られて
  いるため書き込みで開けず、ERROR_SHARING_VIOLATION になる。
  **読み取りで開く判定は使えない**（Windows は実行中の exe の読み取りも、
  FILE_SHARE_DELETE による削除も許すため、実測で素通りする）。
  OPEN_EXISTING かつ書き込みを行わないので、ファイルの内容は変わらない。
  共有違反・ロック違反だけを「実行中」とし、ACL や I/O error は別状態として
  呼び出し元で「アクセスできないため中止」と表示する。 }
function FileUseState(const FileName: String): Integer;
var
  Handle: LongWord;
  ErrorCode: LongWord;
begin
  Result := FILE_USE_AVAILABLE;
  if not FileExists(FileName) then
    Exit;

  Handle := CreateFileW(FileName, GENERIC_WRITE, 0, 0, OPEN_EXISTING, 0, 0);
  if Handle <> INVALID_HANDLE_VALUE then
  begin
    CloseHandle(Handle);
    Exit;
  end;

  ErrorCode := GetLastError();
  if (ErrorCode = ERROR_SHARING_VIOLATION) or (ErrorCode = ERROR_LOCK_VIOLATION) then
    Result := FILE_USE_IN_USE
  else
  begin
    Result := FILE_USE_UNAVAILABLE;
    Log(Format('sdp.exeの使用中判定で予期しないエラー: %d', [ErrorCode]));
  end;
end;

function ExecutableAvailabilityError(const FileName: String): String;
var
  State: Integer;
begin
  Result := '';
  State := FileUseState(FileName);
  if State = FILE_USE_IN_USE then
    Result := 'sdp が実行中です。' + #13#10 +
      'sdp を終了してから、もう一度実行してください。'
  else if State = FILE_USE_UNAVAILABLE then
    { ISPP は行頭の # を前処理ディレクティブと解釈するため、#13#10 を行頭へ置かない。 }
    Result := 'sdp.exe にアクセスできないため、インストールを中止しました。' + #13#10 +
      'ファイルの権限、read-only属性、セキュリティ製品のブロックを確認してください。' + #13#10 +
      FileName;
end;

{ 起動中の sdp を無断で強制終了しない。

  Restart Manager（CloseApplications）は silent 実行時に既定でアプリを閉じてしまい、
  それは PrepareToInstall より前に起きる。そのため判定は InitializeSetup で行う。
  ここは Restart Manager が動く前なので、起動中なら確実に検出して中止できる。
  登録済みAppIdが無い初回installでは、偶然同名のsdp.exeを旧installと誤認しない。 }
function InitializeSetup(): Boolean;
var
  Registered: String;
  ErrorMessage: String;
begin
  Result := EnsureVCRuntime();
  if not Result then
    Exit;

  if not RegisteredInstallDirectory(Registered) then
    Exit;

  ErrorMessage := ExecutableAvailabilityError(Registered + '\sdp.exe');
  if ErrorMessage <> '' then
  begin
    Result := False;
    if not WizardSilent then
      MsgBox(ErrorMessage, mbError, MB_OK);
  end;
end;

function MovePathToBackup(const Source: String; const Destination: String): Boolean;
begin
  Result := RenameFile(Source, Destination);
  if not Result then
    Log(Format('upgrade backupへの移動に失敗しました: %s -> %s', [Source, Destination]));
end;

function RestoreBackupEntry(const BackupRoot: String; const Target: String;
  const FindRec: TFindRec): Boolean;
var
  Source: String;
  Destination: String;
begin
  Result := True;
  if IsDotDirectory(FindRec.Name) then
    Exit;

  Source := BackupRoot + '\' + FindRec.Name;
  Destination := Target + '\' + FindRec.Name;

  if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
  begin
    if DirExists(Destination) then
    begin
      Result := DelTree(Destination, True, True, True);
      if not Result then
      begin
        Log(Format('rollback前のdirectory削除に失敗しました: %s', [Destination]));
        Exit;
      end;
    end;
  end
  else if FileExists(Destination) then
  begin
    Result := DeleteFile(Destination);
    if not Result then
    begin
      Log(Format('rollback前のfile削除に失敗しました: %s', [Destination]));
      Exit;
    end;
  end;

  Result := RenameFile(Source, Destination);
  if not Result then
    Log(Format('upgrade backupからの復元に失敗しました: %s -> %s', [Source, Destination]));
end;

function RestoreUpgradeBackup(const Target: String): Boolean;
var
  BackupRoot: String;
  FindRec: TFindRec;
begin
  Result := True;
  BackupRoot := UpgradeBackupDirectory(Target);
  if not DirExists(BackupRoot) then
    Exit;

  Log(Format('upgrade backupを復元します: %s', [BackupRoot]));
  if FindFirst(BackupRoot + '\*', FindRec) then
  begin
    try
      repeat
        Result := RestoreBackupEntry(BackupRoot, Target, FindRec) and Result;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;

  if Result then
  begin
    Result := DelTree(BackupRoot, True, True, True);
    if not Result then
      Log(Format('復元済みupgrade backupの削除に失敗しました: %s', [BackupRoot]));
  end;
end;

function MoveRootFilesToBackup(const Target: String; const BackupRoot: String): Boolean;
var
  FindRec: TFindRec;
  Source: String;
  Destination: String;
begin
  Result := True;
  if FindFirst(Target + '\*', FindRec) then
  begin
    try
      repeat
        if ((FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0) and
          (not IsUninstallerFile(FindRec.Name)) then
        begin
          Source := Target + '\' + FindRec.Name;
          Destination := BackupRoot + '\' + FindRec.Name;
          Result := MovePathToBackup(Source, Destination) and Result;
        end;
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

{ upgrade で旧 runtime DLL や不要になった plugin が残らないよう、
  ファイル展開の直前に旧runtimeを同一volume上のbackupへ移動する。
  cleanup対象は固定AppIdのuninstall登録があり、その登録済みinstall directoryと
  現在のインストール先が一致する場合だけに限定する。初回installで偶然sdp.exeがある
  無関係なdirectoryを削除しない。
  （Pascal のブロックコメント内では最初の閉じ波括弧でコメントが終わるため、
  app などの Inno 定数を波括弧付きで書かないこと。） }
function CleanPreviousInstall(): Boolean;
var
  Target: String;
  BackupRoot: String;
begin
  Result := True;
  Target := ExpandConstant('{app}');
  UpgradeBackupActive := False;
  UpgradeCommitted := False;

  if not IsRegisteredPreviousInstall(Target) then
  begin
    Log(Format('登録済みの既存sdp install先ではないためcleanupをスキップします: %s', [Target]));
    Exit;
  end;

  BackupRoot := UpgradeBackupDirectory(Target);
  if DirExists(BackupRoot) then
  begin
    Log('前回のupgrade backupが残っているため、先に復元します。');
    if not RestoreUpgradeBackup(Target) then
    begin
      Result := False;
      Exit;
    end;
  end;

  if not CreateDir(BackupRoot) then
  begin
    Log(Format('upgrade backup directoryを作成できません: %s', [BackupRoot]));
    Result := False;
    Exit;
  end;

  if DirExists(Target + '\_internal') then
    Result := MovePathToBackup(Target + '\_internal', BackupRoot + '\_internal') and Result;
  Result := MoveRootFilesToBackup(Target, BackupRoot) and Result;

  if Result then
    UpgradeBackupActive := True
  else
  begin
    Log('upgrade backup作成に失敗したため、移動済みファイルを復元します。');
    RestoreUpgradeBackup(Target);
  end;
end;

function CommitUpgradeBackup(): Boolean;
var
  BackupRoot: String;
begin
  Result := True;
  BackupRoot := UpgradeBackupDirectory(ExpandConstant('{app}'));
  if not DirExists(BackupRoot) then
    Exit;
  Result := DelTree(BackupRoot, True, True, True);
  if not Result then
    Log(Format('upgrade成功後のbackup削除に失敗しました: %s', [BackupRoot]));
end;

function ShouldInjectFailureAfterCleanup(): Boolean;
begin
  { installer-smoke専用。cleanup後・展開前の失敗時rollbackを検証するための隠しparam。 }
  Result := ExpandConstant('{param:SDP_FAIL_AFTER_CLEANUP|0}') = '1';
end;

{ InitializeSetup 通過後にウィザード操作中へ起動された場合の二段目の防波堤。
  ここで中止すれば、旧 exe と新 DLL が混在した状態は作られない。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ErrorMessage: String;
begin
  Result := '';
  ErrorMessage := ExecutableAvailabilityError(InstalledExecutable());
  if ErrorMessage <> '' then
    Result := ErrorMessage;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    if not CleanPreviousInstall() then
      RaiseException('既存インストールの安全な退避に失敗したため、インストールを中止しました。');
    if ShouldInjectFailureAfterCleanup() then
      RaiseException('installer smoke用にcleanup後の失敗を発生させました。');
  end
  else if CurStep = ssPostInstall then
  begin
    if UpgradeBackupActive then
    begin
      if not CommitUpgradeBackup() then
        Log('upgrade backupを削除できませんでした。次回install時に復元・整理を試みます。');
      UpgradeCommitted := True;
      UpgradeBackupActive := False;
    end;
  end;
end;

procedure DeinitializeSetup();
begin
  if UpgradeBackupActive and (not UpgradeCommitted) then
  begin
    if RestoreUpgradeBackup(ExpandConstant('{app}')) then
      Log('インストール未完了のため、旧installをbackupから復元しました。')
    else
      Log('インストール未完了後の旧install復元に失敗しました。');
  end;
end;

{ 実行中のアンインストールは行わない（保存中のユーザーデータを壊さないため）。
  再起動後削除へは送らず、利用者へ終了を依頼する。 }
function InitializeUninstall(): Boolean;
var
  ErrorMessage: String;
begin
  Result := True;
  ErrorMessage := ExecutableAvailabilityError(InstalledExecutable());
  if ErrorMessage <> '' then
  begin
    Result := False;
    if not UninstallSilent then
      MsgBox(ErrorMessage, mbError, MB_OK);
  end;
end;
