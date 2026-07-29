"""プレイリストと単曲再生の調停。

PlaybackController は「1 つの source の再生」だけを担当し、曲順は知らない。
このクラスが「今どの entry を再生しているか」と曲送りを担当する。

依存の向き: PlaylistPlaybackController → PlaybackController → PlaybackBackend
             PlaylistPlaybackController → PlaylistModel

リピートとシャッフルは P2-C2 で追加する。
"""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus
from sdp.core.playlist.model import PlaylistModel

_logger = logging.getLogger(__name__)

MISSING_FILE_MESSAGE = "ファイルが見つからないため再生できません。"
END_OF_PLAYLIST_MESSAGE = "プレイリストの最後まで再生しました。"

_STALE_END_OF_MEDIA_RATIO = 0.5
"""古い ``END_OF_MEDIA`` 通知とみなす位置の割合。

新しい曲を読み込んだ直後に前の曲の終了通知が届くことがあるため、
「再生位置が判明していて、かつ曲の前半にいる」場合は曲末の通知ではないとみなす。
位置が 0（読み込み直後の未確定）や duration 未確定のときは判定に使わない。
"""


class PlaylistPlaybackController(QObject):
    """プレイリストからの逐次再生を管理する。

    現在 entry は **entry_id** で追跡する。同じパスの行が複数あるため、
    パスから現在 entry を逆引きしてはならない。
    PlaylistModel は現在再生状態を持たず、ここが唯一の所有者。
    """

    # ``str | None`` は PySide6 の Signal で型指定できないため object を使う。
    current_entry_changed = Signal(object)
    navigation_availability_changed = Signal(bool, bool)
    """(前の曲が可能か, 次の曲が可能か)。"""

    message_requested = Signal(str)

    def __init__(
        self,
        playback: PlaybackController,
        playlist: PlaylistModel,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playback = playback
        self._playlist = playlist
        self._current_entry_id: str | None = None
        # play_entry() から load している最中だけ設定する。source_changed を
        # 受けたときに「自分が読み込んだのか、外から開かれたのか」を区別する。
        self._loading_entry_id: str | None = None
        # 自動次曲を 1 回の終了通知につき 1 度だけにするためのフラグ。
        self._auto_advance_scheduled = False
        self._can_play_previous = False
        self._can_play_next = False

        playback.source_changed.connect(self._on_source_changed)
        playback.media_status_changed.connect(self._on_media_status_changed)
        playlist.rowsInserted.connect(self._on_playlist_rows_changed)
        playlist.rowsRemoved.connect(self._on_playlist_rows_changed)
        playlist.rowsMoved.connect(self._on_playlist_rows_moved)
        playlist.modelReset.connect(self._on_playlist_reset)
        playlist.dataChanged.connect(self._on_playlist_data_changed)

        self._update_navigation_availability()

    # -- 状態 ---------------------------------------------------------------

    @property
    def current_entry_id(self) -> str | None:
        """再生中の source がどの entry から読み込まれたか。

        プレイリスト以外から読み込まれた場合や、その entry が消えた場合は ``None``。
        「選択されている行」ではない。
        """
        return self._current_entry_id

    @property
    def can_play_previous(self) -> bool:
        return self._can_play_previous

    @property
    def can_play_next(self) -> bool:
        return self._can_play_next

    # -- 操作 ---------------------------------------------------------------

    def play_entry(self, entry_id: str) -> bool:
        """指定した entry を再生する。再生を開始できたら ``True``。

        ユーザーが明示的に選んだ操作のため、**欠損していても別の曲へは移動しない**。
        デコードできないファイルの扱いは PlaybackController の既存エラー処理へ委ねる。
        """
        row = self._playlist.row_of_entry_id(entry_id)
        if row is None:
            return False

        self._playlist.refresh_entry_status(entry_id)
        entry = self._playlist.entry_at(row)
        if entry.is_missing:
            self.message_requested.emit(MISSING_FILE_MESSAGE)
            self._update_navigation_availability()
            return False

        if not self._load_entry(entry_id, entry.path):
            return False
        self._playback.play()
        return True

    def play_next(self) -> bool:
        """次の再生可能な entry へ進む。末尾で折り返さない（P2-C2 の Repeat ALL の責務）。"""
        return self._play_row(self._find_playable_row(forward=True))

    def play_previous(self) -> bool:
        """前の再生可能な entry へ戻る。先頭で折り返さない。

        「数秒以上再生していたら曲頭へ戻す」という挙動は P2-C1 では入れない。
        """
        return self._play_row(self._find_playable_row(forward=False))

    # -- 内部: 再生 ---------------------------------------------------------

    def _play_row(self, row: int | None) -> bool:
        if row is None:
            return False
        return self.play_entry(self._playlist.entry_at(row).entry_id)

    def _load_entry(self, entry_id: str, path: Path) -> bool:
        """entry を読み込み、現在 entry として関連付けられたかを返す。

        ``load`` は同期的に ``source_changed`` を出す。その通知を
        :meth:`_on_source_changed` が受け取り、``_loading_entry_id`` を見て
        「自分が読み込んだ」と判断する。パスの一致では判断しない。
        """
        self._loading_entry_id = entry_id
        try:
            self._playback.load(path)
        finally:
            self._loading_entry_id = None
        # 一致しない場合は load が成立しなかった（欠損などで Controller がエラーにした）。
        return self._current_entry_id == entry_id

    def _find_playable_row(self, *, forward: bool) -> int | None:
        """再生可能な行を探す。欠損はスキップする。

        探索は最大でも行数で終わる。候補が無ければ ``None``。
        現在 entry が無い場合は、前方探索なら先頭から、後方探索なら末尾から探す。
        """
        total = self._playlist.rowCount()
        if total == 0:
            return None
        current_row = (
            None
            if self._current_entry_id is None
            else self._playlist.row_of_entry_id(self._current_entry_id)
        )
        if forward:
            start = 0 if current_row is None else current_row + 1
            candidates = range(start, total)
        else:
            start = total - 1 if current_row is None else current_row - 1
            candidates = range(start, -1, -1)

        for row in candidates:
            entry = self._playlist.entry_at(row)
            # 探索の途中で見つけた欠損は Model へ反映し、グレー表示を更新する。
            self._playlist.refresh_entry_status(entry.entry_id)
            if not self._playlist.entry_at(row).is_missing:
                return row
        return None

    # -- 内部: 通知の受信 ---------------------------------------------------

    def _on_source_changed(self, source: object) -> None:
        """source の変化から現在 entry を決める。

        自分の ``play_entry`` 由来なら、その entry_id を関連付ける。
        それ以外（「開く...」による直接読み込みなど）は関連付けを解除する。
        重複パスがあるため、パスの一致では判断しない。
        """
        del source
        self._set_current_entry_id(self._loading_entry_id)

    def _on_media_status_changed(self, status: MediaStatus) -> None:
        if status is not MediaStatus.END_OF_MEDIA:
            return
        if self._current_entry_id is None:
            # 「開く...」で直接開いた単曲。プレイリストへ勝手に移らない。
            return
        if self._auto_advance_scheduled:
            # 同じ終了で 2 曲以上進めない。
            return
        if self._is_stale_end_of_media():
            return

        entry_id = self._current_entry_id
        source = self._playback.source
        self._auto_advance_scheduled = True
        # イベントループの次のターンまで遅らせ、その間に current や source が
        # 変わっていないことを再確認してから進める。
        QTimer.singleShot(0, lambda: self._advance_after_end_of_media(entry_id, source))

    def _is_stale_end_of_media(self) -> bool:
        """明らかに曲末ではない終了通知かどうか。

        新しい曲を読み込んだ直後に前の曲の通知が届く場合の防御。
        位置や duration が未確定のときは判定に使わない（正当な通知を捨てないため）。
        """
        duration_ms = self._playback.duration_ms
        position_ms = self._playback.position_ms
        if duration_ms <= 0 or position_ms <= 0:
            return False
        if position_ms >= duration_ms * _STALE_END_OF_MEDIA_RATIO:
            return False
        _logger.debug(
            "曲末ではない END_OF_MEDIA を無視: position=%dms duration=%dms",
            position_ms,
            duration_ms,
        )
        return True

    def _advance_after_end_of_media(self, entry_id: str, source: Path | None) -> None:
        self._auto_advance_scheduled = False
        if self._current_entry_id != entry_id or self._playback.source != source:
            # 遅延している間に手動で切り替わった。古い通知は適用しない。
            return
        row = self._find_playable_row(forward=True)
        if row is None:
            # 末尾。current_entry_id は最後の entry のまま保つ。
            self.message_requested.emit(END_OF_PLAYLIST_MESSAGE)
            self._update_navigation_availability()
            return
        self._play_row(row)

    def _on_playlist_rows_changed(self, parent: object, first: int, last: int) -> None:
        del parent, first, last
        self._drop_current_if_gone()
        self._update_navigation_availability()

    def _on_playlist_rows_moved(
        self,
        parent: object,
        start: int,
        end: int,
        destination: object,
        row: int,
    ) -> None:
        # 行が動いても entry_id は変わらないため現在 entry は維持し、
        # 新しい行順で前後曲の可否だけ計算し直す。
        del parent, start, end, destination, row
        self._update_navigation_availability()

    def _on_playlist_reset(self) -> None:
        self._drop_current_if_gone()
        self._update_navigation_availability()

    def _on_playlist_data_changed(self, top_left: object, bottom_right: object) -> None:
        # 欠損状態の変化で前後曲の可否が変わりうる。
        del top_left, bottom_right
        self._update_navigation_availability()

    # -- 内部: 状態の更新 ---------------------------------------------------

    def _drop_current_if_gone(self) -> None:
        """現在 entry がプレイリストから消えたら関連付けだけ解除する。

        再生中の音声は止めない（`stop()` を呼ばない）。source は残るが、
        もうプレイリストのどの行にも対応しないため、自動次曲も行わない。
        """
        if self._current_entry_id is None:
            return
        if self._playlist.row_of_entry_id(self._current_entry_id) is None:
            self._set_current_entry_id(None)

    def _set_current_entry_id(self, entry_id: str | None) -> None:
        if entry_id == self._current_entry_id:
            return
        self._current_entry_id = entry_id
        self.current_entry_changed.emit(entry_id)
        self._update_navigation_availability()

    def _update_navigation_availability(self) -> None:
        """前後曲の可否を計算し、変化したときだけ通知する。

        ここでは保持済みの欠損状態だけを見て、ファイルシステムへは触らない
        （モデル変更のたびに全行を stat しないため）。実際の可否は
        :meth:`play_next` / :meth:`play_previous` の探索時に確定する。
        """
        can_previous = self._has_available_row(forward=False)
        can_next = self._has_available_row(forward=True)
        if (can_previous, can_next) == (self._can_play_previous, self._can_play_next):
            return
        self._can_play_previous = can_previous
        self._can_play_next = can_next
        self.navigation_availability_changed.emit(can_previous, can_next)

    def _has_available_row(self, *, forward: bool) -> bool:
        total = self._playlist.rowCount()
        if total == 0:
            return False
        current_row = (
            None
            if self._current_entry_id is None
            else self._playlist.row_of_entry_id(self._current_entry_id)
        )
        if forward:
            start = 0 if current_row is None else current_row + 1
            candidates = range(start, total)
        else:
            start = total - 1 if current_row is None else current_row - 1
            candidates = range(start, -1, -1)
        return any(not self._playlist.entry_at(row).is_missing for row in candidates)
