"""P0-D: PyInstaller onedir パッケージ版の動作検証プローブ（使い捨ての検証コード）。

開発環境と同じことがパッケージ版（独立 exe）でもできるかを確認する。
未検証事項 U7（exe 化後の再生、日本語・空白パス）に対応する。

確認する範囲:

- 基本再生（主要 6 形式 + 日本語・空白パス）
- 速度・ピッチ補正 API がパッケージ版でも使えること
- QAudioBufferOutput による PCM 取得（WAV と Opus）
- 440Hz 入力の FFT ピーク

P0-B で実施済みの長時間の再生時間測定や聴感確認は、exe 上で繰り返さない。
API が利用可能であることだけを確認する。

音源は exe へ埋め込まず、`--audio-dir` で外部ディレクトリを受け取る。
これにより、ファイル関連付け起動に近い外部パス処理も検証する。

音は鳴らさない（音量 0.0 固定）。

使い方:

    uv run python spike/p0d_packaged_probe.py --audio-dir assets/test_audio
    dist\\p0d_probe\\p0d_probe.exe --audio-dir "C:\\...\\assets\\test_audio"
"""

import argparse
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import (
    QCoreApplication,
    QtMsgType,
    QUrl,
    qInstallMessageHandler,
    qVersion,
)
from PySide6.QtMultimedia import (
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioFormat,
    QAudioOutput,
    QMediaDevices,
    QMediaPlayer,
)
from PySide6.QtWidgets import QApplication

SUFFIXES = (".wav", ".mp3", ".ogg", ".opus", ".flac", ".m4a")
JAPANESE_DIR_NAME = "日本語 ディレクトリ"
JAPANESE_FILE_STEM = "テスト 音源 440Hz"

LOAD_TIMEOUT_SEC = 15.0
PLAY_TIMEOUT_SEC = 15.0
END_TIMEOUT_SEC = 20.0

POSITION_ADVANCED_MS = 200
SEEK_MARGIN_MS = 600
SEEK_TOLERANCE_MS = 400

# FFT ピークの許容誤差。48kHz の Opus では 4096 点 FFT の分解能が 11.7Hz になるため、
# ビン量子化を吸収できる幅を取る。P0-C と同じ値。
FFT_SIZE = 4096
FFT_PEAK_TOLERANCE_HZ = 25.0
EXPECTED_PEAK_HZ = 440.0

# QMediaPlayer.playbackRate は float32 精度で保持される（P0-B 実測）。
# 厳密な等値比較は誤検出になるため相対誤差で判定する。
RATE_MATCH_RELATIVE_TOLERANCE = 1e-6

QT_MESSAGES: list[str] = []


def _message_handler(msg_type: QtMsgType, _context: object, message: str) -> None:
    QT_MESSAGES.append(f"[{msg_type.name}] {message}")


@dataclass
class CheckResult:
    """1 検証項目の結果。"""

    name: str
    passed: bool
    detail: str = ""


class UnsupportedSampleFormatError(RuntimeError):
    """P0-C で観測されていない sampleFormat を受け取った場合に送出する。"""


def to_float_frames(data: bytes, sample_format: QAudioFormat.SampleFormat, channels: int):
    """生 PCM を (フレーム数, チャンネル数) の float32 配列へ変換する。

    P0-C の実測で観測されたのは Int16 と Float の 2 種類だけ。
    未観測の形式は暗黙に変換せず、明示的に失敗させる。
    """
    if sample_format == QAudioFormat.SampleFormat.Int16:
        raw = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_format == QAudioFormat.SampleFormat.Float:
        raw = np.frombuffer(data, dtype=np.float32)
    else:
        raise UnsupportedSampleFormatError(f"未対応の sampleFormat です: {sample_format.name}")
    return raw.reshape(-1, channels)


def fft_peak_hz(mono, sample_rate: int) -> float:
    if mono.size < 256:
        return 0.0
    size = min(FFT_SIZE, mono.size)
    segment = mono[:size] * np.hanning(size)
    spectrum = np.abs(np.fft.rfft(segment))
    return float(np.fft.rfftfreq(size, 1.0 / sample_rate)[int(np.argmax(spectrum))])


def rate_matches(actual: float, expected: float) -> bool:
    return abs(actual - expected) <= abs(expected) * RATE_MATCH_RELATIVE_TOLERANCE


def wait_until(app: QCoreApplication, predicate, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def pump(app: QCoreApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)


class BufferCollector:
    """audioBufferReceived を受け取り、最小限の情報だけを保持する。

    P0-C の実測で constData() が None を返すことがあると分かっている。
    None は想定内の空バッファとして安全にスキップし、件数を記録する。
    それ以外の予期しない例外は握り潰さず記録して FAIL に反映する。
    """

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.sample_format: QAudioFormat.SampleFormat | None = None
        self.sample_rate = 0
        self.channel_count = 0
        self.frame_counts: list[int] = []
        self.null_data_count = 0
        self.unexpected_errors: list[str] = []

    def on_buffer(self, buffer: QAudioBuffer) -> None:
        try:
            audio_format = buffer.format()
            if self.sample_format is None:
                self.sample_format = audio_format.sampleFormat()
                self.sample_rate = audio_format.sampleRate()
                self.channel_count = audio_format.channelCount()
            self.frame_counts.append(buffer.frameCount())

            raw = buffer.constData()
            if raw is None:
                # 想定内。空バッファとして安全にスキップする。
                self.null_data_count += 1
                return
            self.frames.append(bytes(raw))
        except Exception:  # noqa: BLE001 - スロット内で例外を外へ出さないため
            # Qt スロット内で未処理例外を発生させないが、握り潰しもしない。
            self.unexpected_errors.append(traceback.format_exc())


# --- 検証本体 ------------------------------------------------------------------


def report_environment(app: QCoreApplication) -> None:
    print("=== 実行環境 ===")
    print(f"Python                : {sys.version.split()[0]}")
    print(f"PySide6               : {pyside_version}")
    print(f"Qt                    : {qVersion()}")
    print(f"実行ファイル (sys.executable) : {sys.executable}")
    print(f"実行ディレクトリ (cwd)         : {Path.cwd()}")
    print(f"frozen (PyInstaller)  : {getattr(sys, 'frozen', False)}")
    meipass = getattr(sys, "_MEIPASS", None)
    print(f"_MEIPASS              : {meipass if meipass else '(なし)'}")
    print("Qt library paths:")
    for path in QCoreApplication.libraryPaths():
        print(f"  - {path}")
    device = QMediaDevices.defaultAudioOutput()
    print(f"既定の音声出力        : {device.description() or '(なし)'}")
    print(f"音声出力デバイス数    : {len(QMediaDevices.audioOutputs())}")
    print()
    _ = app


def collect_targets(audio_dir: Path) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = [
        (f"ASCII {suffix}", audio_dir / f"sine440{suffix}") for suffix in SUFFIXES
    ]
    japanese_dir = audio_dir / JAPANESE_DIR_NAME
    targets.extend(
        (f"日本語 {suffix}", japanese_dir / f"{JAPANESE_FILE_STEM}{suffix}")
        for suffix in SUFFIXES
    )
    return targets


def check_playback(app: QCoreApplication, targets: list[tuple[str, Path]]) -> list[CheckResult]:
    """基本再生: 読み込み・duration・位置前進・シーク・EndOfMedia・エラーなし。"""
    print("=== 基本再生 ===")
    header = (
        f"{'対象':<14} {'読込':<5} {'duration':>9} {'位置前進':<9} "
        f"{'シーク':<20} {'EndOfMedia':<11} 判定"
    )
    print(header)
    print("-" * len(header))

    results: list[CheckResult] = []
    for label, path in targets:
        errors: list[str] = []
        audio_output = QAudioOutput()
        audio_output.setVolume(0.0)
        player = QMediaPlayer()
        player.setAudioOutput(audio_output)
        statuses: list[QMediaPlayer.MediaStatus] = []
        player.mediaStatusChanged.connect(statuses.append)
        player.errorOccurred.connect(
            lambda error, message: errors.append(f"{error.name}: {message}")
        )

        player.setSource(QUrl.fromLocalFile(str(path)))
        loaded = wait_until(
            app,
            lambda p=player: p.mediaStatus()
            in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia),
            LOAD_TIMEOUT_SEC,
        )
        duration = player.duration()

        advanced = False
        seek_ok = False
        seek_text = "-"
        end_of_media = False
        if loaded:
            player.play()
            advanced = wait_until(
                app, lambda p=player: p.position() >= POSITION_ADVANCED_MS, PLAY_TIMEOUT_SEC
            )
            target = max(0, duration - SEEK_MARGIN_MS)
            player.setPosition(target)
            wait_until(
                app, lambda p=player, t=target: abs(p.position() - t) <= SEEK_TOLERANCE_MS, 3.0
            )
            observed = player.position()
            seek_ok = abs(observed - target) <= SEEK_TOLERANCE_MS
            seek_text = f"{target}→{observed}ms"
            end_of_media = wait_until(
                app,
                lambda: QMediaPlayer.MediaStatus.EndOfMedia in statuses,
                END_TIMEOUT_SEC,
            )
        else:
            errors.append(f"読み込み失敗 (mediaStatus={player.mediaStatus().name})")

        passed = loaded and duration > 0 and advanced and seek_ok and end_of_media and not errors
        print(
            f"{label:<14} {'OK' if loaded else 'NG':<5} {duration:>7}ms "
            f"{'OK' if advanced else 'NG':<9} {seek_text:<20} "
            f"{'OK' if end_of_media else 'NG':<11} {'PASS' if passed else 'FAIL'}"
        )
        for message in errors:
            print(f"    errorOccurred: {message}")
        results.append(
            CheckResult(
                name=f"基本再生 {label}",
                passed=passed,
                detail="; ".join(errors) if errors else "",
            )
        )

        player.stop()
        player.setSource(QUrl())
        pump(app, 0.05)

    print()
    return results


def check_speed_pitch_api(app: QCoreApplication, sample: Path) -> list[CheckResult]:
    """速度・ピッチ補正 API がパッケージ版でも利用できることを確認する。"""
    print("=== 速度・ピッチ補正 API ===")
    results: list[CheckResult] = []
    errors: list[str] = []

    audio_output = QAudioOutput()
    audio_output.setVolume(0.0)
    player = QMediaPlayer()
    player.setAudioOutput(audio_output)
    player.errorOccurred.connect(lambda error, message: errors.append(f"{error.name}: {message}"))

    player.setSource(QUrl.fromLocalFile(str(sample)))
    wait_until(
        app,
        lambda: player.mediaStatus()
        in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia),
        LOAD_TIMEOUT_SEC,
    )

    availability = player.pitchCompensationAvailability()
    available = availability == QMediaPlayer.PitchCompensationAvailability.Available
    print(f"  pitchCompensationAvailability: {availability.name}")
    results.append(
        CheckResult("pitchCompensationAvailability == Available", available, availability.name)
    )

    print(f"  pitchCompensation の初期値   : {player.pitchCompensation()}")
    pitch_ok = True
    for value in (False, True, False, True):
        player.setPitchCompensation(value)
        pump(app, 0.1)
        actual = player.pitchCompensation()
        matched = actual == value
        pitch_ok = pitch_ok and matched
        print(f"    設定 {value!s:<5} → 実値 {actual!s:<5} [{'OK' if matched else 'NG'}]")
    results.append(CheckResult("pitchCompensation の ON/OFF 設定", pitch_ok))

    print(f"  playbackRate の初期値        : {player.playbackRate()}")
    rate_ok = True
    for rate in (0.5, 1.0, 2.0):
        player.setPlaybackRate(rate)
        pump(app, 0.1)
        actual = player.playbackRate()
        # float32 精度のため厳密比較はしない。
        matched = rate_matches(actual, rate)
        rate_ok = rate_ok and matched
        print(f"    設定 {rate:<5.2f} → 実値 {actual:<10.6f} [{'OK' if matched else 'NG'}]")
    results.append(CheckResult("playbackRate 0.5 / 1.0 / 2.0 の設定と読み戻し", rate_ok))

    results.append(
        CheckResult("速度・ピッチ API で errorOccurred なし", not errors, "; ".join(errors))
    )
    for message in errors:
        print(f"    errorOccurred: {message}")

    player.stop()
    player.setSource(QUrl())
    pump(app, 0.05)
    print()
    return results


def check_pcm(app: QCoreApplication, audio_dir: Path) -> list[CheckResult]:
    """QAudioBufferOutput から PCM を取得し、形式と FFT ピークを確認する。"""
    print("=== QAudioBufferOutput による PCM 取得 ===")
    header = (
        f"{'対象':<12} {'sampleFormat':<13} {'rate':>6} {'ch':>3} {'件数':>5} "
        f"{'frameCount':<14} {'None':>5} {'FFT peak':>10} 判定"
    )
    print(header)
    print("-" * len(header))

    results: list[CheckResult] = []
    for label, path in (("WAV", audio_dir / "sine440.wav"), ("Opus", audio_dir / "sine440.opus")):
        errors: list[str] = []
        collector = BufferCollector()
        audio_output = QAudioOutput()
        audio_output.setVolume(0.0)
        player = QMediaPlayer()
        player.setAudioOutput(audio_output)
        buffer_output = QAudioBufferOutput()
        player.setAudioBufferOutput(buffer_output)
        buffer_output.audioBufferReceived.connect(collector.on_buffer)
        player.errorOccurred.connect(
            lambda error, message: errors.append(f"{error.name}: {message}")
        )

        player.setSource(QUrl.fromLocalFile(str(path)))
        wait_until(
            app,
            lambda p=player: p.mediaStatus()
            in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia),
            LOAD_TIMEOUT_SEC,
        )
        player.play()
        wait_until(app, lambda c=collector: len(c.frames) >= 10, 10.0)
        player.stop()
        pump(app, 0.05)

        peak = 0.0
        got_pcm = bool(collector.frames)
        if got_pcm and collector.sample_format is not None:
            usable = [
                data
                for data, count in zip(collector.frames, collector.frame_counts, strict=False)
                if count >= 512
            ] or collector.frames
            frames = np.concatenate(
                [
                    to_float_frames(data, collector.sample_format, collector.channel_count)
                    for data in usable
                ]
            )
            mono = frames.mean(axis=1).astype(np.float32)
            peak = fft_peak_hz(mono, collector.sample_rate)

        peak_ok = abs(peak - EXPECTED_PEAK_HZ) <= FFT_PEAK_TOLERANCE_HZ
        passed = (
            got_pcm and peak_ok and not errors and not collector.unexpected_errors
        )
        frame_counts = sorted(set(collector.frame_counts))
        frame_text = ",".join(str(v) for v in frame_counts[:3])
        if len(frame_counts) > 3:
            frame_text += ",..."

        print(
            f"{label:<12} "
            f"{(collector.sample_format.name if collector.sample_format else '-'):<13} "
            f"{collector.sample_rate:>6} {collector.channel_count:>3} "
            f"{len(collector.frames):>5} {frame_text:<14} {collector.null_data_count:>5} "
            f"{peak:>8.1f}Hz {'PASS' if passed else 'FAIL'}"
        )
        for message in errors:
            print(f"    errorOccurred: {message}")
        for trace in collector.unexpected_errors:
            print(f"    予期しない例外:\n{trace}")

        results.append(CheckResult(f"PCM 取得 {label}", got_pcm))
        results.append(CheckResult(f"FFT ピーク {label}", peak_ok, f"{peak:.1f}Hz"))
        results.append(
            CheckResult(
                f"スロット内で予期しない例外なし {label}",
                not collector.unexpected_errors,
            )
        )

        player.setSource(QUrl())
        pump(app, 0.05)

    print()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-D: パッケージ版の動作検証プローブ")
    parser.add_argument(
        "--audio-dir",
        type=Path,
        required=True,
        help="テスト音源のディレクトリ（exe へ埋め込まず外部から渡す）",
    )
    args = parser.parse_args(argv)
    audio_dir: Path = args.audio_dir.resolve()

    qInstallMessageHandler(_message_handler)
    app = QApplication(sys.argv[:1])

    report_environment(app)
    print(f"音源ディレクトリ: {audio_dir}")
    print()

    targets = collect_targets(audio_dir)
    missing = [path for _label, path in targets if not path.exists()]
    if missing:
        print("音源が見つかりません:", file=sys.stderr)
        for path in missing:
            print(f"  - {path}", file=sys.stderr)
        print("最終終了コード: 1")
        return 1

    results: list[CheckResult] = []
    results.extend(check_playback(app, targets))
    results.extend(check_speed_pitch_api(app, audio_dir / "sine440.wav"))
    results.extend(check_pcm(app, audio_dir))

    print("=== Qt のログ（qInstallMessageHandler 経由） ===")
    if QT_MESSAGES:
        for message in QT_MESSAGES:
            print(f"  {message}")
    else:
        print("  なし")
    print()

    failed = [result for result in results if not result.passed]
    print("=== 検証項目の結果 ===")
    for result in results:
        detail = f"  ({result.detail})" if result.detail else ""
        print(f"  [{'PASS' if result.passed else 'FAIL'}] {result.name}{detail}")
    print()
    print(f"合計 {len(results)} 項目中 {len(results) - len(failed)} 項目が PASS")

    # Python 側のメッセージハンドラーを外してから終了する。
    # 付けたままだと、Python の終了処理が進んだ後に Qt がログを出した際に
    # 死んだインタプリタを呼び出してアクセス違反（0xC0000005）で落ちる。
    # QT_DEBUG_PLUGINS=1 のように終了時のログが多い状況で再現する。
    qInstallMessageHandler(None)

    exit_code = 0 if not failed else 1
    print(f"最終終了コード: {exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
