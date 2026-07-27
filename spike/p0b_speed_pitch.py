"""P0-B: 再生速度とピッチ補正の検証（使い捨ての検証スクリプト）。

開発計画 P0 の項目 2（0.5〜2.0 倍再生）と項目 3（ピッチ補正 ON/OFF）に対応し、
未検証事項 U1（ピッチ補正 API の対応状況）と U2（varispeed / time-stretch の音質）を扱う。

重要な前提（混同してはならないこと）:

- QAudioBufferOutput が出力する PCM は、Qt の仕様上、現在の playbackRate に応じて
  伸縮された後の音声ではない。したがって QAudioBufferOutput から取得した PCM で
  varispeed / time-stretch の実出力ピッチを判定することはできない。
  本スクリプトは QAudioBufferOutput を一切使用しない。
- 本スクリプトが書き出す比較音源は、NumPy で生成した「期待値となる参照音源」だけである。
  参照音源は Qt Multimedia による処理後の音声ではない。
- 実出力のピッチと音質の判定は、人間による可聴確認でのみ行う。
  AI が聴感結果を推測して合否を決めてはならない。

モード:

    （既定）自動検証。音量は 0.0 に固定され、音は鳴らない。
    --manual audible      P0-A で保留していた可聴確認（WAV / MP3 / Opus / 日本語パス）
    --manual varispeed    ピッチ補正 OFF。各倍率の Qt 再生と参照音を聴き比べる
    --manual timestretch  ピッチ補正 ON。440Hz 参照音との音程差と音質を確認する

手動モードは音を鳴らすため、`--volume` の明示指定を必須とする。

使い方:

    uv run python spike/p0b_speed_pitch.py
    uv run python spike/p0b_speed_pitch.py --manual audible --volume 0.2
    uv run python spike/p0b_speed_pitch.py --manual varispeed --volume 0.2
    uv run python spike/p0b_speed_pitch.py --manual timestretch --volume 0.2 --source "D:\\music\\曲.flac"

生成物は .sdp-local/p0b/ へ置く（.gitignore 済み。リポジトリへはコミットしない）。
--source で指定したユーザー所有の音源は、コピーもコミットもしない。
"""

import argparse
import sys
import time
import wave
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QCoreApplication, QtMsgType, QUrl, qInstallMessageHandler, qVersion
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaFormat, QMediaPlayer
from PySide6.QtWidgets import QApplication

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_AUDIO_DIR = REPO_ROOT / "assets" / "test_audio"
# 生成物の置き場。リポジトリへコミットしない（.gitignore の /.sdp-local/ に該当）。
WORK_DIR = REPO_ROOT / ".sdp-local" / "p0b"

SAMPLE_RATE = 44100
# 速度検証用 WAV の長さ。ロード時間の影響を相対的に小さくするため 10 秒とする。
SPEED_WAV_SEC = 10.0
REFERENCE_WAV_SEC = 3.0
BASE_FREQ_HZ = 440.0

# 検証する再生速度。45/33 は指示された変則的な倍率で、440Hz に対する期待ピッチは 600Hz。
RATES: tuple[Fraction, ...] = (
    Fraction(1, 2),
    Fraction(3, 4),
    Fraction(1, 1),
    Fraction(45, 33),
    Fraction(5, 4),
    Fraction(3, 2),
    Fraction(2, 1),
)

# Fraction は既約分数へ正規化されるため（45/33 → 15/11）、
# 指示された表記のまま表示したい倍率だけラベルを持たせる。
RATE_LABELS: dict[Fraction, str] = {Fraction(45, 33): "45/33"}


def rate_label(rate: Fraction) -> str:
    return RATE_LABELS.get(rate, str(rate))


# QMediaPlayer.playbackRate は float32 精度で保持される（実測で確認）。
# 0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0 のように float32 で厳密に表せる値は往復一致するが、
# 45/33 のような値は往復で相対 1e-8 程度ずれる。厳密比較は誤検出になるため、
# float32 のイプシロン（約 1.19e-7）に余裕を見た相対 1e-6 で比較する。
RATE_MATCH_RELATIVE_TOLERANCE = 1e-6


def rate_matches(actual: float, expected: float) -> bool:
    """playbackRate の往復一致を float32 精度で判定する。"""
    return abs(actual - expected) <= abs(expected) * RATE_MATCH_RELATIVE_TOLERANCE


# --- 再生時間の許容誤差 -------------------------------------------------------
# 期待値は (duration - 計測開始位置) / playbackRate。実測との差には次が乗る。
#   1. ポーリング間隔 5ms（開始・終了の検出粒度）
#   2. 「position が最初に進んだ時点」の検出遅れ。position の更新粒度に依存する
#   3. EndOfMedia は音声出力バッファが排出された後に届くため、
#      出力バッファ長（Bluetooth 機器では特に大きい）分の遅延が乗る
# 初回実測では誤差の最大が -94ms（相対 -0.5%）、大半は 50ms 未満だった。
# その実測に対しておよそ 2 倍の余裕を取り、絶対 200ms / 相対 2% の大きい方とする。
# 「通すために広げた値」ではなく、実測分布から決めた値である。
TOLERANCE_ABS_MS = 200.0
TOLERANCE_RATIO = 0.02

LOAD_TIMEOUT_SEC = 10.0
FIRST_POSITION_TIMEOUT_SEC = 10.0
# 0.5 倍では 10 秒の音源が 20 秒かかる。余裕を持たせる。
END_TIMEOUT_SEC = 40.0


@dataclass
class QtMessage:
    """qInstallMessageHandler が受け取った Qt のログ。"""

    type_name: str
    text: str


# Qt のログを集約する。FFmpeg ライブラリが直接 stderr へ出す診断は
# このハンドラーを経由しないため、別途 stderr を確認する必要がある。
QT_MESSAGES: list[QtMessage] = []


def _message_handler(msg_type: QtMsgType, _context: object, message: str) -> None:
    QT_MESSAGES.append(QtMessage(type_name=msg_type.name, text=message))


@dataclass
class SpeedResult:
    """1 回の速度計測の結果。"""

    rate: Fraction
    pitch_compensation_requested: bool
    pitch_compensation_effective: bool = False
    applied_rate: float = 0.0
    duration_ms: int = 0
    start_position_ms: int = 0
    measured_ms: float = 0.0
    expected_ms: float = 0.0
    end_of_media: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def error_ms(self) -> float:
        return self.measured_ms - self.expected_ms

    @property
    def error_ratio(self) -> float:
        return self.error_ms / self.expected_ms if self.expected_ms else 0.0

    @property
    def tolerance_ms(self) -> float:
        return max(TOLERANCE_ABS_MS, self.expected_ms * TOLERANCE_RATIO)

    @property
    def passed(self) -> bool:
        return (
            self.end_of_media
            and not self.errors
            and abs(self.error_ms) <= self.tolerance_ms
            and rate_matches(self.applied_rate, float(self.rate))
        )


# --- 音源生成（NumPy による期待値の生成のみ。Qt の出力ではない） ----------------


def write_wav(path: Path, samples: NDArray[np.float32]) -> None:
    """float32 [-1.0, 1.0] のステレオサンプルを 16bit PCM WAV として書き出す。"""
    as_int16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(2)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(as_int16.tobytes())


def make_tone(frequency_hz: float, seconds: float) -> NDArray[np.float32]:
    """指定周波数の正弦波を生成する。両端に短いフェードを入れてクリックを防ぐ。"""
    count = int(SAMPLE_RATE * seconds)
    t = np.arange(count, dtype=np.float64) / SAMPLE_RATE
    tone = np.sin(2.0 * np.pi * frequency_hz * t) * 0.4

    fade_len = int(SAMPLE_RATE * 0.02)
    envelope = np.ones(count, dtype=np.float64)
    envelope[:fade_len] = np.linspace(0.0, 1.0, fade_len)
    envelope[-fade_len:] = np.linspace(1.0, 0.0, fade_len)
    tone *= envelope

    return np.stack((tone, tone), axis=1).astype(np.float32)


def reference_frequency(rate: Fraction) -> float:
    """440Hz を基準にした、varispeed 時の期待ピッチ。"""
    return BASE_FREQ_HZ * float(rate)


def speed_wav_path() -> Path:
    return WORK_DIR / f"speed_{int(SPEED_WAV_SEC)}s_{int(BASE_FREQ_HZ)}Hz.wav"


def reference_wav_path(frequency_hz: float) -> Path:
    return WORK_DIR / f"reference_{frequency_hz:.0f}Hz.wav"


def ensure_generated_audio() -> None:
    """速度検証用 WAV と参照音源を生成する（既存なら再生成しない）。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    speed_path = speed_wav_path()
    if not speed_path.exists():
        write_wav(speed_path, make_tone(BASE_FREQ_HZ, SPEED_WAV_SEC))

    for rate in RATES:
        frequency = reference_frequency(rate)
        path = reference_wav_path(frequency)
        if not path.exists():
            write_wav(path, make_tone(frequency, REFERENCE_WAV_SEC))


# --- Qt の共通処理 ------------------------------------------------------------


def wait_until(app: QCoreApplication, predicate, timeout_sec: float) -> bool:
    """条件が満たされるまでイベントループを回す。"""
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


def load_source(app: QCoreApplication, player: QMediaPlayer, path: Path) -> bool:
    """音源を読み込み、再生可能になるまで待つ。"""
    player.setSource(QUrl.fromLocalFile(str(path)))
    return wait_until(
        app,
        lambda: player.mediaStatus()
        in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia),
        LOAD_TIMEOUT_SEC,
    )


def make_player(volume: float) -> tuple[QMediaPlayer, QAudioOutput]:
    """QMediaPlayer と QAudioOutput を用意する（参照を保持しないと破棄される）。"""
    audio_output = QAudioOutput()
    audio_output.setVolume(volume)
    player = QMediaPlayer()
    player.setAudioOutput(audio_output)
    return player, audio_output


# --- 自動検証 ----------------------------------------------------------------


def report_environment(app: QCoreApplication) -> None:
    """バージョンとバックエンドの診断情報を出力する。"""
    import os

    print("=== 実行環境 ===")
    print(f"Python          : {sys.version.split()[0]}")
    print(f"PySide6         : {pyside_version}")
    print(f"Qt              : {qVersion()}")
    print(f"QT_MEDIA_BACKEND: {os.environ.get('QT_MEDIA_BACKEND', '未設定（既定）')}")

    default_output = QMediaDevices.defaultAudioOutput()
    print(f"既定の音声出力  : {default_output.description() or '(なし)'}")

    media_format = QMediaFormat()
    codecs = media_format.supportedAudioCodecs(QMediaFormat.ConversionMode.Decode)
    print(f"デコード可能な音声コーデック: {', '.join(codec.name for codec in codecs)}")
    print(
        "  ※ 上の一覧と、実行時に stderr へ出る FFmpeg 形式の診断ログが、"
        "Qt が FFmpeg バックエンドで動作していることを示す。"
    )
    print()


def report_api_surface() -> QMediaPlayer.PitchCompensationAvailability:
    """ピッチ補正 API の存在と availability を確認する。"""
    print("=== ピッチ補正 API の存在確認 ===")
    for name in (
        "pitchCompensation",
        "setPitchCompensation",
        "pitchCompensationAvailability",
        "pitchCompensationChanged",
        "playbackRate",
        "setPlaybackRate",
        "playbackRateChanged",
    ):
        print(f"  hasattr(QMediaPlayer, {name!r}): {hasattr(QMediaPlayer, name)}")
    print()

    player, _output = make_player(0.0)
    availability = player.pitchCompensationAvailability()
    print("=== pitchCompensationAvailability ===")
    print(f"  実値: {availability.name}")
    print(f"  Available   : {availability == QMediaPlayer.PitchCompensationAvailability.Available}")
    print(f"  AlwaysOn    : {availability == QMediaPlayer.PitchCompensationAvailability.AlwaysOn}")
    print(f"  Unavailable : {availability == QMediaPlayer.PitchCompensationAvailability.Unavailable}")
    print()
    return availability


def check_pitch_toggle(app: QCoreApplication, source: Path) -> bool:
    """再生前・再生中・一時停止中でのピッチ補正の切替を確認する。"""
    print("=== ピッチ補正の切替（再生前 / 再生中 / 一時停止中） ===")
    player, _output = make_player(0.0)

    notifications: list[bool] = []
    player.pitchCompensationChanged.connect(notifications.append)
    errors: list[str] = []
    player.errorOccurred.connect(lambda error, message: errors.append(f"{error.name}: {message}"))

    print(f"  source 未設定時の初期値      : {player.pitchCompensation()}")

    if not load_source(app, player, source):
        print("  音源の読み込みに失敗した", file=sys.stderr)
        return False
    initial = player.pitchCompensation()
    print(f"  音源読み込み後の初期値       : {initial}")

    ok = True

    def apply(label: str, value: bool) -> None:
        nonlocal ok
        before = len(notifications)
        player.setPitchCompensation(value)
        pump(app, 0.2)
        actual = player.pitchCompensation()
        emitted = notifications[before:]
        matched = actual == value
        ok = ok and matched
        print(
            f"  {label:<28}: 設定 {value} → 実値 {actual} "
            f"[{'OK' if matched else 'NG'}] "
            f"pitchCompensationChanged={emitted}"
        )

    # 再生前
    apply("再生前 False", False)
    apply("再生前 True", True)

    # 再生中
    player.play()
    wait_until(app, lambda: player.position() > 0, FIRST_POSITION_TIMEOUT_SEC)
    apply("再生中 False", False)
    apply("再生中 True", True)

    # 一時停止中
    player.pause()
    pump(app, 0.2)
    apply("一時停止中 False", False)
    apply("一時停止中 True", True)

    player.stop()
    player.setSource(QUrl())
    pump(app, 0.1)

    if errors:
        print("  errorOccurred:", file=sys.stderr)
        for message in errors:
            print(f"    - {message}", file=sys.stderr)
        ok = False
    print(f"  判定: {'OK' if ok else 'NG'}")
    print()
    return ok


def check_playback_rate(app: QCoreApplication, source: Path) -> bool:
    """playbackRate の初期値・設定結果・変更通知を確認する。"""
    print("=== playbackRate の初期値と設定結果 ===")
    player, _output = make_player(0.0)

    notifications: list[float] = []
    player.playbackRateChanged.connect(notifications.append)
    errors: list[str] = []
    player.errorOccurred.connect(lambda error, message: errors.append(f"{error.name}: {message}"))

    print(f"  source 未設定時の初期値: {player.playbackRate()}")
    if not load_source(app, player, source):
        print("  音源の読み込みに失敗した", file=sys.stderr)
        return False
    print(f"  音源読み込み後の初期値 : {player.playbackRate()}")

    ok = True

    def apply(label: str, rate: Fraction) -> None:
        nonlocal ok
        before = len(notifications)
        player.setPlaybackRate(float(rate))
        pump(app, 0.15)
        actual = player.playbackRate()
        matched = rate_matches(actual, float(rate))
        ok = ok and matched
        emitted = [round(value, 6) for value in notifications[before:]]
        print(
            f"  {label:<22}: 設定 {float(rate):.4f} → 実値 {actual:.4f} "
            f"[{'OK' if matched else 'NG'}] playbackRateChanged={emitted}"
        )

    print("  -- 再生前 --")
    for rate in RATES:
        apply(f"再生前 {rate_label(rate)} 倍", rate)

    print("  -- 再生中 --")
    player.setPlaybackRate(1.0)
    player.play()
    wait_until(app, lambda: player.position() > 0, FIRST_POSITION_TIMEOUT_SEC)
    for rate in (Fraction(1, 2), Fraction(2, 1), Fraction(1, 1)):
        apply(f"再生中 {rate_label(rate)} 倍", rate)

    print("  -- 一時停止中 --")
    player.pause()
    pump(app, 0.2)
    for rate in (Fraction(3, 4), Fraction(3, 2)):
        apply(f"一時停止中 {rate_label(rate)} 倍", rate)

    player.stop()
    player.setSource(QUrl())
    pump(app, 0.1)

    if errors:
        print("  errorOccurred:", file=sys.stderr)
        for message in errors:
            print(f"    - {message}", file=sys.stderr)
        ok = False
    print(f"  判定: {'OK' if ok else 'NG'}")
    print()
    return ok


def measure_speed(
    app: QCoreApplication, source: Path, rate: Fraction, pitch_compensation: bool
) -> SpeedResult:
    """1 つの倍率・ピッチ補正設定について、再生完了までの経過時間を計測する。"""
    result = SpeedResult(rate=rate, pitch_compensation_requested=pitch_compensation)

    player, _output = make_player(0.0)
    statuses: list[QMediaPlayer.MediaStatus] = []
    player.mediaStatusChanged.connect(statuses.append)
    player.errorOccurred.connect(
        lambda error, message: result.errors.append(f"{error.name}: {message}")
    )
    rate_notifications: list[float] = []
    player.playbackRateChanged.connect(rate_notifications.append)

    if not load_source(app, player, source):
        result.errors.append("音源の読み込みに失敗した")
        return result

    result.duration_ms = player.duration()
    player.setPitchCompensation(pitch_compensation)
    player.setPlaybackRate(float(rate))
    pump(app, 0.1)
    result.pitch_compensation_effective = player.pitchCompensation()
    result.applied_rate = player.playbackRate()

    player.play()
    if not wait_until(app, lambda: player.position() > 0, FIRST_POSITION_TIMEOUT_SEC):
        result.errors.append("再生位置が進まない")
        player.stop()
        return result

    # 初期バッファリングを計測へ混ぜないため、position が進んだ時点を起点とする。
    start_monotonic = time.monotonic()
    result.start_position_ms = player.position()

    result.end_of_media = wait_until(
        app, lambda: QMediaPlayer.MediaStatus.EndOfMedia in statuses, END_TIMEOUT_SEC
    )
    result.measured_ms = (time.monotonic() - start_monotonic) * 1000.0
    result.expected_ms = (result.duration_ms - result.start_position_ms) / float(rate)

    if not result.end_of_media:
        result.errors.append("EndOfMedia が届かない")

    player.stop()
    player.setSource(QUrl())
    pump(app, 0.05)
    return result


def run_speed_measurements(app: QCoreApplication, source: Path) -> list[SpeedResult]:
    """全倍率 × ピッチ補正 ON/OFF の経過時間を計測する。"""
    print("=== 再生速度の検証（経過時間の計測） ===")
    print(f"  音源: {source.name}")
    print(
        f"  許容誤差: 絶対 {TOLERANCE_ABS_MS:.0f}ms と相対 {TOLERANCE_RATIO:.0%} の大きい方"
        "（根拠はソースのコメントを参照）"
    )
    print()

    results: list[SpeedResult] = []
    for pitch_compensation in (False, True):
        label = "ON " if pitch_compensation else "OFF"
        print(f"--- ピッチ補正 {label} ---")
        header = (
            f"{'倍率':<10} {'設定値':<8} {'補正':<5} {'期待':>9} {'実測':>9} "
            f"{'誤差':>9} {'相対':>7} {'許容':>8} 判定"
        )
        print(header)
        print("-" * len(header))
        for rate in RATES:
            result = measure_speed(app, source, rate, pitch_compensation)
            results.append(result)
            print(
                f"{rate_label(rate):<10} "
                f"{result.applied_rate:<8.4f} "
                f"{('ON' if result.pitch_compensation_effective else 'OFF'):<5} "
                f"{result.expected_ms:>8.0f}ms "
                f"{result.measured_ms:>8.0f}ms "
                f"{result.error_ms:>+8.0f}ms "
                f"{result.error_ratio:>+6.1%} "
                f"{result.tolerance_ms:>7.0f}ms "
                f"{'合格' if result.passed else '不合格'}"
            )
        print()
    return results


def run_auto(app: QCoreApplication) -> int:
    """自動検証を実行する。音は鳴らさない。"""
    report_environment(app)
    availability = report_api_surface()

    if availability != QMediaPlayer.PitchCompensationAvailability.Available:
        print(
            "pitchCompensationAvailability が Available ではありません "
            f"（実値: {availability.name}）。\n"
            "開発計画の判断ゲート 1 を満たさないため、P0-B は不合格候補です。\n"
            "Qt Multimedia 向けの実装をこれ以上進めず、mpv 昇格の判断を提示してください。",
            file=sys.stderr,
        )
        return 1

    source = speed_wav_path()
    toggle_ok = check_pitch_toggle(app, source)
    rate_ok = check_playback_rate(app, source)
    results = run_speed_measurements(app, source)

    print("=== Qt のログ（qInstallMessageHandler 経由） ===")
    if QT_MESSAGES:
        for message in QT_MESSAGES:
            print(f"  [{message.type_name}] {message.text}")
    else:
        print("  なし（警告・エラーともに出力されなかった）")
    print()

    failed = [r for r in results if not r.passed]
    print("=== 自動検証の判定 ===")
    print(f"  pitchCompensationAvailability : {availability.name}")
    print(f"  ピッチ補正の切替               : {'OK' if toggle_ok else 'NG'}")
    print(f"  playbackRate の設定             : {'OK' if rate_ok else 'NG'}")
    print(f"  速度計測                       : {len(results) - len(failed)}/{len(results)} 合格")
    if failed:
        for result in failed:
            detail = "; ".join(result.errors) if result.errors else "許容誤差を超過"
            print(
                f"    - {rate_label(result.rate)} 倍 / ピッチ補正 "
                f"{'ON' if result.pitch_compensation_requested else 'OFF'}: {detail}",
                file=sys.stderr,
            )
    print()
    print(
        "※ varispeed と time-stretch の実出力ピッチおよび音質は、"
        "自動検証では判定できません（手動確認待ち）。\n"
        "   --manual varispeed / --manual timestretch を人が実行して判定してください。"
    )

    return 0 if toggle_ok and rate_ok and not failed else 1


# --- 手動確認モード ------------------------------------------------------------


def prompt(message: str) -> None:
    """人による確認を待つ。"""
    input(f"    >>> {message}（Enter で次へ） ")


def play_for(
    app: QCoreApplication,
    player: QMediaPlayer,
    path: Path,
    rate: float,
    pitch_compensation: bool,
    seconds: float,
) -> None:
    """指定条件で一定時間だけ再生する。"""
    if not load_source(app, player, path):
        print(f"    読み込みに失敗: {path}", file=sys.stderr)
        return
    player.setPitchCompensation(pitch_compensation)
    player.setPlaybackRate(rate)
    player.play()
    pump(app, seconds)
    player.stop()
    player.setSource(QUrl())
    pump(app, 0.1)


def run_manual_audible(app: QCoreApplication, volume: float) -> int:
    """P0-A で保留していた可聴確認。代表的な形式と日本語パスを実際に鳴らす。"""
    print("=== 手動確認: 可聴確認（P0-A の保留分） ===")
    print("実際に音が鳴ります。440Hz の正弦波が正しい音程・音量で聞こえるか確認してください。")
    print()

    targets: list[tuple[str, Path]] = [
        ("WAV", TEST_AUDIO_DIR / "sine440.wav"),
        ("MP3", TEST_AUDIO_DIR / "sine440.mp3"),
        ("OGG Opus", TEST_AUDIO_DIR / "sine440.opus"),
        ("日本語・空白パス (WAV)", TEST_AUDIO_DIR / "日本語 ディレクトリ" / "テスト 音源 440Hz.wav"),
        ("日本語・空白パス (M4A)", TEST_AUDIO_DIR / "日本語 ディレクトリ" / "テスト 音源 440Hz.m4a"),
    ]

    player, _output = make_player(volume)
    for label, path in targets:
        print(f"  [Qt 再生] {label}: {path.name}（1.0 倍）")
        play_for(app, player, path, 1.0, True, 2.2)
        prompt(f"{label} は正しく聞こえましたか？")
    print()
    print("結果を docs/p0-report.md の P0-B 節へ記入してください。")
    return 0


def run_manual_varispeed(app: QCoreApplication, volume: float) -> int:
    """ピッチ補正 OFF。各倍率の Qt 再生と、期待ピッチの参照音を聴き比べる。"""
    print("=== 手動確認: varispeed（ピッチ補正 OFF） ===")
    print("Qt の再生音と、NumPy が生成した参照音を交互に鳴らします。")
    print("参照音は Qt の処理結果ではなく『期待値』です。混同しないでください。")
    print("参照音は必ず 1.0 倍で再生します。")
    print()

    player, _output = make_player(volume)
    source = speed_wav_path()

    for rate in RATES:
        expected_hz = reference_frequency(rate)
        print(f"--- {rate_label(rate)} 倍（{float(rate):.4f}）: 期待ピッチ {expected_hz:.0f}Hz ---")

        print(f"  [Qt 再生 / 検証対象] {source.name} @ {float(rate):.4f} 倍・ピッチ補正 OFF")
        play_for(app, player, source, float(rate), False, 4.0)

        reference = reference_wav_path(expected_hz)
        print(f"  [参照音源 / 期待値] {reference.name} @ 1.0 倍（NumPy 生成）")
        play_for(app, player, reference, 1.0, True, 3.0)

        prompt(f"{rate_label(rate)} 倍の Qt 再生音は、参照音 {expected_hz:.0f}Hz と同じ音程でしたか？")
        print()

    print("すべての倍率で音程が連動していれば、判断ゲート 3（varispeed）は合格です。")
    print("結果を docs/p0-report.md の P0-B 節へ記入してください。")
    return 0


def run_manual_timestretch(app: QCoreApplication, volume: float, source: Path | None) -> int:
    """ピッチ補正 ON。音程維持と音質を確認する。"""
    print("=== 手動確認: time-stretch（ピッチ補正 ON） ===")

    tone_source = speed_wav_path()
    material = source if source is not None else tone_source
    if source is None:
        print("警告: --source が指定されていないため、440Hz の正弦波で確認します。")
        print("      ロボット声・エコー感・うなりなどの音質評価には、")
        print("      会話音声や音楽を --source で指定することを強く推奨します。")
        print("      指定したファイルはリポジトリへコピーもコミットもしません。")
    else:
        print(f"素材: {material}（ユーザー所有。リポジトリへは一切保存しません）")
    print()

    player, _output = make_player(volume)
    reference = reference_wav_path(BASE_FREQ_HZ)

    print(f"  [参照音源 / 基準] {reference.name} @ 1.0 倍（NumPy 生成の 440Hz）")
    play_for(app, player, reference, 1.0, True, 3.0)
    prompt("基準の 440Hz を覚えてください")
    print()

    # 音程維持の確認は、音程が明確な正弦波で行う。
    print("--- 音程維持の確認（440Hz 正弦波・ピッチ補正 ON） ---")
    for rate in (Fraction(1, 2), Fraction(3, 4), Fraction(3, 2), Fraction(2, 1)):
        print(f"  [Qt 再生 / 検証対象] {tone_source.name} @ {float(rate):.2f} 倍・ピッチ補正 ON")
        play_for(app, player, tone_source, float(rate), True, 4.0)
        print(f"  [参照音源 / 期待値] {reference.name} @ 1.0 倍（440Hz のままであるべき）")
        play_for(app, player, reference, 1.0, True, 3.0)
        prompt(f"{rate} 倍でも 440Hz を保っていましたか？（変化していれば不合格）")
        print()

    print("--- 音質の確認 ---")
    for rate in (Fraction(1, 2), Fraction(3, 4), Fraction(3, 2), Fraction(2, 1)):
        print(f"  [Qt 再生 / 検証対象] {Path(material).name} @ {float(rate):.2f} 倍・ピッチ補正 ON")
        play_for(app, player, Path(material), float(rate), True, 8.0)
        prompt(f"{rate} 倍の音質を評価してください（ロボット声・金属的な揺れ・エコー感・うなり）")
        print()

    print("--- 操作中の挙動 ---")
    if not load_source(app, player, Path(material)):
        print("素材の読み込みに失敗しました", file=sys.stderr)
        return 1
    player.setPlaybackRate(1.5)
    player.setPitchCompensation(True)
    player.play()
    pump(app, 3.0)

    print("  再生中に ピッチ補正 ON → OFF へ切替")
    player.setPitchCompensation(False)
    pump(app, 3.0)
    print("  再生中に ピッチ補正 OFF → ON へ切替")
    player.setPitchCompensation(True)
    pump(app, 3.0)
    prompt("切替時にクリックノイズ・停止・音飛びはありませんでしたか？")

    print("  シーク（現在位置 + 5 秒）")
    player.setPosition(player.position() + 5000)
    pump(app, 4.0)
    prompt("シーク後も音質は保たれていましたか？")

    print("  一時停止 → 3 秒後に再開")
    player.pause()
    pump(app, 3.0)
    player.play()
    pump(app, 4.0)
    prompt("一時停止と再開は正常でしたか？")

    rate_before = player.playbackRate()
    pitch_before = player.pitchCompensation()
    player.stop()
    load_source(app, player, Path(material))
    pump(app, 0.3)
    print(
        f"  トラック再読み込み後の設定: playbackRate {rate_before} → {player.playbackRate()}, "
        f"pitchCompensation {pitch_before} → {player.pitchCompensation()}"
    )
    player.stop()
    player.setSource(QUrl())

    prompt("再読み込み後の設定保持の挙動を記録してください")
    print()
    print("結果を docs/p0-report.md の P0-B 節へ記入してください。")
    print("合格 / 条件付き合格 / 不合格 のいずれかと、自由記述を残してください。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P0-B: 再生速度とピッチ補正の検証（Qt Multimedia）",
    )
    parser.add_argument(
        "--manual",
        choices=("audible", "varispeed", "timestretch"),
        default=None,
        help="手動確認モード。指定しない場合は音を鳴らさない自動検証を行う",
    )
    parser.add_argument(
        "--volume",
        type=float,
        default=None,
        help="音量 0.0〜1.0。手動確認モードでは明示指定が必須（推奨 0.2）",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="time-stretch の音質評価に使うユーザー所有の音源。コピーもコミットもしない",
    )
    args = parser.parse_args(argv)

    if args.manual is None and args.volume not in (None, 0.0):
        parser.error("自動検証では音を鳴らしません。--volume は --manual と併用してください。")
    if args.manual is not None and (args.volume is None or args.volume <= 0.0):
        parser.error(
            "手動確認モードでは音を鳴らすため、--volume を明示してください（例: --volume 0.2）。"
        )
    if args.source is not None and args.manual != "timestretch":
        parser.error("--source は --manual timestretch でのみ使用します。")
    if args.source is not None and not args.source.exists():
        parser.error(f"--source のファイルが見つかりません: {args.source}")

    qInstallMessageHandler(_message_handler)
    app = QApplication(sys.argv[:1])
    ensure_generated_audio()

    if args.manual is None:
        return run_auto(app)

    volume: float = args.volume
    print(f"音量 {volume} で再生します。ヘッドホンの音量に注意してください。")
    print()
    if args.manual == "audible":
        return run_manual_audible(app, volume)
    if args.manual == "varispeed":
        return run_manual_varispeed(app, volume)
    return run_manual_timestretch(app, volume, args.source)


if __name__ == "__main__":
    sys.exit(main())
