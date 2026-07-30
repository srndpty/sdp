"""再生設定・可視化表示設定の設定ファイル、および保存ライフサイクル。

- 設定の検証・JSON変換・schema移行はQt非依存な関数として置く。
- :class:`AppSettingsController` が実行時の適用（PlaybackControllerと可視化）を調停し、
  :class:`SettingsSession` が復元とデバウンス保存だけを担う。
- この層からUI（QWidget）とBackendの具体型は参照しない。
"""

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.preferences import MAX_PLAYBACK_RATE, MIN_PLAYBACK_RATE

_logger = logging.getLogger(__name__)

SETTINGS_SCHEMA_VERSION = 2
"""現在のschema version。version 1（可視化設定なし）も読み込める。"""

SUPPORTED_SETTINGS_SCHEMA_VERSIONS = (1, 2)
"""読み込みを許可するschema version。未知のversionは既定値へ丸めず失敗させる。"""

DEFAULT_DEBOUNCE_MS = 1_500
DEFAULT_RETRY_MS = 5_000
RESTORE_FAILED_MESSAGE = "設定の復元に失敗しました。既定値で起動します。"


class SettingsFileError(Exception):
    """settings.jsonが壊れている、または契約どおり解釈できない。"""


@dataclass(frozen=True, slots=True)
class AppSettings:
    """永続化する設定一式（再生と可視化の表示ON/OFF）。

    可視化の色・バンド数・FPS・Peak hold時間などは保存対象に含めない（P6-A範囲外）。
    """

    playback_rate: float
    pitch_compensation: bool
    waveform_visible: bool = True
    spectrum_visible: bool = True
    level_meter_visible: bool = True


_VISIBILITY_FIELDS = ("waveform_visible", "spectrum_visible", "level_meter_visible")


def load_settings(file_path: Path, defaults: AppSettings) -> AppSettings:
    """設定を読み込む。未作成ならアプリ既定値をそのまま返す。

    schema version 1 には可視化設定が存在しないため、``defaults`` の値
    （通常はすべて表示ON）で補う。**読み込みだけでファイルは書き換えない。**
    欠落した既知キーは既定値で補い、値が不正な既知キーは明示的に失敗させる。
    """
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
    if type(version) is not int or version not in SUPPORTED_SETTINGS_SCHEMA_VERSIONS:
        raise SettingsFileError(
            "未対応の設定schema_versionです"
            f"（対応 {list(SUPPORTED_SETTINGS_SCHEMA_VERSIONS)}、実際 {version!r}）"
        )

    rate = _rate_from_json(document.get("playback_rate", defaults.playback_rate))
    pitch = _bool_from_json(
        "pitch_compensation", document.get("pitch_compensation", defaults.pitch_compensation)
    )
    if version == 1:
        # v2のキーが未知キーとして混入していても、v1の意味へ影響させない。
        visibility = {name: getattr(defaults, name) for name in _VISIBILITY_FIELDS}
    else:
        visibility = {
            name: _bool_from_json(name, document.get(name, getattr(defaults, name)))
            for name in _VISIBILITY_FIELDS
        }
    return AppSettings(playback_rate=rate, pitch_compensation=pitch, **visibility)


def save_settings(file_path: Path, settings: AppSettings) -> None:
    """検証済み設定を同一ディレクトリの一時ファイル経由でアトミック保存する。"""
    validate_settings(settings)
    document: dict[str, Any] = {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "playback_rate": float(settings.playback_rate),
        "pitch_compensation": settings.pitch_compensation,
        "waveform_visible": settings.waveform_visible,
        "spectrum_visible": settings.spectrum_visible,
        "level_meter_visible": settings.level_meter_visible,
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


def _bool_from_json(name: str, value: object) -> bool:
    # 0／1／"true" を受理しない（bool欄は厳密に判定する）。
    if type(value) is not bool:
        raise SettingsFileError(f"{name}がboolではありません: {value!r}")
    return value


def validate_settings(settings: AppSettings) -> None:
    """保存・適用の前に値域と型を検証する（不正なら :class:`ValueError`）。"""
    try:
        _rate_from_json(settings.playback_rate)
    except SettingsFileError as error:
        raise ValueError(str(error)) from error
    for name in ("pitch_compensation", *_VISIBILITY_FIELDS):
        value: object = getattr(settings, name)
        if type(value) is not bool:
            raise ValueError(f"{name}はboolで指定してください: {value!r}")


class AppSettingsController(QObject):
    """適用済み設定のsnapshotを保持し、変更を各層へ配る調停サービス。

    - 再生速度とピッチ補正は :class:`PlaybackController` へ適用する。
    - 可視化の表示ON/OFFは :attr:`settings_changed` で公開するだけで、
      どのWidgetをどう隠すかは知らない（MainWindowの配置責務）。
    - JSON読み書き、QDialog、Backend具体型、PCM解析、FFT／レベル計算、
      プレイリスト操作は持たない。

    SpeedPanelやショートカットからControllerが直接変更された場合も、
    snapshotを追従させて保存対象を1か所に保つ。
    """

    settings_changed = Signal(object)
    """適用済み設定が変化した（引数は :class:`AppSettings`）。"""

    def __init__(self, playback: PlaybackController, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._playback = playback
        self._shutdown = False
        self._applying = False
        self._settings = AppSettings(
            playback_rate=playback.playback_rate,
            pitch_compensation=playback.pitch_compensation,
        )
        playback.playback_rate_changed.connect(self._on_playback_rate_changed)
        playback.pitch_compensation_changed.connect(self._on_pitch_compensation_changed)

    @property
    def settings(self) -> AppSettings:
        """現在適用済みの設定snapshot。"""
        return self._settings

    def apply(self, settings: AppSettings) -> None:
        """Controller適用成功後に実効値を1回だけ公開する（同値なら通知しない）。"""
        if self._shutdown:
            raise RuntimeError("shutdown後のAppSettingsControllerへ設定は適用できません")
        validate_settings(settings)
        if settings == self._settings:
            return
        previous_rate = self._playback.playback_rate
        previous_pitch = self._playback.pitch_compensation
        self._applying = True
        try:
            if settings.playback_rate != previous_rate:
                self._playback.set_playback_rate(settings.playback_rate)
            if settings.pitch_compensation != previous_pitch:
                self._playback.set_pitch_compensation(settings.pitch_compensation)
        except Exception:
            # 2項目目の失敗で1項目目だけ残さない。rollback失敗は記録し、元の例外を再送出する。
            self._restore_playback(previous_rate, previous_pitch)
            raise
        finally:
            self._applying = False

        applied = replace(
            settings,
            playback_rate=self._playback.playback_rate,
            pitch_compensation=self._playback.pitch_compensation,
        )
        self._set_settings(applied)

    def shutdown(self) -> None:
        """Controller監視を解除する（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        for signal, slot in (
            (self._playback.playback_rate_changed, self._on_playback_rate_changed),
            (self._playback.pitch_compensation_changed, self._on_pitch_compensation_changed),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                _logger.debug("AppSettingsControllerのController接続は既に解除されています")

    @Slot(float)
    def _on_playback_rate_changed(self, value: float) -> None:
        if self._shutdown or self._applying:
            return
        self._set_settings(replace(self._settings, playback_rate=float(value)))

    @Slot(bool)
    def _on_pitch_compensation_changed(self, value: bool) -> None:
        if self._shutdown or self._applying:
            return
        self._set_settings(replace(self._settings, pitch_compensation=bool(value)))

    def _restore_playback(self, playback_rate: float, pitch_compensation: bool) -> None:
        """適用途中の失敗後、Controllerを直前の値へ可能な限り戻す。"""
        for name, restore in (
            ("ピッチ補正", lambda: self._playback.set_pitch_compensation(pitch_compensation)),
            ("再生速度", lambda: self._playback.set_playback_rate(playback_rate)),
        ):
            try:
                restore()
            except Exception:
                _logger.exception("設定適用失敗後に%sを元へ戻せませんでした", name)

    def _set_settings(self, settings: AppSettings) -> None:
        if settings == self._settings:
            return
        self._settings = settings
        self.settings_changed.emit(settings)


class SettingsSession(QObject):
    """設定snapshotとsettings.jsonの復元・デバウンス保存を取り持つ。"""

    def __init__(
        self,
        file_path: Path,
        app_settings: AppSettingsController,
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        retry_ms: int = DEFAULT_RETRY_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._file_path = file_path
        self._app_settings = app_settings
        self._save_enabled = True
        self._started = False
        self._debounce_ms = max(1, debounce_ms)
        self._retry_ms = max(1, retry_ms)
        self._retry_attempted = False
        self._last_saved = self._snapshot()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._debounce_ms)
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
        """現在のsnapshotを欠落キーの既定値として復元し、適用する。

        version 1 のファイルを読んでもここでは保存しない（起動直後に
        version 2 で無条件に書き換えない）。次にユーザーが設定を変更したとき、
        通常の保存契機で version 2 として保存される。
        """
        defaults = self._snapshot()
        try:
            settings = load_settings(self._file_path, defaults)
        except (SettingsFileError, OSError):
            _logger.exception("設定の復元に失敗しました: %s", self._file_path)
            self._save_enabled = False
            return RESTORE_FAILED_MESSAGE

        self._app_settings.apply(settings)
        self._last_saved = settings
        return None

    def start(self) -> None:
        """変更監視を開始する（冪等）。復元適用中は呼ばない。"""
        if self._started:
            return
        self._started = True
        self._app_settings.settings_changed.connect(self._schedule_save)

    def flush(self) -> bool:
        """未保存snapshotを即時保存する。失敗しても例外を外へ出さない。"""
        timer_triggered = self.sender() is self._timer
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
            if timer_triggered and self._started and not self._retry_attempted:
                self._retry_attempted = True
                self._timer.start(self._retry_ms)
                _logger.info("設定保存を%dミリ秒後に1回再試行します", self._retry_ms)
            return False
        self._retry_attempted = False
        self._last_saved = settings
        return True

    def stop(self) -> None:
        """タイマーと変更監視を止める（冪等）。flushは呼び出し側が先に行う。"""
        self._timer.stop()
        if not self._started:
            return
        self._started = False
        self._app_settings.settings_changed.disconnect(self._schedule_save)

    def _schedule_save(self, value: object) -> None:
        del value
        if self._save_enabled:
            self._retry_attempted = False
            self._timer.start(self._debounce_ms)

    def _snapshot(self) -> AppSettings:
        return self._app_settings.settings
