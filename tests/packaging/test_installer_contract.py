"""Inno Setup scriptのparserと、installer契約の検査を検証する。

Inno Setup compilerが無い環境でも、per-user／HKCUのみ／既定アプリ非変更／
uninstall時のユーザーデータ保持といった契約を確認できるようにする。
"""

from pathlib import Path

import pytest

from sdp.inno_script import InnoScript, parse_inno_script, registry_roots
from sdp.installer_contract import (
    APP_ID,
    AUDIO_FILE_EXTENSIONS,
    EXTERNAL_DEFINES,
    ICON_REFERENCE,
    INSTALL_DIRECTORY,
    OPEN_COMMAND,
    PROG_ID,
    app_id,
    file_associations,
    install_directory,
    non_comment_source,
    prog_id,
    validate_installer_contract,
)

_REPO_ROOT = Path(__file__).parents[2]
_INSTALLER = _REPO_ROOT / "packaging" / "installer.iss"


@pytest.fixture(scope="module")
def script() -> InnoScript:
    """実際の`packaging/installer.iss`を解析する。"""
    return parse_inno_script(_INSTALLER.read_text(encoding="utf-8"))


# --- parser -----------------------------------------------------------------


def test_parser_reads_sections_defines_and_quoted_parameters() -> None:
    """section、#define、引用符付きパラメータ、行コメントを扱える。"""
    parsed = parse_inno_script(
        "\n".join(
            (
                "; 先頭のコメント",
                '#define ProgId "demo.Type"',
                "[Setup]",
                "AppName=demo",
                "DefaultDirName={localappdata}\\Programs\\demo",
                "[Registry]",
                "; 登録のコメント",
                'Root: HKCU; Subkey: "Software\\Classes\\{#ProgId}"; '
                'ValueData: """{app}\\demo.exe"" ""%1"""; Flags: uninsdeletekey',
            )
        )
    )

    assert parsed.defines["ProgId"] == "demo.Type"
    assert parsed.setup("appname") == "demo"
    assert parsed.setup("DefaultDirName") == "{localappdata}\\Programs\\demo"
    entry = parsed.section_entries("registry")[0]
    assert entry.value("Subkey") == "Software\\Classes\\demo.Type"
    assert entry.value("ValueData") == '"{app}\\demo.exe" "%1"'
    assert entry.flags() == {"uninsdeletekey"}
    assert registry_roots(parsed.section_entries("registry")) == ("HKCU",)


def test_parser_keeps_undefined_references_and_records_them() -> None:
    """外部注入のdefineは未展開のまま残し、参照したことを記録する。"""
    parsed = parse_inno_script("[Setup]\nAppVersion={#AppVersion}\n")

    assert parsed.setup("AppVersion") == "{#AppVersion}"
    assert "AppVersion" in parsed.referenced_defines
    assert "AppVersion" not in parsed.defines


def test_parser_keeps_code_section_as_raw_text() -> None:
    """[Code]はパラメータとして解釈せず、生テキストで保持する。"""
    parsed = parse_inno_script("[Code]\nfunction Foo(): Boolean;\nbegin\n  Result := True;\nend;\n")

    assert "function Foo(): Boolean;" in parsed.code
    assert parsed.entries == ()


def test_parser_ignores_preprocessor_directives_other_than_define() -> None:
    """#ifndef／#error等は無視し、解析を止めない。"""
    parsed = parse_inno_script("#ifndef AppVersion\n  #error missing\n#endif\n[Setup]\nAppName=x\n")

    assert parsed.defines == {}
    assert parsed.setup("AppName") == "x"


# --- 実installerの契約 ------------------------------------------------------


def test_real_installer_satisfies_the_contract(script: InnoScript) -> None:
    """`packaging/installer.iss`がP7-Cの契約をすべて満たす。"""
    assert validate_installer_contract(script) == ()


def test_installer_is_per_user_without_elevation(script: InnoScript) -> None:
    """per-userで、UAC昇格もコマンドラインからの昇格指定も要求しない。"""
    assert script.setup("PrivilegesRequired") == "lowest"
    assert script.setup("PrivilegesRequiredOverridesAllowed") == ""
    assert install_directory(script) == INSTALL_DIRECTORY
    assert "Program Files" not in script.source
    assert "{commonpf" not in script.source
    assert "{autopf" not in script.source


def test_app_id_is_stable_and_version_independent(script: InnoScript) -> None:
    """AppIdは固定。versionを含めるとupgradeが別アプリ扱いになる。"""
    assert app_id(script) == APP_ID
    assert "{#" not in app_id(script)
    assert "{#AppVersion}" not in (script.setup("AppId") or "")


def test_version_and_source_are_injected_from_outside(script: InnoScript) -> None:
    """version・入力配布物を.issへ手書きせず、build scriptから注入する。"""
    for name in EXTERNAL_DEFINES:
        assert name not in script.defines
        assert name in script.referenced_defines
    assert script.setup("AppVersion") == "{#AppVersion}"
    assert script.setup("OutputBaseFilename") == "sdp-{#AppVersion}-windows-x64-setup"


def test_files_come_from_the_verified_package_only(script: InnoScript) -> None:
    """installerの入力は検証済み配布物1件で、ユーザーデータを含めない。"""
    entries = script.section_entries("files")
    assert len(entries) == 1
    source = entries[0].value("Source") or ""
    assert source.startswith("{#SourceDir}")
    excludes = entries[0].value("Excludes") or ""
    for name in ("settings.json", "playlist.json", "ui-state.json", "*.py"):
        assert name in excludes
    assert "test_audio" not in script.source
    assert "sine440" not in script.source


def test_registry_writes_stay_in_hkcu(script: InnoScript) -> None:
    """registryはHKCUだけ。HKLM／HKCRへ書かない。"""
    assert registry_roots(script.section_entries("registry")) == ("HKCU",)


def test_default_application_is_never_taken(script: InnoScript) -> None:
    """UserChoiceとFileExtsへ触れず、既定アプリを奪わない。"""
    body = non_comment_source(script)
    for term in ("UserChoice", "FileExts", "HKLM", "HKCR"):
        assert term not in body


def test_open_with_registration(script: InnoScript) -> None:
    """「プログラムから開く」候補として、command・icon・SupportedTypesを登録する。"""
    entries = script.section_entries("registry")
    commands = [
        entry.value("ValueData")
        for entry in entries
        if (entry.value("Subkey") or "").endswith(r"Applications\sdp.exe\shell\open\command")
    ]
    assert commands == [OPEN_COMMAND]
    icons = [
        entry.value("ValueData")
        for entry in entries
        if (entry.value("Subkey") or "").endswith(r"Applications\sdp.exe\DefaultIcon")
    ]
    assert icons == [ICON_REFERENCE]
    supported = {
        entry.value("ValueName")
        for entry in entries
        if (entry.value("Subkey") or "").endswith(r"Applications\sdp.exe\SupportedTypes")
    }
    assert supported == set(AUDIO_FILE_EXTENSIONS)


def test_single_prog_id_covers_all_target_extensions(script: InnoScript) -> None:
    """形式別に分けず1つのProgIDで7拡張子を扱う。"""
    assert prog_id(script) == PROG_ID
    assert file_associations(script) == AUDIO_FILE_EXTENSIONS
    assert len(AUDIO_FILE_EXTENSIONS) == 7

    open_with = {
        entry.value("Subkey")
        for entry in script.section_entries("registry")
        if (entry.value("Subkey") or "").endswith(r"\OpenWithProgids")
    }
    assert open_with == {
        rf"Software\Classes\{extension}\OpenWithProgids" for extension in AUDIO_FILE_EXTENSIONS
    }


def test_open_with_entries_only_remove_their_own_value(script: InnoScript) -> None:
    """uninstallでは自分の値だけを消し、他アプリの登録を壊さない。"""
    for entry in script.section_entries("registry"):
        subkey = entry.value("Subkey") or ""
        if subkey.endswith(r"\OpenWithProgids"):
            assert entry.value("ValueName") == PROG_ID
            assert "uninsdeletevalue" in entry.flags()
            assert "uninsdeletekey" not in entry.flags()


def test_commands_quote_the_path_and_argument(script: InnoScript) -> None:
    """空白・日本語を含むpathのため、exeと`%1`の双方を引用する。"""
    commands = [
        entry.value("ValueData")
        for entry in script.section_entries("registry")
        if (entry.value("Subkey") or "").endswith(r"\shell\open\command")
    ]
    assert commands
    for command in commands:
        assert command == OPEN_COMMAND
        assert command is not None
        assert command.startswith('"{app}\\sdp.exe"')
        assert command.endswith('"%1"')


def test_start_menu_is_default_and_desktop_shortcut_is_optional(script: InnoScript) -> None:
    """スタートメニューは標準作成、desktopはtaskで任意にする。"""
    tasks = script.section_entries("tasks")
    assert [entry.value("Name") for entry in tasks] == ["desktopicon"]
    assert "unchecked" in tasks[0].flags()

    icons = script.section_entries("icons")
    assert len(icons) == 2
    start_menu = next(entry for entry in icons if (entry.value("Name") or "").startswith("{group}"))
    desktop = next(
        entry for entry in icons if (entry.value("Name") or "").startswith("{userdesktop}")
    )
    assert start_menu.value("Tasks") is None
    assert desktop.value("Tasks") == "desktopicon"


def test_icons_are_declared_for_setup_and_uninstall(script: InnoScript) -> None:
    """installer・uninstaller・shortcut・Apps & Featuresへiconを与える。"""
    assert (script.setup("SetupIconFile") or "").endswith("sdp.ico")
    assert script.setup("UninstallDisplayIcon") == r"{app}\sdp.exe"
    for entry in script.section_entries("icons"):
        assert entry.value("IconFilename") == r"{app}\sdp.exe"
    assert (_REPO_ROOT / "assets" / "sdp.ico").is_file()


def test_uninstall_keeps_user_data(script: InnoScript) -> None:
    """uninstallは`%LOCALAPPDATA%\\sdp`を消さない。"""
    for entry in script.section_entries("uninstalldelete"):
        name = entry.value("Name") or ""
        assert name.startswith("{app}\\")
    body = non_comment_source(script)
    assert "{localappdata}\\sdp" not in body
    assert "{userappdata}" not in body
    confirm = script.directive("Messages", "japanese.ConfirmUninstall") or ""
    assert "保持" in confirm


def test_upgrade_removes_obsolete_runtime_files(script: InnoScript) -> None:
    """upgrade前にinstall先を掃除し、旧DLLやpluginを残さない。"""
    assert r"DelTree(Target + '\_internal'" in script.code
    assert "unins" in script.code
    assert "if not FileExists(Target + '\\sdp.exe') then" in script.code


def test_running_instance_is_asked_to_exit_but_never_force_killed(script: InnoScript) -> None:
    """起動中は終了を依頼し、拒否されたらinstall／uninstallを中止する。"""
    assert script.setup("CloseApplications") == "yes"
    assert script.setup("RestartApplications") == "yes"
    # Restart Managerはsilent実行時に既定でアプリを閉じるため、判定はその前に走る
    # InitializeSetupで行う（PrepareToInstallは二段目）。
    assert "function InitializeSetup" in script.code
    assert "PrepareToInstall" in script.code
    assert "InitializeUninstall" in script.code
    assert "FORCECLOSEAPPLICATIONS" not in non_comment_source(script).upper()


def test_wizard_states_the_build_is_for_technical_verification(script: InnoScript) -> None:
    """ライセンス未解決のため、公開配布可能と表示しない。"""
    welcome = script.directive("Messages", "japanese.WelcomeLabel2") or ""
    assert "技術検証用" in welcome
    assert "公開配布物ではありません" in welcome
    assert (script.setup("LicenseFile") or "").endswith("LICENSE")


# --- 契約違反の検出 ----------------------------------------------------------


def _mutate(source: str, old: str, new: str) -> tuple[str, ...]:
    assert old in source
    return validate_installer_contract(parse_inno_script(source.replace(old, new, 1)))


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("PrivilegesRequired=lowest", "PrivilegesRequired=admin", "PrivilegesRequired=lowest"),
        (
            "DefaultDirName={localappdata}\\Programs\\{#AppName}",
            "DefaultDirName={commonpf}\\{#AppName}",
            "DefaultDirName",
        ),
        (
            'Root: HKCU; Subkey: "Software\\Classes\\{#ProgId}";',
            'Root: HKLM; Subkey: "Software\\Classes\\{#ProgId}";',
            "HKLM",
        ),
        ("AppVersion={#AppVersion}", "AppVersion=0.0.1", "AppVersion"),
        ("Flags: unchecked", "Flags: checkedonce", "desktopicon"),
        ("CloseApplications=yes", "CloseApplications=no", "CloseApplications"),
        # 昇格の抜け道
        (
            "PrivilegesRequiredOverridesAllowed=",
            "PrivilegesRequiredOverridesAllowed=commandline",
            "PrivilegesRequiredOverridesAllowed",
        ),
        ("MinVersion=10.0", "MinVersionDisabled=10.0", "MinVersion"),
        # upgrade契約
        (
            '#define AppIdGuid "8F3B7C21-5D4E-4A96-9C2F-1E7A6B0D3F58"',
            '#define AppIdGuid "8F3B7C21-5D4E-4A96-9C2F-1E7A6B0D3F59"',
            "AppId",
        ),
        ("function InitializeSetup", "function OnInitialize", "function InitializeSetup"),
        (
            "OutputBaseFilename=sdp-{#AppVersion}-windows-{#Architecture}-setup",
            "OutputBaseFilename=sdp-windows-{#Architecture}-setup",
            "OutputBaseFilename",
        ),
        ("RestartApplications=yes", "RestartApplicationsDisabled=yes", "RestartApplications"),
        (r"DelTree(Target + '\_internal', True, True, True);", "", "_internal"),
        ("function PrepareToInstall", "function OnPrepare", "function PrepareToInstall"),
        # 入力配布物
        ('Source: "{#SourceDir}\\*"', 'Source: "..\\dist\\sdp\\*"', "Source"),
        ('DestDir: "{app}"', 'DestDir: "{userappdata}"', "DestDir"),
        ("settings.json,playlist.json,ui-state.json,", "", "settings.json"),
        (
            "Flags: ignoreversion recursesubdirs createallsubdirs",
            "Flags: ignoreversion",
            "recursesubdirs",
        ),
        # 関連付け
        ('ValueName: ".aac"; ValueData: "{#ProgId}"', 'ValueName: ".aac"; ValueData: ""', "aac"),
        (
            'Subkey: "Software\\Classes\\.opus\\OpenWithProgids"; ValueType: string; '
            'ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletevalue',
            'Subkey: "Software\\Classes\\.opus\\OpenWithProgids"; ValueType: string; '
            'ValueName: "{#ProgId}"; ValueData: ""; Flags: uninsdeletekey',
            "uninsdeletekey",
        ),
        (
            'ValueName: "{#AppName}"; ValueData: "Software\\{#AppName}\\Capabilities"; '
            "Flags: uninsdeletevalue",
            'ValueName: "{#AppName}"; ValueData: "Software\\{#AppName}\\Capabilities"',
            "RegisteredApplications",
        ),
        (
            'ValueData: """{app}\\sdp.exe"" ""%1"""\n\n; --- 「プログラムから開く」',
            'ValueData: "{app}\\sdp.exe %1"\n\n; --- 「プログラムから開く」',
            "ProgIDのopen command",
        ),
        # 表示とライセンス
        ("SetupIconFile={#IconFile}", "SetupIconFile=", "SetupIconFile"),
        ("UninstallDisplayIcon={app}\\sdp.exe", "UninstallDisplayIcon=", "UninstallDisplayIcon"),
        ("LicenseFile={#SourceDir}\\LICENSE", "LicenseFile=", "LicenseFile"),
        ("ChangesAssociations=yes", "ChangesAssociations=no", "ChangesAssociations"),
        ("技術検証用です", "公開用です", "技術検証用"),
        ("設定・プレイリスト・キャッシュは保持されます", "削除します", "保持"),
        # shortcut
        ('Name: "{userdesktop}\\{#AppName}"', 'Name: "{userstartup}\\{#AppName}"', "desktop"),
        ("; Tasks: desktopicon", "", "desktop shortcut"),
    ],
)
def test_contract_violations_are_reported(old: str, new: str, expected: str) -> None:
    """契約を崩す変更は検査で落ちる（検査が素通りしていないことの確認）。"""
    failures = _mutate(_INSTALLER.read_text(encoding="utf-8"), old, new)

    assert failures
    assert any(expected in failure for failure in failures)


def test_user_choice_write_is_reported() -> None:
    """UserChoiceへの書き込みを追加すると検査が落ちる。"""
    source = _INSTALLER.read_text(encoding="utf-8") + (
        "\n[Registry]\nRoot: HKCU; "
        'Subkey: "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\FileExts'
        '\\.wav\\UserChoice"; ValueType: string; ValueName: "ProgId"; ValueData: "sdp.AudioFile"\n'
    )
    failures = validate_installer_contract(parse_inno_script(source))

    assert any("UserChoice" in failure for failure in failures)


def test_user_data_deletion_is_reported() -> None:
    """ユーザーデータを消す[UninstallDelete]を追加すると検査が落ちる。"""
    source = _INSTALLER.read_text(encoding="utf-8") + (
        '\n[UninstallDelete]\nType: filesandordirs; Name: "{localappdata}\\sdp"\n'
    )
    failures = validate_installer_contract(parse_inno_script(source))

    assert any("install先の外" in failure for failure in failures) or any(
        "{localappdata}\\sdp" in failure for failure in failures
    )
