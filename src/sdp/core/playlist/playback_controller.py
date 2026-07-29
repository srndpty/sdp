"""プレイリストと単曲再生の調停。

PlaybackController は「1 つの source の再生」だけを担当し、曲順は知らない。
このクラスが「今どの entry を再生しているか」と曲送りを担当する。

依存の向き: PlaylistPlaybackController → PlaybackController → PlaybackBackend
             PlaylistPlaybackController → PlaylistModel

リピート・シャッフル・再生履歴もここが持つ。プレイリストの表示順（Model の行順）は
シャッフルで変更せず、再生順だけを別に管理する。
"""

import random
from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.types import MediaStatus
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.types import RepeatMode, next_repeat_mode

MISSING_FILE_MESSAGE = "ファイルが見つからないため再生できません。"
END_OF_PLAYLIST_MESSAGE = "プレイリストの最後まで再生しました。"


class _PlayAttempt(Enum):
    """曲送り候補への再生要求結果。"""

    STARTED = auto()
    MISSING = auto()
    NOT_FOUND = auto()
    REJECTED = auto()


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

    repeat_mode_changed = Signal(RepeatMode)
    shuffle_enabled_changed = Signal(bool)

    def __init__(
        self,
        playback: PlaybackController,
        playlist: PlaylistModel,
        *,
        rng: random.Random | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._playback = playback
        self._playlist = playlist
        # モジュールのグローバル random 状態へ依存しない。テストだけ seed を注入する。
        self._rng = random.Random() if rng is None else rng
        self._repeat_mode = RepeatMode.OFF
        self._shuffle_enabled = False
        # シャッフル中に実際に通った順序。cursor が現在 entry の位置を指す。
        # 「前の曲」は再抽選ではなく、この履歴を戻る操作にする。
        self._history: list[str] = []
        self._history_cursor = -1
        # 現在のシャッフルサイクルで再生済みの entry_id。
        self._visited: set[str] = set()
        self._current_entry_id: str | None = None
        # play_entry() から load している最中だけ設定する。source_changed を
        # 受けたときに「自分が読み込んだのか、外から開かれたのか」を区別する。
        self._loading_entry_id: str | None = None
        # END_OF_MEDIA は source 世代ごとに 1 回だけ消費する。タイマー処理後も
        # source が変わるまでは解除せず、遅れて届く重複通知を抑止する。
        self._source_generation = 0
        self._end_consumed_generation: int | None = None
        self._current_generation_started = False
        self._can_play_previous = False
        self._can_play_next = False

        playback.source_changed.connect(self._on_source_changed)
        playback.position_changed.connect(self._on_position_changed)
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

    @property
    def repeat_mode(self) -> RepeatMode:
        return self._repeat_mode

    @property
    def shuffle_enabled(self) -> bool:
        return self._shuffle_enabled

    # -- 設定 ---------------------------------------------------------------

    def set_repeat_mode(self, mode: RepeatMode) -> None:
        """繰り返しの種類を設定する。不正な値は silent fallback せず ``TypeError``。"""
        # 型注釈があるため静的には不要だが、UI やシグナル経由の呼び出しは実行時に
        # 型検査されない。既定値へ丸めず、その場で誤りを検出できるようにする。
        if not isinstance(mode, RepeatMode):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"RepeatMode を指定してください: {mode!r}")
        if mode is self._repeat_mode:
            return
        self._repeat_mode = mode
        self.repeat_mode_changed.emit(mode)
        self._update_navigation_availability()

    def cycle_repeat_mode(self) -> None:
        """OFF → ALL → ONE → OFF の順で切り替える。"""
        self.set_repeat_mode(next_repeat_mode(self._repeat_mode))

    def set_shuffle_enabled(self, enabled: bool) -> None:
        """シャッフルを切り替える。再生中の音声は止めず、読み込み直しもしない。

        ON にした時点の現在 entry は、そのシャッフルセッションの最初の訪問済みとして扱う。
        OFF にすると履歴とサイクルを破棄し、次の曲送りから Model の行順を使う。
        再度 ON にしても古い履歴は復元せず、新しいセッションとして始める。
        """
        if type(enabled) is not bool:
            raise TypeError(f"bool を指定してください: {enabled!r}")
        if enabled == self._shuffle_enabled:
            return
        self._shuffle_enabled = enabled
        self._reset_shuffle_session()
        self.shuffle_enabled_changed.emit(enabled)
        self._update_navigation_availability()

    # -- 操作 ---------------------------------------------------------------

    def play_entry(self, entry_id: str) -> bool:
        """指定entryの読み込み・再生要求を発行できたら ``True``。

        ユーザーが明示的に選んだ操作のため、**欠損していても別の曲へは移動しない**。
        実際にデコード・再生開始できたかは、PlaybackController の状態・エラーシグナルで
        後から通知される。再生エラーでも現在entryは維持し、自動スキップしない。

        シャッフル中は、要求が成立してから履歴へ反映する（未来履歴は切り捨てる）。
        """
        started = self._attempt_play_entry(entry_id, report_missing=True) is _PlayAttempt.STARTED
        if started and self._shuffle_enabled:
            self._append_history(entry_id)
        return started

    def play_next(self) -> bool:
        """次の再生可能な entry へ進む。

        シャッフル中は「未来履歴 → 未訪問候補 → （Repeat ALL なら）新サイクル」の順。
        Repeat ONE は自動の曲終わりだけへ効き、手動の「次の曲」は妨げない。
        """
        if self._shuffle_enabled:
            return self._play_shuffled_next() is _PlayAttempt.STARTED
        return self._play_candidates(forward=True) is _PlayAttempt.STARTED

    def play_previous(self) -> bool:
        """前の再生可能な entry へ戻る。

        シャッフル中は再抽選せず、実際に通った履歴を戻る。履歴の先頭より前へは進まない
        （Repeat ALL でも折り返さない。折り返すのはシャッフル OFF の行順のときだけ）。

        「数秒以上再生していたら曲頭へ戻す」という挙動は入れない。
        """
        if self._shuffle_enabled:
            return self._play_shuffled_previous()
        return self._play_candidates(forward=False) is _PlayAttempt.STARTED

    # -- 内部: 再生 ---------------------------------------------------------

    def _attempt_play_entry(self, entry_id: str, *, report_missing: bool) -> _PlayAttempt:
        """1件へ再生要求し、同期的に判定できる失敗理由を返す。"""
        row = self._playlist.row_of_entry_id(entry_id)
        if row is None:
            return _PlayAttempt.NOT_FOUND

        self._playlist.refresh_entry_status(entry_id)
        row = self._playlist.row_of_entry_id(entry_id)
        if row is None:
            return _PlayAttempt.NOT_FOUND
        entry = self._playlist.entry_at(row)
        if entry.is_missing:
            if report_missing:
                self.message_requested.emit(MISSING_FILE_MESSAGE)
                self._update_navigation_availability()
            return _PlayAttempt.MISSING

        if self._load_entry(entry_id, entry.path):
            self._playback.play()
            return _PlayAttempt.STARTED

        # 存在確認と load の間に消えた場合は欠損として Model へ反映する。
        self._playlist.refresh_entry_status(entry_id)
        row = self._playlist.row_of_entry_id(entry_id)
        if row is None:
            return _PlayAttempt.NOT_FOUND
        if self._playlist.entry_at(row).is_missing:
            return _PlayAttempt.MISSING
        return _PlayAttempt.REJECTED

    def _play_candidates(self, *, forward: bool) -> _PlayAttempt:
        """曲順に候補を試し、欠損・削除済みのentryだけをスキップする。"""
        for entry_id in self._candidate_entry_ids(forward=forward):
            attempt = self._attempt_play_entry(entry_id, report_missing=False)
            if attempt is _PlayAttempt.STARTED:
                return attempt
            if attempt is _PlayAttempt.REJECTED:
                # デコード失敗など同期的に欠損と断定できない失敗は隠さない。
                return attempt
        return _PlayAttempt.NOT_FOUND

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

    def _candidate_entry_ids(self, *, forward: bool) -> tuple[str, ...]:
        """現在位置から指定方向にあるentry_idを曲順で返す。

        現在 entry が無い場合は、前方探索なら先頭から、後方探索なら末尾から探す。
        Repeat ALL では端で折り返し、最後に現在 entry 自身も候補へ含める
        （利用可能な entry が 1 件だけのときに繰り返せるようにするため）。
        entry_idのスナップショットにすることで、状態更新中の行変化に影響されない。
        """
        total = self._playlist.rowCount()
        if total == 0:
            return ()
        current_row = (
            None
            if self._current_entry_id is None
            else self._playlist.row_of_entry_id(self._current_entry_id)
        )
        if forward:
            start = 0 if current_row is None else current_row + 1
            rows = list(range(start, total))
            if self._repeat_mode is RepeatMode.ALL and current_row is not None:
                rows.extend(range(current_row + 1))
        else:
            start = total - 1 if current_row is None else current_row - 1
            rows = list(range(start, -1, -1))
            if self._repeat_mode is RepeatMode.ALL and current_row is not None:
                rows.extend(range(total - 1, current_row - 1, -1))

        return tuple(self._playlist.entry_at(row).entry_id for row in rows)

    # -- 内部: シャッフル ---------------------------------------------------

    def _play_shuffled_next(self) -> _PlayAttempt:
        """未来履歴 → 未訪問候補 → 新サイクルの順に試す。"""
        history_attempt = self._play_history_forward()
        if history_attempt is not _PlayAttempt.NOT_FOUND:
            return history_attempt
        candidate_attempt = self._play_random_candidates(self._unvisited_entry_ids())
        if candidate_attempt is not _PlayAttempt.NOT_FOUND:
            return candidate_attempt
        if self._repeat_mode is not RepeatMode.ALL:
            # Repeat ONE でも手動の「次の曲」は OFF と同じ扱い（現在曲を繰り返さない）。
            return _PlayAttempt.NOT_FOUND
        return self._start_new_shuffle_cycle()

    def _play_shuffled_previous(self) -> bool:
        """履歴を戻る。新しい候補は選ばない。"""
        if self._current_entry_id is None:
            return False
        index = self._history_cursor - 1
        while index >= 0:
            entry_id = self._history[index]
            attempt = self._attempt_play_entry(entry_id, report_missing=False)
            if attempt is _PlayAttempt.STARTED:
                self._history_cursor = index
                self._update_navigation_availability()
                return True
            if attempt is _PlayAttempt.REJECTED:
                return False
            # 削除済み・欠損の履歴要素は取り除いて安全に飛ばす。
            del self._history[index]
            self._history_cursor -= 1
            index -= 1
        return False

    def _play_history_forward(self) -> _PlayAttempt:
        """previous で戻ったあとの「次の曲」で、まず履歴の未来側へ進む。"""
        index = self._history_cursor + 1
        while index < len(self._history):
            entry_id = self._history[index]
            attempt = self._attempt_play_entry(entry_id, report_missing=False)
            if attempt is _PlayAttempt.STARTED:
                self._history_cursor = index
                self._visited.add(entry_id)
                self._update_navigation_availability()
                return attempt
            if attempt is _PlayAttempt.REJECTED:
                return attempt
            del self._history[index]
        return _PlayAttempt.NOT_FOUND

    def _play_random_candidates(self, entry_ids: list[str]) -> _PlayAttempt:
        """候補をランダム順に試し、最初に再生できたものを履歴へ積む。

        欠損や削除済みは飛ばす（候補は有限なので探索も有限で終わる）。
        デコード失敗のような同期的に判定できない失敗では次候補へ移らない。
        """
        self._rng.shuffle(entry_ids)
        for entry_id in entry_ids:
            attempt = self._attempt_play_entry(entry_id, report_missing=False)
            if attempt is _PlayAttempt.STARTED:
                self._append_history(entry_id)
                return attempt
            if attempt is _PlayAttempt.REJECTED:
                return attempt
        return _PlayAttempt.NOT_FOUND

    def _start_new_shuffle_cycle(self) -> _PlayAttempt:
        """Repeat ALL で全候補を消化したあと、新しいサイクルを始める。"""
        candidates = self._available_entry_ids()
        if not candidates:
            return _PlayAttempt.NOT_FOUND
        self._visited.clear()
        if len(candidates) > 1 and self._current_entry_id in candidates:
            # 候補が 2 件以上あるなら、サイクル境界で直前の曲を即座に選び直さない。
            candidates.remove(self._current_entry_id)
        return self._play_random_candidates(candidates)

    def _available_entry_ids(self) -> list[str]:
        """保持済みの状態で再生できそうな entry_id（ファイルへは触らない）。"""
        return [entry.entry_id for entry in self._playlist.entries() if not entry.is_missing]

    def _unvisited_entry_ids(self) -> list[str]:
        return [
            entry_id for entry_id in self._available_entry_ids() if entry_id not in self._visited
        ]

    def _append_history(self, entry_id: str) -> None:
        """履歴の現在位置より後ろを捨て、末尾へ追加する。

        現在位置と同じ entry_id なら重複追加しない。
        """
        self._visited.add(entry_id)
        if 0 <= self._history_cursor < len(self._history):
            if self._history[self._history_cursor] == entry_id:
                self._update_navigation_availability()
                return
            del self._history[self._history_cursor + 1 :]
        self._history.append(entry_id)
        self._history_cursor = len(self._history) - 1
        self._update_navigation_availability()

    def _reset_shuffle_session(self) -> None:
        """履歴とサイクルを捨てる。現在 entry と再生中の音声はそのまま。"""
        self._history.clear()
        self._visited.clear()
        self._history_cursor = -1
        if self._shuffle_enabled and self._current_entry_id is not None:
            # ON にした時点の現在 entry を、このセッションの最初の訪問済みとして扱う。
            self._history.append(self._current_entry_id)
            self._history_cursor = 0
            self._visited.add(self._current_entry_id)

    def _prune_history(self) -> None:
        """プレイリストから消えた entry を履歴から取り除き、cursor を保つ。"""
        existing_ids = {entry.entry_id for entry in self._playlist.entries()}
        old_history = self._history
        old_cursor = self._history_cursor
        new_history: list[str] = []
        new_cursor = -1

        for old_index, entry_id in enumerate(old_history):
            if entry_id not in existing_ids:
                continue
            new_history.append(entry_id)
            if old_index <= old_cursor:
                new_cursor = len(new_history) - 1

        self._history = new_history
        self._history_cursor = new_cursor
        # 未来履歴から外れていても、Modelに残る訪問済みentryは維持する。
        self._visited.intersection_update(existing_ids)

    # -- 内部: 通知の受信 ---------------------------------------------------

    def _on_source_changed(self, source: object) -> None:
        """source の変化から現在 entry を決める。

        自分の ``play_entry`` 由来なら、その entry_id を関連付ける。
        それ以外（「開く...」による直接読み込みなど）は関連付けを解除する。
        重複パスがあるため、パスの一致では判断しない。
        """
        del source
        self._source_generation += 1
        self._end_consumed_generation = None
        self._current_generation_started = False
        self._set_current_entry_id(self._loading_entry_id)

    def _on_position_changed(self, position_ms: int) -> None:
        """現在sourceが実際に進み始めたことを記録する。"""
        if position_ms > 0:
            self._current_generation_started = True

    def _on_media_status_changed(self, status: MediaStatus) -> None:
        if status is not MediaStatus.END_OF_MEDIA:
            return
        if self._current_entry_id is None:
            # 「開く...」で直接開いた単曲。プレイリストへ勝手に移らない。
            return
        if not self._current_generation_started:
            # source切替直後の未開始状態へ届いた、前source由来の通知を無視する。
            return
        generation = self._source_generation
        if self._end_consumed_generation == generation:
            return

        entry_id = self._current_entry_id
        source = self._playback.source
        self._end_consumed_generation = generation
        # イベントループの次のターンまで遅らせ、その間に current や source が
        # 変わっていないことを再確認してから進める。
        QTimer.singleShot(
            0,
            lambda: self._advance_after_end_of_media(generation, entry_id, source),
        )

    def _advance_after_end_of_media(
        self, generation: int, entry_id: str, source: Path | None
    ) -> None:
        if generation != self._source_generation:
            return
        if self._current_entry_id != entry_id or self._playback.source != source:
            # 遅延している間に手動で切り替わった。古い通知は適用しない。
            return

        if self._repeat_mode is RepeatMode.ONE:
            # 現在 entry を読み込み直して再生する（seek(0) では Backend の状態差を隠す）。
            # 履歴とサイクルの消化状態は進めない。
            self._attempt_play_entry(entry_id, report_missing=False)
            return

        if self._shuffle_enabled:
            attempt = self._play_shuffled_next()
            if attempt is _PlayAttempt.NOT_FOUND:
                self.message_requested.emit(END_OF_PLAYLIST_MESSAGE)
                self._update_navigation_availability()
            return

        attempt = self._play_candidates(forward=True)
        if attempt is _PlayAttempt.NOT_FOUND:
            # 末尾。current_entry_id は最後の entry のまま保つ。
            self.message_requested.emit(END_OF_PLAYLIST_MESSAGE)
            self._update_navigation_availability()

    def _on_playlist_rows_changed(self, parent: object, first: int, last: int) -> None:
        del parent, first, last
        self._drop_current_if_gone()
        # 現在 entry が残っている場合は、消えた entry だけ履歴から取り除く。
        # 追加された entry は未訪問なので、自然に次のサイクルの候補へ入る。
        self._prune_history()
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
        self._prune_history()
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
        if entry_id is None:
            # 現在 entry の削除、または「開く...」による直接読み込み。
            # シャッフルのセッションは意味を失うため捨てる（設定自体は変えない）。
            self._history.clear()
            self._visited.clear()
            self._history_cursor = -1
        self.current_entry_changed.emit(entry_id)
        self._update_navigation_availability()

    def _update_navigation_availability(self) -> None:
        """前後曲の可否を計算し、変化したときだけ通知する。

        ここでは保持済みの欠損状態だけを見て、ファイルシステムへは触らない
        （モデル変更のたびに全行を stat しないため）。実際の可否は
        :meth:`play_next` / :meth:`play_previous` の探索時に確定する。
        """
        can_previous, can_next = self._compute_navigation_availability()
        if (can_previous, can_next) == (self._can_play_previous, self._can_play_next):
            return
        self._can_play_previous = can_previous
        self._can_play_next = can_next
        self.navigation_availability_changed.emit(can_previous, can_next)

    def _compute_navigation_availability(self) -> tuple[bool, bool]:
        """リピート・シャッフルを踏まえた前後曲の可否。

        Repeat ONE は自動の曲終わりだけへ効くので、手動の可否は OFF と同じ。
        """
        if self._shuffle_enabled:
            return self._compute_shuffle_navigation()
        if self._repeat_mode is RepeatMode.ALL:
            available = any(not entry.is_missing for entry in self._playlist.entries())
            return (available, available)
        return (self._has_available_row(forward=False), self._has_available_row(forward=True))

    def _compute_shuffle_navigation(self) -> tuple[bool, bool]:
        if self._current_entry_id is None:
            return (False, bool(self._available_entry_ids()))
        can_previous = self._has_playable_history(range(self._history_cursor - 1, -1, -1))
        can_next = (
            self._has_playable_history(range(self._history_cursor + 1, len(self._history)))
            or bool(self._unvisited_entry_ids())
            or (self._repeat_mode is RepeatMode.ALL and bool(self._available_entry_ids()))
        )
        return (can_previous, can_next)

    def _has_playable_history(self, indexes: range) -> bool:
        for index in indexes:
            entry_id = self._history[index]
            row = self._playlist.row_of_entry_id(entry_id)
            if row is not None and not self._playlist.entry_at(row).is_missing:
                return True
        return False

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
