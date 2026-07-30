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


# -- codec test（P7-B2）------------------------------------------------------


def test_codec_test_requires_at_least_one_path() -> None:
    """検査対象を渡さない`--codec-test`は拒否する（製品へ音源を同梱しないため）。"""
    command = parse_cli_arguments(["--codec-test"])

    assert command.mode is CliMode.INVALID
    assert command.path_arguments == ()
    assert command.error_message is not None


def test_codec_test_takes_every_path() -> None:
    """複数pathを順序どおり検査対象にする。"""
    command = parse_cli_arguments(["--codec-test", "a.wav", "日本語 曲.mp3", "b.flac"])

    assert command == CliCommand(
        CliMode.CODEC_TEST,
        path_arguments=("a.wav", "日本語 曲.mp3", "b.flac"),
    )


def test_codec_test_paths_are_not_playlist_arguments() -> None:
    """codec testのpathを通常のplaylist追加と混同しない。"""
    codec = parse_cli_arguments(["--codec-test", "a.wav"])
    player = parse_cli_arguments(["a.wav"])

    assert codec.mode is CliMode.CODEC_TEST
    assert player.mode is CliMode.PLAYER
    assert codec.path_arguments == player.path_arguments


def test_codec_test_must_come_first() -> None:
    """pathの後ろへ置いた場合は曖昧なので拒否する。"""
    command = parse_cli_arguments(["a.wav", "--codec-test"])

    assert command.mode is CliMode.INVALID
    assert command.error_message is not None


def test_codec_test_and_selftest_are_exclusive() -> None:
    """2つのモードoptionを同時に指定できない。"""
    command = parse_cli_arguments(["--codec-test", "--selftest"])

    assert command.mode is CliMode.INVALID
    assert "--selftest" in (command.error_message or "")


def test_unknown_option_after_codec_test_is_rejected() -> None:
    """検査対象の位置にある未知optionをファイル名として扱わない。"""
    command = parse_cli_arguments(["--codec-test", "a.wav", "--unknown"])

    assert command.mode is CliMode.INVALID
    assert command.error_message == "未知のoptionです: --unknown"


def test_option_terminator_allows_paths_that_look_like_options() -> None:
    """`--`以降は`--`始まりでもファイルpathとして検査対象にする。"""
    command = parse_cli_arguments(["--codec-test", "--", "--sample.wav"])

    assert command == CliCommand(
        CliMode.CODEC_TEST,
        path_arguments=("--sample.wav",),
    )


def test_option_terminator_keeps_earlier_paths() -> None:
    """`--`の前後のpathを両方とも検査対象にする。"""
    command = parse_cli_arguments(["--codec-test", "a.wav", "--", "--b.wav"])

    assert command == CliCommand(
        CliMode.CODEC_TEST,
        path_arguments=("a.wav", "--b.wav"),
    )


def test_unknown_option_before_terminator_is_still_rejected() -> None:
    """`--`より前の未知optionは従来どおり拒否する。"""
    command = parse_cli_arguments(["--codec-test", "--unknown", "--", "a.wav"])

    assert command.mode is CliMode.INVALID
    assert command.error_message == "未知のoptionです: --unknown"


def test_option_terminator_without_paths_is_rejected() -> None:
    """`--`だけでpathが無ければ従来どおり拒否する。"""
    command = parse_cli_arguments(["--codec-test", "--"])

    assert command.mode is CliMode.INVALID
    assert command.error_message is not None
