"""復元失敗・保存失敗メッセージの整形と、通知抑制を検証する。

生の例外文とファイルパスをユーザーへ見せないことを固定する。
"""

import pytest
from pytestqt.qtbot import QtBot

from sdp.services.save_status import (
    MAX_MESSAGE_LENGTH,
    SaveCategory,
    SaveStatusReporter,
    restore_failure_message,
    save_failure_message,
    save_recovered_message,
)

ALL_CATEGORIES = (SaveCategory.SETTINGS, SaveCategory.PLAYLIST, SaveCategory.UI_STATE)


# -- 復元失敗メッセージ -----------------------------------------------------


def test_no_failure_has_no_message() -> None:
    """失敗が無ければメッセージを出さない。"""
    assert restore_failure_message([]) is None


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        ([SaveCategory.SETTINGS], "設定の復元に失敗しました。既定状態で起動します。"),
        ([SaveCategory.PLAYLIST], "プレイリストの復元に失敗しました。既定状態で起動します。"),
        ([SaveCategory.UI_STATE], "ウィンドウ状態の復元に失敗しました。既定状態で起動します。"),
        (
            [SaveCategory.SETTINGS, SaveCategory.PLAYLIST],
            "設定とプレイリストの復元に失敗しました。既定状態で起動します。",
        ),
        (
            list(ALL_CATEGORIES),
            "設定とプレイリストとウィンドウ状態の復元に失敗しました。既定状態で起動します。",
        ),
    ],
)
def test_message_lists_every_failed_category(categories: list[SaveCategory], expected: str) -> None:
    """1〜3カテゴリを読みやすい1文へまとめる。"""
    assert restore_failure_message(categories) == expected


def test_duplicates_are_removed_and_order_is_stable() -> None:
    """重複を除き、渡した順序によらず同じ文になる。"""
    duplicated = [SaveCategory.PLAYLIST, SaveCategory.SETTINGS, SaveCategory.PLAYLIST]

    assert restore_failure_message(duplicated) == restore_failure_message(
        [SaveCategory.SETTINGS, SaveCategory.PLAYLIST]
    )


def test_messages_fit_in_the_status_bar() -> None:
    """3カテゴリ同時でもステータスバーに収まる長さにする。"""
    message = restore_failure_message(ALL_CATEGORIES)

    assert message is not None
    assert len(message) <= MAX_MESSAGE_LENGTH


def test_messages_do_not_expose_paths_or_exceptions() -> None:
    """パスや例外本文を含めない。"""
    message = restore_failure_message(ALL_CATEGORIES)
    assert message is not None

    for forbidden in ("json", ":\\", "/", "Error", "Traceback"):
        assert forbidden not in message


def test_non_category_values_are_rejected() -> None:
    """SaveCategory以外を渡したら失敗させる（文字列を混ぜない）。"""
    with pytest.raises(TypeError):
        restore_failure_message(["settings"])  # type: ignore[list-item]


# -- 保存失敗メッセージ -----------------------------------------------------


@pytest.mark.parametrize("category", ALL_CATEGORIES)
def test_save_messages_name_the_category(category: SaveCategory) -> None:
    """どのファイルの保存に失敗したかを区別できる。"""
    failure = save_failure_message(category)
    recovered = save_recovered_message(category)

    assert category.label in failure
    assert "保存できませんでした" in failure
    assert category.label in recovered
    assert len(failure) <= MAX_MESSAGE_LENGTH
    assert len(recovered) <= MAX_MESSAGE_LENGTH


def test_save_failure_messages_are_distinct() -> None:
    """3カテゴリの失敗メッセージは互いに異なる。"""
    messages = {save_failure_message(category) for category in ALL_CATEGORIES}

    assert len(messages) == len(ALL_CATEGORIES)


# -- 通知の抑制 -------------------------------------------------------------


def test_repeated_failures_notify_only_once(qtbot: QtBot) -> None:
    """デバウンス保存が連続で失敗しても、通知は状態変化のときだけ。"""
    del qtbot
    reporter = SaveStatusReporter()
    messages: list[str] = []
    reporter.message_requested.connect(messages.append)

    for _ in range(5):
        reporter.report_failure(SaveCategory.SETTINGS)

    assert messages == [save_failure_message(SaveCategory.SETTINGS)]
    assert reporter.failed_categories == frozenset({SaveCategory.SETTINGS})


def test_recovery_is_notified_once_after_a_failure(qtbot: QtBot) -> None:
    """失敗→成功のときだけ復旧を伝える。"""
    del qtbot
    reporter = SaveStatusReporter()
    messages: list[str] = []
    reporter.message_requested.connect(messages.append)

    reporter.report_failure(SaveCategory.UI_STATE)
    reporter.report_recovery(SaveCategory.UI_STATE)
    reporter.report_recovery(SaveCategory.UI_STATE)

    assert messages == [
        save_failure_message(SaveCategory.UI_STATE),
        save_recovered_message(SaveCategory.UI_STATE),
    ]
    assert reporter.failed_categories == frozenset()


def test_success_without_a_previous_failure_is_silent(qtbot: QtBot) -> None:
    """失敗していないカテゴリの成功では何も出さない。"""
    del qtbot
    reporter = SaveStatusReporter()
    messages: list[str] = []
    reporter.message_requested.connect(messages.append)

    reporter.report_recovery(SaveCategory.PLAYLIST)

    assert messages == []


def test_categories_are_tracked_independently(qtbot: QtBot) -> None:
    """カテゴリごとに独立して失敗・復旧を扱う。"""
    del qtbot
    reporter = SaveStatusReporter()
    messages: list[str] = []
    reporter.message_requested.connect(messages.append)

    reporter.report_failure(SaveCategory.SETTINGS)
    reporter.report_failure(SaveCategory.UI_STATE)
    reporter.report_recovery(SaveCategory.SETTINGS)

    assert messages == [
        save_failure_message(SaveCategory.SETTINGS),
        save_failure_message(SaveCategory.UI_STATE),
        save_recovered_message(SaveCategory.SETTINGS),
    ]
    assert reporter.failed_categories == frozenset({SaveCategory.UI_STATE})
