"""entry pointがselftestをplayer起動経路から分離することを検証する。"""

import pytest

from sdp import __main__ as main_module


def test_selftest_does_not_start_player_or_create_launch_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """selftestでcomposition・単一instance・LaunchRequestの経路に入らない。"""
    calls: list[list[str]] = []

    def record_selftest(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    def fail_player(argv: list[str]) -> int:
        del argv
        raise AssertionError("selftestでapp.run()を呼んではいけません")

    def fail_launch_request(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("selftestでLaunchRequestを作ってはいけません")

    monkeypatch.setattr(main_module, "run_selftest", record_selftest)
    monkeypatch.setattr(main_module.app, "run", fail_player)
    monkeypatch.setattr(
        main_module.app,
        "parse_launch_request",
        fail_launch_request,
    )

    assert main_module.main(["sdp.exe", "--selftest"]) == 0
    assert calls == [["sdp.exe", "--selftest"]]


def test_normal_path_keeps_existing_player_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通常pathはP7-Aのapp.run経路へargv全体を渡す。"""
    calls: list[list[str]] = []

    def record_player(argv: list[str]) -> int:
        calls.append(argv)
        return 17

    monkeypatch.setattr(main_module.app, "run", record_player)

    assert main_module.main(["sdp.exe", "日本語 曲.mp3"]) == 17
    assert calls == [["sdp.exe", "日本語 曲.mp3"]]


@pytest.mark.parametrize("arguments", [["--selftest", "track.wav"], ["--unknown"]])
def test_invalid_arguments_return_two_without_starting_player(
    arguments: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正CLIは固定コード2を返し、playerとselftestを始めない。"""

    def fail_player(argv: list[str]) -> int:
        pytest.fail(f"不正CLIでplayerを起動してはいけません: {argv}")

    def fail_selftest(argv: list[str]) -> int:
        pytest.fail(f"不正CLIでselftestを起動してはいけません: {argv}")

    monkeypatch.setattr(main_module.logging_setup, "configure_logging", lambda: None)
    monkeypatch.setattr(main_module.logging_setup, "install_excepthook", lambda: None)
    monkeypatch.setattr(main_module.app, "run", fail_player)
    monkeypatch.setattr(main_module, "run_selftest", fail_selftest)

    assert main_module.main(["sdp.exe", *arguments]) == main_module.INVALID_ARGUMENT_EXIT_CODE
