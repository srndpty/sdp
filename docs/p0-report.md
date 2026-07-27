# P0 技術検証レポート

[開発計画 §1.3](./development-plan.md#13-未検証事項p0-で検証結果次第で設計変更) の
未検証事項（U1〜U8）を実機で確認し、結果を記録する。
再生エンジンの判定基準は [ADR-0001](./adr/0001-playback-engine.md) を参照。

進行状況:

| 区分 | 内容 | 状態 |
|---|---|---|
| P0 準備 | 開発環境の確認とテスト音源の生成 | 完了 |
| P0-A | Qt Multimedia 基本検証（形式・シーク・終了通知・日本語パス） | 未実施 |
| P0-B | 速度とピッチ補正（U1、U2） | 自動検証は完了・**手動聴感確認待ち** |
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

## 4. P0-B: 再生速度とピッチ補正の検証

検証スクリプト: [`spike/p0b_speed_pitch.py`](../spike/p0b_speed_pitch.py)
対応する開発計画の P0 項目: 2（速度）・3（ピッチ補正 ON/OFF）／未検証事項: U1・U2

### 4.0 前提（混同してはならないこと）

- **QAudioBufferOutput は本検証で一切使用していない。** Qt の仕様上、QAudioBufferOutput が
  出力する PCM は現在の playbackRate に応じて伸縮された後の音声ではないため、
  そこから varispeed / time-stretch の実出力ピッチを判定することはできない。
- 本検証で書き出した比較音源は、**NumPy で生成した「期待値となる参照音源」だけ**である。
  Qt Multimedia による処理後の音声ではない。
- したがって**実出力のピッチと音質は自動検証では判定できず、人の可聴確認でのみ判定する**。

### 4.1 実行環境

| 項目 | 値 |
|---|---|
| Python | 3.13.11 |
| PySide6 | 6.10.3 |
| Qt | 6.10.3 |
| `QT_MEDIA_BACKEND` | 未設定（既定） |
| バックエンド診断 | Qt ログ: `Using Qt multimedia with FFmpeg version 7.1.3 LGPL version 2.1 or later` |
| 既定の音声出力 | ヘッドホン (WH-XB910N)（P0-A 実施時は Realtek Digital Output） |

バックエンドが FFmpeg 7.1.3 であることが Qt 自身のログで明示された。
これは §1 の開発用 FFmpeg CLI（6.0 系相当の 2023-10-12 ビルド）とは**別の実体**である。

既定の音声出力デバイスが P0-A の実行時から変わっているが、
後述の経過時間の実測はいずれも誤差 0.5% 以内であり、計測への影響は見られなかった。

**注意すべき発見**: `QMediaFormat.supportedAudioCodecs(Decode)` が返したのは
`MP3, AAC, AC3, EAC3, FLAC, Wave, WMA, ALAC` で、**Vorbis と Opus が含まれていない**。
しかし P0-A では `.ogg`（Vorbis）と `.opus` の再生に成功している。
すなわち **`QMediaFormat` の列挙は実際のデコード可否の判定に使えない**。
開発計画 PLAY-12 の「拡張子だけで対応可否を断定しない」方針に加えて、
**`QMediaFormat` の列挙でも断定できない**ことを設計に反映する必要がある。
実際の可否は「読み込んでみて `errorOccurred` / `InvalidMedia` を見る」方式で判定する。

### 4.2 ピッチ補正 API（U1）

API の存在確認（`hasattr(QMediaPlayer, ...)`）は次のとおり、**すべて True**。

| 名前 | 結果 |
|---|---|
| `pitchCompensation` | True |
| `setPitchCompensation` | True |
| `pitchCompensationAvailability` | True |
| `pitchCompensationChanged` | True |
| `playbackRate` | True |
| `setPlaybackRate` | True |
| `playbackRateChanged` | True |

**`pitchCompensationAvailability()` の実値: `Available`**
（`AlwaysOn` ではなく、`Unavailable` でもない。すなわち ON/OFF を切り替えられる。）

**pitchCompensation の初期値: `True`**（source 未設定時・音源読み込み後のいずれも True）。
つまり Qt の既定はピッチ維持（time-stretch）である。

ON/OFF の切替結果（`pitchCompensationChanged` の通知内容を併記）:

| 状態 | 設定 | 実値 | 判定 | 通知 |
|---|---|---|---|---|
| 再生前 | False | False | OK | `[False]` |
| 再生前 | True | True | OK | `[True]` |
| 再生中 | False | False | OK | `[False]` |
| 再生中 | True | True | OK | `[True]` |
| 一時停止中 | False | False | OK | `[False]` |
| 一時停止中 | True | True | OK | `[True]` |

再生前・再生中・一時停止中のいずれでも設定値が実際に切り替わり、
そのつど `pitchCompensationChanged` が 1 回だけ発火した。`errorOccurred` は発生していない。

### 4.3 playbackRate の設定（U1）

**初期値は `1.0`**（source 未設定時・読み込み後とも）。

| 状態 | 倍率 | 実値 | 判定 | `playbackRateChanged` |
|---|---|---|---|---|
| 再生前 | 0.5 | 0.5000 | OK | `[0.5]` |
| 再生前 | 0.75 | 0.7500 | OK | `[0.75]` |
| 再生前 | 1.0 | 1.0000 | OK | `[1.0]` |
| 再生前 | 45/33 | 1.3636 | OK | `[1.363636]` |
| 再生前 | 1.25 | 1.2500 | OK | `[1.25]` |
| 再生前 | 1.5 | 1.5000 | OK | `[1.5]` |
| 再生前 | 2.0 | 2.0000 | OK | `[2.0]` |
| 再生中 | 0.5 / 2.0 / 1.0 | 同上 | OK | 各 1 回 |
| 一時停止中 | 0.75 / 1.5 | 同上 | OK | 各 1 回 |

**発見: `QMediaPlayer.playbackRate` は float32 精度で保持される。**
検証中に 45/33 倍だけが厳密比較で不一致になったため精密に測ったところ、
設定値 `1.3636363636363635`（float64）に対し取得値は `1.3636363744735718` で、
これは `float(numpy.float32(45/33))` と厳密に一致した。
0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0 のように float32 で厳密に表せる値は往復一致する。

設計への影響:

- **playbackRate を厳密比較してはならない。** 相対 1e-6 程度の許容で比較する。
- **UI が要求した倍率の真値は UI 側（PlaybackController）が保持する。**
  バックエンドから読み戻した値を正とすると、`45/33` のような倍率で表示が揺れる。
- 実用上の音への影響は相対 1e-8 であり、無視できる。

### 4.4 再生速度の実測（経過時間）

計測方法: 自己生成した 10 秒・44.1kHz・ステレオの 440Hz WAV を使用。
**position が最初に進んだ時点**を起点とし、`EndOfMedia` の受信までを計測して、
初期バッファリング時間を測定値へ混ぜないようにした。
期待値は `(duration - 計測開始位置) / playbackRate`。

許容誤差の根拠: 誤差要因は (1) ポーリング間隔 5ms、(2) 計測開始点の検出遅れ、
(3) `EndOfMedia` が音声出力バッファ排出後に届くこと（Bluetooth 機器では特に大きい）。
初回実測での最大誤差が -94ms（相対 -0.5%）だったため、**その約 2 倍の余裕**として
**絶対 200ms と相対 2% の大きい方**を採用した。通すために広げた値ではなく、
実測分布から決めた値である。

**結果: 14/14 すべて合格。** 誤差の最大は -105ms（相対 -0.5%）。

ピッチ補正 OFF:

| 倍率 | 設定値 | 期待 | 実測 | 誤差 | 相対 | 許容 | 判定 |
|---|---|---|---|---|---|---|---|
| 0.5 | 0.5000 | 19816ms | 19734ms | -82ms | -0.4% | 396ms | 合格 |
| 0.75 | 0.7500 | 13211ms | 13169ms | -42ms | -0.3% | 264ms | 合格 |
| 1.0 | 1.0000 | 9908ms | 9882ms | -26ms | -0.3% | 200ms | 合格 |
| 45/33 | 1.3636 | 7266ms | 7269ms | +3ms | +0.0% | 200ms | 合格 |
| 1.25 | 1.2500 | 7926ms | 7915ms | -12ms | -0.1% | 200ms | 合格 |
| 1.5 | 1.5000 | 6605ms | 6626ms | +21ms | +0.3% | 200ms | 合格 |
| 2.0 | 2.0000 | 4954ms | 4961ms | +7ms | +0.1% | 200ms | 合格 |

ピッチ補正 ON:

| 倍率 | 設定値 | 期待 | 実測 | 誤差 | 相対 | 許容 | 判定 |
|---|---|---|---|---|---|---|---|
| 0.5 | 0.5000 | 19816ms | 19711ms | -105ms | -0.5% | 396ms | 合格 |
| 0.75 | 0.7500 | 13211ms | 13141ms | -69ms | -0.5% | 264ms | 合格 |
| 1.0 | 1.0000 | 9908ms | 9881ms | -27ms | -0.3% | 200ms | 合格 |
| 45/33 | 1.3636 | 7266ms | 7242ms | -24ms | -0.3% | 200ms | 合格 |
| 1.25 | 1.2500 | 7926ms | 7906ms | -21ms | -0.3% | 200ms | 合格 |
| 1.5 | 1.5000 | 6605ms | 6600ms | -6ms | -0.1% | 200ms | 合格 |
| 2.0 | 2.0000 | 4954ms | 4957ms | +3ms | +0.1% | 200ms | 合格 |

**ピッチ補正 ON と OFF で経過時間に有意差はない。** どちらも期待値どおりに伸縮している。
これは「ピッチ補正 ON でも再生長は変わらない（= 正しく time-stretch している）」ことと
整合するが、**ピッチが実際に維持されているかどうかの証拠ではない**。それは可聴確認で判定する。

### 4.5 エラーと警告

- `errorOccurred`: 発生なし。
- `qInstallMessageHandler` が受けた Qt のログ: 情報 1 件のみ
  （`Using Qt multimedia with FFmpeg version 7.1.3 LGPL version 2.1 or later`）。警告・エラーなし。
- FFmpeg ライブラリが直接 stderr へ出す診断（`Input #0, wav, ...`）は
  Qt のメッセージハンドラーを経由しない。これは異常ではない。

### 4.6 参照音源の妥当性

手動確認で人が音程の基準にするため、生成した参照音源そのものを検証した。
すべて `pcm_s16le` / 44100Hz / 2ch / 3.000 秒（速度検証用のみ 10.000 秒）で、
FFT のピーク周波数は次のとおり期待値と一致した
（FFT の分解能 2.7Hz による量子化誤差の範囲内）。

| ファイル | 期待 | 実測ピーク |
|---|---|---|
| `reference_220Hz.wav` | 220Hz | 220.7Hz |
| `reference_330Hz.wav` | 330Hz | 331.1Hz |
| `reference_440Hz.wav` | 440Hz | 438.7Hz |
| `reference_550Hz.wav` | 550Hz | 549.1Hz |
| `reference_600Hz.wav` | 600Hz | 600.2Hz |
| `reference_660Hz.wav` | 660Hz | 659.5Hz |
| `reference_880Hz.wav` | 880Hz | 880.2Hz |
| `speed_10s_440Hz.wav` | 440Hz | 438.7Hz |

生成物は `.sdp-local/p0b/`（`.gitignore` 済み）に置き、リポジトリへはコミットしない。

---

## 5. P0-B の手動確認（**未実施 / 手動確認待ち**）

以下はいずれも**人が実行して記入する**。AI は聴感結果を推測して合否を判断しない。
音を鳴らすため `--volume` の明示指定を必須にしてある（既定では鳴らない）。

### 5.1 可聴確認（P0-A の保留分）

```powershell
uv run python spike/p0b_speed_pitch.py --manual audible --volume 0.2
```

WAV / MP3 / OGG Opus / 日本語・空白パス（WAV・M4A）を 1.0 倍で順に再生する。

| 対象 | 結果 | 備考 |
|---|---|---|
| WAV | 手動確認待ち | |
| MP3 | 手動確認待ち | |
| OGG Opus | 手動確認待ち | |
| 日本語・空白パス (WAV) | 手動確認待ち | |
| 日本語・空白パス (M4A) | 手動確認待ち | |

### 5.2 varispeed（ピッチ補正 OFF）

```powershell
uv run python spike/p0b_speed_pitch.py --manual varispeed --volume 0.2
```

各倍率で「Qt 再生（検証対象）」と「参照音源（NumPy 生成の期待値、必ず 1.0 倍で再生）」を
交互に鳴らし、コンソールにどちらを再生中か表示する。

| 倍率 | 期待ピッチ | 一致したか | 備考 |
|---|---|---|---|
| 0.5 | 220Hz | 手動確認待ち | |
| 0.75 | 330Hz | 手動確認待ち | |
| 1.0 | 440Hz | 手動確認待ち | |
| 45/33 | 600Hz | 手動確認待ち | |
| 1.25 | 550Hz | 手動確認待ち | |
| 1.5 | 660Hz | 手動確認待ち | |
| 2.0 | 880Hz | 手動確認待ち | |

判定（合格 / 条件付き合格 / 不合格）: **手動確認待ち**

自由記述:

### 5.3 time-stretch（ピッチ補正 ON）

```powershell
uv run python spike/p0b_speed_pitch.py --manual timestretch --volume 0.2
uv run python spike/p0b_speed_pitch.py --manual timestretch --volume 0.2 --source "D:\music\曲.flac"
```

`--source` にはユーザー所有の会話音声や音楽を指定できる。
**指定したファイルはリポジトリへコピーもコミットもしない。**
正弦波だけではロボット声やエコー感を評価できないため、`--source` の指定を強く推奨する。

音程維持（440Hz 正弦波と参照音の比較）:

| 倍率 | 440Hz を保っているか | 備考 |
|---|---|---|
| 0.5 | 手動確認待ち | |
| 0.75 | 手動確認待ち | |
| 1.5 | 手動確認待ち | |
| 2.0 | 手動確認待ち | |

音質評価（各倍率について記入）:

| 評価項目 | 0.5 | 0.75 | 1.5 | 2.0 |
|---|---|---|---|---|
| 音程維持 | 手動確認待ち | 手動確認待ち | 手動確認待ち | 手動確認待ち |
| ロボット声 | 手動確認待ち | 手動確認待ち | 手動確認待ち | 手動確認待ち |
| 金属的な揺れ | 手動確認待ち | 手動確認待ち | 手動確認待ち | 手動確認待ち |
| エコー感 | 手動確認待ち | 手動確認待ち | 手動確認待ち | 手動確認待ち |
| 周期的なうなり | 手動確認待ち | 手動確認待ち | 手動確認待ち | 手動確認待ち |
| クリックノイズ | 手動確認待ち | 手動確認待ち | 手動確認待ち | 手動確認待ち |

操作中の挙動:

| 項目 | 結果 | 備考 |
|---|---|---|
| 再生中の ON/OFF 切替 | 手動確認待ち | |
| シーク後の品質 | 手動確認待ち | |
| 一時停止と再開 | 手動確認待ち | |
| トラック再読み込み後の設定 | 手動確認待ち | |

判定（合格 / 条件付き合格 / 不合格）: **手動確認待ち**

自由記述:

---

## 6. P0-B の判断ゲート

| # | ゲート | 結果 | 根拠 |
|---|---|---|---|
| 1 | `pitchCompensationAvailability` が `Available` | **満たす** | §4.2 |
| 2 | ON/OFF 設定値が実際に切り替わる | **満たす** | §4.2（再生前・再生中・一時停止中すべて） |
| 3 | varispeed で速度とピッチが連動する | **手動確認待ち** | §5.2 |
| 4 | time-stretch でピッチがおおむね維持される | **手動確認待ち** | §5.3 |
| 5 | 0.5〜2.0 倍の再生時間が期待値と整合する | **満たす** | §4.4（14/14、誤差 0.5% 以内） |
| 6 | 再生中切替でクラッシュ・停止・重大なノイズがない | **一部満たす** | クラッシュ・停止・エラーは無し（§4.2、§4.5）。ノイズの有無は手動確認待ち |
| 7 | 個人利用として time-stretch の音質が許容可能 | **手動確認待ち** | §5.3 |

**ゲート 1・2・5 は満たし、3・4・7 は未判定。**
ゲート 3・4 のいずれかを満たさないことが確定した場合は、Qt Multimedia 向け P0-C へ進まず
mpv 昇格の判断を提示する。現時点では**不合格が確定していないため、mpv 昇格は提案しない**。

---

## 7. 判定と次の行動

### 7.1 Qt Multimedia の暫定判定

**暫定的に有望。ただし合否は未確定。**

自動検証で確認できる範囲（API の対応、ON/OFF の切替、倍率の適用、再生時間の整合）は
すべて期待どおりで、Qt Multimedia を不合格とする材料は現時点で見つかっていない。
とくに **ADR-0001 の合格基準 1（ピッチ補正の切替可否）は `Available` により満たされた**。

一方、**ADR-0001 の合格基準 2（varispeed と time-stretch 双方の音質）は未判定**である。
これは原理的に人の耳でしか判定できず、AI が推測で合格にしてはならない。

### 7.2 ADR-0001 の状態

**`Proposed` のまま変更しない。**

- 音質（基準 2）が未確認のため `Accepted` にはできない。
- Qt Multimedia の不合格が確定していないため `Superseded` 候補としても提示しない。
- 仮に音質が合格しても、P0-C（PCM 取得）と P0-D（パッケージ検証）が残るため、
  `Accepted` への更新はそれらの完了後に行う。

### 7.3 次の行動

1. **§5 の手動確認を人が実施する**（最優先。ゲート 3・4・7 の判定に必須）。
2. 手動確認が合格または条件付き合格なら、**P0-C（QAudioBufferOutput による PCM 取得、
   およびシーク精度の PCM 照合）** へ進む。
3. 手動確認でゲート 3 または 4 が不合格なら、Qt 向け P0-C へは進まず、
   mpv 昇格の判断を ADR として提示する。
