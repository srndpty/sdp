"""再生設定・可視化表示設定の設定ファイル、および保存ライフサイクル。

- 設定の検証・JSON変換・schema移行はQt非依存な関数として置く。
- :class:`AppSettingsController` が実行時の適用（PlaybackControllerと
  PlaylistPlaybackController、および可視化）を調停し、
  :class:`SettingsSession` が復元とデバウンス保存だけを担う。
- この層からUI（QWidget）とBackendの具体型は参照しない。

保存する値のうち Repeat は、``RepeatMode``（core側のenum、値は ``auto()`` で
永続化を意図していない）をそのままJSONへ書かず、**安定した文字列**
:class:`RepeatModeSetting` へ写して保存する。core enumの定義順や実装が変わっても
ファイル互換を壊さないため。
"""

import json
import logging
import math
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QObject, QTimer, Signal, SignalInstance, Slot

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.preferences import MAX_PLAYBACK_RATE, MIN_PLAYBACK_RATE
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.core.playlist.types import RepeatMode

_logger = logging.getLogger(__name__)

SETTINGS_SCHEMA_VERSION = 3
"""現在のschema version。

- version 1: 速度とピッチ補正だけ
- version 2: 可視化3項目を追加
- version 3: 音量・ミュート・Repeat・Shuffleを追加
"""

SUPPORTED_SETTINGS_SCHEMA_VERSIONS = (1, 2, 3)
"""読み込みを許可するschema version。未知のversionは既定値へ丸めず失敗させる。"""

MIN_VOLUME = 0.0
MAX_VOLUME = 1.0
"""``PlaybackController.set_volume`` の公開契約に合わせた音量の範囲。"""

DEFAULT_DEBOUNCE_MS = 1_500
DEFAULT_RETRY_MS = 5_000
RESTORE_FAILED_MESSAGE = "設定の復元に失敗しました。既定値で起動します。"


class SettingsFileError(Exception):
    """settings.jsonが壊れている、または契約どおり解釈できない。"""


class RepeatModeSetting(Enum):
    """Repeatの保存表現（JSONへ書く安定した文字列）。

    core の :class:`~sdp.core.playlist.types.RepeatMode` は ``auto()`` の値を持ち
    永続化を意図していないため、保存層では別のenumへ写す。
    """

    OFF = "off"
    ALL = "all"
    ONE = "one"

    @classmethod
    def from_repeat_mode(cls, mode: RepeatMode) -> "RepeatModeSetting":
        return _REPEAT_MODE_TO_SETTING[mode]

    def to_repeat_mode(self) -> RepeatMode:
        return _SETTING_TO_REPEAT_MODE[self]


_REPEAT_MODE_TO_SETTING: dict[RepeatMode, RepeatModeSetting] = {
    RepeatMode.OFF: RepeatModeSetting.OFF,
    RepeatMode.ALL: RepeatModeSetting.ALL,
    RepeatMode.ONE: RepeatModeSetting.ONE,
}
"""core enumと保存表現の対応。値が増えたらここで必ず失敗する（暗黙の既定値にしない）。"""

_SETTING_TO_REPEAT_MODE: dict[RepeatModeSetting, RepeatMode] = {
    setting: mode for mode, setting in _REPEAT_MODE_TO_SETTING.items()
}


@dataclass(frozen=True, slots=True)
class AppSettings:
    """永続化する設定一式（再生設定と可視化の表示ON/OFF）。

    可視化の色・バンド数・FPS・Peak hold時間、再生位置、再生中かどうかは
    保存対象に含めない。
    """

    playback_rate: float
    pitch_compensation: bool
    waveform_visible: bool = True
    spectrum_visible: bool = True
    level_meter_visible: bool = True
    volume: float = 1.0
    muted: bool = False
    repeat_mode: RepeatModeSetting = RepeatModeSetting.OFF
    shuffle_enabled: bool = False


_VISIBILITY_FIELDS = ("waveform_visible", "spectrum_visible", "level_meter_visible")
_BOOL_FIELDS = ("pitch_compensation", *_VISIBILITY_FIELDS, "muted", "shuffle_enabled")


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
    # 古いversionでは、後のversionのキーが混入していても未知キーとして無視する
    # （v1のファイルへ手で書いた"volume"などをv1の意味へ影響させない）。
    known_visibility = version >= 2
    known_playback_state = version >= 3

    def visibility(name: str) -> bool:
        fallback: bool = getattr(defaults, name)
        if not known_visibility:
            return fallback
        return _bool_from_json(name, document.get(name, fallback))

    return AppSettings(
        playback_rate=rate,
        pitch_compensation=pitch,
        waveform_visible=visibility("waveform_visible"),
        spectrum_visible=visibility("spectrum_visible"),
        level_meter_visible=visibility("level_meter_visible"),
        volume=(
            _volume_from_json(document.get("volume", defaults.volume))
            if known_playback_state
            else defaults.volume
        ),
        muted=(
            _bool_from_json("muted", document.get("muted", defaults.muted))
            if known_playback_state
            else defaults.muted
        ),
        repeat_mode=(
            _repeat_mode_from_json(document.get("repeat_mode", defaults.repeat_mode))
            if known_playback_state
            else defaults.repeat_mode
        ),
        shuffle_enabled=(
            _bool_from_json(
                "shuffle_enabled", document.get("shuffle_enabled", defaults.shuffle_enabled)
            )
            if known_playback_state
            else defaults.shuffle_enabled
        ),
    )


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
        "volume": float(settings.volume),
        "muted": settings.muted,
        "repeat_mode": settings.repeat_mode.value,
        "shuffle_enabled": settings.shuffle_enabled,
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


def _volume_from_json(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsFileError(f"volumeが数値ではありません: {value!r}")
    volume = float(value)
    # 範囲外は暗黙にclampせず復元失敗にする（設定ファイルの誤りを黙って直さない）。
    if not math.isfinite(volume) or not MIN_VOLUME <= volume <= MAX_VOLUME:
        raise SettingsFileError(f"volumeが復元可能な範囲外です: {value!r}")
    return volume


def _repeat_mode_from_json(value: object) -> RepeatModeSetting:
    if isinstance(value, RepeatModeSetting):
        return value
    if not isinstance(value, str):
        raise SettingsFileError(f"repeat_modeが文字列ではありません: {value!r}")
    try:
        return RepeatModeSetting(value)
    except ValueError as error:
        raise SettingsFileError(f"未知のrepeat_modeです: {value!r}") from error


def _bool_from_json(name: str, value: object) -> bool:
    # 0／1／"true" を受理しない（bool欄は厳密に判定する）。
    if type(value) is not bool:
        raise SettingsFileError(f"{name}がboolではありません: {value!r}")
    return value


def validate_settings(settings: AppSettings) -> None:
    """保存・適用の前に値域と型を検証する（不正なら :class:`ValueError`）。"""
    for check in (
        lambda: _rate_from_json(settings.playback_rate),
        lambda: _volume_from_json(settings.volume),
    ):
        try:
            check()
        except SettingsFileError as error:
            raise ValueError(str(error)) from error
    for name in _BOOL_FIELDS:
        value: object = getattr(settings, name)
        if type(value) is not bool:
            raise ValueError(f"{name}はboolで指定してください: {value!r}")
    if type(settings.repeat_mode) is not RepeatModeSetting:
        raise ValueError(
            f"repeat_modeはRepeatModeSettingで指定してください: {settings.repeat_mode!r}"
        )


class AppSettingsController(QObject):
    """適用済み設定のsnapshotを保持し、変更を各層へ配る調停サービス。

    実効値の持ち主は2つある。

    - 再生速度・ピッチ補正・音量・ミュート: :class:`PlaybackController`
    - Repeat・Shuffle: :class:`PlaylistPlaybackController`

    このクラスは両者を調停し、**適用が全部成功したときだけ**実効値のsnapshotを
    1回だけ公開する。可視化の表示ON/OFFは :attr:`settings_changed` で公開するだけで、
    どのWidgetをどう隠すかは知らない（MainWindowの配置責務）。
    JSON読み書き、QDialog、Backend具体型、PCM解析、FFT／レベル計算、
    プレイリストの曲順操作は持たない。

    SpeedPanel・PlayerControls・ショートカットから各Controllerが直接変更された場合も、
    snapshotを追従させて保存対象を1か所に保つ。
    """

    settings_changed = Signal(object)
    """適用済み設定が変化した（引数は :class:`AppSettings`）。"""

    settings_rollback_failed = Signal(object)
    """適用失敗後のrollbackも失敗した（引数は戻せなかった項目名のtuple）。

    公開snapshotは実状態から作り直すため矛盾は残らないが、利用者から見ると
    「操作していない設定が変わった」状態になり得るので、UIから通知できるようにする。
    """

    def __init__(
        self,
        playback: PlaybackController,
        playlist_playback: PlaylistPlaybackController | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playback = playback
        self._playlist_playback = playlist_playback
        self._shutdown = False
        self._applying = False
        self._settings = AppSettings(
            playback_rate=playback.playback_rate,
            pitch_compensation=playback.pitch_compensation,
            volume=playback.volume,
            muted=playback.muted,
            repeat_mode=(
                RepeatModeSetting.OFF
                if playlist_playback is None
                else RepeatModeSetting.from_repeat_mode(playlist_playback.repeat_mode)
            ),
            shuffle_enabled=(
                False if playlist_playback is None else playlist_playback.shuffle_enabled
            ),
        )
        playback.playback_rate_changed.connect(self._on_playback_rate_changed)
        playback.pitch_compensation_changed.connect(self._on_pitch_compensation_changed)
        playback.volume_changed.connect(self._on_volume_changed)
        playback.muted_changed.connect(self._on_muted_changed)
        if playlist_playback is not None:
            playlist_playback.repeat_mode_changed.connect(self._on_repeat_mode_changed)
            playlist_playback.shuffle_enabled_changed.connect(self._on_shuffle_enabled_changed)

    @property
    def settings(self) -> AppSettings:
        """現在適用済みの設定snapshot。"""
        return self._settings

    def apply(self, settings: AppSettings) -> None:
        """適用成功後に実効値を1回だけ公開する（同値なら通知しない）。

        途中で失敗した場合は部分適用を残さないよう、変更済みのControllerを
        直前の値へ戻してから例外を送出する。**rollbackの成否に関わらず、
        公開snapshotは各Controllerの実状態から作り直す**（「戻せたはず」の値を
        公開して、snapshotと実状態が食い違ったままになるのを避ける）。
        rollbackにも失敗した項目があれば :attr:`settings_rollback_failed` で通知する。
        """
        if self._shutdown:
            raise RuntimeError("shutdown後のAppSettingsControllerへ設定は適用できません")
        validate_settings(settings)
        if settings == self._settings:
            return
        previous = self._effective_settings()
        self._applying = True
        try:
            self._apply_to_controllers(settings, previous)
        except Exception:
            rollback_failures = self._restore_controllers(previous)
            self._applying = False
            self._set_settings(self._effective_settings(fallback=previous))
            if rollback_failures:
                self.settings_rollback_failed.emit(tuple(rollback_failures))
            raise
        finally:
            self._applying = False
        self._set_settings(self._effective_settings(fallback=settings))

    def shutdown(self) -> None:
        """Controller監視を解除する（冪等）。"""
        if self._shutdown:
            return
        self._shutdown = True
        connections: list[tuple[SignalInstance, object]] = [
            (self._playback.playback_rate_changed, self._on_playback_rate_changed),
            (self._playback.pitch_compensation_changed, self._on_pitch_compensation_changed),
            (self._playback.volume_changed, self._on_volume_changed),
            (self._playback.muted_changed, self._on_muted_changed),
        ]
        playlist_playback = self._playlist_playback
        if playlist_playback is not None:
            connections.append(
                (playlist_playback.repeat_mode_changed, self._on_repeat_mode_changed)
            )
            connections.append(
                (playlist_playback.shuffle_enabled_changed, self._on_shuffle_enabled_changed)
            )
        for signal, slot in connections:
            try:
                signal.disconnect(slot)
            except RuntimeError:
                _logger.debug("AppSettingsControllerのController接続は既に解除されています")

    # -- Controller からの通知 ----------------------------------------------

    @Slot(float)
    def _on_playback_rate_changed(self, value: float) -> None:
        self._mirror(playback_rate=float(value))

    @Slot(bool)
    def _on_pitch_compensation_changed(self, value: bool) -> None:
        self._mirror(pitch_compensation=bool(value))

    @Slot(float)
    def _on_volume_changed(self, value: float) -> None:
        self._mirror(volume=float(value))

    @Slot(bool)
    def _on_muted_changed(self, value: bool) -> None:
        self._mirror(muted=bool(value))

    @Slot(RepeatMode)
    def _on_repeat_mode_changed(self, value: RepeatMode) -> None:
        self._mirror(repeat_mode=RepeatModeSetting.from_repeat_mode(value))

    @Slot(bool)
    def _on_shuffle_enabled_changed(self, value: bool) -> None:
        self._mirror(shuffle_enabled=bool(value))

    # -- 内部 ---------------------------------------------------------------

    def _mirror(self, **changes: object) -> None:
        """UI操作などによるController側の変更をsnapshotへ取り込む。"""
        if self._shutdown or self._applying:
            return
        self._set_settings(replace(self._settings, **changes))  # pyright: ignore[reportArgumentType]

    def _apply_to_controllers(self, settings: AppSettings, previous: AppSettings) -> None:
        if settings.playback_rate != previous.playback_rate:
            self._playback.set_playback_rate(settings.playback_rate)
        if settings.pitch_compensation != previous.pitch_compensation:
            self._playback.set_pitch_compensation(settings.pitch_compensation)
        if settings.volume != previous.volume:
            self._playback.set_volume(settings.volume)
        if settings.muted != previous.muted:
            self._playback.set_muted(settings.muted)
        playlist_playback = self._playlist_playback
        if playlist_playback is None:
            return
        if settings.repeat_mode != previous.repeat_mode:
            playlist_playback.set_repeat_mode(settings.repeat_mode.to_repeat_mode())
        if settings.shuffle_enabled != previous.shuffle_enabled:
            playlist_playback.set_shuffle_enabled(settings.shuffle_enabled)

    def _restore_controllers(self, previous: AppSettings) -> list[str]:
        """適用途中の失敗後、各Controllerを直前の値へ可能な限り戻す。

        戻せなかった項目名を返す。完全なトランザクションは保証できないため、
        呼び出し側は結果に関わらず実状態を読み直してsnapshotへ反映する。
        """
        playlist_playback = self._playlist_playback
        restores: list[tuple[str, Callable[[], None]]] = [
            ("ミュート", lambda: self._playback.set_muted(previous.muted)),
            ("音量", lambda: self._playback.set_volume(previous.volume)),
            (
                "ピッチ補正",
                lambda: self._playback.set_pitch_compensation(previous.pitch_compensation),
            ),
            ("再生速度", lambda: self._playback.set_playback_rate(previous.playback_rate)),
        ]
        if playlist_playback is not None:
            restores.insert(
                0,
                (
                    "シャッフル",
                    lambda: playlist_playback.set_shuffle_enabled(previous.shuffle_enabled),
                ),
            )
            restores.insert(
                0,
                (
                    "リピート",
                    lambda: playlist_playback.set_repeat_mode(
                        previous.repeat_mode.to_repeat_mode()
                    ),
                ),
            )
        failed: list[str] = []
        for name, restore in restores:
            try:
                restore()
            except Exception:
                _logger.exception("設定適用失敗後に%sを元へ戻せませんでした", name)
                failed.append(name)
        return failed

    def _effective_settings(self, fallback: AppSettings | None = None) -> AppSettings:
        """各Controllerの実効値と、可視化設定を合わせたsnapshotを作る。"""
        base = self._settings if fallback is None else fallback
        playlist_playback = self._playlist_playback
        return replace(
            base,
            playback_rate=self._playback.playback_rate,
            pitch_compensation=self._playback.pitch_compensation,
            volume=self._playback.volume,
            muted=self._playback.muted,
            repeat_mode=(
                base.repeat_mode
                if playlist_playback is None
                else RepeatModeSetting.from_repeat_mode(playlist_playback.repeat_mode)
            ),
            shuffle_enabled=(
                base.shuffle_enabled
                if playlist_playback is None
                else playlist_playback.shuffle_enabled
            ),
        )

    def _set_settings(self, settings: AppSettings) -> None:
        if settings == self._settings:
            return
        self._settings = settings
        self.settings_changed.emit(settings)


class SettingsSession(QObject):
    """設定snapshotとsettings.jsonの復元・デバウンス保存を取り持つ。"""

    save_failed = Signal()
    """保存に失敗した（**成功→失敗へ変わったときだけ**）。"""

    save_recovered = Signal()
    """失敗のあとに保存できた（**失敗→成功へ変わったときだけ**）。"""

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
        self._save_failed = False
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
        # 保存済み基準は「ファイルの要求値」ではなく「適用後の実効snapshot」にする。
        # Backendが要求値を丸める場合（例: 1.5 → float32の1.4999999…）に、
        # ユーザーが何も操作していなくても終了時に変更ありと誤判定するため。
        self._last_saved = self._snapshot()
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
            self._report_failure()
            return False
        self._retry_attempted = False
        self._last_saved = settings
        self._report_success()
        return True

    def _report_failure(self) -> None:
        """状態が変わったときだけ通知する（デバウンスの度に溢れさせない）。"""
        if self._save_failed:
            return
        self._save_failed = True
        self.save_failed.emit()

    def _report_success(self) -> None:
        if not self._save_failed:
            return
        self._save_failed = False
        self.save_recovered.emit()

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
