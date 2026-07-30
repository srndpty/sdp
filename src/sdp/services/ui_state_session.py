"""UI状態（ui-state.json）の起動時復元とデバウンス保存。

Qt側の調停だけを担い、JSON解析・schema検証・アトミック保存は
:mod:`sdp.services.ui_state`（Qt非依存）へ委譲する。

MainWindowへは「現在状態の取得」「復元の適用」「変更通知」という小さなAPIだけを
要求し、保存先やschema versionはWindowへ持ち込まない。
"""

import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.services.ui_state import (
    RESTORE_FAILED_MESSAGE,
    UiState,
    UiStateFileError,
    load_ui_state,
    save_ui_state,
)

_logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_MS = 1_200
"""移動・リサイズが続くあいだ書き込まないための待ち時間。"""

DEFAULT_RETRY_MS = 5_000


class UiStateHolder(Protocol):
    """UI状態のやり取りに必要な最小のWindow契約。

    具体的な :class:`~sdp.ui.main_window.MainWindow` へは依存しない
    （services層からui層をimportしないため）。Signalそのものではなく購読・解除の
    メソッドを要求し、Qtの型をこの層の契約へ持ち込まない。
    """

    def capture_ui_state(self) -> UiState: ...

    def restore_ui_state(self, state: UiState) -> None: ...

    def connect_ui_state_changed(self, slot: Callable[[], None]) -> None: ...

    def disconnect_ui_state_changed(self, slot: Callable[[], None]) -> None: ...


class PlaylistUiStateSource(QObject):
    """WindowのUI状態へ「現在曲のentry_id」を合成するアダプター。

    :class:`UiStateSession` はこれも :class:`UiStateHolder` として扱うため、
    セッション側は PlaylistModel も entry_id の照合も知らない。
    現在曲の復元（存在しないentry_idの無視）はここで行う。
    """

    def __init__(
        self,
        window: UiStateHolder,
        playlist_playback: PlaylistPlaybackController,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._playlist_playback = playlist_playback

    def capture_ui_state(self) -> UiState:
        # 現在曲が消えていればControllerのcurrent_entry_idがNoneになり、保存対象からも消える。
        return replace(
            self._window.capture_ui_state(),
            current_playlist_entry_id=self._playlist_playback.current_entry_id,
        )

    def restore_ui_state(self, state: UiState) -> None:
        """Windowの状態を適用し、前回の現在曲があれば選び直す（再生はしない）。

        entry_idがPlaylistModelに無い・欠損している場合は復元をあきらめるだけで、
        ui-state全体を破損扱いにしない。次の保存で自然に消える。
        """
        self._window.restore_ui_state(state)
        entry_id = state.current_playlist_entry_id
        if entry_id is None:
            return
        if not self._playlist_playback.select_entry_by_id(entry_id):
            _logger.info("前回の現在曲は復元できませんでした（削除・欠損の可能性）")

    def connect_ui_state_changed(self, slot: Callable[[], None]) -> None:
        self._window.connect_ui_state_changed(slot)
        self._playlist_playback.current_entry_changed.connect(slot)

    def disconnect_ui_state_changed(self, slot: Callable[[], None]) -> None:
        self._window.disconnect_ui_state_changed(slot)
        self._playlist_playback.current_entry_changed.disconnect(slot)


class UiStateSession(QObject):
    """ui-state.jsonとMainWindowの間を取り持つ。

    復元に失敗した起動では保存を無効化して、壊れたファイルを上書きしない
    （settings.json / playlist.json と同じ方針で、障害は互いに独立させる）。
    """

    save_failed = Signal()
    """保存に失敗した（**成功→失敗へ変わったときだけ**）。"""

    save_recovered = Signal()
    """失敗のあとに保存できた（**失敗→成功へ変わったときだけ**）。"""

    def __init__(
        self,
        file_path: Path,
        window: UiStateHolder,
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        retry_ms: int = DEFAULT_RETRY_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._file_path = file_path
        self._window = window
        self._save_enabled = True
        self._started = False
        self._shutdown = False
        self._debounce_ms = max(1, debounce_ms)
        self._retry_ms = max(1, retry_ms)
        self._retry_attempted = False
        self._save_failed = False
        self._last_saved = UiState()
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

    def load_into_window(self) -> str | None:
        """保存済みUI状態をWindowへ適用し、UI向けメッセージを返す。

        ファイルが無ければ初回起動として既定のまま正常終了する。壊れている場合は
        既定状態で起動しつつ**この起動中の保存を無効化**する。
        復元の適用は保存契機にしない（``start()`` 前に呼ぶ）。
        """
        try:
            state = load_ui_state(self._file_path)
        except (UiStateFileError, OSError):
            _logger.exception("UI状態の復元に失敗しました: %s", self._file_path)
            self._save_enabled = False
            return RESTORE_FAILED_MESSAGE

        self._window.restore_ui_state(state)
        self._last_saved = state
        return None

    def start(self) -> None:
        """変更監視を開始する（冪等）。復元適用後・Window表示後に呼ぶ。"""
        if self._started or self._shutdown:
            return
        self._started = True
        self._window.connect_ui_state_changed(self.schedule_save)

    def schedule_save(self) -> None:
        """デバウンス保存を予約する（移動・リサイズごとには書き込まない）。"""
        if self._save_enabled and not self._shutdown:
            self._retry_attempted = False
            self._timer.start(self._debounce_ms)

    def flush(self) -> bool:
        """現在のUI状態を即時保存する。保存したら ``True``。

        Windowが破棄済みの場合は取得せずに諦める（終了処理を止めない）。
        """
        timer_triggered = self.sender() is self._timer
        self._timer.stop()
        if not self._save_enabled:
            _logger.info("復元に失敗したため、UI状態を保存しません: %s", self._file_path)
            return False
        try:
            state = self._window.capture_ui_state()
        except RuntimeError:
            # Windowが先に破棄された場合。geometryは取得できない。
            _logger.warning("Windowが破棄済みのため、UI状態を保存しません: %s", self._file_path)
            return False
        if state == self._last_saved:
            return False
        try:
            save_ui_state(self._file_path, state)
        except (OSError, ValueError):
            _logger.exception("UI状態の保存に失敗しました: %s", self._file_path)
            if timer_triggered and self._started and not self._retry_attempted:
                self._retry_attempted = True
                self._timer.start(self._retry_ms)
                _logger.info("UI状態の保存を%dミリ秒後に1回再試行します", self._retry_ms)
            self._report_failure()
            return False
        self._retry_attempted = False
        self._last_saved = state
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
        self._shutdown = True
        if not self._started:
            return
        self._started = False
        try:
            self._window.disconnect_ui_state_changed(self.schedule_save)
        except RuntimeError:
            # Windowが既に破棄済み。終了処理を妨げない。
            _logger.debug("UiStateSessionのWindow接続は既に解除されています")
