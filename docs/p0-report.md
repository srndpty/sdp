# P0 技術検証レポート

[開発計画 §1.3](./development-plan.md#13-未検証事項p0-で検証結果次第で設計変更) の
未検証事項（U1〜U8）を実機で確認し、結果を記録する。
再生エンジンの判定基準は [ADR-0001](./adr/0001-playback-engine.md) を参照。

進行状況:

| 区分 | 内容 | 状態 |
|---|---|---|
| P0 準備 | 開発環境の確認とテスト音源の生成 | 完了 |
| P0-A | Qt Multimedia 基本検証（形式・シーク・終了通知・日本語パス） | 未実施 |
| P0-B | 速度とピッチ補正（U1、U2） | 未実施 |
| P0-C | PCM 取得と可視化（U3、U5、U7 の一部） | 未実施 |
| P0-D | exe 化後の動作（U7） | 未実施 |

---

## 1. 開発環境（P0 準備）

記録日: 2026-07-28 / OS: Windows 11 Home 10.0.26200

### 1.1 FFmpeg CLI の位置づけ（重要）

FFmpeg CLI は**テスト音源生成専用の開発ツール**であり、以下を厳守する。

- `pyproject.toml` の依存関係へ追加しない。
- sdp 本体から実行しない。
- PyInstaller 成果物へ同梱しない。
- Qt Multimedia が内部で用いる FFmpeg バックエンドとは**別物**として扱う。
- **本節の FFmpeg CLI での成功は、Qt Multimedia の形式対応の証拠にはならない。**
  Qt Multimedia の対応状況は P0-A 以降で別途検証する。

### 1.2 実行パス

```
> where.exe ffmpeg
C:\tools\ffmpeg.exe

> where.exe ffprobe
C:\tools\ffprobe.exe
```

PATH 上に単一の実体のみが存在し、重複導入はない。

### 1.3 バージョン

`ffmpeg -version` と `ffprobe -version` は同一ビルド。

```
ffmpeg version  N-112450-gab95338a20-20231012
ffprobe version N-112450-gab95338a20-20231012
built with gcc 13.2.0 (crosstool-NG 1.25.0.232_c175b21)

libavutil      58. 27.100
libavcodec     60. 30.102
libavformat    60. 15.100
libavdevice    60.  2.101
libavfilter     9. 11.100
libswscale      7.  4.100
libswresample   4. 11.100
libpostproc    57.  2.100
```

ビルド構成のうち本プロジェクトに関係する主なもの:
`--enable-gpl --enable-version3 --enable-libmp3lame --enable-libvorbis --enable-libopus
--enable-libsoxr --enable-librubberband --disable-libfdk-aac`

- `--disable-libfdk-aac` のため、AAC エンコードには FFmpeg 内蔵の `aac` エンコーダーを使う。
  テスト音源用途としては十分。
- `librubberband` と `libsoxr` が有効だが、これらは **FFmpeg CLI 側の機能**であり、
  sdp の再生速度・ピッチ補正の実現手段とは無関係。混同しないこと。

### 1.4 必要なエンコーダーの確認

`ffmpeg -hide_banner -encoders` の出力（全 234 件）のうち、本プロジェクトで使うもの。

| 対象形式 | エンコーダー | 出力行 |
|---|---|---|
| WAV | pcm_s16le | `A....D pcm_s16le  PCM signed 16-bit little-endian` |
| MP3 | libmp3lame | `A....D libmp3lame libmp3lame MP3 (MPEG audio layer 3) (codec mp3)` |
| OGG Vorbis | libvorbis | `A....D libvorbis  libvorbis (codec vorbis)` |
| OGG Opus | libopus | `A....D libopus    libopus Opus (codec opus)` |
| FLAC | flac | `A....D flac       FLAC (Free Lossless Audio Codec)` |
| M4A/AAC | aac | `A....D aac        AAC (Advanced Audio Coding)` |

必要な 6 種類がすべて利用可能。
（`aac_mf` = MediaFoundation 経由の AAC も存在するが使用しない。）

---

## 2. テスト音源の生成と検証（P0 準備）

生成スクリプト: [`tools/gen_test_audio.py`](../tools/gen_test_audio.py)
出力先: `assets/test_audio/`

### 2.1 生成方針

- 音源はすべて自己作成（著作権上の問題がない）。WAV を NumPy で生成し、
  他形式は FFmpeg CLI で変換する。
- 音源は 3 種類。いずれも 44.1kHz / ステレオ / 2.00 秒。
  - `sine440` — 440Hz 正弦波。左右で振幅を変えてある（左 0.5 / 右 0.25）ため、
    ステレオ→mono 変換やチャンネル処理の検証にも使える。
  - `sweep` — 100Hz→10kHz の対数スイープ。スペクトラム表示の目視確認用。
  - `silence` — 完全な無音。無音時の減衰表示やレベルメーターの検証用。
- 日本語・空白を含むパスの検証（NF-04）のため、
  `assets/test_audio/日本語 ディレクトリ/テスト 音源 440Hz.<拡張子>` を全形式で用意した。
  **ディレクトリ名にも**日本語と空白を含めてある。
- スクリプトは起動時に `ffmpeg` と `ffprobe` の存在を確認し、
  不在の場合は導入方法を含む日本語エラーを表示して終了コード 1 で終わる。
  **別実装への silent fallback は設けていない**（[AGENTS.md](../AGENTS.md) の方針）。
  PATH から FFmpeg を外した状態で、この経路が意図どおり動作することを確認済み。

### 2.2 ffprobe による検証結果

全 24 ファイルについて、コンテナ形式・音声コーデック・サンプルレート・チャンネル数・
おおよその再生時間を検証し、**すべて合格**した（`gen_test_audio.py` は終了コード 0）。

| 拡張子 | コンテナ（format_name） | コーデック | サンプルレート | ch | 長さ | サイズの目安 |
|---|---|---|---|---|---|---|
| `.wav` | `wav` | `pcm_s16le` | 44100 Hz | 2 | 2.00s | 344.6 KB |
| `.mp3` | `mp3` | `mp3` | 44100 Hz | 2 | 2.04s | 48.4 KB |
| `.ogg` | `ogg` | `vorbis` | 44100 Hz | 2 | 2.00s | 4.5〜10.7 KB |
| `.opus` | `ogg` | `opus` | **48000 Hz** | 2 | 2.01s | 0.6〜42.3 KB |
| `.flac` | `flac` | `flac` | 44100 Hz | 2 | 2.00s | 8.4〜56.3 KB |
| `.m4a` | `mov,mp4,m4a,3gp,3g2,mj2` | `aac` | 44100 Hz | 2 | 2.00s | 1.7〜48.4 KB |

記録しておくべき点:

- **Opus は 48000Hz になる。** Opus は 48kHz 系のみを扱う仕様のため、ffmpeg が自動的に
  リサンプルする。可視化やテストで「入力ファイルのサンプルレート = 44100」を
  前提にしてはならない。
- **MP3 は約 2.04 秒**とわずかに長い。エンコーダー遅延とパディングによるもの。
  再生時間の検証には許容誤差（本スクリプトでは ±0.35 秒）が必要。
  完全なギャップレス再生を初期スコープ外としている判断とも整合する。
- `.opus` の ffprobe 上のコンテナ名は `opus` ではなく `ogg`。
- `.m4a` の `format_name` は複数名（`mov,mp4,m4a,3gp,3g2,mj2`）が返るため、
  完全一致ではなくカンマ区切りの集合として判定する必要がある。

### 2.3 生成物の扱い

`assets/test_audio/` の音源はリポジトリへコミットする。理由は、テスト実行に
FFmpeg CLI の導入を必須にしたくないため。合計約 2.3MB で、
最大ファイル（WAV 344.6KB）は pre-commit の `check-added-large-files`（1024KB）内に収まる。

---

## 3. P0-A: Qt Multimedia 基本検証

検証スクリプト: [`spike/p0a_basic_playback.py`](../spike/p0a_basic_playback.py)
対応する開発計画の P0 項目: 1（形式）・4（シーク）・5（終了通知）・10（日本語・空白パス）

### 3.1 実行環境

| 項目 | 値 |
|---|---|
| PySide6 / Qt | 6.10.3 |
| `QT_MEDIA_BACKEND` | 未設定（既定のまま） |
| 既定の音声出力デバイス | Realtek Digital Output (Realtek(R) Audio) |
| 音声出力デバイス数 | 1 |
| 実行時の音量 | 0.0（無音。デコードと再生パイプラインは動作する） |

実行中に FFmpeg 形式の診断ログ（`Input #0, mov,mp4,m4a,... Stream #0:0: Audio: aac (LC) ...`）が
標準エラーへ出力された。これにより **Qt Multimedia が同梱の FFmpeg バックエンドで
デコードしていること**が実測で確認できた。これは §1 の FFmpeg CLI とは別物である。

### 3.2 結果（全 12 件合格、終了コード 0）

`sine440.<拡張子>`（ASCII パス）と
`日本語 ディレクトリ/テスト 音源 440Hz.<拡張子>`（日本語＋空白のディレクトリ名・ファイル名）
の各 6 形式。

| 対象 | 読込 | duration | 位置前進 | シーク（目標→実測） | EndOfMedia | 判定 |
|---|---|---|---|---|---|---|
| ASCII `.wav` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| ASCII `.mp3` | OK | 2037ms | OK | 1437→1437ms | OK | 合格 |
| ASCII `.ogg` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| ASCII `.opus` | OK | 2006ms | OK | 1406→1406ms | OK | 合格 |
| ASCII `.flac` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| ASCII `.m4a` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| 日本語 `.wav` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| 日本語 `.mp3` | OK | 2037ms | OK | 1437→1437ms | OK | 合格 |
| 日本語 `.ogg` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| 日本語 `.opus` | OK | 2006ms | OK | 1406→1406ms | OK | 合格 |
| 日本語 `.flac` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |
| 日本語 `.m4a` | OK | 2000ms | OK | 1400→1400ms | OK | 合格 |

### 3.3 わかったこと

- **U4（形式対応）は肯定的**。Windows 版 PySide6 6.10.3 同梱の FFmpeg バックエンドで
  WAV / MP3 / OGG Vorbis / OGG Opus / FLAC / M4A(AAC) をすべて読み込み・再生できた。
- **U6（シークと終了通知）は肯定的**。全形式で `EndOfMedia` が届き、
  シーク後も終端まで再生が継続して終了通知に至った。
- **NF-04（日本語・空白パス）は問題なし**。ディレクトリ名とファイル名の双方に
  日本語と空白を含む場合でも、`QUrl.fromLocalFile` 経由で正しく再生できた。
- Qt が返す duration は ffprobe の実測と整合する（MP3 は 2037ms、Opus は 2006ms）。
  §2.2 で記録したエンコーダー由来の長さのずれが、再生側にもそのまま現れている。

### 3.4 未解決・要注意（P0-A では確定できなかったこと）

- **シーク精度は「見かけ上」一致に過ぎない可能性がある。**
  シーク後の `position()` が目標値と 1ms も違わずに一致した。これは Qt が
  要求値をそのまま返しているだけで、実際のデコード位置とは異なる可能性がある。
  実位置の確認は、PCM を取得できる **P0-C で波形の位相・内容を突き合わせて判定する**。
- **可聴確認は未実施。** 本検証は音量 0.0 で実行しており、
  「デコードと再生パイプラインが動作すること」は確認できたが、
  「実際に正しい音が鳴ること」は人の耳による確認が必要。
  `--volume 0.2` を付けて手動実行する項目として残す。
- `.opus` の再生開始時に FFmpeg の警告
  `[opus] Could not update timestamps for discarded samples` が出る。
  再生・シーク・終了通知はいずれも正常だったため実害は確認されていないが、
  Opus のプリスキップに関する既知の警告として記録しておく。
- 本検証は音声出力デバイスが 1 つ（Realtek Digital Output）の環境で行った。
  デバイス切り替え時の挙動は未検証。

### 3.5 P0-A の判定

**合格。** ADR-0001 の合格基準のうち「3. P0 検証項目に致命的な欠陥がないこと」について、
基本再生の範囲では欠陥は見つからなかった。

ただし ADR-0001 の判定を左右するのは**基準 1・2（ピッチ補正の切替と音質）**であり、
これは次の P0-B で検証する。**現時点で Qt Multimedia の採用は確定していない。**

---

## 4. 判定と次の行動

（P0-B〜P0-D 完了後に記入する。ADR-0001 の状態更新もここで行う。）

次の作業: **P0-B（速度とピッチ補正、U1・U2）**。
`QMediaPlayer.playbackRate` による 0.5〜2.0 倍再生と、
ピッチ補正関連 API の存在確認および varispeed / time-stretch の音質判定を行う。
