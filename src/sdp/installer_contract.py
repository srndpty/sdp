"""Inno Setup installerの契約を、compilerなしで検査する（Qt非依存）。

`packaging/installer.iss` がinstaller仕様のsource of truthであるため、ここでは
「そこに書かれている内容が、P7-Cで決めた契約を満たしているか」だけを見る。
値そのものを別ファイルへ二重管理せず、拡張子一覧やProgIDは.issから読み出す。

検査する主な契約:

- per-user（``{localappdata}\\Programs\\sdp``）で、UAC昇格を要求しない
- registryはHKCUだけ。HKLM・UserChoice・FileExtsへ書かない
- 「プログラムから開く」候補として登録するが、既定アプリは変更しない
- uninstallでユーザーデータ（``%LOCALAPPDATA%\\sdp``）を削除しない
- versionと入力配布物を外部から注入し、.issへ手書きしない
"""

from collections.abc import Sequence
from typing import Final

from sdp.inno_script import InnoEntry, InnoScript

APP_ID: Final = "{8F3B7C21-5D4E-4A96-9C2F-1E7A6B0D3F58}"
PROG_ID: Final = "sdp.AudioFile"
INSTALL_DIRECTORY: Final = r"{localappdata}\Programs\sdp"
INSTALL_DIRECTORY_DISPLAY: Final = r"%LOCALAPPDATA%\Programs\sdp"
USER_DATA_DIRECTORY_DISPLAY: Final = r"%LOCALAPPDATA%\sdp"
OPEN_COMMAND: Final = r'"{app}\sdp.exe" "%1"'
ICON_REFERENCE: Final = r"{app}\sdp.exe,0"
DESKTOP_ICON_TASK: Final = "desktopicon"
INSTALLER_KIND: Final = "inno-setup"

AUDIO_FILE_EXTENSIONS: Final = (".wav", ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac")
"""関連付け対象の拡張子（.issの登録内容と一致することを検査する）。"""

EXTERNAL_DEFINES: Final = ("AppVersion", "VersionInfoVersion", "SourceDir")
"""build scriptが ``/D`` で注入し、.iss側で定義してはならないもの。"""

_APPLICATIONS_KEY: Final = r"Software\Classes\Applications\sdp.exe"
_CAPABILITIES_KEY: Final = r"Software\sdp\Capabilities"
# コメント以外の行に現れてはならない語（既定アプリの奪取とmachine全体への書き込みの防止）。
_FORBIDDEN_TERMS: Final = (
    "UserChoice",
    "FileExts",
    "HKLM",
    "HKEY_LOCAL_MACHINE",
    "HKCR",
    "HKEY_CLASSES_ROOT",
    "HKA",
    r"{localappdata}\sdp",
)


def non_comment_source(script: InnoScript) -> str:
    """``;`` 始まりの行コメントを除いたscript本文。

    「書いてはならない語」の検査は、注意書きのコメント（``HKLMへ書かない`` など）へ
    反応させたくないためこちらを見る。
    """
    return "\n".join(
        line for line in script.source.splitlines() if not line.strip().startswith(";")
    )


def app_id(script: InnoScript) -> str:
    """``[Setup] AppId`` の実値（Inno Setupの ``{{`` escapeを戻す）。"""
    raw = script.setup("AppId") or ""
    return raw[1:] if raw.startswith("{{") else raw


def prog_id(script: InnoScript) -> str:
    """``#define ProgId`` で決めたProgID。"""
    return script.defines.get("ProgId", "")


def install_directory(script: InnoScript) -> str:
    """``[Setup] DefaultDirName``。"""
    return script.setup("DefaultDirName") or ""


def file_associations(script: InnoScript) -> tuple[str, ...]:
    """Capabilitiesへ登録している拡張子を宣言順で返す。"""
    extensions: list[str] = []
    associations = rf"{_CAPABILITIES_KEY}\FileAssociations".lower()
    for entry in script.section_entries("registry"):
        if (entry.value("Subkey") or "").lower() != associations:
            continue
        name = entry.value("ValueName") or ""
        if name and name not in extensions:
            extensions.append(name)
    return tuple(extensions)


def validate_installer_contract(script: InnoScript) -> tuple[str, ...]:
    """契約違反を人が読める文字列で返す（空タプルなら合格）。"""
    failures: list[str] = []
    failures.extend(_check_scope(script))
    failures.extend(_check_version_injection(script))
    failures.extend(_check_files(script))
    failures.extend(_check_registry(script))
    failures.extend(_check_shortcuts(script))
    failures.extend(_check_uninstall(script))
    failures.extend(_check_presentation(script))
    failures.extend(_check_running_instance(script))
    return tuple(failures)


def _check_scope(script: InnoScript) -> list[str]:
    failures: list[str] = []
    if script.setup("PrivilegesRequired") != "lowest":
        failures.append("PrivilegesRequired=lowest ではありません（per-user installでなくなる）")
    if script.setup("PrivilegesRequiredOverridesAllowed") != "":
        failures.append(
            "PrivilegesRequiredOverridesAllowed が空ではありません"
            "（コマンドラインからの昇格を許してしまう）"
        )
    if install_directory(script) != INSTALL_DIRECTORY:
        failures.append(
            f"DefaultDirNameが{INSTALL_DIRECTORY}ではありません: {install_directory(script)!r}"
        )
    if app_id(script) != APP_ID:
        failures.append(f"AppIdが固定値ではありません: {app_id(script)!r}")
    if "{#" in app_id(script):
        failures.append("AppIdへdefineを埋め込んでいます（version等でupgrade契約が壊れる）")
    if not script.setup("MinVersion"):
        failures.append("MinVersionが未指定です")
    return failures


def _check_version_injection(script: InnoScript) -> list[str]:
    failures: list[str] = []
    for name in EXTERNAL_DEFINES:
        if name in script.defines:
            failures.append(f"{name}を.iss内で定義しています（versionの二重管理になる）")
        if name not in script.referenced_defines:
            failures.append(f"{name}を参照していません（外部注入が効かない）")
    output_name = script.setup("OutputBaseFilename") or ""
    if "{#AppVersion}" not in output_name:
        failures.append(f"OutputBaseFilenameへversionが入っていません: {output_name!r}")
    if not output_name.endswith("-setup"):
        failures.append(f"OutputBaseFilenameが-setupで終わっていません: {output_name!r}")
    for directive in ("AppVersion", "VersionInfoVersion"):
        if "{#" not in (script.setup(directive) or ""):
            failures.append(f"{directive}が外部注入されていません")
    return failures


def _check_files(script: InnoScript) -> list[str]:
    failures: list[str] = []
    entries = script.section_entries("files")
    if len(entries) != 1:
        failures.append(f"[Files]は配布物1件だけにしてください: {len(entries)}件")
        return failures
    entry = entries[0]
    source = entry.value("Source") or ""
    if not source.startswith("{#SourceDir}"):
        failures.append(f"[Files]のSourceが外部注入の配布物ではありません: {source!r}")
    if (entry.value("DestDir") or "") != "{app}":
        failures.append(f"[Files]のDestDirが{{app}}ではありません: {entry.value('DestDir')!r}")
    flags = entry.flags()
    for required in ("ignoreversion", "recursesubdirs", "createallsubdirs"):
        if required not in flags:
            failures.append(f"[Files]のFlagsに{required}がありません")
    excludes = (entry.value("Excludes") or "").lower()
    for name in ("settings.json", "playlist.json", "ui-state.json", "*.py"):
        if name not in excludes:
            failures.append(f"[Files]のExcludesが{name}を除外していません")
    return failures


def _check_registry(script: InnoScript) -> list[str]:
    failures: list[str] = []
    entries = script.section_entries("registry")
    if not entries:
        return ["[Registry]の登録がありません"]

    roots = {(entry.value("Root") or "").upper() for entry in entries}
    if roots != {"HKCU"}:
        failures.append(f"[Registry]のRootがHKCU以外を含みます: {sorted(roots)}")
    failures.extend(_check_forbidden_terms(script))

    failures.extend(
        _require_value(
            entries,
            rf"Software\Classes\{PROG_ID}\shell\open\command",
            "",
            OPEN_COMMAND,
            "ProgIDのopen command",
        )
    )
    failures.extend(
        _require_value(
            entries,
            rf"Software\Classes\{PROG_ID}\DefaultIcon",
            "",
            ICON_REFERENCE,
            "ProgIDのDefaultIcon",
        )
    )
    failures.extend(
        _require_value(
            entries,
            rf"{_APPLICATIONS_KEY}\shell\open\command",
            "",
            OPEN_COMMAND,
            "Open Withのopen command",
        )
    )
    failures.extend(
        _require_value(
            entries, _APPLICATIONS_KEY, "FriendlyAppName", "sdp", "Open WithのFriendlyAppName"
        )
    )

    declared = file_associations(script)
    if declared != AUDIO_FILE_EXTENSIONS:
        failures.append(
            f"Capabilitiesの拡張子一覧が想定と違います: {declared} != {AUDIO_FILE_EXTENSIONS}"
        )
    for extension in AUDIO_FILE_EXTENSIONS:
        failures.extend(
            _require_value(
                entries,
                rf"{_APPLICATIONS_KEY}\SupportedTypes",
                extension,
                "",
                f"SupportedTypes {extension}",
            )
        )
        failures.extend(
            _require_value(
                entries,
                rf"{_CAPABILITIES_KEY}\FileAssociations",
                extension,
                PROG_ID,
                f"Capabilities {extension}",
            )
        )
        subkey = rf"Software\Classes\{extension}\OpenWithProgids"
        matches = _find(entries, subkey, PROG_ID)
        if not matches:
            failures.append(f"{extension}のOpenWithProgidsへ{PROG_ID}を登録していません")
            continue
        for match in matches:
            flags = match.flags()
            if "uninsdeletevalue" not in flags:
                failures.append(f"{extension}のOpenWithProgidsにuninsdeletevalueがありません")
            if "uninsdeletekey" in flags:
                failures.append(
                    f"{extension}のOpenWithProgidsがuninsdeletekeyです"
                    "（他アプリの登録まで消してしまう）"
                )
    return failures


def _check_forbidden_terms(script: InnoScript) -> list[str]:
    """コメント以外の行に禁止語が無いことを確かめる。"""
    failures: list[str] = []
    for number, line in enumerate(script.source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(";"):
            continue
        lowered = stripped.lower()
        for term in _FORBIDDEN_TERMS:
            if term.lower() in lowered:
                failures.append(f"{number}行目に書いてはならない語 {term!r} があります: {stripped}")
    return failures


def _check_shortcuts(script: InnoScript) -> list[str]:
    failures: list[str] = []
    tasks = script.section_entries("tasks")
    desktop_tasks = [entry for entry in tasks if (entry.value("Name") or "") == DESKTOP_ICON_TASK]
    if not desktop_tasks:
        failures.append(f"[Tasks]に{DESKTOP_ICON_TASK}がありません")
    elif "unchecked" not in desktop_tasks[0].flags():
        failures.append(f"{DESKTOP_ICON_TASK}が既定ONです（desktop shortcutは任意にする）")

    icons = script.section_entries("icons")
    start_menu = [entry for entry in icons if (entry.value("Name") or "").startswith("{group}\\")]
    desktop = [
        entry for entry in icons if (entry.value("Name") or "").startswith("{userdesktop}\\")
    ]
    if len(start_menu) != 1:
        failures.append(f"スタートメニューshortcutが1件ではありません: {len(start_menu)}件")
    elif start_menu[0].value("Tasks"):
        failures.append("スタートメニューshortcutが任意扱いになっています")
    if len(desktop) != 1:
        failures.append(f"desktop shortcutが1件ではありません: {len(desktop)}件")
    elif (desktop[0].value("Tasks") or "") != DESKTOP_ICON_TASK:
        failures.append(f"desktop shortcutが{DESKTOP_ICON_TASK} taskへ紐づいていません")
    for entry in icons:
        if (entry.value("Filename") or "") != r"{app}\sdp.exe":
            failures.append(f"shortcutの参照先が想定と違います: {entry.value('Filename')!r}")
    return failures


def _check_uninstall(script: InnoScript) -> list[str]:
    failures: list[str] = []
    for entry in script.section_entries("uninstalldelete"):
        name = entry.value("Name") or ""
        if not name.startswith("{app}\\"):
            failures.append(f"[UninstallDelete]がinstall先の外を消そうとしています: {name!r}")
    for entry in script.section_entries("installdelete"):
        name = entry.value("Name") or ""
        if not name.startswith("{app}\\"):
            failures.append(f"[InstallDelete]がinstall先の外を消そうとしています: {name!r}")
    registered = _find(
        script.section_entries("registry"), r"Software\RegisteredApplications", "sdp"
    )
    if not registered:
        failures.append("RegisteredApplicationsへ登録していません")
    else:
        for entry in registered:
            if "uninsdeletevalue" not in entry.flags():
                failures.append(
                    "RegisteredApplicationsの登録がuninsdeletevalueではありません"
                    "（他アプリの登録まで消してしまう）"
                )
    return failures


def _check_presentation(script: InnoScript) -> list[str]:
    failures: list[str] = []
    icon_file = script.setup("SetupIconFile") or ""
    if not icon_file.lower().endswith(".ico"):
        failures.append(f"SetupIconFileがICOではありません: {icon_file!r}")
    if (script.setup("UninstallDisplayIcon") or "") != r"{app}\sdp.exe":
        failures.append("UninstallDisplayIconがsdp.exeではありません")
    if not (script.setup("LicenseFile") or ""):
        failures.append("LicenseFileが未指定です（wizardでLICENSEを表示しない）")
    if script.setup("ChangesAssociations") != "yes":
        failures.append("ChangesAssociations=yes ではありません")
    welcome = script.directive("Messages", "japanese.WelcomeLabel2") or ""
    if "技術検証用" not in welcome:
        failures.append("wizardが技術検証用である旨を表示していません")
    if "公開配布" not in welcome:
        failures.append("wizardが公開配布物でない旨を表示していません")
    confirm = script.directive("Messages", "japanese.ConfirmUninstall") or ""
    if "保持" not in confirm:
        failures.append("uninstall確認がユーザーデータ保持を伝えていません")
    return failures


def _check_running_instance(script: InnoScript) -> list[str]:
    failures: list[str] = []
    if script.setup("CloseApplications") != "yes":
        failures.append("CloseApplications=yes ではありません（起動中upgradeを検出できない）")
    if not script.setup("RestartApplications"):
        failures.append("RestartApplicationsを明示していません")
    # InitializeSetupはRestart Managerがアプリを閉じる前に走る唯一の入口なので、
    # 「起動中なら中止する」契約はここが本命。PrepareToInstallは二段目の防波堤。
    # コメントでの言及ではなく、実際の宣言があることを見る。
    required_code = (
        "function IsFileInUse",
        "function InitializeSetup",
        "function PrepareToInstall",
        "function InitializeUninstall",
        "procedure CleanPreviousInstall",
    )
    for declaration in required_code:
        if declaration not in script.code:
            failures.append(f"[Code]に{declaration}がありません")
    if "FORCECLOSEAPPLICATIONS" in non_comment_source(script).upper():
        failures.append("強制終了（/FORCECLOSEAPPLICATIONS）を既定にしています")
    if r"DelTree(Target + '\_internal'" not in script.code:
        failures.append("upgrade前に旧_internalを掃除していません（旧DLLが残る）")
    if "unins" not in script.code:
        failures.append("掃除処理がアンインストーラーを保護していません")
    return failures


def _find(entries: Sequence[InnoEntry], subkey: str, value_name: str) -> tuple[InnoEntry, ...]:
    """Subkeyと ValueName（大文字小文字を無視）で ``[Registry]`` 行を探す。"""
    return tuple(
        entry
        for entry in entries
        if (entry.value("Subkey") or "").lower() == subkey.lower()
        and (entry.value("ValueName") or "").lower() == value_name.lower()
    )


def _require_value(
    entries: Sequence[InnoEntry], subkey: str, value_name: str, expected: str, label: str
) -> list[str]:
    matches = _find(entries, subkey, value_name)
    if not matches:
        return [f"{label}の登録がありません（{subkey}）"]
    actual = matches[0].value("ValueData") or ""
    if actual != expected:
        return [f"{label}の値が想定と違います: {actual!r} != {expected!r}"]
    return []
