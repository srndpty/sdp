"""起動モードと通常path引数を分けるQt非依存のCLI境界。"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto


class CliMode(Enum):
    """起動モード。"""

    PLAYER = auto()
    SELFTEST = auto()
    INVALID = auto()


@dataclass(frozen=True, slots=True)
class CliCommand:
    """解析済みの起動モードとplayer用引数。"""

    mode: CliMode
    path_arguments: tuple[str, ...] = ()
    error_message: str | None = None


def parse_cli_arguments(arguments: Sequence[str]) -> CliCommand:
    """`--selftest`と通常のファイルpath群を過不足なく分類する。"""
    values = tuple(arguments)
    if values == ("--selftest",):
        return CliCommand(CliMode.SELFTEST)
    if "--selftest" in values:
        return CliCommand(
            CliMode.INVALID,
            error_message="--selftestとファイルpathは同時に指定できません。",
        )
    unknown_options = tuple(value for value in values if value.startswith("--"))
    if unknown_options:
        return CliCommand(
            CliMode.INVALID,
            error_message=f"未知のoptionです: {unknown_options[0]}",
        )
    return CliCommand(CliMode.PLAYER, path_arguments=values)
