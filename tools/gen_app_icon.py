"""`assets/sdp.ico` を生成する（開発ツール。sdp本体からは実行しない）。

**素材の出所とライセンス**: このアイコンはフォント・クリップアート・第三者素材を
一切使わず、本スクリプトの図形描画だけで生成する完全な自作物である。したがって
sdp本体と同じ MIT License で扱える（[LICENSE](../LICENSE)）。
外部からダウンロードした画像を混ぜてはならない。

意匠は「暗い角丸の背景 + 5本のイコライザーバー」。Pillow等の画像ライブラリへ
依存させないため、PNG（zlib）とICO（BITMAPINFOHEADER）を標準ライブラリだけで書く。
同じ入力から常に同じbyte列を出力する（生成物をcommitして差分を見られるようにする）。

    uv run python tools/gen_app_icon.py
"""

from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
"""ICOへ格納する解像度（Explorer・タスクバー・Alt+Tab・大アイコン表示をカバーする）。"""

SUPERSAMPLE = 4
"""各辺の分割数。二値の被覆率を平均してアンチエイリアスする。"""

_BACKGROUND_TOP = (0x22, 0x30, 0x3F)
_BACKGROUND_BOTTOM = (0x0E, 0x14, 0x1C)
_BAR_TOP = (0x7F, 0xE1, 0xFF)
_BAR_BOTTOM = (0x21, 0x8A, 0xD6)
# 中央を最も高くした左右非対称のバー高さ（音のレベル表示に見えるようにする）。
_BAR_HEIGHTS = (0.40, 0.68, 0.94, 0.58, 0.30)

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def render_rgba(size: int) -> bytes:
    """1辺 ``size`` のRGBA画像（上から下へ、行あたり ``size * 4`` byte）を作る。"""
    scale = size * SUPERSAMPLE
    # supersample面では各pixelが図形の内か外かの二値。平均すると被覆率になる。
    coverage = _render_supersampled(scale)
    return _downsample(coverage, scale, size)


def build_ico(sizes: tuple[int, ...] = ICON_SIZES) -> bytes:
    """複数解像度を1つのICOへまとめる。"""
    images: list[bytes] = []
    for size in sizes:
        rgba = render_rgba(size)
        # 256はPNG圧縮（Vista以降の慣例）、それ以下は互換性の高いDIBで格納する。
        images.append(_encode_png(size, rgba) if size >= 256 else _encode_dib(size, rgba))

    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(header) + 16 * len(sizes)
    directory = bytearray()
    for size, image in zip(sizes, images, strict=True):
        dimension = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(image), offset)
        offset += len(image)
    return header + bytes(directory) + b"".join(images)


def _render_supersampled(scale: int) -> tuple[bytearray, bytearray]:
    """背景と前景を合成した、premultiplied RGB と alpha を返す。"""
    premultiplied = bytearray(scale * scale * 3)
    alpha = bytearray(scale * scale)

    radius = scale * 0.22
    bars = _bar_rectangles(scale)

    for y in range(scale):
        vertical = y / max(scale - 1, 1)
        background = _mix(_BACKGROUND_TOP, _BACKGROUND_BOTTOM, vertical)
        row = y * scale
        for x in range(scale):
            if not _inside_rounded_rect(x + 0.5, y + 0.5, 0.0, 0.0, scale, scale, radius):
                continue
            color = background
            for left, top, right, bottom, bar_radius in bars:
                if _inside_rounded_rect(x + 0.5, y + 0.5, left, top, right, bottom, bar_radius):
                    color = _mix(_BAR_TOP, _BAR_BOTTOM, (y - top) / max(bottom - top, 1.0))
                    break
            index = row + x
            premultiplied[index * 3 : index * 3 + 3] = bytes(color)
            alpha[index] = 255
    return premultiplied, alpha


def _bar_rectangles(scale: int) -> tuple[tuple[float, float, float, float, float], ...]:
    """イコライザーバーの矩形（左, 上, 右, 下, 角丸半径）を返す。"""
    count = len(_BAR_HEIGHTS)
    margin = scale * 0.18
    inner = scale - 2 * margin
    pitch = inner / count
    width = pitch * 0.56
    center_y = scale / 2.0
    rectangles: list[tuple[float, float, float, float, float]] = []
    for index, height_ratio in enumerate(_BAR_HEIGHTS):
        center_x = margin + pitch * (index + 0.5)
        half_height = inner * height_ratio / 2.0
        rectangles.append(
            (
                center_x - width / 2.0,
                center_y - half_height,
                center_x + width / 2.0,
                center_y + half_height,
                width / 2.0,
            )
        )
    return tuple(rectangles)


def _inside_rounded_rect(
    x: float, y: float, left: float, top: float, right: float, bottom: float, radius: float
) -> bool:
    """角丸矩形の内側かどうか。"""
    if not (left <= x <= right and top <= y <= bottom):
        return False
    dx = max(left + radius - x, 0.0, x - (right - radius))
    dy = max(top + radius - y, 0.0, y - (bottom - radius))
    return dx * dx + dy * dy <= radius * radius


def _downsample(coverage: tuple[bytearray, bytearray], scale: int, size: int) -> bytes:
    """supersample面をRGBA（非premultiplied）へ縮小する。"""
    premultiplied, alpha = coverage
    factor = SUPERSAMPLE
    samples = factor * factor
    result = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            red = green = blue = opacity = 0
            for sub_y in range(factor):
                base = (y * factor + sub_y) * scale + x * factor
                for sub_x in range(factor):
                    index = base + sub_x
                    opacity += alpha[index]
                    red += premultiplied[index * 3]
                    green += premultiplied[index * 3 + 1]
                    blue += premultiplied[index * 3 + 2]
            covered = opacity // 255
            target = (y * size + x) * 4
            if covered == 0:
                continue
            result[target] = red // covered
            result[target + 1] = green // covered
            result[target + 2] = blue // covered
            result[target + 3] = opacity // samples
    return bytes(result)


def _mix(start: tuple[int, int, int], end: tuple[int, int, int], ratio: float) -> tuple[int, ...]:
    """2色を線形補間する。"""
    clamped = min(max(ratio, 0.0), 1.0)
    return tuple(round(a + (b - a) * clamped) for a, b in zip(start, end, strict=True))


def _encode_png(size: int, rgba: bytes) -> bytes:
    """RGBA画像を8bit truecolor+alphaのPNGへ符号化する。"""
    stride = size * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(size))
    chunks = [
        _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)),
        _png_chunk(b"IDAT", zlib.compress(raw, 9)),
        _png_chunk(b"IEND", b""),
    ]
    return _PNG_SIGNATURE + b"".join(chunks)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _encode_dib(size: int, rgba: bytes) -> bytes:
    """ICO内へ置く32bit BGRA DIB（BITMAPINFOHEADER + XOR + ANDマスク）。"""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, size * size * 4, 0, 0, 0, 0)
    rows: list[bytes] = []
    for y in range(size - 1, -1, -1):  # DIBは下から上へ格納する
        row = bytearray()
        for x in range(size):
            index = (y * size + x) * 4
            row += bytes((rgba[index + 2], rgba[index + 1], rgba[index], rgba[index + 3]))
        rows.append(bytes(row))
    # alphaを持つ32bit DIBでもANDマスクは必須。全て0（不透明扱い）にする。
    mask_stride = ((size + 31) // 32) * 4
    mask = bytes(mask_stride * size)
    return header + b"".join(rows) + mask


def main(argv: list[str] | None = None) -> int:
    """ICOを書き出す。"""
    default_output = Path(__file__).resolve().parents[1] / "assets" / "sdp.ico"
    parser = argparse.ArgumentParser(description="sdpのアプリアイコン（ICO）を生成します。")
    parser.add_argument("output", type=Path, nargs="?", default=default_output)
    arguments = parser.parse_args(argv)

    payload = build_ico()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_bytes(payload)
    print(f"アイコンを生成しました: {arguments.output.name}（{len(payload):,} byte）")
    print(f"  解像度: {', '.join(f'{size}x{size}' for size in ICON_SIZES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
