"""アプリケーションの composition root。

具体的な再生実装（QtMultimediaBackend）を知ってよいのはこのモジュールだけで、
UI は PlaybackController しか知らない。

依存方向: MainWindow / PlayerControls → PlaybackController → PlaybackBackend
"""

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtWidgets import QApplication

from sdp.core.metadata.reader import MetadataReader
from sdp.core.playback.controller import PlaybackController
from sdp.core.playback.qt_backend import QtMultimediaBackend
from sdp.core.playlist.model import PlaylistModel
from sdp.core.playlist.playback_controller import PlaylistPlaybackController
from sdp.services import logging_setup
from sdp.services.pcm_tap import PcmTap
from sdp.services.playlist_session import PlaylistSession, default_playlist_path
from sdp.services.save_status import SaveCategory, SaveStatusReporter, restore_failure_message
from sdp.services.settings import AppSettingsController, SettingsSession
from sdp.services.ui_state_session import PlaylistUiStateSource, UiStateSession
from sdp.services.user_paths import (
    default_settings_path,
    default_ui_state_path,
    default_waveform_cache_directory,
)
from sdp.services.waveform_analysis import WaveformAnalysisService
from sdp.ui.main_window import MainWindow

_logger = logging.getLogger(__name__)

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
    pcm_tap: PcmTap
    app_settings: AppSettingsController
    ui_state_session: UiStateSession
    save_status: SaveStatusReporter
    window: MainWindow


def build_player(
    playlist_file: Path | None = None,
    settings_file: Path | None = None,
    waveform_cache_directory: Path | None = None,
    ui_state_file: Path | None = None,
) -> PlayerComposition:
    """Backend → Controller → PlaylistModel → プレイリスト再生 → MainWindow の順に組み立てる。

    保存済み設定をControllerへ適用してから、プレイリストとUIを構築する
    （UI は永続化を知らない）。
    保存対象は ``PlaylistModel.entries()`` だけで、現在 entry や再生位置は保存しない。
    ``playlist_file``、``settings_file``、``waveform_cache_directory``、
    ``ui_state_file``はテストから保存先を差し替えるための入口。

    UI状態（ウィンドウ位置・サイズ・最大化・Splitter比率・前回フォルダー）は
    設定とは別ファイル（``ui-state.json``）とし、可視化の表示設定を適用したあと、
    **Window表示前**に復元する。

    QApplication が既に存在していることが前提（ウィジェットの生成に必要）。
    """
    backend = QtMultimediaBackend()
    controller = PlaybackController(backend)
    playlist_model = PlaylistModel()
    playlist_playback = PlaylistPlaybackController(controller, playlist_model)
    # 設定の適用先は PlaybackController（速度・ピッチ・音量・ミュート）、
    # PlaylistPlaybackController（Repeat・Shuffle）、可視化の3系統。
    # 調停はAppSettingsControllerが持ち、SettingsSessionはファイル復元と
    # デバウンス保存だけを担う。
    app_settings = AppSettingsController(controller, playlist_playback)
    settings_session = SettingsSession(
        default_settings_path() if settings_file is None else settings_file,
        app_settings,
    )
    settings_session.load()
    session = PlaylistSession(default_playlist_path() if playlist_file is None else playlist_file)
    playlist_restore_message = session.load_into(playlist_model)
    # 生成だけで読み取りは始めない（start() は run() が呼ぶ）。
    metadata_reader = MetadataReader(playlist_model)
    waveform_analysis = WaveformAnalysisService(
        controller,
        default_waveform_cache_directory()
        if waveform_cache_directory is None
        else waveform_cache_directory,
    )
    # 再生中PCMの供給は Qt Multimedia 固有の補助ポート。ここだけが具体Backendの
    # QAudioBufferOutput を知る（PlaybackBackend の契約には含めない）。
    pcm_tap = PcmTap(controller)
    pcm_tap.connect_audio_buffer_output(backend.audio_buffer_output)
    # MainWindowは復元済み設定を表示前に反映する（可視化のフリッカーを避ける）。
    window = MainWindow(
        controller,
        playlist_model,
        playlist_playback,
        waveform_analysis,
        pcm_tap,
        app_settings,
    )
    # 可視化の表示設定（AppSettings）を適用済みのWindowへ、UI状態を重ねて復元する。
    # 現在曲の復元はentry_idの照合が要るため、Windowではなくこのアダプターが担う。
    ui_state_source = PlaylistUiStateSource(window, playlist_playback)
    ui_state_session = UiStateSession(
        default_ui_state_path() if ui_state_file is None else ui_state_file,
        ui_state_source,
    )
    ui_state_session.load_into_window()
    # 復元に失敗したカテゴリだけをまとめて1文にする（生の例外もパスも見せない）。
    failed = [
        category
        for category, enabled in (
            (SaveCategory.SETTINGS, settings_session.is_save_enabled),
            (SaveCategory.PLAYLIST, session.is_save_enabled),
            (SaveCategory.UI_STATE, ui_state_session.is_save_enabled),
        )
        if not enabled
    ]
    restore_message = restore_failure_message(failed)
    if restore_message is not None:
        window.show_status_message(restore_message)
    elif playlist_restore_message is not None:
        window.show_status_message(playlist_restore_message)

    # 保存失敗は操作中でも短く伝える（同じ失敗を出し続けない）。
    save_status = SaveStatusReporter()
    save_status.watch(
        SaveCategory.SETTINGS, settings_session.save_failed, settings_session.save_recovered
    )
    save_status.watch(
        SaveCategory.UI_STATE, ui_state_session.save_failed, ui_state_session.save_recovered
    )
    save_status.message_requested.connect(window.show_status_message)
    return PlayerComposition(
        backend=backend,
        controller=controller,
        playlist_model=playlist_model,
        playlist_playback=playlist_playback,
        playlist_session=session,
        settings_session=settings_session,
        metadata_reader=metadata_reader,
        waveform_analysis=waveform_analysis,
        pcm_tap=pcm_tap,
        app_settings=app_settings,
        ui_state_session=ui_state_session,
        save_status=save_status,
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
    # 表示で生じるmove／resizeを「ユーザー変更」として保存しないよう、show後に監視を始める。
    composition.ui_state_session.start()
    exit_code = app.exec()
    shutdown(composition)
    return exit_code


def shutdown(composition: PlayerComposition) -> None:
    """終了処理をカテゴリごとに分離して実行する。

    1カテゴリの例外で後続を飛ばさないよう、各段を独立して実行する
    （大きなライフサイクル基盤は作らず、順序と分離だけをここで守る）。
    保存の失敗はログへ残すだけにする（ウィンドウが閉じた後は提示できないため）。
    """
    steps: list[tuple[str, Callable[[], object]]] = [
        # 可視化を先に止め、破棄済みQObjectへシグナルが飛ばないようにする。
        ("可視化の停止", composition.window.spectrum_panel.shutdown),
        ("PCMタップの停止", composition.pcm_tap.shutdown),
        ("波形解析の停止", composition.waveform_analysis.shutdown),
        ("メタデータ読み取りの停止", composition.metadata_reader.shutdown),
        # Windowが生きているあいだにUI状態を確定させる（破棄後はgeometryを取得できない）。
        ("ウィンドウ状態の保存", composition.ui_state_session.flush),
        ("設定の保存", composition.settings_session.flush),
        (
            "プレイリストの保存",
            lambda: composition.playlist_session.save_from(composition.playlist_model),
        ),
        ("ウィンドウ状態監視の停止", composition.ui_state_session.stop),
        ("設定監視の停止", composition.settings_session.stop),
        ("設定調停の停止", composition.app_settings.shutdown),
    ]
    for name, step in steps:
        try:
            step()
        except Exception:
            _logger.exception("終了処理の%sに失敗しました", name)
