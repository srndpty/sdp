"""テスト音源生成スクリプト（開発ツールであり sdp 本体の一部ではない）。

自己作成した短い音源（正弦波・スイープ・無音）を WAV として生成し、
FFmpeg CLI で MP3 / OGG Vorbis / OGG Opus / FLAC / M4A(AAC) へ変換したうえ、
ffprobe で コンテナ形式・音声コーデック・サンプルレート・チャンネル数・再生時間
を検証する。

FFmpeg CLI の位置づけ:

- テスト音源生成専用の開発ツールであり、pyproject.toml の依存関係には含めない。
- sdp 本体から実行しない。PyInstaller 成果物にも同梱しない。
- Qt Multimedia が内部で用いる FFmpeg バックエンドとは別物である。
  本スクリプトの成功は Qt Multimedia の形式対応の証拠にはならない。
  Qt Multimedia の対応状況は P0 の再生検証で別途確認する。

使い方:

    uv run python tools/gen_test_audio.py
    uv run python tools/gen_test_audio.py --output-dir assets/test_audio
"""

import argparse
import json
import shutil
import subprocess
import sys
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

# 音源の基本パラメータ。短時間で扱いやすく、かつ再生時間の検証が意味を持つ長さにする。
SAMPLE_RATE = 44100
DURATION_SEC = 2.0
CHANNELS = 2

# ffprobe が返す再生時間の許容誤差（秒）。
# MP3 や AAC はエンコーダ遅延とパディングにより元の長さから僅かにずれる。
DURATION_TOLERANCE_SEC = 0.35

# 日本語と空白を含むパスの検証用（NF-04）。ディレクトリ名にも空白と日本語を含める。
JAPANESE_DIR_NAME = "日本語 ディレクトリ"
JAPANESE_FILE_STEM = "テスト 音源 440Hz"


class FFmpegNotFoundError(RuntimeError):
    """FFmpeg CLI（ffmpeg / ffprobe）が見つからない場合に送出する。"""


class TestAudioGenerationError(RuntimeError):
    """音源の生成または検証に失敗した場合に送出する。"""


@dataclass(frozen=True)
class SourceSpec:
    """WAV として生成する音源の定義。"""

    stem: str
    make_samples: Callable[[], NDArray[np.float32]]


@dataclass(frozen=True)
class FormatSpec:
    """変換先フォーマットと、ffprobe による期待値の定義。"""

    suffix: str
    ffmpeg_args: tuple[str, ...]
    expected_container: str
    expected_codec: str
    expected_sample_rate: int


# Opus は 48kHz 系のみを扱うため、ffmpeg が自動的にリサンプルする。
FORMATS: tuple[FormatSpec, ...] = (
    FormatSpec(".wav", (), "wav", "pcm_s16le", SAMPLE_RATE),
    FormatSpec(".mp3", ("-c:a", "libmp3lame", "-b:a", "192k"), "mp3", "mp3", SAMPLE_RATE),
    FormatSpec(".ogg", ("-c:a", "libvorbis", "-q:a", "5"), "ogg", "vorbis", SAMPLE_RATE),
    FormatSpec(".opus", ("-c:a", "libopus", "-b:a", "128k"), "ogg", "opus", 48000),
    FormatSpec(".flac", ("-c:a", "flac"), "flac", "flac", SAMPLE_RATE),
    FormatSpec(".m4a", ("-c:a", "aac", "-b:a", "192k"), "mp4", "aac", SAMPLE_RATE),
)


def _time_axis() -> NDArray[np.float64]:
    """サンプル時刻の配列（秒）を返す。"""
    count = int(SAMPLE_RATE * DURATION_SEC)
    return np.arange(count, dtype=np.float64) / SAMPLE_RATE


def _to_stereo(left: NDArray[np.float64], right: NDArray[np.float64]) -> NDArray[np.float32]:
    """左右チャンネルを (サンプル数, 2) の float32 配列へまとめる。"""
    return np.stack((left, right), axis=1).astype(np.float32)


def make_sine() -> NDArray[np.float32]:
    """440Hz の正弦波。左右で振幅を変え、ステレオ→mono 変換の検証にも使えるようにする。"""
    t = _time_axis()
    wave_440 = np.sin(2.0 * np.pi * 440.0 * t)
    return _to_stereo(wave_440 * 0.5, wave_440 * 0.25)


def make_sweep() -> NDArray[np.float32]:
    """100Hz から 10kHz への対数スイープ。スペクトラム表示の目視確認に使う。"""
    t = _time_axis()
    f_start, f_end = 100.0, 10_000.0
    ratio = f_end / f_start
    phase = (
        2.0 * np.pi * f_start * DURATION_SEC / np.log(ratio) * (ratio ** (t / DURATION_SEC) - 1.0)
    )
    swept = np.sin(phase)
    return _to_stereo(swept * 0.5, swept * 0.5)


def make_silence() -> NDArray[np.float32]:
    """完全な無音。無音時の減衰表示やレベルメーターの検証に使う。"""
    t = _time_axis()
    zeros = np.zeros_like(t)
    return _to_stereo(zeros, zeros)


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec("sine440", make_sine),
    SourceSpec("sweep", make_sweep),
    SourceSpec("silence", make_silence),
)


def resolve_ffmpeg_tools() -> tuple[str, str]:
    """ffmpeg と ffprobe の実行パスを解決する。

    見つからない場合は導入方法を含むエラーを送出する。
    別実装へのフォールバックは行わない。
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if path is None]
    if missing:
        raise FFmpegNotFoundError(
            "FFmpeg CLI が見つかりません: " + " と ".join(missing) + "\n"
            "\n"
            "テスト音源の生成には FFmpeg CLI（ffmpeg と ffprobe）が必要です。\n"
            "インストール例:\n"
            "    winget install --id Gyan.FFmpeg -e\n"
            "  もしくは https://www.gyan.dev/ffmpeg/builds/ から取得し、\n"
            "  ffmpeg.exe と ffprobe.exe を含むディレクトリを PATH へ追加してください。\n"
            "\n"
            "インストール後、新しいターミナルで次が動作することを確認してください:\n"
            "    ffmpeg -version\n"
            "    ffprobe -version\n"
            "\n"
            "FFmpeg CLI はテスト音源生成用の開発ツールです。"
            "sdp 本体からは実行しないため、実行環境への導入は開発時のみ必要です。"
        )
    # missing が空であれば両方とも None ではない。
    assert ffmpeg is not None and ffprobe is not None
    return ffmpeg, ffprobe


def write_wav(path: Path, samples: NDArray[np.float32]) -> None:
    """float32 の [-1.0, 1.0] のサンプルを 16bit PCM の WAV として書き出す。"""
    clipped = np.clip(samples, -1.0, 1.0)
    as_int16 = (clipped * 32767.0).astype(np.int16)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(2)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(as_int16.tobytes())


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """外部コマンドを実行する。

    ffprobe の JSON 出力には日本語のファイル名が含まれるため、
    Windows の既定ロケール（cp932）ではなく UTF-8 で復号する。
    """
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def convert(ffmpeg: str, source_wav: Path, destination: Path, args: tuple[str, ...]) -> None:
    """ffmpeg で WAV から目的のフォーマットへ変換する。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_wav)]
    command.extend(args)
    command.append(str(destination))
    completed = _run(command)
    if completed.returncode != 0:
        raise TestAudioGenerationError(
            f"ffmpeg による変換に失敗しました: {destination.name}\n"
            f"終了コード: {completed.returncode}\n"
            f"標準エラー出力:\n{completed.stderr.strip()}"
        )


def probe(ffprobe: str, path: Path) -> dict[str, Any]:
    """ffprobe でコンテナとストリームの情報を JSON として取得する。"""
    command = [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-select_streams",
        "a:0",
        str(path),
    ]
    completed = _run(command)
    if completed.returncode != 0:
        raise TestAudioGenerationError(
            f"ffprobe による解析に失敗しました: {path.name}\n"
            f"終了コード: {completed.returncode}\n"
            f"標準エラー出力:\n{completed.stderr.strip()}"
        )
    parsed: dict[str, Any] = json.loads(completed.stdout)
    return parsed


def verify(ffprobe: str, path: Path, spec: FormatSpec) -> list[str]:
    """生成物を ffprobe で検証し、問題点の一覧を返す（空なら合格）。"""
    info = probe(ffprobe, path)
    problems: list[str] = []

    container_format: dict[str, Any] = info.get("format", {})
    streams: list[dict[str, Any]] = info.get("streams", [])
    if not streams:
        return [f"{path.name}: 音声ストリームが見つかりません"]
    stream = streams[0]

    # コンテナ形式。ffprobe は "mov,mp4,m4a,3gp,3g2,mj2" のように複数名を返すことがある。
    container_names = str(container_format.get("format_name", "")).split(",")
    if spec.expected_container not in container_names:
        problems.append(
            f"コンテナ形式が期待と異なります（期待: {spec.expected_container} / "
            f"実際: {container_format.get('format_name')}）"
        )

    # 音声コーデック
    codec_name = str(stream.get("codec_name", ""))
    if codec_name != spec.expected_codec:
        problems.append(
            f"音声コーデックが期待と異なります（期待: {spec.expected_codec} / 実際: {codec_name}）"
        )

    # サンプルレート
    sample_rate = int(stream.get("sample_rate", 0))
    if sample_rate != spec.expected_sample_rate:
        problems.append(
            f"サンプルレートが期待と異なります（期待: {spec.expected_sample_rate} / "
            f"実際: {sample_rate}）"
        )

    # チャンネル数
    channels = int(stream.get("channels", 0))
    if channels != CHANNELS:
        problems.append(f"チャンネル数が期待と異なります（期待: {CHANNELS} / 実際: {channels}）")

    # おおよその再生時間
    duration = float(container_format.get("duration", 0.0))
    if abs(duration - DURATION_SEC) > DURATION_TOLERANCE_SEC:
        problems.append(
            f"再生時間が期待と異なります（期待: 約 {DURATION_SEC:.2f} 秒 ± "
            f"{DURATION_TOLERANCE_SEC:.2f} / 実際: {duration:.3f} 秒）"
        )

    return [f"{path.name}: {problem}" for problem in problems]


def describe(ffprobe: str, path: Path) -> str:
    """検証結果の表示用に、実測値を 1 行へまとめる。"""
    info = probe(ffprobe, path)
    container_format: dict[str, Any] = info.get("format", {})
    streams: list[dict[str, Any]] = info.get("streams", [])
    stream = streams[0] if streams else {}
    size_kb = path.stat().st_size / 1024.0
    return (
        f"{container_format.get('format_name', '?'):<28} "
        f"{stream.get('codec_name', '?'):<10} "
        f"{stream.get('sample_rate', '?'):>6} Hz  "
        f"{stream.get('channels', '?')}ch  "
        f"{float(container_format.get('duration', 0.0)):5.2f}s  "
        f"{size_kb:7.1f} KB"
    )


def generate_all(output_dir: Path, ffmpeg: str, ffprobe: str) -> tuple[list[Path], list[str]]:
    """全音源・全フォーマットを生成し、生成物と問題点の一覧を返す。"""
    generated: list[Path] = []
    problems: list[str] = []

    for source in SOURCES:
        wav_path = output_dir / f"{source.stem}.wav"
        write_wav(wav_path, source.make_samples())
        generated.append(wav_path)

        for spec in FORMATS:
            if spec.suffix == ".wav":
                continue
            destination = output_dir / f"{source.stem}{spec.suffix}"
            convert(ffmpeg, wav_path, destination, spec.ffmpeg_args)
            generated.append(destination)

    # 日本語と空白を含むディレクトリ・ファイル名の版（NF-04 の検証用）。
    japanese_dir = output_dir / JAPANESE_DIR_NAME
    for spec in FORMATS:
        source_path = output_dir / f"sine440{spec.suffix}"
        destination = japanese_dir / f"{JAPANESE_FILE_STEM}{spec.suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        generated.append(destination)

    for path in generated:
        spec = next(fmt for fmt in FORMATS if fmt.suffix == path.suffix)
        problems.extend(verify(ffprobe, path, spec))

    return generated, problems


def main(argv: list[str] | None = None) -> int:
    """テスト音源を生成し、ffprobe で検証する。"""
    parser = argparse.ArgumentParser(
        description="sdp のテスト音源を生成し、ffprobe で検証する開発ツール。"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "test_audio",
        help="出力先ディレクトリ（既定: assets/test_audio）",
    )
    args = parser.parse_args(argv)
    output_dir: Path = args.output_dir

    ffmpeg, ffprobe = resolve_ffmpeg_tools()
    print(f"ffmpeg : {ffmpeg}")
    print(f"ffprobe: {ffprobe}")
    print(f"出力先 : {output_dir}")
    print()

    generated, problems = generate_all(output_dir, ffmpeg, ffprobe)

    print(
        f"{'ファイル':<44} {'コンテナ':<28} {'コーデック':<10} {'レート':>9}  ch   長さ     サイズ"
    )
    for path in generated:
        relative = path.relative_to(output_dir)
        print(f"{relative!s:<44} {describe(ffprobe, path)}")
    print()

    if problems:
        print(f"検証に失敗しました（{len(problems)} 件）:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"{len(generated)} 個のテスト音源を生成し、ffprobe による検証に成功しました。")
    return 0


if __name__ == "__main__":
    # 環境不備（FFmpeg 未導入）と生成失敗は利用者が対処できる失敗のため、
    # traceback ではなくメッセージのみを表示する。それ以外の例外はそのまま送出する。
    try:
        sys.exit(main())
    except (FFmpegNotFoundError, TestAudioGenerationError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
