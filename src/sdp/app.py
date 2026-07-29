"""アプリケーションの composition root。

具体的な再生実装（QtMultimediaBackend）を知ってよいのはこのモジュールだけで、
UI は PlaybackController しか知らない。

依存方向: MainWindow / PlayerControls → PlaybackController → PlaybackBackend
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import QApplication

from sdp.core.metadata.reader import MetadataReader
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.services import logging_setup
from sdp.services.playlist_session import PlaylistSession, default_playlist_path
from sdp.services.settings import SettingsSession
from sdp.services.user_paths import default_settings_path, default_waveform_cache_directory
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui.main_window import MainWindow

APPLICATION_NAME = "sdp"
ORGANIZATION_NAME = "sdp"


@dataclass(frozen=True, slots=True)
class PlayerComposition:
    """組み立て済みのアプリ一式。

    Backend・Controller・PlaylistModel・永続化サービス・MainWindow はいずれも
    QObject の親を持たないため、この dataclass への参照が生きているあいだだけ
    寿命が保証される。:func:`run` はイベントループの実行中これを保持し続ける。
    グローバル変数へは置かない。MainWindow に Backend は所有させない。
    """

    backend: QtMultimediaBackend
    controller: PlaybackController
    playlist_model: PlaylistModel
    playlist_playback: PlaylistPlaybackController
    playlist_session: PlaylistSession
    settings_session: SettingsSession
    metadata_reader: MetadataReader
    waveform_analysis: WaveformAnalysisService
    window: MainWindow


def build_player(
    playlist_file: Path | None = None,
    settings_file: Path | None = None,
    waveform_cache_directory: Path | None = None,
) -> PlayerComposition:
    """Backend → Controller → PlaylistModel → プレイリスト再生 → MainWindow の順に組み立てる。

    保存済み設定をControllerへ適用してから、プレイリストとUIを構築する
    （UI は永続化を知らない）。
    保存対象は ``PlaylistModel.entries()`` だけで、現在 entry や再生位置は保存しない。
    ``playlist_file``、``settings_file``、``waveform_cache_directory``は
    テストから保存先を差し替えるための入口。

    QApplication が既に存在していることが前提（ウィジェットの生成に必要）。
    """
    backend = QtMultimediaBackend()
    controller = PlaybackController(backend)
    settings_session = SettingsSession(
        default_settings_path() if settings_file is None else settings_file,
        controller,
    )
    settings_restore_message = settings_session.load()
    playlist_model = PlaylistModel()
    playlist_playback = PlaylistPlaybackController(controller, playlist_model)
    session = PlaylistSession(default_playlist_path() if playlist_file is None else playlist_file)
    restore_message = session.load_into(playlist_model)
    # 生成だけで読み取りは始めない（start() は run() が呼ぶ）。
    metadata_reader = MetadataReader(playlist_model)
    waveform_analysis = WaveformAnalysisService(
        controller,
        default_waveform_cache_directory()
        if waveform_cache_directory is None
        else waveform_cache_directory,
    )
    window = MainWindow(controller, playlist_model, playlist_playback, waveform_analysis)
    restore_messages = [
        message for message in (restore_message, settings_restore_message) if message is not None
    ]
    if restore_messages:
        window.show_status_message(" ".join(restore_messages))
    return PlayerComposition(
        backend=backend,
        controller=controller,
        playlist_model=playlist_model,
        playlist_playback=playlist_playback,
        playlist_session=session,
        settings_session=settings_session,
        metadata_reader=metadata_reader,
        waveform_analysis=waveform_analysis,
        window=window,
    )


def create_application(argv: list[str]) -> QApplication:
    """QApplication を用意し、アプリのメタ情報を設定する。

    QApplication はプロセスに 1 つだけで、二重生成は例外になる。通常起動では
    まだ存在しないが、テスト環境では pytest-qt が先に生成しているため再利用する。
    """
    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication(argv)
    app.setApplicationName(APPLICATION_NAME)
    app.setApplicationDisplayName(APPLICATION_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    return app


def run(argv: list[str] | None = None) -> int:
    """アプリを起動し、終了コードを返す。

    コマンドライン引数による音声ファイルの読み込みは P7 の責務のため扱わない。
    """
    logging_setup.configure_logging()
    logging_setup.install_excepthook()

    app = create_application(list(argv if argv is not None else sys.argv))
    # composition はイベントループ実行中ずっと参照され続ける（寿命の保証）。
    composition = build_player()
    # 復元完了後から変更監視を始める（load中のSignalを自動保存扱いしない）。
    composition.settings_session.start()
    # メタデータの読み取りはここで開始する（GUI スレッドはブロックしない）。
    composition.metadata_reader.start()
    composition.waveform_analysis.start()
    composition.window.show()
    exit_code = app.exec()
    # ワーカーを止めてから保存する。保存の失敗はログへ残すだけにする
    # （ウィンドウが閉じた後でユーザーへ提示できないため）。
    composition.waveform_analysis.shutdown()
    composition.metadata_reader.shutdown()
    composition.settings_session.flush()
    composition.playlist_session.save_from(composition.playlist_model)
    composition.settings_session.stop()
    return exit_code
