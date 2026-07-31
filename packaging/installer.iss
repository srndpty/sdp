; sdp の Windows per-user インストーラー定義（Inno Setup 6.3 以降）。
;
; この .iss が installer 仕様の source of truth である。Inno Setup の GUI で
; 設定したローカル状態には依存しない。compile は scripts\build-installer.ps1 から行い、
; version と入力配布物は必ず外部から注入する。
;
;   ISCC.exe /DAppVersion=0.0.1 /DVersionInfoVersion=0.0.1.0 /DSourceDir=<dist\sdp> ...
;
; 重要な契約:
;   - per-user（%LOCALAPPDATA%\Programs\sdp）。UAC 昇格を要求せず HKLM へ書かない。
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
; per-user インストール。UAC 昇格を求めず、コマンドラインからの昇格指定も許可しない。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=
DefaultDirName={localappdata}\Programs\{#AppName}
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
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\sdp.exe"; IconFilename: "{app}\sdp.exe"; Tasks: desktopicon

[Registry]
; --- ProgID（形式別に分けず 1 つへまとめる。sdp は全形式を同じ扱いで開くため） ---
Root: HKCU; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: ""; ValueData: "sdp 音声ファイル"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\{#ProgId}"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "sdp 音声ファイル"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\sdp.exe,0"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"
Root: HKCU; Subkey: "Software\Classes\{#ProgId}\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\sdp.exe"" ""%1"""

; --- 「プログラムから開く」候補（HKCU\Software\Classes\Applications\sdp.exe） ---
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\sdp.exe,0"
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\shell\open"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "{#AppName}"
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\sdp.exe"" ""%1"""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".wav"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".mp3"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".flac"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".ogg"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".opus"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".m4a"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\Applications\sdp.exe\SupportedTypes"; ValueType: string; ValueName: ".aac"; ValueData: ""

; --- 拡張子ごとの OpenWithProgids（自分の値だけを足し、自分の値だけを消す） ---
Root: HKCU; Subkey: "Software\Classes\.wav\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.mp3\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.flac\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.ogg\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.opus\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.m4a\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\.aac\OpenWithProgids"; ValueType: string; ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue

; --- Capabilities（Windows の「既定のアプリ」画面へ sdp を列挙させるため） ---
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Windows 11 向け個人用ローカル音声プレイヤー"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".wav"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mp3"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".flac"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".ogg"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".opus"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m4a"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\{#AppName}\Capabilities\FileAssociations"; ValueType: string; ValueName: ".aac"; ValueData: "{#ProgId}"
Root: HKCU; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "{#AppName}"; ValueData: "Software\{#AppName}\Capabilities"; Flags: uninsdeletevalue

[UninstallDelete]
; インストール先に残った実行時生成物だけを消す。%LOCALAPPDATA%\sdp（ユーザーデータ）は消さない。
Type: filesandordirs; Name: "{app}\_internal"

[Run]
Filename: "{app}\sdp.exe"; Description: "sdp を起動する"; Flags: nowait postinstall skipifsilent

[Code]
const
  GENERIC_WRITE = $40000000;
  OPEN_EXISTING = 3;
  INVALID_HANDLE_VALUE = $FFFFFFFF;

function CreateFileW(lpFileName: String; dwDesiredAccess: LongWord; dwShareMode: LongWord;
  lpSecurityAttributes: LongWord; dwCreationDisposition: LongWord;
  dwFlagsAndAttributes: LongWord; hTemplateFile: LongWord): LongWord;
  external 'CreateFileW@kernel32.dll stdcall';

function CloseHandle(hObject: LongWord): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

{ 実行中（image として map 済み）かどうかを判定する。

  書き込みアクセスを要求して開く。実行中の exe / DLL は image section が張られて
  いるため書き込みで開けず、ERROR_SHARING_VIOLATION になる。
  **読み取りで開く判定は使えない**（Windows は実行中の exe の読み取りも、
  FILE_SHARE_DELETE による削除も許すため、実測で素通りする）。
  OPEN_EXISTING かつ書き込みを行わないので、ファイルの内容は変わらない。
  失敗時は INVALID_HANDLE_VALUE が返る（CloseHandle はこの値でも成功を返すため、
  成否判定には使えない。実測で確認済み）。 }
function IsFileInUse(const FileName: String): Boolean;
var
  Handle: LongWord;
begin
  Result := False;
  if not FileExists(FileName) then
    Exit;
  Handle := CreateFileW(FileName, GENERIC_WRITE, 0, 0, OPEN_EXISTING, 0, 0);
  if Handle = INVALID_HANDLE_VALUE then
    Result := True
  else
    CloseHandle(Handle);
end;

function InstalledExecutable(): String;
begin
  Result := ExpandConstant('{app}\sdp.exe');
end;

{ 前回のインストール先。Inno Setup 自身が uninstall キーへ書く値から取る
  （既定以外の場所へ入れていても正しく見つけられる）。 }
function PreviousInstallDirectory(): String;
var
  Key: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    '{' + '{#AppIdGuid}' + '}_is1';
  if not RegQueryStringValue(HKEY_CURRENT_USER, Key, 'Inno Setup: App Path', Result) then
    Result := ExpandConstant('{localappdata}\Programs\{#AppName}');
end;

{ 起動中の sdp を無断で強制終了しない。

  Restart Manager（CloseApplications）は silent 実行時に既定でアプリを閉じてしまい、
  それは PrepareToInstall より前に起きる。そのため判定は InitializeSetup で行う。
  ここは Restart Manager が動く前なので、起動中なら確実に検出して中止できる。 }
function InitializeSetup(): Boolean;
begin
  Result := True;
  if IsFileInUse(PreviousInstallDirectory() + '\sdp.exe') then
  begin
    Result := False;
    if not WizardSilent then
      MsgBox('sdp が実行中です。' + #13#10 +
        'sdp を終了してから、もう一度実行してください。', mbError, MB_OK);
  end;
end;

{ upgrade で旧 runtime DLL や不要になった plugin が残らないよう、
  ファイル展開の直前にインストール先を掃除する。アンインストーラーは消さない。 }
procedure CleanPreviousInstall();
var
  Target: String;
  FindRec: TFindRec;
begin
  Target := ExpandConstant('{app}');
  { 以前の sdp インストール先だと確認できたときだけ触る。 }
  if not FileExists(Target + '\sdp.exe') then
    Exit;
  DelTree(Target + '\_internal', True, True, True);
  if FindFirst(Target + '\*', FindRec) then
  begin
    try
      repeat
        if (FindRec.Attributes and FILE_ATTRIBUTE_DIRECTORY) = 0 then
          if Lowercase(Copy(FindRec.Name, 1, 5)) <> 'unins' then
            DeleteFile(Target + '\' + FindRec.Name);
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

{ InitializeSetup 通過後にウィザード操作中へ起動された場合の二段目の防波堤。
  ここで中止すれば、旧 exe と新 DLL が混在した状態は作られない。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if IsFileInUse(InstalledExecutable()) then
    Result := 'sdp が実行中のため、インストールを中止しました。' + #13#10 +
      'sdp を終了してから、もう一度実行してください。';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    CleanPreviousInstall();
end;

{ 実行中のアンインストールは行わない（保存中のユーザーデータを壊さないため）。
  再起動後削除へは送らず、利用者へ終了を依頼する。 }
function InitializeUninstall(): Boolean;
begin
  Result := True;
  if IsFileInUse(InstalledExecutable()) then
  begin
    Result := False;
    if not UninstallSilent then
      MsgBox('sdp が実行中です。' + #13#10 +
        'sdp を終了してから、もう一度アンインストールしてください。', mbError, MB_OK);
  end;
end;
