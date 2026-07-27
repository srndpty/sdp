"""P0-A: Qt Multimedia の基本検証（使い捨ての検証スクリプト）。

検証項目（開発計画 P0 の 1・4・5・10 に対応）:

1. WAV / MP3 / OGG Vorbis / OGG Opus / FLAC / M4A が Qt Multimedia で再生できるか
2. duration を取得できるか
3. 再生位置が進むか
4. シークが機能するか
5. 再生終了通知（EndOfMedia）が届くか
6. 日本語・空白を含むパスで再生できるか

本スクリプトは spike であり、lint と coverage の対象外。本体からは参照しない。
FFmpeg CLI での音源生成が成功していることは、Qt Multimedia の対応の証拠にはならない。
ここで実際に確認する。

使い方:

    uv run python spike/p0a_basic_playback.py
    uv run python spike/p0a_basic_playback.py --volume 0.2   # 実際に音を出して確認する
"""

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_AUDIO_DIR = REPO_ROOT / "assets" / "test_audio"

SUFFIXES = (".wav", ".mp3", ".ogg", ".opus", ".flac", ".m4a")

LOAD_TIMEOUT_SEC = 10.0
PLAY_TIMEOUT_SEC = 10.0
END_TIMEOUT_SEC = 15.0

# 再生位置が進んだと判断する閾値（ミリ秒）
POSITION_ADVANCED_MS = 300
# シーク先を「終端から何ミリ秒手前」にするか
SEEK_MARGIN_MS = 600
# シーク後の位置の許容誤差（ミリ秒）。可逆・非可逆でフレーム境界の丸めが入る。
SEEK_TOLERANCE_MS = 400


@dataclass
class Result:
    """1 ファイル分の検証結果。"""

    label: str
    path: Path
    loaded: bool = False
    duration_ms: int = 0
    position_advanced: bool = False
    seek_ok: bool = False
    seeked_to_ms: int = 0
    observed_after_seek_ms: int = 0
    end_of_media: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.loaded
            and self.duration_ms > 0
            and self.position_advanced
            and self.seek_ok
            and self.end_of_media
            and not self.errors
        )


def pump(app: QCoreApplication, seconds: float) -> None:
    """指定時間だけイベントループを回す。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


def wait_until(app: QCoreApplication, predicate, timeout_sec: float) -> bool:
    """条件が満たされるまでイベントループを回す。満たされたら True。"""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def check_file(app: QCoreApplication, label: str, path: Path, volume: float) -> Result:
    """1 ファイルについて読み込み・再生・シーク・終了通知を確認する。"""
    result = Result(label=label, path=path)

    audio_output = QAudioOutput()
    audio_output.setVolume(volume)
    player = QMediaPlayer()
    player.setAudioOutput(audio_output)

    statuses: list[QMediaPlayer.MediaStatus] = []
    player.mediaStatusChanged.connect(statuses.append)
    player.errorOccurred.connect(
        lambda error, message: result.errors.append(f"{error.name}: {message}")
    )

    player.setSource(QUrl.fromLocalFile(str(path)))

    # 1. 読み込み
    result.loaded = wait_until(
        app,
        lambda: player.mediaStatus()
        in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ),
        LOAD_TIMEOUT_SEC,
    )
    result.duration_ms = player.duration()
    if not result.loaded:
        result.errors.append(f"読み込みに失敗（mediaStatus={player.mediaStatus().name}）")
        player.setSource(QUrl())
        return result

    # 2. 再生開始と位置の前進
    player.play()
    result.position_advanced = wait_until(
        app, lambda: player.position() >= POSITION_ADVANCED_MS, PLAY_TIMEOUT_SEC
    )
    if not result.position_advanced:
        result.errors.append(
            f"再生位置が進まない（position={player.position()}ms, "
            f"state={player.playbackState().name}）"
        )

    # 3. シーク（終端手前へ跳ばし、そのまま終了通知を待つ）
    target = max(0, result.duration_ms - SEEK_MARGIN_MS)
    result.seeked_to_ms = target
    player.setPosition(target)
    wait_until(app, lambda: abs(player.position() - target) <= SEEK_TOLERANCE_MS, 3.0)
    result.observed_after_seek_ms = player.position()
    result.seek_ok = abs(result.observed_after_seek_ms - target) <= SEEK_TOLERANCE_MS
    if not result.seek_ok:
        result.errors.append(
            f"シーク後の位置がずれている（目標 {target}ms / 実測 {result.observed_after_seek_ms}ms）"
        )

    # 4. 再生終了通知
    result.end_of_media = wait_until(
        app,
        lambda: QMediaPlayer.MediaStatus.EndOfMedia in statuses,
        END_TIMEOUT_SEC,
    )
    if not result.end_of_media:
        observed = ", ".join(status.name for status in statuses)
        result.errors.append(f"EndOfMedia が届かない（観測した mediaStatus: {observed}）")

    player.stop()
    player.setSource(QUrl())
    pump(app, 0.05)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-A: Qt Multimedia 基本検証")
    parser.add_argument(
        "--volume",
        type=float,
        default=0.0,
        help="音量 0.0〜1.0（既定 0.0 = 無音。デコードと再生パイプラインは動作する）",
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1])

    print("=== 実行環境 ===")
    print(f"PySide6 backend (QT_MEDIA_BACKEND): {__import__('os').environ.get('QT_MEDIA_BACKEND', '未設定（既定）')}")
    default_output = QMediaDevices.defaultAudioOutput()
    print(f"既定の音声出力デバイス: {default_output.description() or '(なし)'}")
    print(f"利用可能な音声出力デバイス数: {len(QMediaDevices.audioOutputs())}")
    print(f"音量設定: {args.volume}")
    print()

    targets: list[tuple[str, Path]] = []
    for suffix in SUFFIXES:
        targets.append((f"ASCII{suffix}", TEST_AUDIO_DIR / f"sine440{suffix}"))
    for suffix in SUFFIXES:
        targets.append(
            (f"日本語{suffix}", TEST_AUDIO_DIR / "日本語 ディレクトリ" / f"テスト 音源 440Hz{suffix}")
        )

    missing = [path for _, path in targets if not path.exists()]
    if missing:
        print("テスト音源が見つかりません。先に次を実行してください:", file=sys.stderr)
        print("    uv run python tools/gen_test_audio.py", file=sys.stderr)
        for path in missing:
            print(f"  - {path}", file=sys.stderr)
        return 1

    results = [check_file(app, label, path, args.volume) for label, path in targets]

    print("=== 検証結果 ===")
    header = f"{'対象':<16} {'読込':<5} {'長さ':>8} {'位置前進':<9} {'シーク':<21} {'終了通知':<9} 判定"
    print(header)
    print("-" * len(header))
    for r in results:
        seek_text = f"{r.seeked_to_ms}→{r.observed_after_seek_ms}ms {'OK' if r.seek_ok else 'NG'}"
        print(
            f"{r.label:<16} "
            f"{'OK' if r.loaded else 'NG':<5} "
            f"{r.duration_ms:>6}ms "
            f"{'OK' if r.position_advanced else 'NG':<9} "
            f"{seek_text:<21} "
            f"{'OK' if r.end_of_media else 'NG':<9} "
            f"{'合格' if r.passed else '不合格'}"
        )
    print()

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"不合格 {len(failed)} 件:", file=sys.stderr)
        for r in failed:
            for message in r.errors:
                print(f"  - {r.label}: {message}", file=sys.stderr)
        return 1

    print(f"全 {len(results)} 件が合格しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
