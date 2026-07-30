"""起動モードと通常path引数を分けるQt非依存のCLI境界。"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

SELFTEST_OPTION = "--selftest"
CODEC_TEST_OPTION = "--codec-test"


class CliMode(Enum):
    """起動モード。"""

    PLAYER = auto()
    SELFTEST = auto()
    CODEC_TEST = auto()
    INVALID = auto()


@dataclass(frozen=True, slots=True)
class CliCommand:
    """解析済みの起動モードとplayer用引数。

    ``path_arguments`` はモードによって意味が変わる。

    - ``PLAYER``: プレイリストへ追加する候補
    - ``CODEC_TEST``: decode検査の対象（**プレイリストへは追加しない**）
    """

    mode: CliMode
    path_arguments: tuple[str, ...] = ()
    error_message: str | None = None


def parse_cli_arguments(arguments: Sequence[str]) -> CliCommand:
    """起動モードと通常のファイルpath群を過不足なく分類する。

    モードoptionは排他とし、`--codec-test` は検査対象pathを1つ以上必須にする
    （製品配布物へ検査用の音源を同梱しないため、pathは呼び出し側が渡す）。
    """
    values = tuple(arguments)
    mode_options = tuple(value for value in values if value in {SELFTEST_OPTION, CODEC_TEST_OPTION})
    if len(set(mode_options)) > 1:
        return CliCommand(
            CliMode.INVALID,
            error_message=f"{SELFTEST_OPTION}と{CODEC_TEST_OPTION}は同時に指定できません。",
        )

    if SELFTEST_OPTION in values:
        if values == (SELFTEST_OPTION,):
            return CliCommand(CliMode.SELFTEST)
        return CliCommand(
            CliMode.INVALID,
            error_message=f"{SELFTEST_OPTION}とファイルpathは同時に指定できません。",
        )

    if CODEC_TEST_OPTION in values:
        if values[0] != CODEC_TEST_OPTION:
            return CliCommand(
                CliMode.INVALID,
                error_message=f"{CODEC_TEST_OPTION}は最初に指定してください。",
            )
        targets = values[1:]
        unknown_options = tuple(value for value in targets if value.startswith("--"))
        if unknown_options:
            return CliCommand(
                CliMode.INVALID,
                error_message=f"未知のoptionです: {unknown_options[0]}",
            )
        if not targets:
            return CliCommand(
                CliMode.INVALID,
                error_message=f"{CODEC_TEST_OPTION}には検査するファイルpathを指定してください。",
            )
        return CliCommand(CliMode.CODEC_TEST, path_arguments=targets)

    unknown_options = tuple(value for value in values if value.startswith("--"))
    if unknown_options:
        return CliCommand(
            CliMode.INVALID,
            error_message=f"未知のoptionです: {unknown_options[0]}",
        )
    return CliCommand(CliMode.PLAYER, path_arguments=values)
