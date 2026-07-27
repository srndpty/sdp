"""P0-C: QAudioBufferOutput による PCM 取得と可視化適合性の検証（使い捨ての検証スクリプト）。

未検証事項 U3（QAudioBufferOutput の安定性・オーバーヘッド・接続スレッド・速度変更時の挙動）と、
P0-A で保留した「シーク精度の PCM 照合」を扱う。

重要な前提（混同してはならないこと）:

- QAudioBufferOutput から取得できる PCM は、playbackRate や pitchCompensation を
  適用した後の可聴出力音声とは限らない。
  したがってこの PCM で P0-B の varispeed / time-stretch の実出力ピッチを再判定しない。
  本スクリプトの FFT ピークは、あくまで QAudioBufferOutput 側 PCM の性質を見る参考値である。
- 本スクリプトが確認するのは次の 5 点だけである。
    1. デコード済み PCM を可視化へ利用できるか
    2. PCM 通知と再生位置の同期特性
    3. シーク後に実際の PCM 内容が対象区間へ移るか
    4. 速度変更時の PCM 通知挙動
    5. 可視化用の軽量処理を追加しても再生が安定するか

本体の PcmTap / RingBuffer / SpectrumWidget はまだ作らない。
P0 の結果が確定する前に本番アーキテクチャへ組み込まない。

使い方:

    uv run python spike/p0c_pcm_output.py
    uv run python spike/p0c_pcm_output.py --only seek,speed

生成物は .sdp-local/p0c/ へ置く（.gitignore 済み。リポジトリへはコミットしない）。
"""

import argparse
import statistics
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    Qt,
    QtMsgType,
    QThread,
    QUrl,
    Slot,
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

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_AUDIO_DIR = REPO_ROOT / "assets" / "test_audio"
WORK_DIR = REPO_ROOT / ".sdp-local" / "p0c"

SAMPLE_RATE = 44100

# シーク検証用の診断 WAV。2 秒ごとに周波数が変わるので、
# 取得した PCM の FFT ピークからどの区間を再生しているかが判別できる。
SEGMENTS: tuple[tuple[float, float, float], ...] = (
    (0.0, 2.0, 220.0),
    (2.0, 4.0, 330.0),
    (4.0, 6.0, 440.0),
    (6.0, 8.0, 550.0),
    (8.0, 10.0, 660.0),
)
SEEK_TARGETS_SEC: tuple[float, ...] = (1.0, 3.0, 5.0, 7.0, 9.0)

TONE_SEC = 10.0
TONE_FREQ_HZ = 440.0

# FFT ピークの許容誤差。1 バッファ 1024 フレーム程度でも判別できるよう広めに取るが、
# 診断音源の隣接区間（220/330/440/550/660Hz）は 110Hz 以上離れているため取り違えない。
FFT_PEAK_TOLERANCE_HZ = 25.0

FFT_SIZE = 4096

LOAD_TIMEOUT_SEC = 10.0

QT_MESSAGES: list[str] = []


def _message_handler(msg_type: QtMsgType, _context: object, message: str) -> None:
    QT_MESSAGES.append(f"[{msg_type.name}] {message}")


# --- PCM 変換 -----------------------------------------------------------------
# 実環境で観測された sampleFormat は Int16（WAV / FLAC）と Float（MP3 / Vorbis / Opus / AAC）
# の 2 種類だけだった。UInt8 と Int32 は発生しなかったため、変換を実装しない。
# 未対応の形式が来た場合は明示的に失敗させる（silent fallback を作らない）。
_OBSERVED_FORMATS = {
    QAudioFormat.SampleFormat.Int16,
    QAudioFormat.SampleFormat.Float,
}


class UnsupportedSampleFormatError(RuntimeError):
    """実環境で観測されていない sampleFormat を受け取った場合に送出する。"""


def to_float_frames(data: bytes, sample_format: QAudioFormat.SampleFormat, channels: int):
    """生 PCM を (フレーム数, チャンネル数) の float32 配列 [-1.0, 1.0] へ変換する。"""
    if sample_format == QAudioFormat.SampleFormat.Int16:
        raw = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_format == QAudioFormat.SampleFormat.Float:
        raw = np.frombuffer(data, dtype=np.float32)
    else:
        raise UnsupportedSampleFormatError(
            f"未対応の sampleFormat です: {sample_format.name}\n"
            "P0-C の実測では Int16 と Float のみが観測されたため、他形式の変換は実装していません。\n"
            "この形式が実際に発生したのであれば、根拠を docs/p0-report.md へ記録したうえで"
            "変換を追加してください。"
        )
    return raw.reshape(-1, channels)


def to_mono(frames) -> NDArray[np.float32]:
    """チャンネル平均で mono 化する。"""
    return frames.mean(axis=1).astype(np.float32)


def fft_peak_hz(mono: NDArray[np.float32], sample_rate: int) -> float:
    """mono 信号の FFT ピーク周波数を返す。"""
    if mono.size < 256:
        return 0.0
    size = min(FFT_SIZE, mono.size)
    segment = mono[:size] * np.hanning(size)
    spectrum = np.abs(np.fft.rfft(segment))
    return float(np.fft.rfftfreq(size, 1.0 / sample_rate)[int(np.argmax(spectrum))])


def segment_frequency(seconds: float) -> float:
    """診断音源の指定時刻における周波数。"""
    for start, end, frequency in SEGMENTS:
        if start <= seconds < end:
            return frequency
    return 0.0


# --- 診断音源の生成（NumPy のみ。Qt の出力ではない） --------------------------


def write_wav(path: Path, samples: NDArray[np.float32], sample_rate: int = SAMPLE_RATE) -> None:
    as_int16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(as_int16.tobytes())


def segmented_wav_path() -> Path:
    return WORK_DIR / "diagnostic_segments_10s.wav"


def tone_wav_path() -> Path:
    return WORK_DIR / "tone_440Hz_10s.wav"


def ensure_generated_audio() -> None:
    """診断音源を生成する（既存なら再生成しない）。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    if not segmented_wav_path().exists():
        pieces: list[NDArray[np.float64]] = []
        for start, end, frequency in SEGMENTS:
            count = int(SAMPLE_RATE * (end - start))
            t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
            pieces.append(np.sin(2.0 * np.pi * frequency * t) * 0.5)
        signal = np.concatenate(pieces)
        write_wav(segmented_wav_path(), np.stack((signal, signal), axis=1).astype(np.float32))

    if not tone_wav_path().exists():
        count = int(SAMPLE_RATE * TONE_SEC)
        t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
        tone = np.sin(2.0 * np.pi * TONE_FREQ_HZ * t)
        # 左右で振幅を変え、チャンネル別の確認にも使えるようにする。
        write_wav(tone_wav_path(), np.stack((tone * 0.5, tone * 0.25), axis=1).astype(np.float32))


# --- バッファ受信 --------------------------------------------------------------


@dataclass
class BufferRecord:
    """1 バッファ分の記録。コールバック内では最小限の処理しか行わない。"""

    received_monotonic: float
    frame_count: int
    byte_count: int
    duration_us: int
    start_time_us: int
    position_ms: int
    sample_format: QAudioFormat.SampleFormat
    sample_rate: int
    channel_count: int
    bytes_per_frame: int
    data: bytes
    copy_ms: float
    callback_ms: float


@dataclass
class ThreadInfo:
    """audioBufferReceived を受信したスロットのスレッド情報。"""

    python_thread_ident: int
    python_thread_name: str
    qt_current_thread: str
    receiver_thread: str
    application_thread: str
    same_as_gui: bool


class BufferReceiver(QObject):
    """audioBufferReceived を受け取り、最小限の記録だけを行う。"""

    def __init__(self, player: QMediaPlayer, keep_data: bool = True) -> None:
        super().__init__()
        self._player = player
        self._keep_data = keep_data
        self.records: list[BufferRecord] = []
        self.thread_info: ThreadInfo | None = None
        # constData() が None を返した回数（無効バッファ）。
        self.null_data_count = 0

    @Slot(QAudioBuffer)
    def on_buffer(self, buffer: QAudioBuffer) -> None:
        callback_start = time.perf_counter()

        if self.thread_info is None:
            application = QCoreApplication.instance()
            current = QThread.currentThread()
            self.thread_info = ThreadInfo(
                python_thread_ident=threading.get_ident(),
                python_thread_name=threading.current_thread().name,
                qt_current_thread=repr(current),
                receiver_thread=repr(self.thread()),
                application_thread=repr(application.thread()) if application else "(なし)",
                same_as_gui=bool(application is not None and current == application.thread()),
            )

        audio_format = buffer.format()
        copy_start = time.perf_counter()
        data = b""
        if self._keep_data:
            # 実測で constData() が None を返すことがある（再生停止直後などに観測）。
            # 握り潰さず件数を記録する。
            raw = buffer.constData()
            if raw is None:
                self.null_data_count += 1
            else:
                data = bytes(raw)
        copy_ms = (time.perf_counter() - copy_start) * 1000.0

        self.records.append(
            BufferRecord(
                received_monotonic=time.monotonic(),
                frame_count=buffer.frameCount(),
                byte_count=buffer.byteCount(),
                duration_us=buffer.duration(),
                start_time_us=buffer.startTime(),
                position_ms=self._player.position(),
                sample_format=audio_format.sampleFormat(),
                sample_rate=audio_format.sampleRate(),
                channel_count=audio_format.channelCount(),
                bytes_per_frame=audio_format.bytesPerFrame(),
                data=data,
                copy_ms=copy_ms,
                callback_ms=(time.perf_counter() - callback_start) * 1000.0,
            )
        )

    def clear(self) -> None:
        self.records.clear()


class CaptureSession:
    """QMediaPlayer + QAudioOutput + QAudioBufferOutput 一式。"""

    def __init__(self, volume: float = 0.0, keep_data: bool = True) -> None:
        self.audio_output = QAudioOutput()
        self.audio_output.setVolume(volume)
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio_output)
        self.buffer_output = QAudioBufferOutput()
        self.player.setAudioBufferOutput(self.buffer_output)
        self.receiver = BufferReceiver(self.player, keep_data)
        # 接続方式は既定の AutoConnection。実際に Direct/Queued のどちらとして
        # 振る舞ったかは、受信スレッドの実測（ThreadInfo）で判断する。
        self.buffer_output.audioBufferReceived.connect(
            self.receiver.on_buffer, Qt.ConnectionType.AutoConnection
        )
        self.errors: list[str] = []
        self.player.errorOccurred.connect(
            lambda error, message: self.errors.append(f"{error.name}: {message}")
        )

    @property
    def records(self) -> list[BufferRecord]:
        return self.receiver.records

    def load(self, app: QCoreApplication, path: Path) -> bool:
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        return wait_until(
            app,
            lambda: self.player.mediaStatus()
            in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia),
            LOAD_TIMEOUT_SEC,
        )

    def teardown(self, app: QCoreApplication) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        pump(app, 0.05)


def wait_until(app: QCoreApplication, predicate, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


def pump(app: QCoreApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)


def concat_mono(records: list[BufferRecord]) -> tuple[NDArray[np.float32], int]:
    """複数バッファを連結して mono 配列と sampleRate を返す。"""
    if not records:
        return np.zeros(0, dtype=np.float32), 0
    frames = [
        to_float_frames(r.data, r.sample_format, r.channel_count) for r in records if r.data
    ]
    if not frames:
        return np.zeros(0, dtype=np.float32), records[0].sample_rate
    return to_mono(np.concatenate(frames)), records[0].sample_rate


# --- 1. 環境と API ------------------------------------------------------------


def report_environment() -> None:
    print("=== 実行環境と API ===")
    print(f"Python  : {sys.version.split()[0]}")
    print(f"PySide6 : {pyside_version}")
    print(f"Qt      : {qVersion()}")
    output_device = QMediaDevices.defaultAudioOutput()
    print(f"既定の音声出力: {output_device.description() or '(なし)'}")
    print(f"QAudioBufferOutput の存在        : {QAudioBufferOutput is not None}")
    print(f"QMediaPlayer.setAudioBufferOutput: {hasattr(QMediaPlayer, 'setAudioBufferOutput')}")
    print(f"QMediaPlayer.audioBufferOutput   : {hasattr(QMediaPlayer, 'audioBufferOutput')}")
    print(f"QAudioBufferOutput.audioBufferReceived: {hasattr(QAudioBufferOutput, 'audioBufferReceived')}")
    print()


# --- 2. 形式ごとの PCM 取得 ----------------------------------------------------


def test_formats(app: QCoreApplication) -> bool:
    """主要 6 形式と日本語パスで PCM を取得し、形式と FFT ピークを確認する。"""
    print("=== 形式ごとの PCM 取得 ===")
    print("音源はいずれも 440Hz 正弦波（左 0.5 / 右 0.25 の振幅差あり）")
    print()

    targets: list[tuple[str, Path]] = [
        ("WAV", TEST_AUDIO_DIR / "sine440.wav"),
        ("MP3", TEST_AUDIO_DIR / "sine440.mp3"),
        ("OGG Vorbis", TEST_AUDIO_DIR / "sine440.ogg"),
        ("OGG Opus", TEST_AUDIO_DIR / "sine440.opus"),
        ("FLAC", TEST_AUDIO_DIR / "sine440.flac"),
        ("M4A/AAC", TEST_AUDIO_DIR / "sine440.m4a"),
        ("日本語・空白パス", TEST_AUDIO_DIR / "日本語 ディレクトリ" / "テスト 音源 440Hz.wav"),
    ]

    header = (
        f"{'対象':<18} {'sampleFormat':<13} {'rate':>6} {'ch':>3} {'bpf':>4} "
        f"{'件数':>5} {'frameCount':<16} {'duration':>9} {'L peak':>9} {'R peak':>9} {'mono':>9} 判定"
    )
    print(header)
    print("-" * len(header))

    all_ok = True
    for label, path in targets:
        session = CaptureSession()
        if not session.load(app, path):
            print(f"{label:<18} 読み込み失敗")
            all_ok = False
            continue
        session.player.play()
        wait_until(app, lambda s=session: len(s.records) >= 12, 6.0)
        session.player.stop()
        pump(app, 0.05)

        records = list(session.records)
        if not records:
            print(f"{label:<18} バッファ未受信")
            all_ok = False
            session.teardown(app)
            continue

        first = records[0]
        # 先頭バッファはコーデックのプライミングで短いことがあるため、
        # 解析には十分な長さのバッファだけを使う。
        usable = [r for r in records if r.frame_count >= 512] or records
        frames = np.concatenate(
            [to_float_frames(r.data, r.sample_format, r.channel_count) for r in usable]
        )
        left_peak = fft_peak_hz(frames[:, 0].copy(), first.sample_rate)
        right_peak = fft_peak_hz(frames[:, 1].copy(), first.sample_rate)
        mono_peak = fft_peak_hz(to_mono(frames), first.sample_rate)

        ok = all(
            abs(peak - 440.0) <= FFT_PEAK_TOLERANCE_HZ
            for peak in (left_peak, right_peak, mono_peak)
        )
        all_ok = all_ok and ok

        frame_counts = sorted({r.frame_count for r in records})
        frame_text = ",".join(str(v) for v in frame_counts[:4])
        if len(frame_counts) > 4:
            frame_text += ",..."
        mean_duration = statistics.mean(r.duration_us for r in records) / 1000.0

        print(
            f"{label:<18} {first.sample_format.name:<13} {first.sample_rate:>6} "
            f"{first.channel_count:>3} {first.bytes_per_frame:>4} {len(records):>5} "
            f"{frame_text:<16} {mean_duration:>7.1f}ms "
            f"{left_peak:>7.1f}Hz {right_peak:>7.1f}Hz {mono_peak:>7.1f}Hz "
            f"{'合格' if ok else '不合格'}"
        )
        if session.errors:
            for message in session.errors:
                print(f"    errorOccurred: {message}", file=sys.stderr)
            all_ok = False
        session.teardown(app)

    print()

    # 左右の振幅差（左 0.5 / 右 0.25）が保たれているかを WAV で確認する。
    session = CaptureSession()
    session.load(app, TEST_AUDIO_DIR / "sine440.wav")
    session.player.play()
    wait_until(app, lambda s=session: len(s.records) >= 6, 5.0)
    session.player.stop()
    frames = np.concatenate(
        [to_float_frames(r.data, r.sample_format, r.channel_count) for r in session.records]
    )
    left_rms = float(np.sqrt(np.mean(frames[:, 0] ** 2)))
    right_rms = float(np.sqrt(np.mean(frames[:, 1] ** 2)))
    ratio = left_rms / right_rms if right_rms else 0.0
    ratio_ok = abs(ratio - 2.0) < 0.1
    print(
        f"チャンネル別確認（WAV）: 左 RMS {left_rms:.4f} / 右 RMS {right_rms:.4f} "
        f"→ 比 {ratio:.3f}（期待 2.000）{'OK' if ratio_ok else 'NG'}"
    )
    session.teardown(app)
    print()
    return all_ok and ratio_ok


# --- 3. スレッド境界 -----------------------------------------------------------


def test_thread(app: QCoreApplication) -> bool:
    """audioBufferReceived の受信スレッドを実測する（推測しない）。"""
    print("=== スレッド境界 ===")
    session = CaptureSession()
    if not session.load(app, tone_wav_path()):
        print("読み込み失敗", file=sys.stderr)
        return False
    session.player.play()
    wait_until(app, lambda: len(session.records) >= 3, 6.0)
    session.player.stop()

    info = session.receiver.thread_info
    if info is None:
        print("スレッド情報を取得できなかった", file=sys.stderr)
        session.teardown(app)
        return False

    application = QCoreApplication.instance()
    main_ident = threading.main_thread().ident
    print(f"  スロット内 threading.get_ident()      : {info.python_thread_ident}")
    print(f"  スロット内 threading スレッド名       : {info.python_thread_name}")
    print(f"  Python のメインスレッド ident         : {main_ident}")
    print(f"  スロット内 QThread.currentThread()    : {info.qt_current_thread}")
    print(f"  受信 QObject の thread()              : {info.receiver_thread}")
    print(f"  QApplication.instance().thread()      : {info.application_thread}")
    print("  接続方式                              : Qt.ConnectionType.AutoConnection（既定）")
    print(f"  GUI スレッドと同一か                  : {info.same_as_gui}")
    print(
        f"  Python 側でもメインスレッドか         : "
        f"{info.python_thread_ident == main_ident}"
    )
    print(
        "  → AutoConnection で送信元と受信先が同一スレッドの場合、Qt は Direct 接続として"
        "呼び出す。"
    )
    session.teardown(app)
    print()
    _ = application
    return info.same_as_gui and info.python_thread_ident == main_ident


# --- 4. シーク後の実 PCM ------------------------------------------------------


def test_seek(app: QCoreApplication) -> bool:
    """シーク後、実際に取得した PCM が対象区間へ移るかを確認する。"""
    print("=== シーク後の実 PCM 内容 ===")
    print("診断音源: 0-2s=220Hz, 2-4s=330Hz, 4-6s=440Hz, 6-8s=550Hz, 8-10s=660Hz")
    print("player.position() が要求値を返しただけでは合格としない。")
    print()

    session = CaptureSession()
    if not session.load(app, segmented_wav_path()):
        print("読み込み失敗", file=sys.stderr)
        return False

    session.player.play()
    wait_until(app, lambda: len(session.records) >= 3, 6.0)

    header = (
        f"{'目標':>6} {'期待Hz':>7} {'直前peak':>9} {'position':>9} {'古いﾊﾞｯﾌｧ':>10} "
        f"{'古い合計':>9} {'一致まで':>9} {'一致後peak':>11} 判定"
    )
    print(header)
    print("-" * len(header))

    all_ok = True
    stale_details: list[str] = []

    for target_sec in SEEK_TARGETS_SEC:
        expected_hz = segment_frequency(target_sec)

        # 直前に再生していた区間と目標区間が同じだと「シークが効いたか」を判別できない。
        # 目標と異なる周波数の区間へ一度移動し、そこを実際に再生してから目標へシークする。
        preset_sec = next(
            start + 0.5 for start, _end, frequency in SEGMENTS if frequency != expected_hz
        )
        session.player.setPosition(int(preset_sec * 1000))
        session.receiver.clear()
        wait_until(app, lambda s=session: len(s.records) >= 4, 3.0)
        preset_peak = 0.0
        for record in session.records:
            if record.data and record.frame_count >= 256:
                frames = to_float_frames(record.data, record.sample_format, record.channel_count)
                preset_peak = fft_peak_hz(to_mono(frames), record.sample_rate)
                break

        session.receiver.clear()
        session.player.setPosition(int(target_sec * 1000))
        reported_position = session.player.position()
        # シーク直後のバッファを十分に集める
        wait_until(app, lambda s=session: len(s.records) >= 15, 4.0)

        records = [r for r in session.records if r.data and r.frame_count >= 256]
        stale_count = 0
        stale_duration_us = 0
        matched_index = -1
        matched_peak = 0.0
        for index, record in enumerate(records):
            frames = to_float_frames(record.data, record.sample_format, record.channel_count)
            peak = fft_peak_hz(to_mono(frames), record.sample_rate)
            if abs(peak - expected_hz) <= FFT_PEAK_TOLERANCE_HZ:
                matched_index = index
                matched_peak = peak
                break
            stale_count += 1
            stale_duration_us += record.duration_us
            stale_details.append(
                f"    目標 {target_sec:.0f}s: 古いバッファ #{index} "
                f"startTime={record.start_time_us / 1000.0:.1f}ms "
                f"duration={record.duration_us / 1000.0:.1f}ms "
                f"position={record.position_ms}ms peak={peak:.1f}Hz "
                f"（startTime を秒へ直すと {record.start_time_us / 1e6:.3f}s → "
                f"その区間の期待周波数 {segment_frequency(record.start_time_us / 1e6):.0f}Hz）"
            )

        ok = matched_index >= 0
        all_ok = all_ok and ok
        print(
            f"{target_sec:>5.0f}s {expected_hz:>6.0f}Hz {preset_peak:>7.1f}Hz "
            f"{reported_position:>8}ms {stale_count:>10} {stale_duration_us / 1000.0:>7.1f}ms "
            f"{'#' + str(matched_index) if ok else '-':>9} {matched_peak:>9.1f}Hz "
            f"{'合格' if ok else '不合格'}"
        )

    session.player.stop()
    null_count = session.receiver.null_data_count
    session.teardown(app)
    print()
    print(f"constData() が None を返したバッファ: {null_count} 件")
    print()

    if stale_details:
        print("シーク直後に届いた「古いバッファ」の詳細:")
        for line in stale_details:
            print(line)
    else:
        print("シーク直後に古いバッファは観測されなかった。")
    print()
    return all_ok


# --- 5. 速度変更時の通知挙動 ---------------------------------------------------


@dataclass
class SpeedObservation:
    rate: float
    pitch_compensation: bool
    buffers_per_sec: float = 0.0
    frames_per_sec: float = 0.0
    mean_frame_count: float = 0.0
    frame_counts: tuple[int, ...] = ()
    mean_interval_ms: float = 0.0
    position_per_sec_ms: float = 0.0
    peak_hz: float = 0.0
    sample_rate: int = 0
    errors: list[str] = field(default_factory=list)


def test_speed(app: QCoreApplication) -> list[SpeedObservation]:
    """0.5 / 1.0 / 2.0 倍 × ピッチ補正 ON/OFF で通知挙動を実測する。"""
    print("=== 速度変更時の PCM 通知挙動 ===")
    print("FFT ピークは QAudioBufferOutput 側 PCM の性質を見る参考値であり、")
    print("処理後の可聴ピッチの判定には使用しない。")
    print()

    observations: list[SpeedObservation] = []
    measure_sec = 3.0

    header = (
        f"{'倍率':>5} {'補正':<5} {'rate':>6} {'buf/s':>7} {'frames/s':>9} "
        f"{'frames/s÷rate':>13} {'平均frame':>9} {'間隔':>8} {'position/s':>11} {'peak':>9}"
    )
    print(header)
    print("-" * len(header))

    for rate in (0.5, 1.0, 2.0):
        for pitch in (False, True):
            session = CaptureSession()
            if not session.load(app, tone_wav_path()):
                continue
            session.player.setPitchCompensation(pitch)
            session.player.setPlaybackRate(rate)
            session.player.play()
            wait_until(app, lambda s=session: len(s.records) >= 3, 6.0)

            session.receiver.clear()
            position_start = session.player.position()
            start = time.monotonic()
            pump(app, measure_sec)
            elapsed = time.monotonic() - start
            position_delta = session.player.position() - position_start
            records = list(session.records)
            session.player.stop()

            observation = SpeedObservation(rate=rate, pitch_compensation=pitch)
            observation.errors = list(session.errors)
            if records:
                observation.buffers_per_sec = len(records) / elapsed
                observation.frames_per_sec = sum(r.frame_count for r in records) / elapsed
                observation.mean_frame_count = statistics.mean(r.frame_count for r in records)
                observation.frame_counts = tuple(sorted({r.frame_count for r in records}))
                if len(records) >= 2:
                    intervals = [
                        (b.received_monotonic - a.received_monotonic) * 1000.0
                        for a, b in zip(records, records[1:], strict=False)
                    ]
                    observation.mean_interval_ms = statistics.mean(intervals)
                observation.position_per_sec_ms = position_delta / elapsed
                mono, sample_rate = concat_mono(records)
                observation.sample_rate = sample_rate
                observation.peak_hz = fft_peak_hz(mono, sample_rate) if sample_rate else 0.0
            observations.append(observation)

            ratio = (
                observation.frames_per_sec / observation.sample_rate
                if observation.sample_rate
                else 0.0
            )
            print(
                f"{rate:>5.2f} {('ON' if pitch else 'OFF'):<5} {observation.sample_rate:>6} "
                f"{observation.buffers_per_sec:>7.1f} {observation.frames_per_sec:>9.0f} "
                f"{ratio:>13.3f} {observation.mean_frame_count:>9.0f} "
                f"{observation.mean_interval_ms:>6.1f}ms {observation.position_per_sec_ms:>9.0f}ms "
                f"{observation.peak_hz:>7.1f}Hz"
            )
            session.teardown(app)

    print()
    print("frames/s ÷ sampleRate が playbackRate と一致するかどうかが、")
    print("QAudioBufferOutput の PCM が『伸縮前のデコード済み音声』かを判断する材料になる。")
    print()
    return observations


# --- 6. 音量とミュート ---------------------------------------------------------


def test_volume_mute(app: QCoreApplication) -> bool:
    """音量・ミュートが QAudioBufferOutput の PCM 内容へ影響するかを実測する。"""
    print("=== 音量・ミュートと PCM 内容の関係 ===")
    print("推測せず RMS と peak で比較する。")
    print()

    header = f"{'volume':>7} {'muted':<7} {'件数':>5} {'RMS':>10} {'peak':>10}"
    print(header)
    print("-" * len(header))

    measurements: list[tuple[float, bool, float, float]] = []
    for volume, muted in ((0.0, False), (0.2, False), (0.2, True), (0.0, True)):
        session = CaptureSession(volume=volume)
        session.audio_output.setMuted(muted)
        if not session.load(app, tone_wav_path()):
            continue
        session.player.play()
        wait_until(app, lambda s=session: len(s.records) >= 10, 6.0)
        session.player.stop()

        records = [r for r in session.records if r.frame_count >= 512]
        mono, _rate = concat_mono(records)
        rms = float(np.sqrt(np.mean(mono**2))) if mono.size else 0.0
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        measurements.append((volume, muted, rms, peak))
        print(f"{volume:>7.1f} {str(muted):<7} {len(records):>5} {rms:>10.6f} {peak:>10.6f}")
        session.teardown(app)

    print()
    if len(measurements) >= 2:
        rms_values = [m[2] for m in measurements]
        spread = max(rms_values) - min(rms_values)
        unchanged = spread < 0.01
        print(f"RMS の最大差: {spread:.6f} → 音量・ミュートで PCM が{'変化しない' if unchanged else '変化する'}")
        if unchanged:
            print("→ 可視化は『音量設定を適用する前』の信号を表すことになる。")
        else:
            print("→ 可視化は『音量設定を適用した後』の信号を表すことになる。")
        return True
    return False


# --- 7. 曲切替と形式切替 -------------------------------------------------------


def test_track_switch(app: QCoreApplication) -> bool:
    """形式の異なる曲を連続で切り替え、形式と内容が更新されるかを確認する。"""
    print("=== 曲切替・形式切替 ===")
    sequence: list[tuple[str, Path]] = [
        ("44.1kHz WAV", TEST_AUDIO_DIR / "sine440.wav"),
        ("48kHz Opus", TEST_AUDIO_DIR / "sine440.opus"),
        ("MP3", TEST_AUDIO_DIR / "sine440.mp3"),
        ("FLAC", TEST_AUDIO_DIR / "sine440.flac"),
    ]

    session = CaptureSession()
    header = (
        f"{'順序':<14} {'sampleFormat':<13} {'rate':>6} {'ch':>3} {'件数':>5} "
        f"{'peak':>9} {'混入':>6} 判定"
    )
    print(header)
    print("-" * len(header))

    all_ok = True
    previous_signature: tuple[str, int, int] | None = None
    contamination_notes: list[str] = []

    for label, path in sequence:
        session.receiver.clear()
        if not session.load(app, path):
            print(f"{label:<14} 読み込み失敗")
            all_ok = False
            continue
        session.player.play()
        wait_until(app, lambda s=session: len(s.records) >= 12, 6.0)
        session.player.stop()
        pump(app, 0.05)

        records = [r for r in session.records if r.data]
        if not records:
            print(f"{label:<14} バッファ未受信")
            all_ok = False
            continue

        # 直前の曲の形式のまま届いたバッファ（＝混入）を数える。
        contaminated = 0
        if previous_signature is not None:
            for record in records:
                signature = (record.sample_format.name, record.sample_rate, record.channel_count)
                if signature == previous_signature and signature != (
                    records[-1].sample_format.name,
                    records[-1].sample_rate,
                    records[-1].channel_count,
                ):
                    contaminated += 1
                else:
                    break
            if contaminated:
                contamination_notes.append(
                    f"    {label}: 前曲形式のバッファが {contaminated} 件混入"
                )

        current = records[-1]
        usable = [r for r in records if r.frame_count >= 512] or records
        mono, sample_rate = concat_mono(usable)
        peak = fft_peak_hz(mono, sample_rate)
        ok = abs(peak - 440.0) <= FFT_PEAK_TOLERANCE_HZ
        all_ok = all_ok and ok

        print(
            f"{label:<14} {current.sample_format.name:<13} {current.sample_rate:>6} "
            f"{current.channel_count:>3} {len(records):>5} {peak:>7.1f}Hz "
            f"{contaminated:>6} {'合格' if ok else '不合格'}"
        )
        previous_signature = (
            current.sample_format.name,
            current.sample_rate,
            current.channel_count,
        )

    session.teardown(app)
    print()
    if contamination_notes:
        print("前曲 PCM の混入:")
        for note in contamination_notes:
            print(note)
    else:
        print("setSource 後に前曲形式のバッファは観測されなかった。")
    print()
    return all_ok


# --- 8. 処理コスト -------------------------------------------------------------


def test_processing_cost(app: QCoreApplication) -> bool:
    """可視化に必要な処理のコストを実測し、GUI スレッドで実行可能か判断する。"""
    print("=== 処理コスト ===")
    session = CaptureSession()
    if not session.load(app, tone_wav_path()):
        return False
    session.player.play()
    wait_until(app, lambda: len(session.records) >= 40, 8.0)
    session.player.stop()

    records = [r for r in session.records if r.frame_count >= 512]
    if not records:
        print("計測に足るバッファを取得できなかった", file=sys.stderr)
        session.teardown(app)
        return False

    callback_times = [r.callback_ms for r in records]
    copy_times = [r.copy_ms for r in records]

    convert_times: list[float] = []
    mono_times: list[float] = []
    for record in records:
        start = time.perf_counter()
        frames = to_float_frames(record.data, record.sample_format, record.channel_count)
        convert_times.append((time.perf_counter() - start) * 1000.0)
        start = time.perf_counter()
        to_mono(frames)
        mono_times.append((time.perf_counter() - start) * 1000.0)

    mono, sample_rate = concat_mono(records)
    fft_times: list[float] = []
    window = np.hanning(FFT_SIZE)
    for _ in range(200):
        segment = mono[-FFT_SIZE:] * window
        start = time.perf_counter()
        np.abs(np.fft.rfft(segment))
        fft_times.append((time.perf_counter() - start) * 1000.0)

    def summary(label: str, values: list[float]) -> None:
        print(
            f"  {label:<28}: 平均 {statistics.mean(values):.4f}ms / "
            f"中央 {statistics.median(values):.4f}ms / 最大 {max(values):.4f}ms"
        )

    summary("コールバック全体", callback_times)
    summary("PCM コピー（bytes 化）", copy_times)
    summary("float32 変換", convert_times)
    summary("stereo → mono 変換", mono_times)
    summary(f"{FFT_SIZE} 点 FFT", fft_times)

    buffers_per_sec = len(records) / (
        records[-1].received_monotonic - records[0].received_monotonic
    )
    callback_load = statistics.mean(callback_times) * buffers_per_sec / 1000.0
    fps_cost = statistics.mean(fft_times) * 30.0 / 1000.0
    print()
    print(f"  バッファ通知頻度              : {buffers_per_sec:.1f} 件/秒")
    print(f"  コールバックの CPU 占有率     : {callback_load:.2%}（1 秒あたり）")
    print(f"  30FPS で FFT した場合の占有率 : {fps_cost:.2%}（1 秒あたり）")

    # バッファの連続性（欠落の検出）
    gaps: list[str] = []
    for previous, current in zip(records, records[1:], strict=False):
        expected = previous.start_time_us + previous.duration_us
        drift = current.start_time_us - expected
        if abs(drift) > 1000:
            gaps.append(
                f"    startTime の不連続: {previous.start_time_us / 1000.0:.1f}ms + "
                f"{previous.duration_us / 1000.0:.1f}ms → {current.start_time_us / 1000.0:.1f}ms "
                f"(差 {drift / 1000.0:+.1f}ms)"
            )
    print(f"  startTime の不連続            : {len(gaps)} 件")
    for gap in gaps[:5]:
        print(gap)
    if session.errors:
        for message in session.errors:
            print(f"  errorOccurred: {message}", file=sys.stderr)

    session.teardown(app)
    print()
    return not session.errors


# --- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P0-C: QAudioBufferOutput による PCM 取得の検証")
    parser.add_argument(
        "--only",
        default="",
        help="実行する検証をカンマ区切りで指定（formats,thread,seek,speed,volume,switch,cost）",
    )
    args = parser.parse_args(argv)
    selected = {name.strip() for name in args.only.split(",") if name.strip()}

    qInstallMessageHandler(_message_handler)
    app = QApplication(sys.argv[:1])
    ensure_generated_audio()

    report_environment()

    results: dict[str, bool] = {}

    def should_run(name: str) -> bool:
        return not selected or name in selected

    if should_run("formats"):
        results["形式ごとの PCM 取得"] = test_formats(app)
    if should_run("thread"):
        results["スレッド境界"] = test_thread(app)
    if should_run("seek"):
        results["シーク後の実 PCM"] = test_seek(app)
    if should_run("speed"):
        test_speed(app)
    if should_run("volume"):
        results["音量・ミュート"] = test_volume_mute(app)
    if should_run("switch"):
        results["曲切替・形式切替"] = test_track_switch(app)
    if should_run("cost"):
        results["処理コスト"] = test_processing_cost(app)

    print("=== Qt のログ ===")
    if QT_MESSAGES:
        for message in QT_MESSAGES:
            print(f"  {message}")
    else:
        print("  なし")
    print()

    print("=== 判定 ===")
    for label, ok in results.items():
        print(f"  {label:<22}: {'合格' if ok else '不合格'}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
