"""配布版の起動モード解析をQtなしで検証する。"""

from sdp.cli import CliCommand, CliMode, parse_cli_arguments


def test_no_arguments_starts_player() -> None:
    """引数なしは従来どおりplayer起動とする。"""
    assert parse_cli_arguments([]) == CliCommand(CliMode.PLAYER)


def test_paths_are_forwarded_without_reparsing() -> None:
    """通常pathの順序・空白・重複をそのまま保つ。"""
    arguments = ("C:/Music/日本語 曲.mp3", "relative.wav", "relative.wav")

    assert parse_cli_arguments(arguments) == CliCommand(
        CliMode.PLAYER,
        path_arguments=arguments,
    )


def test_selftest_is_an_exclusive_mode() -> None:
    """--selftestだけを指定した場合に自己診断モードにする。"""
    assert parse_cli_arguments(["--selftest"]) == CliCommand(CliMode.SELFTEST)


def test_selftest_mixed_with_path_is_rejected() -> None:
    """自己診断とplaylist追加を1processで混在させない。"""
    command = parse_cli_arguments(["--selftest", "track.wav"])

    assert command.mode is CliMode.INVALID
    assert command.path_arguments == ()
    assert command.error_message is not None


def test_unknown_option_is_rejected() -> None:
    """未実装optionを欠損ファイルとしてplaylistへ追加しない。"""
    command = parse_cli_arguments(["--unknown"])

    assert command.mode is CliMode.INVALID
    assert command.error_message == "未知のoptionです: --unknown"
