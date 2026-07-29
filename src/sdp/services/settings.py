"""再生速度とピッチ補正の設定ファイル、および保存ライフサイクル。"""

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QObject, QTimer

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.preferences import MAX_PLAYBACK_RATE, MIN_PLAYBACK_RATE

_logger = logging.getLogger(__name__)

SETTINGS_SCHEMA_VERSION = 1
DEFAULT_DEBOUNCE_MS = 1_500
RESTORE_FAILED_MESSAGE = "設定の復元に失敗しました。既定値で起動します。"


class SettingsFileError(Exception):
    """settings.jsonが壊れている、または契約どおり解釈できない。"""


@dataclass(frozen=True, slots=True)
class AppSettings:
    """今回永続化する再生設定。"""

    playback_rate: float
    pitch_compensation: bool


def load_settings(file_path: Path, defaults: AppSettings) -> AppSettings:
    """設定を読み込む。未作成ならController由来の既定値をそのまま返す。"""
    try:
        text = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return defaults
    except UnicodeDecodeError as error:
        raise SettingsFileError(f"設定ファイルがUTF-8として不正です: {file_path}") from error
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise SettingsFileError(f"設定ファイルがJSONとして不正です: {file_path}") from error
    if not isinstance(parsed, dict):
        raise SettingsFileError(f"設定ファイルのルートがオブジェクトではありません: {file_path}")
    document = cast("dict[str, object]", parsed)

    version = document.get("schema_version")
    if type(version) is not int or version != SETTINGS_SCHEMA_VERSION:
        raise SettingsFileError(
            f"未対応の設定schema_versionです（期待 {SETTINGS_SCHEMA_VERSION}、実際 {version!r}）"
        )

    rate = _rate_from_json(document.get("playback_rate", defaults.playback_rate))
    pitch = document.get("pitch_compensation", defaults.pitch_compensation)
    if type(pitch) is not bool:
        raise SettingsFileError(f"pitch_compensationがboolではありません: {pitch!r}")
    return AppSettings(playback_rate=rate, pitch_compensation=pitch)


def save_settings(file_path: Path, settings: AppSettings) -> None:
    """検証済み設定を同一ディレクトリの一時ファイル経由でアトミック保存する。"""
    _validate_settings_for_save(settings)
    document: dict[str, Any] = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "playback_rate": float(settings.playback_rate),
        "pitch_compensation": settings.pitch_compensation,
    }
    # 検証に成功するまでディレクトリも一時ファイルも作らない。
    file_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=file_path.parent, prefix=f"{file_path.name}.", suffix=".tmp"
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, file_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _rate_from_json(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsFileError(f"playback_rateが数値ではありません: {value!r}")
    rate = float(value)
    if not math.isfinite(rate) or not MIN_PLAYBACK_RATE <= rate <= MAX_PLAYBACK_RATE:
        raise SettingsFileError(f"playback_rateが復元可能な範囲外です: {value!r}")
    return rate


def _validate_settings_for_save(settings: AppSettings) -> None:
    try:
        _rate_from_json(settings.playback_rate)
    except SettingsFileError as error:
        raise ValueError(str(error)) from error
    if type(settings.pitch_compensation) is not bool:
        raise ValueError(
            f"pitch_compensationはboolで指定してください: {settings.pitch_compensation!r}"
        )


class SettingsSession(QObject):
    """Controllerとsettings.jsonの復元・デバウンス保存を取り持つ。"""

    def __init__(
        self,
        file_path: Path,
        controller: PlaybackController,
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._file_path = file_path
        self._controller = controller
        self._save_enabled = True
        self._started = False
        self._last_saved = self._snapshot()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(1, debounce_ms))
        self._timer.timeout.connect(self.flush)

    @property
    def file_path(self) -> Path:
        return self._file_path

    @property
    def is_save_enabled(self) -> bool:
        return self._save_enabled

    @property
    def is_running(self) -> bool:
        return self._started

    def load(self) -> str | None:
        """Controller初期値を欠落キーの既定値として復元する。"""
        defaults = self._snapshot()
        try:
            settings = load_settings(self._file_path, defaults)
        except (SettingsFileError, OSError):
            _logger.exception("設定の復元に失敗しました: %s", self._file_path)
            self._save_enabled = False
            return RESTORE_FAILED_MESSAGE

        self._controller.set_playback_rate(settings.playback_rate)
        self._controller.set_pitch_compensation(settings.pitch_compensation)
        self._last_saved = settings
        return None

    def start(self) -> None:
        """変更監視を開始する（冪等）。復元適用中は呼ばない。"""
        if self._started:
            return
        self._started = True
        self._controller.playback_rate_changed.connect(self._schedule_save)
        self._controller.pitch_compensation_changed.connect(self._schedule_save)

    def flush(self) -> bool:
        """未保存snapshotを即時保存する。失敗しても例外を外へ出さない。"""
        self._timer.stop()
        if not self._save_enabled:
            _logger.info("復元に失敗したため、設定を保存しません: %s", self._file_path)
            return False
        settings = self._snapshot()
        if settings == self._last_saved:
            return False
        try:
            save_settings(self._file_path, settings)
        except (OSError, ValueError):
            _logger.exception("設定の保存に失敗しました: %s", self._file_path)
            return False
        self._last_saved = settings
        return True

    def stop(self) -> None:
        """タイマーと変更監視を止める（冪等）。flushは呼び出し側が先に行う。"""
        self._timer.stop()
        if not self._started:
            return
        self._started = False
        self._controller.playback_rate_changed.disconnect(self._schedule_save)
        self._controller.pitch_compensation_changed.disconnect(self._schedule_save)

    def _schedule_save(self, value: object) -> None:
        del value
        if self._save_enabled:
            self._timer.start()

    def _snapshot(self) -> AppSettings:
        return AppSettings(
            playback_rate=self._controller.playback_rate,
            pitch_compensation=self._controller.pitch_compensation,
        )
