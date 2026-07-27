# P0 技術検証レポート

[開発計画 §1.3](./development-plan.md#13-未検証事項p0-で検証結果次第で設計変更) の
未検証事項（U1〜U8）を実機で確認し、結果を記録する。
再生エンジンの判定基準は [ADR-0001](./adr/0001-playback-engine.md) を参照。

進行状況:

| 区分 | 内容 | 状態 |
|---|---|---|
| P0 準備 | 開発環境の確認とテスト音源の生成 | 完了 |
| P0-A | Qt Multimedia 基本検証（形式・シーク・終了通知・日本語パス） | 完了（§3、全 12 件合格） |
| P0-B | 速度とピッチ補正（U1、U2） | 完了。audible, varispeedともに全て正常に再生された。Time-stretchも合格。 |
| P0-C | PCM 取得と可視化（U3） | 完了（§8、全 6 項目合格） |
| P0-D | exe 化後の動作（U7） | 完了（§9、全 12 条件合格） |

### P0-B 手動聴感確認

実施日: 2026-07-28
出力機器: （ヘッドホンまたはスピーカー）
確認音源:
- 自己生成440Hz正弦波
- 会話またはボーカル音源
- 音楽音源

| 項目 | 判定 | 備考 |
|---|---|---|
| 通常再生 | 合格 | WAV、MP3、Opusで確認 |
| Varispeed 0.5倍 | 合格 | 220Hz参照と概ね一致 |
| Varispeed 45/33倍 | 合格 | 600Hz参照と概ね一致 |
| Varispeed 1.5倍 | 合格 | 660Hz参照と概ね一致 |
| Varispeed 2.0倍 | 合格 | 880Hz参照と概ね一致 |
| Time-stretch 0.75倍 | 合格 | 音程維持、軽微な人工感 |
| Time-stretch 1.5倍 | 合格 | 実用可能 |
| Time-stretch 0.5倍 | 条件付き合格 | 若干の残響感 |
| Time-stretch 2.0倍 | 条件付き合格 | 子音に軽い人工感 |
| 再生中ON/OFF切替 | 合格 | 停止・重大なノイズなし |
| シーク後 | 合格 | 設定と音質を維持 |
| 一時停止・再開 | 合格 | 問題なし |
| 総合判定 | 条件付き合格 | Qt Multimediaで続行 |

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
   → **実施済み。冒頭の「P0-B 手動聴感確認」表のとおり総合「条件付き合格」。**
2. 手動確認が合格または条件付き合格なら、**P0-C（QAudioBufferOutput による PCM 取得、
   およびシーク精度の PCM 照合）** へ進む。→ **§8 で実施済み。**
3. 手動確認でゲート 3 または 4 が不合格なら、Qt 向け P0-C へは進まず、
   mpv 昇格の判断を ADR として提示する。→ 該当せず。

---

## 8. P0-C: QAudioBufferOutput による PCM 取得と可視化適合性

検証スクリプト: [`spike/p0c_pcm_output.py`](../spike/p0c_pcm_output.py)
未検証事項: U3（PCM 取得の安定性・オーバーヘッド・接続スレッド・速度変更時の挙動）と、
P0-A で保留した「シーク精度の PCM 照合」（§3.4）。

### 8.0 前提（混同してはならないこと）

**QAudioBufferOutput から取得できる PCM は、playbackRate や pitchCompensation を
適用した後の可聴出力音声ではない。** これは本節 §8.6 で実測により確認した。
したがって本節の FFT ピークは**QAudioBufferOutput 側 PCM の性質を見る参考値**であり、
P0-B の varispeed / time-stretch の実出力ピッチを再判定するものではない。

### 8.1 実行環境と API

| 項目 | 値 |
|---|---|
| Python | 3.13.11 |
| PySide6 | 6.10.3 |
| Qt | 6.10.3 |
| バックエンド | `Using Qt multimedia with FFmpeg version 7.1.3 LGPL version 2.1 or later` |
| 既定の音声出力 | ヘッドホン (WH-XB910N) |
| `QAudioBufferOutput` の存在 | True |
| `QMediaPlayer.setAudioBufferOutput` | True |
| `QMediaPlayer.audioBufferOutput` | True |
| `QAudioBufferOutput.audioBufferReceived` | True |

`errorOccurred` は全検証を通じて 0 件。Qt の警告・エラーも 0 件
（情報ログ 1 件のみ = 上記バックエンド表示）。

### 8.2 入力ファイル

- `assets/test_audio/sine440.{wav,mp3,ogg,opus,flac,m4a}`（440Hz、左 0.5 / 右 0.25 の振幅差）
- `assets/test_audio/日本語 ディレクトリ/テスト 音源 440Hz.wav`
- `.sdp-local/p0c/diagnostic_segments_10s.wav`（本検証で生成。0-2s=220Hz, 2-4s=330Hz,
  4-6s=440Hz, 6-8s=550Hz, 8-10s=660Hz）
- `.sdp-local/p0c/tone_440Hz_10s.wav`（本検証で生成。440Hz 10 秒、左右に振幅差）

生成物は `.gitignore` 済みでコミットしない。

### 8.3 取得した PCM 形式（全 7 対象で取得成功）

| 対象 | sampleFormat | sampleRate | ch | bytesPerFrame | frameCount | 1 バッファ duration | L peak | R peak | mono peak |
|---|---|---|---|---|---|---|---|---|---|
| WAV | **Int16** | 44100 | 2 | 4 | 4096 | 92.9ms | 441.4Hz | 441.4Hz | 441.4Hz |
| MP3 | **Float** | 44100 | 2 | 8 | 47, 1152 | 24.0ms | 441.4Hz | 441.4Hz | 441.4Hz |
| OGG Vorbis | **Float** | 44100 | 2 | 8 | 576, 1024 | 22.4ms | 441.4Hz | 441.4Hz | 441.4Hz |
| OGG Opus | **Float** | **48000** | 2 | 8 | 648, 960 | 19.5ms | 445.3Hz | 445.3Hz | 445.3Hz |
| FLAC | **Int16** | 44100 | 2 | 4 | 4608 | 104.5ms | 441.4Hz | 441.4Hz | 441.4Hz |
| M4A/AAC | **Float** | 44100 | 2 | 8 | 1024 | 23.2ms | 441.4Hz | 441.4Hz | 441.4Hz |
| 日本語・空白パス (WAV) | **Int16** | 44100 | 2 | 4 | 4096 | 92.9ms | 441.4Hz | 441.4Hz | 441.4Hz |

**観測された sampleFormat は Int16 と Float の 2 種類のみ。**
`UInt8` と `Int32` は一度も発生しなかったため、spike では変換を実装していない。
未対応形式を受け取った場合は `UnsupportedSampleFormatError` で明示的に失敗させ、
silent fallback を作っていない（[AGENTS.md](../AGENTS.md) の方針）。
本体実装でも同じ方針を取り、実際に発生した時点で根拠とともに追加する。

その他の重要な観測:

- **`frameCount` はコーデックごとに大きく異なり、同一ファイル内でも一定でない**
  （先頭バッファは 47 / 576 / 648 のようにプライミングで短い）。
  可視化は固定バッファ長を前提にしてはならない。
- **`bytesPerFrame` は Int16 ステレオで 4、Float ステレオで 8。**
- **Opus は 48000Hz** で届く（§2.2 の記録と整合）。FFT ピークが 445.3Hz なのは
  48000Hz では 4096 点 FFT の分解能が 11.7Hz になるためのビン量子化であり、異常ではない。
- **`startTime` は負値を取りうる。** MP3 で -25057µs、Opus で -6500µs を観測した。
  エンコーダー遅延・プリスキップに由来する。時間軸の計算で負値を弾いてはならない。
- **チャンネル別の内容が保たれている。** WAV で左 RMS 0.3535 / 右 RMS 0.1767、
  比 2.000（音源の振幅比と一致）。ステレオ→mono 平均も正しく機能した。

### 8.4 スレッド境界（実測。推測ではない）

| 項目 | 実測値 |
|---|---|
| スロット内 `threading.get_ident()` | 5704 |
| スロット内 `threading.current_thread().name` | `MainThread` |
| Python のメインスレッド ident | 5704（**一致**） |
| スロット内 `QThread.currentThread()` | `QThread(0x…, name="Qt mainThread")` |
| 受信 `QObject.thread()` | 同一の `Qt mainThread` |
| `QApplication.instance().thread()` | 同一の `Qt mainThread` |
| 接続方式 | `Qt.ConnectionType.AutoConnection`（既定） |
| GUI スレッドと同一か | **True** |

**`audioBufferReceived` は GUI スレッドで受信される**ことを実測で確認した。
送信元と受信先が同一スレッドであるため、AutoConnection は Direct 接続として振る舞う。

→ [architecture.md §5](./architecture.md#5-スレッド境界) の
「リングバッファの writer と reader がともに GUI スレッドとなりロックは不要」という
**方針は妥当**である。ただし前提は「PcmTap の受信 QObject を GUI スレッドに置くこと」であり、
将来 PCM 受信をワーカースレッドへ移す場合はこの前提が崩れる。

### 8.5 シーク後の実 PCM 内容

**`player.position()` が要求値を返しただけでは合格とせず、取得した PCM の FFT ピークが
目的区間の周波数へ移ったことを合格条件とした。**
さらに、シーク直前に**目的と異なる周波数の区間を実際に再生**してから目的位置へシークし、
「たまたま同じ区間だった」ことによる誤合格を排除した。

| 目標 | 期待 | 直前に再生していた区間の実測 peak | `position()` | 古いバッファ | 一致までの件数 | 一致後の peak | 判定 |
|---|---|---|---|---|---|---|---|
| 1s | 220Hz | 333.8Hz | 1000ms | 0 件 / 0.0ms | #0（最初のバッファ） | 215.3Hz | 合格 |
| 3s | 330Hz | 215.3Hz | 3000ms | 0 件 / 0.0ms | #0 | 333.8Hz | 合格 |
| 5s | 440Hz | 215.3Hz | 5000ms | 0 件 / 0.0ms | #0 | 441.4Hz | 合格 |
| 7s | 550Hz | 215.3Hz | 7000ms | 0 件 / 0.0ms | #0 | 549.1Hz | 合格 |
| 9s | 660Hz | 215.3Hz | 9000ms | 0 件 / 0.0ms | #0 | 656.8Hz | 合格 |

**シーク直後に届く「古いバッファ」は 1 件も観測されなかった。**
`setPosition` の直後に到着する最初のバッファが、すでに目的区間の内容だった。

これにより **P0-A §3.4 で保留していた「シーク精度は見かけ上の一致かもしれない」という懸念は
解消した**。`position()` の値は実際のデコード位置と一致している。

なお、将来もし古いバッファが観測された場合の破棄判定には
**`QAudioBuffer.startTime()`（µs 単位の提示時刻）が使える**。
本検証でも startTime から区間周波数を逆算でき、判別材料として有効であることを確認した。

**注意すべき実測事実**: `QAudioBuffer.constData()` が **`None` を返すことがある**
（シーク検証中に 1 件観測。再生停止直後と思われる）。
バイト列へ変換する前に None を判定しないと `TypeError` になる。
さらに **PySide6 はスロット内で発生した例外を握り潰して処理を継続する**
（traceback は標準エラーへ出るが、再生は止まらない）。
本体実装では PcmTap 内で例外を出さない作りにし、無効バッファは件数を数えて捨てる。

### 8.6 速度変更時の通知挙動（最重要の発見）

計測は 3 秒間の実時間ウィンドウ。音源は 440Hz 10 秒の WAV（44100Hz）。

| 倍率 | 補正 | buf/s | frames/s | **frames/s ÷ sampleRate** | 平均 frameCount | 通知間隔 | position/s | FFT peak |
|---|---|---|---|---|---|---|---|---|
| 0.5 | OFF | 5.3 | 21832 | **0.495** | 4096 | 185.7ms | 495ms | 441.4Hz |
| 0.5 | ON | 5.3 | 21841 | **0.495** | 4096 | 185.7ms | 495ms | 441.4Hz |
| 1.0 | OFF | 10.7 | 43670 | **0.990** | 4096 | 92.9ms | 991ms | 441.4Hz |
| 1.0 | ON | 10.7 | 43673 | **0.990** | 4096 | 92.8ms | 991ms | 441.4Hz |
| 2.0 | OFF | 21.3 | 87363 | **1.981** | 4096 | 46.4ms | 2012ms | 441.4Hz |
| 2.0 | ON | 21.3 | 87356 | **1.981** | 4096 | 46.5ms | 2012ms | 441.4Hz |

読み取れること:

1. **`frames/s ÷ sampleRate` が playbackRate と一致する**（0.495 / 0.990 / 1.981。
   1〜2% の不足は計測ウィンドウの端数）。すなわち 2 倍速では 1 秒あたり 2 倍の
   ソースフレームが供給される。
2. **`frameCount` は速度によらず一定**（4096）。変わるのは**通知間隔**で、
   1.0 倍の 92.9ms に対し 0.5 倍は 185.7ms、2.0 倍は 46.4ms と playbackRate に反比例する。
3. **FFT ピークは倍率にもピッチ補正にもよらず 441.4Hz のまま。**
   varispeed（補正 OFF）の 2.0 倍なら可聴ピッチは 880Hz のはずだが、
   QAudioBufferOutput 側は 441.4Hz を返す。
4. `position()` の進み方は playbackRate に比例する（495 / 991 / 2012 ms/秒）。

**結論: QAudioBufferOutput が渡すのは「速度・ピッチ処理を適用する前のデコード済み PCM」である。**
ユーザーの事前指摘（Qt 公式仕様上の前提）が実測で裏付けられた。

**可視化の更新設計に必要な補正:**

- スペクトラム表示は「実際に聞こえている音」ではなく「デコード直後の音」を表す。
  varispeed（ピッチ補正 OFF）で 2.0 倍再生中、耳では 880Hz が鳴っているのに
  表示上のピークは 440Hz のままになる。**これは避けられない仕様上の差**であり、
  受け入れるか、表示側で周波数軸を `playbackRate` 倍にスケールする補正を入れるかを選ぶ。
  ピッチ補正 ON のときは補正してはならない（実際にピッチは変わっていないため）。
- PCM の供給レートが playbackRate に比例するため、**リングバッファが保持する実時間長は
  playbackRate に反比例する**。固定長 16384 サンプルのリングバッファは、
  1.0 倍で約 340ms 相当だが 2.0 倍では約 170ms 相当の「聴取時間」しか保持しない。
  可視化の平滑化時定数を実時間基準で設計するなら、この点を考慮する。
- 描画は固定 30FPS で最新スナップショットを読む設計のままでよい。
  通知間隔（46〜186ms）は描画間隔（33ms）より長いため、
  **描画のほうが速く、同じスナップショットを複数回描くことがある**。
  逆に取りこぼしは起きない。

### 8.7 音量・ミュートと PCM の関係

| volume | muted | バッファ件数 | RMS | peak |
|---|---|---|---|---|
| 0.0 | False | 10 | 0.265119 | 0.374969 |
| 0.2 | False | 10 | 0.265119 | 0.374969 |
| 0.2 | True | 10 | 0.265119 | 0.374969 |
| 0.0 | True | 10 | 0.265119 | 0.374969 |

**RMS の最大差は 0.000000。音量とミュートは QAudioBufferOutput の PCM 内容へ一切影響しない。**

→ **可視化は「音量設定を適用する前」の信号を表す。**
ミュート中でもレベルメーターとスペクトラムは振れ続ける。
これを避けたい場合は、可視化側で `volume`/`muted` を掛けて表示する必要がある
（どちらの挙動にするかは P5 で決める。本 P0 では事実の記録に留める）。

### 8.8 曲切替・形式切替

`44.1kHz WAV → 48kHz Opus → MP3 → FLAC` の順に連続切替。

| 順序 | sampleFormat | sampleRate | ch | バッファ件数 | FFT peak | 前曲形式の混入 | 判定 |
|---|---|---|---|---|---|---|---|
| 44.1kHz WAV | Int16 | 44100 | 2 | 12 | 441.4Hz | 0 件 | 合格 |
| 48kHz Opus | Float | 48000 | 2 | 12 | 445.3Hz | 0 件 | 合格 |
| MP3 | Float | 44100 | 2 | 12 | 441.4Hz | 0 件 | 合格 |
| FLAC | Int16 | 44100 | 2 | 12 | 441.4Hz | 0 件 | 合格 |

**`setSource` 後に前曲の形式のままのバッファが届くことはなかった（混入 0 件）。**
sampleFormat・sampleRate・channelCount・FFT ピークのすべてが新しい曲の値へ更新された。

→ 現時点では**世代番号や明示的な clear は必須ではない**。
ただし 0 件だったのは「`stop()` → `setSource()` → `play()`」という順序で切り替えた場合の結果であり、
再生中に直接 `setSource` した場合は未検証。
本体実装では**曲切替時にリングバッファを明示的に clear する**方が安全で、
コストもほぼ無いため推奨する（フォールバックではなく単純な初期化）。

### 8.9 処理コスト

`audioBufferReceived` 内では「時刻の記録」と「PCM の bytes 化コピー」だけを行い、
変換・FFT はコールバック外で計測した。

| 処理 | 平均 | 中央 | 最大 |
|---|---|---|---|
| コールバック全体（記録 + コピー） | 0.0262ms | 0.0218ms | 0.0902ms |
| PCM コピー（bytes 化） | 0.0059ms | 0.0056ms | 0.0095ms |
| float32 変換 | 0.0075ms | 0.0059ms | 0.0401ms |
| stereo → mono 変換 | 0.0364ms | 0.0301ms | 0.2002ms |
| 4096 点 FFT | 0.0212ms | 0.0192ms | 0.0702ms |

| 指標 | 実測 |
|---|---|
| バッファ通知頻度（1.0 倍） | 11.2 件/秒 |
| コールバックの CPU 占有率 | **0.03%**（1 秒あたり） |
| 30FPS で FFT した場合の占有率 | **0.06%**（1 秒あたり） |
| `startTime` の不連続（欠落の検出） | **0 件** |

**GUI スレッドで実行して問題ない。** 最も重い 4096 点 FFT でも 0.02ms で、
30FPS で回しても CPU 占有は 0.06% にとどまる。
2.0 倍再生時は通知頻度が 21.3 件/秒へ増えるが、それでも占有率は 0.1% 未満。

→ [architecture.md §5](./architecture.md#5-スレッド境界) の
「4096 点 FFT は GUI スレッド実行を既定とする」という判断は**実測で裏付けられた**。
最初からワーカースレッド化する必要はない。

**音切れ・GUI 停止・バッファ欠落は検証中に発生しなかった。**
`startTime` の連続性（`前の startTime + duration ≒ 次の startTime`）に不連続は 0 件。

### 8.10 P0-C の合格条件と判定

| # | 合格条件 | 結果 | 根拠 |
|---|---|---|---|
| 1 | 主要 6 形式で PCM を取得できる | **合格** | §8.3（日本語パス含む 7 対象すべて） |
| 2 | PCM 形式を明確に解釈できる | **合格** | §8.3（Int16 と Float の 2 種のみ。ch/rate/bytesPerFrame も取得可） |
| 3 | 440Hz 入力の FFT ピークが期待範囲に入る | **合格** | §8.3（441.4Hz。Opus のみ 445.3Hz だがビン量子化の範囲） |
| 4 | シーク後の PCM 内容が目的区間へ移る | **合格** | §8.5（5/5、古いバッファ 0 件） |
| 5 | 曲切替後に新しい形式と内容へ更新される | **合格** | §8.8（混入 0 件） |
| 6 | コールバック処理が再生を阻害しない | **合格** | §8.9（CPU 占有 0.03%、欠落 0 件、音切れなし） |
| 7 | 速度変更時の通知挙動を説明できる | **合格** | §8.6（frameCount 一定・通知間隔が rate に反比例と説明できる） |
| 8 | 可視化実装に致命的な同期問題がない | **合格** | §8.4（GUI スレッド受信）、§8.6（描画 30FPS より通知が遅く取りこぼしなし） |

**P0-C は全 8 条件を満たし合格。** 位置駆動方式への変更や mpv 移行を検討する必要はない。

### 8.11 制約と未確認事項

- **可視化は「速度・ピッチ処理前」かつ「音量・ミュート適用前」の信号を表す**（§8.6、§8.7）。
  仕様として受け入れるか補正するかは P5 で決める。
- **再生中に直接 `setSource` した場合の混入は未検証**（§8.8 は stop を挟んだ切替のみ）。
- **モノラル音源・サンプルレート 44.1k/48k 以外の音源は未検証。**
  `channelCount != 2` の場合の mono 化は本体実装時に対応が必要。
- **長時間再生（数十分）での安定性は未検証。** P8 の性能計測で確認する。
- `UInt8` / `Int32` の sampleFormat は未観測のため変換未実装（§8.3）。
- 音声出力デバイスを切り替えた場合の挙動は未検証。

### 8.12 再現コマンド

```powershell
uv run python spike/p0c_pcm_output.py
uv run python spike/p0c_pcm_output.py --only formats,thread
uv run python spike/p0c_pcm_output.py --only seek
uv run python spike/p0c_pcm_output.py --only speed,volume
uv run python spike/p0c_pcm_output.py --only switch,cost
```

音は鳴らさない（音量 0.0 固定）。診断音源は初回実行時に `.sdp-local/p0c/` へ自動生成される。

### 8.13 ADR-0001 の状態

**`Proposed` のまま変更しない。**
P0-C に合格しても、**P0-D（PyInstaller によるパッケージ版の検証）が残っている**ため、
`Accepted` への更新は P0-D の完了後に行う。

### 8.14 次の行動

**P0-D（exe 化後の動作検証、U7）へ進める。** → **§9 で実施済み。**

---

## 9. P0-D: PyInstaller onedir パッケージ版の検証

検証プローブ: [`spike/p0d_packaged_probe.py`](../spike/p0d_packaged_probe.py)
spec: [`packaging/p0d_probe.spec`](../packaging/p0d_probe.spec)
実行スクリプト: [`scripts/p0d_build_and_verify.ps1`](../scripts/p0d_build_and_verify.ps1)
未検証事項: U7（exe 化後の再生、日本語・空白パス）

`packaging/p0d_probe.spec` は**技術検証専用**であり、製品版の spec ではない。
製品版 `packaging/sdp.spec` は P7 で別途作成する。混同しないこと。

### 9.1 ビルド環境とコマンド

| 項目 | 値 |
|---|---|
| Python | 3.13.11 |
| PyInstaller | 6.21.0 |
| PySide6 / Qt | 6.10.3 |
| NumPy | 2.5.1 |
| OS | Windows 11 Home 10.0.26200 |
| 形式 | **onedir**（onefile は使わない） |
| UPX | 不使用 |

```powershell
uv run pyinstaller --clean --noconfirm packaging/p0d_probe.spec
```

方針どおり、PyInstaller 標準の PySide6 hook のみを使用した。
**hidden import も binary の追加収集も一切必要なかった**（実測で不足が出なかったため追加していない）。
不要 Qt モジュールの除外は行っていない（サイズ最適化は P7）。

### 9.2 成果物の構成とサイズ

| 項目 | 値 |
|---|---|
| 出力先 | `dist/p0d_probe/`（onedir） |
| 合計サイズ | **161.8 MB** |
| ファイル数 | **291** |
| 実行ファイル | `p0d_probe.exe` + `_internal/` |

Qt Multimedia の動作に必要なものが正しく同梱されていることを確認した。

| 同梱物 | サイズ | 役割 |
|---|---|---|
| `_internal/PySide6/plugins/multimedia/ffmpegmediaplugin.dll` | 621 KB | **Qt Multimedia の FFmpeg バックエンド** |
| `_internal/PySide6/plugins/multimedia/windowsmediaplugin.dll` | 285 KB | Windows Media バックエンド（未使用） |
| `_internal/PySide6/plugins/platforms/qwindows.dll` | 974 KB | Qt platform plugin |
| `_internal/PySide6/avcodec-61.dll` | 13.6 MB | **Qt が同梱する FFmpeg** |
| `_internal/PySide6/avformat-61.dll` | 2.6 MB | 同上 |
| `_internal/PySide6/avutil-59.dll` | 1.2 MB | 同上 |
| `_internal/PySide6/swresample-5.dll` | 241 KB | 同上 |
| `_internal/PySide6/swscale-8.dll` | 734 KB | 同上 |

plugins ディレクトリ: `generic`, `iconengines`, `imageformats`, `multimedia`,
`networkinformation`, `platforminputcontexts`, `platforms`, `styles`, `tls`。

### 9.3 FFmpeg CLI からの独立性（重要）

**同梱の FFmpeg と開発用 FFmpeg CLI は別物である。**バージョンでも明確に区別できる。

| | 実体 | バージョン |
|---|---|---|
| **Qt Multimedia が使う FFmpeg** | 成果物内の `avcodec-61.dll` 等 | FFmpeg **7.1.3**（Qt ログが明示） |
| 開発用 FFmpeg CLI（§1） | `C:\tools\ffmpeg.exe` | FFmpeg 6.x 系（libavcodec 60.30.102） |

検証では、PATH を OS の動作に必要な最小限へ制限した子プロセス環境で exe を実行した。

制限後の PATH:
`%SystemRoot%\system32; %SystemRoot%; %SystemRoot%\System32\Wbem;
%SystemRoot%\System32\WindowsPowerShell\v1.0`

`C:\tools`（開発用 FFmpeg CLI）と uv / Python のディレクトリは意図的に除外した。
**開発機の FFmpeg CLI をリネーム・削除するような破壊的操作は一切行っていない。**

制限環境での確認結果（全 4 回の実行すべてで同じ）:

| コマンド | 結果 |
|---|---|
| `where.exe ffmpeg` | 見つからない（期待どおり） |
| `where.exe ffprobe` | 見つからない（期待どおり） |
| `where.exe python` | 見つからない（期待どおり） |
| `where.exe uv` | 見つからない（期待どおり） |

**この状態で exe は正常に起動し、全 22 項目が PASS した（終了コード 0）。**
外部の Python・uv・FFmpeg CLI に一切依存していない。

### 9.4 配置場所とカレントディレクトリ

ビルド元（`dist/`）とは別の場所へ onedir 一式をコピーして実行した。

| 配置パス | 結果 |
|---|---|
| `.sdp-local/p0d/run-ascii/` | 22/22 PASS、終了コード 0 |
| `.sdp-local/p0d/日本語 パッケージ/` | 22/22 PASS、終了コード 0 |

カレントディレクトリは `%TEMP%`（リポジトリ外）にして実行した。
ソースツリーや相対パスへの暗黙依存はない。

パッケージ版が報告した実行時情報（日本語・空白パス配置の場合）:

- `sys.executable` = コピー先の `p0d_probe.exe`
- `frozen (PyInstaller)` = True
- `_MEIPASS` = `<コピー先>\_internal`
- Qt library paths = `<コピー先>/_internal/PySide6/plugins` と `<コピー先>`

**パッケージのパス自体に日本語と空白が含まれていても正常に動作した。**

### 9.5 検証結果（全 22 項目 PASS）

音源は exe へ埋め込まず、`--audio-dir` で外部ディレクトリを渡した
（ファイル関連付け起動に近い外部パス処理の検証を兼ねる）。音量は 0.0 固定で音は鳴らしていない。

#### 基本再生（12 項目）

| 対象 | 読込 | duration | 位置前進 | シーク | EndOfMedia | 判定 |
|---|---|---|---|---|---|---|
| ASCII `.wav` | OK | 2000ms | OK | 1400→1400ms | OK | PASS |
| ASCII `.mp3` | OK | 2037ms | OK | 1437→1437ms | OK | PASS |
| ASCII `.ogg` | OK | 2000ms | OK | 1400→1400ms | OK | PASS |
| ASCII `.opus` | OK | 2006ms | OK | 1406→1406ms | OK | PASS |
| ASCII `.flac` | OK | 2000ms | OK | 1400→1400ms | OK | PASS |
| ASCII `.m4a` | OK | 2000ms | OK | 1400→1400ms | OK | PASS |
| 日本語 `.wav` 〜 `.m4a` | すべて OK | 同上 | OK | 同上 | OK | PASS |

`errorOccurred` は 0 件。開発環境（§3）と完全に同じ値が得られた。

#### 速度・ピッチ補正 API（4 項目）

| 項目 | 結果 |
|---|---|
| `pitchCompensationAvailability()` | **`Available`**（パッケージ版でも同じ） |
| `pitchCompensation` の初期値 | True |
| ON/OFF 設定（False→True→False→True） | すべて設定値どおり |
| `playbackRate` 0.5 / 1.0 / 2.0 | すべて設定値どおり（float32 精度のため相対 1e-6 で比較） |
| `errorOccurred` | 0 件 |

P0-B で実施済みの再生時間測定と聴感確認は exe 上で繰り返していない。
**API がパッケージ版でも利用可能であることのみ**を確認した。

#### PCM 取得（6 項目）

| 対象 | sampleFormat | sampleRate | ch | バッファ件数 | frameCount | constData が None | FFT peak | 判定 |
|---|---|---|---|---|---|---|---|---|
| WAV | Int16 | 44100 | 2 | 10 | 4096 | 0 件 | **441.4Hz** | PASS |
| Opus | Float | 48000 | 2 | 10 | 648, 960 | 0 件 | **445.3Hz** | PASS |

いずれも P0-C（開発環境）と同一の値。Opus の 445.3Hz は 48kHz における
4096 点 FFT のビン量子化（分解能 11.7Hz）によるもので異常ではない。
`constData()` が `None` を返した場合は想定内の空バッファとして安全にスキップし件数を記録する
実装にしてあるが、本検証では 0 件だった。
スロット内で予期しない例外は発生していない（例外を無条件に握り潰す実装にはしていない）。

### 9.6 PyInstaller warning ファイルの評価

`build/p0d_probe/warn-p0d_probe.txt`（全 222 行、`missing module` 205 行）を確認した。

**Qt Multimedia・FFmpeg プラグイン・PySide6 の欠落は 1 件もない。**
内訳は次のとおりで、いずれも既知の無害なものである。

| 分類 | 件数 | 評価 |
|---|---|---|
| `numpy.*` の内部 optional import | 175 | NumPy が条件付きで参照するだけ。標準的なノイズ |
| `multiprocessing` 関連 | 6 | 未使用 |
| POSIX 専用（`posix`, `pwd`, `termios`, `resource`, `readline`） | 5 | Windows では存在しないのが正常 |
| `java`, `_dummy_thread`, `psutil`, `threadpoolctl` 等 | 4 | 未使用の optional 依存 |
| `collections.abc` | 1 | PyInstaller の既知の誤検出。実際には同梱されている |

`collections.abc` の行に `PySide6.QtMultimedia` 等が「参照元」として列挙されるが、
これは「PySide6 が欠落している」という意味ではない。実際に全機能が動作していることで確認済み。

**致命的な警告はない。**

### 9.7 Qt プラグイン診断（QT_DEBUG_PLUGINS）

診断時のみ `QT_DEBUG_PLUGINS=1` を設定した。**通常実行時の既定にはしていない。**
ログにはユーザー固有の絶対パスが含まれるため、以下は要約のみを記載する。

| プラグイン | 結果 |
|---|---|
| Qt platform plugin | `plugins/platforms/qwindows.dll` を `loaded library`。キー `"windows"` |
| Qt style | `plugins/styles/qmodernwindowsstyle.dll` を `loaded library` |
| **Qt Multimedia backend** | `plugins/multimedia/ffmpegmediaplugin.dll` を `loaded library`。メタデータのキーは `"ffmpeg"` |
| 併存する別バックエンド | `plugins/multimedia/windowsmediaplugin.dll` も検出されるが、選択されたのは ffmpeg 側 |

**FFmpeg バックエンドが成果物内から正しくロードされている。**

#### QT_DEBUG_PLUGINS 使用時に判明した不具合と、その原因

`QT_DEBUG_PLUGINS=1` を付けた場合に限り、**全 22 項目が PASS して
「最終終了コード: 0」を出力した後**、プロセスが
`-1073741819`（`0xC0000005` アクセス違反）で異常終了した。

切り分けの結果:

- パッケージ版だけでなく**開発環境でも同様に発生**した → **パッケージ固有の問題ではない**。
- 原因は **Python 側の `qInstallMessageHandler` を付けたまま終了していたこと**。
  Python の終了処理が進んだ後に Qt がログを出すと、死んだインタプリタを呼び出して落ちる。
  `QT_DEBUG_PLUGINS=1` は終了時のログが多いため顕在化した。
- **終了前に `qInstallMessageHandler(None)` でハンドラーを外したところ、
  開発環境・パッケージ版ともに終了コード 0 になった**（修正後に再確認済み）。

→ **本体実装への反映が必要**: `services/logging_setup.py` で
`qInstallMessageHandler` を使う場合、アプリ終了時に必ずハンドラーを解除する。
これは P1 の実装項目とする。

### 9.8 クリーンビルド 2 回の再現性

`build/` と `dist/` を削除してからのクリーンビルドを 2 回行い、
各回で ASCII パスと日本語・空白パスの両方を検証した（計 4 回の実行）。

| ラウンド | 成果物サイズ | ファイル数 | warn の missing module | ASCII パス | 日本語・空白パス |
|---|---|---|---|---|---|
| 1 回目 | 161.8 MB | 291 | 205 行 | 22/22 PASS（exit 0） | 22/22 PASS（exit 0） |
| 2 回目 | 161.8 MB | 291 | 205 行 | 22/22 PASS（exit 0） | 22/22 PASS（exit 0） |

**機能的な再現性を確認した。** サイズ・ファイル数・警告件数・検証結果のすべてが一致した。
バイナリのバイト単位の一致は要求していない。

### 9.9 P0-D の合格条件と判定

| # | 合格条件 | 結果 | 根拠 |
|---|---|---|---|
| 1 | Python 3.13 で onedir ビルドが成功する | **合格** | §9.1、§9.2 |
| 2 | Python・uv・外部 ffmpeg CLI に依存せず起動する | **合格** | §9.3（PATH 制限下で 22/22 PASS） |
| 3 | 主要 6 形式を読み込んで再生パイプラインを動かせる | **合格** | §9.5 |
| 4 | 日本語・空白パスを処理できる | **合格** | §9.4（パッケージ配置）、§9.5（音源パス） |
| 5 | `pitchCompensationAvailability` が Available | **合格** | §9.5 |
| 6 | `pitchCompensation` を ON/OFF できる | **合格** | §9.5 |
| 7 | QAudioBufferOutput から PCM を取得できる | **合格** | §9.5 |
| 8 | 440Hz FFT が期待範囲へ入る | **合格** | §9.5（441.4Hz / 445.3Hz） |
| 9 | Qt Multimedia または FFmpeg プラグインの不足がない | **合格** | §9.2、§9.6、§9.7 |
| 10 | 別ディレクトリへコピーした成果物でも動く | **合格** | §9.4 |
| 11 | クリーンビルドを 2 回行って同じ機能結果になる | **合格** | §9.8 |
| 12 | 致命的な PyInstaller 警告がない | **合格** | §9.6 |

**P0-D は全 12 条件を満たし合格。**

### 9.10 再現手順

```powershell
# クリーンビルド 2 回 + ASCII / 日本語パスでの検証（PATH 制限込み）
pwsh -File scripts/p0d_build_and_verify.ps1 -Runs 2

# 開発環境で同じプローブを動かす場合
uv run python spike/p0d_packaged_probe.py --audio-dir assets/test_audio

# 生成された exe を直接動かす場合
dist\p0d_probe\p0d_probe.exe --audio-dir "C:\dev\soft\sdp\assets\test_audio"
```

ログと成果物のコピーは `.sdp-local/p0d/` に出る（`.gitignore` 済み）。
`build/` と `dist/` も Git 管理しない。

### 9.11 残課題と未確認事項

- **サイズは 161.8 MB と大きい。** 不要 Qt モジュール（QtNetwork / QtQml / QtQuick 等）と
  翻訳ファイルの除外を **P7 で行う**。P0-D では意図的に最適化していない。
- 製品版 spec（`packaging/sdp.spec`）は未作成。P7 で作る。
- **GUI ウィンドウを持つアプリとしての検証は未実施**（本プローブはコンソールアプリ）。
  ウィンドウ表示・アイコン・`console=False` での挙動は P7 で確認する。
- コード署名なし。SmartScreen 警告は個人利用のため許容（初期スコープ外）。
- 別マシン・クリーンな Windows 環境での動作は未確認（VC++ ランタイム依存の有無）。
- インストーラー経由の配置は未検証（P7）。
