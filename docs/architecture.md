# sdp アーキテクチャ

sdp の設計文書。要件 ID（PLAY-xx、WAVE-xx 等）とマイルストーンの定義は
[開発計画](./development-plan.md) を参照する。再生エンジンの選定理由は
[ADR-0001](./adr/0001-playback-engine.md) を参照する。

**本文書に記載する設計は、[開発計画 §1.3](./development-plan.md#13-未検証事項p0-で検証結果次第で設計変更)
の未検証事項（U1〜U8）の検証結果によって変更される。**

---

## 1. 設計原則

- UI から QMediaPlayer などの具体的な再生実装を直接操作しない。
  UI が触ってよいのは PlaybackController まで。
- PlaybackController と PlaybackBackend を分離し、
  将来の mpv 差し替えに必要な最小限の `PlaybackBackend` だけを定義する。
- 汎用プラグインシステムは作らない。
- メタデータ取得と波形解析で GUI スレッドをブロックしない。
- `audioBufferReceived` 内で重い FFT や描画を行わない。PCM は固定長リングバッファへ渡す。
- 可視化は固定 FPS のタイマーで最新スナップショットを描画する。
- 非表示のビジュアライザーは処理を停止する。
- 再生失敗、解析失敗、メタデータ失敗を独立して扱う。
- god class を避け、UI・再生制御・音声解析・ファイル操作・設定・Windows 統合を分離する。

---

## 2. モジュール構成

```
sdp/
├── pyproject.toml            # uv 管理。ruff / pyright / pytest / coverage 設定を集約
├── src/sdp/
│   ├── __main__.py           # エントリポイント（引数解析 → 単一インスタンス判定 → App 起動）
│   ├── app.py                # QApplication の組み立てと依存の手動配線、日本語ロケール
│   ├── core/
│   │   ├── playback/
│   │   │   ├── types.py      # 再生状態・メディア状況・エラーの Qt 非依存型
│   │   │   ├── backend.py    # PlaybackBackend（最小インターフェース）
│   │   │   ├── qt_backend.py # QtMultimediaBackend 実装（P1-B で追加予定）
│   │   │   └── controller.py # PlaybackController（曲順・リピート・シャッフル・自動送り）
│   │   ├── playlist/
│   │   │   ├── entry.py      # PlaylistEntry（entry_id / path / メタデータ / 状態）
│   │   │   ├── model.py      # PlaylistModel(QAbstractTableModel)
│   │   │   └── persistence.py# JSON の保存 / 復元。将来の M3U8 入出力もここへ
│   │   ├── metadata/reader.py# Mutagen ラッパーと QRunnable ワーカー
│   │   └── analysis/
│   │       ├── ring_buffer.py    # 固定長 PCM リングバッファ
│   │       ├── pcm_tap.py        # QAudioBufferOutput → 正規化 → mono 化 → リングバッファ
│   │       ├── spectrum.py       # Hann 窓・FFT・バンド集約・平滑化（純粋関数中心）
│   │       ├── waveform.py       # WaveformAnalyzer（QAudioDecoder ワーカー、envelope 縮約）
│   │       └── waveform_cache.py # npz キャッシュ、キー生成、LRU 容量管理
│   ├── services/
│   │   ├── settings.py       # 設定 dataclass と JSON 入出力、スキーマバージョン
│   │   ├── single_instance.py# QLocalServer / QLocalSocket と QLockFile
│   │   ├── win_integration.py# ms-settings の起動、対応形式のプローブ
│   │   └── logging_setup.py  # RotatingFileHandler、Qt ログ統合、未捕捉例外処理
│   └── ui/
│       ├── main_window.py    # レイアウト骨格・メニュー・ドック配置のみ（god class 禁止）
│       ├── player_controls.py# 再生ボタン群・シークバー・音量・時間表示
│       ├── speed_panel.py    # 速度スライダー・プリセット・ピッチ補正トグル
│       ├── playlist_view.py  # QTableView と D&D、コンテキストメニュー
│       ├── waveform_widget.py# 追従波形（QPainter 自前描画）
│       ├── spectrum_widget.py# スペクトラム（QPainter 自前描画）
│       ├── level_meter.py    # Peak / RMS メーター
│       └── shortcuts.py      # QShortcut 定義の一元管理
├── tests/                    # テスト構成は testing-strategy.md を参照
├── assets/test_audio/        # 自作テスト音源（正弦波など、各形式数秒）
├── packaging/
│   ├── sdp.spec              # PyInstaller
│   └── installer.iss         # Inno Setup（ProgID / Capabilities の登録と削除）
├── spike/                    # P0 検証スクリプト（本体から独立、lint / coverage 対象外）
└── docs/
```

### 2.1 描画ライブラリの方針

波形とスペクトラムは描画要件が特殊（中央固定スクロール、バーの減衰表示）で、
PyQtGraph の汎用 API に合わせるコストの方が高いと判断し、**自前 QPainter 描画**を採用する。
依存も 1 つ減る。P5 で描画性能が不足した場合のみ PyQtGraph を再検討する（リスク R6）。

---

## 3. 主要クラスと責務

| クラス | 責務 | 持たないもの |
|---|---|---|
| `PlaybackBackend` | `load` / `play` / `pause` / `stop` / `seek` / `set_volume` / `set_muted` / `set_playback_rate` / `set_pitch_compensation` と、位置・長さ・状態・メディア状況・エラーのシグナル。**mpv 差し替えに必要な最小限のみ** | プレイリストの知識、UI の知識 |
| `QtMultimediaBackend` | QMediaPlayer / QAudioOutput / QAudioBufferOutput の所有と、上記インターフェースへの変換 | 曲順ロジック |
| `PlaybackController` | 「今どのエントリを再生中か」の唯一の管理者。順次 / リピート / シャッフル、曲終了時の次曲決定、欠損スキップ、再生失敗時の方針 | デコード、描画 |
| `PlaylistModel` | 行データ、並べ替え、D&D、欠損フラグ、重複許可（entry_id 採番） | 再生状態の所有（現在行は Controller が entry_id で参照する） |
| `MetadataReader` | ワーカーで Mutagen による読み取りを行い、シグナルで Model へ反映する | GUI スレッドでのブロッキング I/O |
| `WaveformAnalyzer` | 専用 QThread で QAudioDecoder を駆動し、envelope を生成する。進捗 / 完了 / 失敗をシグナルで通知 | 描画 |
| `WaveformCache` | キー生成（path + size + mtime + 解析バージョン）、npz の読み書き、破損検出、LRU 削除 | 解析処理そのもの |
| `PcmTap` / `PcmRingBuffer` | QAudioBuffer の受領、float32 mono への正規化、リングバッファへの書き込み、スナップショット読み出し | FFT、描画 |
| 各可視化ウィジェット | 固定 FPS タイマーでスナップショットを取得し、`spectrum.py` の純粋関数を通して描画する。hide / 最小化でタイマー停止と PCM タップ解除 | PCM 取得の詳細 |
| `SettingsService` | 型付き設定の読み書き、既定値、スキーマバージョン | 任意キーの雑多な保存 |
| `SingleInstanceService` | サーバー / クライアントの判定、パス転送、受信通知 | 受け取ったファイルの解釈 |

### 3.1 再生層の契約（P1-A で確定した範囲）

P1-A で実装済みなのは `types.py` / `backend.py` / `controller.py` の 3 つで、
`qt_backend.py` と UI（`main_window.py` など）はまだ存在しない。
表中の曲順・リピート・シャッフルは P2 以降で Controller へ追加する。

- **状態とエラーの型**（`types.py`。すべて Qt 非依存）
  - `PlaybackState`: `NO_MEDIA` / `STOPPED` / `PLAYING` / `PAUSED`。
    読み込みの進行状況は `MediaStatus`、失敗は `PlaybackError` として別に扱い、
    状態 enum へ混ぜない。
  - `MediaStatus`: `QMediaPlayer.MediaStatus` の 8 値と 1 対 1。
    値を間引くと Backend が未知値を丸めることになり silent fallback になるため。
  - `PlaybackError`: 不変 dataclass。`code`（アプリ内コード）/
    `message`（ユーザー向け日本語）/ `detail`（ログ向け技術詳細）。
    例外オブジェクトを UI へ渡さず、`detail` をそのまま表示しない。
- **値検証**（`PlaybackController`）。暗黙の clamp で呼び出し側のバグを隠さない。
  - 欠損ファイル・ディレクトリの指定は**ユーザー入力由来**として `error_occurred` で通知する
    （例外にしない）。失敗した場合は現在の source を変更しない。
  - 負のシーク位置、範囲外・NaN の音量、0 以下・NaN・無限大の再生速度は
    **プログラミングエラー**として `ValueError` を送出する。
  - duration が確定（1 以上）していれば duration 超えの seek を拒否する。
    duration が未確定（0）のあいだは上限を検証せず転送する
    （読み込み直後の位置復元を拒否しないため。実際の可否は Backend に委ねる）。
  - 拡張子や `QMediaFormat` の列挙で対応可否を判定しない（ADR-0001 の制約 3）。
- **要求値の保持**: `playback_rate` と `pitch_compensation` はユーザーの要求値を真値とする。
  Backend からの読み戻しは float32 精度になりうる（ADR-0001 の制約 2）ため、
  許容誤差（相対 1e-6）内なら要求値を保ったまま再通知しない。
  誤差を超える場合のみ Backend の実値を採用する。厳密な等値比較は行わない。
- **同じ値の再設定**は Backend を呼ばず通知もしない（no-op）ものとして全設定で統一する。

---

## 4. シグナルとデータフロー

```
UI 操作 → PlayerControls ──(メソッド呼び出し)──→ PlaybackController ──→ PlaybackBackend
                                                   │ current_entry_changed(entry_id)
Backend ──position_changed / duration_changed──→ Controller ──→ PlayerControls / WaveformWidget
Backend ──media_status_changed(END_OF_MEDIA)──→ Controller（次曲を決定）
Backend ──error_occurred(PlaybackError)──→ Controller（該当エントリにエラーを記録し方針を適用）→ PlaylistModel
QAudioBufferOutput ──audioBufferReceived──→ PcmTap（軽量変換のみ）→ PcmRingBuffer
QTimer(30FPS) → SpectrumWidget: PcmRingBuffer.snapshot() → FFT → 描画
WaveformAnalyzer(worker) ──progress / finished(envelope)──→ WaveformWidget（部分描画）
MetadataReader(worker) ──metadataReady(entry_id, tags)──→ PlaylistModel
SingleInstanceService ──filesReceived(paths)──→ PlaylistModel へ追加 + Controller で再生 + 前面化
```

原則として、UI から下位へは通常のメソッド呼び出し、下位から UI へはシグナルのみとする。
UI が Backend を直接触ることは禁止し、import 構成のレビューで担保する。

---

## 5. スレッド境界

| スレッド | 処理 | 備考 |
|---|---|---|
| GUI（メイン） | UI、Controller、Model、PcmTap の受信、FFT、描画 | **P0-C で実測済み**: 4096 点 FFT は平均 0.02ms、30FPS で回しても CPU 占有 0.06%。コールバック全体でも 0.03%。GUI スレッド実行で問題ない |
| Qt Multimedia 内部 | デコードと音声出力 | Qt が管理する。直接は触らない |
| WaveformAnalyzer 用 QThread | QAudioDecoder（イベントループが必要）と NumPy による縮約 | 1 ファイル 1 ジョブ。曲切り替え時は先行ジョブをキャンセルする |
| QThreadPool | メタデータ読み取り（QRunnable） | 大量 D&D 時はキューで処理する |

**リングバッファのロック方針**: **P0-C で実測により確定**
（[p0-report.md §8.4](./p0-report.md#84-スレッド境界実測ではない)）。
`audioBufferReceived` は **GUI スレッドで受信される**（Python の `threading.get_ident()` が
メインスレッドと一致し、`QThread.currentThread()` / 受信 QObject の `thread()` /
`QApplication.instance().thread()` がすべて同一の `Qt mainThread`）。
接続方式は既定の `AutoConnection` で、送信元と受信先が同一スレッドのため Direct 接続として
振る舞う。したがって writer と reader がともに GUI スレッドとなり、**ロックは不要**。

この結論の前提は「PcmTap の受信 QObject を GUI スレッドに置くこと」である。
将来 PCM 受信をワーカースレッドへ移す場合は、この前提が崩れるため mutex が必要になる。

---

## 6. PCM リングバッファと FFT・描画更新

- リングバッファは float32 mono の固定長 16384 サンプル（48kHz で約 340ms）。
  書き込みは QAudioBuffer のサンプル形式（int16 / int32 / float）を [-1, 1] へ正規化し、
  ステレオは平均で mono 化してから追記する。
- `snapshot(n)` は末尾 n サンプルのコピーを返す（数十 KB のコピーで無視できる）。
- スペクトラム処理（`spectrum.py`。すべて純粋関数として単体テスト可能にする）:
  1. `snapshot(4096)` → Hann 窓 → `numpy.fft.rfft` → 振幅 → dB（下限 -70dB）
  2. 対数周波数軸でバンド集約（既定は 50Hz〜16kHz を 32 バンド、バンド内は最大値）
  3. 平滑化: `display = max(new, display - release * dt)`。attack は即時
     （foobar2000 的な挙動）。無音時・停止時は release のみが働き自然に減衰する
- 描画は 30FPS の QTimer。`hideEvent` と最小化でタイマーを停止し PCM タップを切断、
  `showEvent` で再開する（SPEC-04）。
- 速度変更時の挙動は **P0-C で実測済み**（[p0-report.md §8.6](./p0-report.md#86-速度変更時の通知挙動最重要の発見)）。
  確定した事実は次のとおり。
  - **QAudioBufferOutput が渡すのは、速度・ピッチ処理を適用する前のデコード済み PCM である。**
    2.0 倍の varispeed 再生中でも、取得 PCM の FFT ピークは元の周波数のままになる。
    可視化はこの差を仕様として受け入れるか、ピッチ補正 OFF のときだけ周波数軸を
    `playbackRate` 倍にスケールするかを P5 で決める。ピッチ補正 ON では補正してはならない。
  - **1 バッファの `frameCount` は playbackRate によらず一定で、変わるのは通知間隔**
    （1.0 倍で約 93ms、0.5 倍で約 186ms、2.0 倍で約 46ms）。
    通知間隔は描画間隔（30FPS = 33ms）より長いため、同じスナップショットを複数回描くことは
    あっても取りこぼしは起きない。
  - PCM の供給レートが playbackRate に比例するため、**固定長リングバッファが保持する
    「聴取時間」は playbackRate に反比例する**（16384 サンプルは 1.0 倍で約 340ms、
    2.0 倍で約 170ms 相当）。平滑化の時定数を実時間基準で設計する場合は考慮する。
- **音量とミュートは取得 PCM へ一切影響しない**（P0-C 実測。RMS の差は 0.000000）。
  可視化は音量設定を適用する前の信号を表す。ミュート中でもレベルは振れる。
- PCM の解釈について P0-C で確定した事実:
  - 実環境で観測された `sampleFormat` は **Int16（WAV / FLAC）と Float（MP3 / Vorbis /
    Opus / AAC）の 2 種類のみ**。`UInt8` と `Int32` は未観測のため実装しない。
    未対応形式は明示的に失敗させ、silent fallback を作らない。
  - **`frameCount` はコーデックごとに異なり、同一ファイル内でも一定でない**
    （先頭バッファはプライミングで短い）。固定バッファ長を前提にしてはならない。
  - **`QAudioBuffer.startTime()` は負値を取りうる**（MP3 で -25ms、Opus で -6.5ms を観測）。
    エンコーダー遅延・プリスキップに由来する。時間軸計算で負値を弾かない。
  - **`QAudioBuffer.constData()` は `None` を返すことがある**。バイト列化の前に判定する。
    また PySide6 はスロット内の例外を握り潰すため、PcmTap 内で例外を投げない作りにし、
    無効バッファは件数を数えて捨てる。
  - 曲切替時に前曲 PCM の混入は観測されなかったが、リングバッファは切替時に
    明示的に clear する（コストがほぼ無く、確実なため）。

---

## 7. 波形解析・縮約・キャッシュ

- 解析は QAudioDecoder で全量をデコードしつつ、**フル PCM を保持せず**逐次
  min/max peak envelope（既定 100 peak-pair/秒、mono）へ縮約する。
  60 分の音源で envelope は約 360k float32 × 2 ≈ 2.9MB。
- 表示は ±30 秒（約 6000 peak-pair）を切り出し、ウィジェット幅に応じてビン集約して描画する。
  中央線は固定で、`positionChanged` によりオフセットを更新する。
  クリック / ドラッグは x 座標を秒へ変換して `seek` する。
- 解析中は解析済み範囲のみを描画し、薄い進捗表示を添える。
  解析失敗や未対応形式の場合はプレースホルダ表示のみで再生を継続する（NF-01、WAVE-05）。
- キャッシュは `%LOCALAPPDATA%\sdp\waveforms\<sha1(絶対パス)>.npz`。
  npz 内にメタ情報（path / size / mtime_ns / 解析バージョン / peaks_per_sec）を同梱し、
  読み込み時に照合する。不一致または読み込み例外の場合は無効化して再解析する。
  総容量が上限（既定 500MB）を超えたら、別途保持する index.json の参照時刻で LRU 削除する。
- mpv へ切り替えた場合は、この envelope とは別に「位置駆動スペクトラム用」の粗い PCM
  （例: 8kHz mono）が必要になる。60 分で約 115MB となりディスクキャッシュ化が要る（U8 で評価）。

---

## 8. プレイリストモデル

- `QAbstractTableModel` を使う。列は 状態アイコン / タイトル / アーティスト / アルバム / 長さ。
  `Qt.UserRole` で entry_id とパスを保持する。
- D&D は外部から `text/uri-list` を受理し、受理順をそのまま表示順とする（PL-01）。
  内部の並べ替えは `moveRows` を用いる。
- 欠損は追加時と復元時に存在チェックを行って `missing` フラグを立て、
  `QStyledItemDelegate` でグレー描画し、再生時はスキップする（PL-05）。
- **重複追加は許可する**（PL-07）。entry_id は単調増加の整数で、
  「現在再生中」の表示・波形・メタデータはすべて entry_id 単位で扱うため、
  同一パスの行が複数あっても破綻しない。
- 妥当性は `QAbstractItemModelTester` で常時検証する。
- 永続化は `playlist.json`（パス配列、現在の entry、再生位置）。
  M3U8 入出力は将来 `persistence.py` に追加する（`#EXTM3U` / `#EXTINF`、UTF-8）。

---

## 9. 設定保存

- 保存先は `%APPDATA%\sdp\settings.json`。dataclass（音量、ミュート、リピートモード、
  シャッフル、速度、ピッチ補正、ウィンドウジオメトリ、可視化の表示状態、キャッシュ上限など）
  と `schema_version` を持つ。
- 読み込み時は未知のキーを無視し、欠落キーは既定値で補う。
  実際の旧バージョンが生まれるまで migration を作り込まない（[AGENTS.md](../AGENTS.md) の方針）。
- 保存タイミングは終了時と、変更から数秒のデバウンス保存（異常終了対策）。
  書き込みは一時ファイルへ書いてから `os.replace` でアトミックに置き換える。

---

## 10. 単一インスタンス

- サーバー名は `sdp-<ユーザー名のハッシュ>`。
- 起動時に `QLocalSocket` で接続を試み、成功した場合は引数の絶対パス群を JSON Lines で
  送信して即座に終了する。失敗した場合は `QLocalServer.listen`
  （残骸対策に `removeServer` を実行）し、`QLockFile` で競合を防ぐ。
- 受信側はパス群をプレイリスト末尾へ追加し、最初に受信した曲を再生して
  `activateWindow` と `raise_` を呼ぶ。Windows のフォアグラウンド制約で前面化できない場合は
  `QApplication.alert` によるタスクバー点滅にフォールバックする
  （発動条件をログへ残す。リスク R7）。

---

## 11. Windows ファイル関連付け

インストーラー（Inno Setup、per-user / HKCU）で以下を登録する。

- `HKCU\Software\sdp\Capabilities`
  （ApplicationName / ApplicationDescription と FileAssociations:
  `.wav` `.mp3` `.ogg` `.opus` `.flac` `.m4a` `.aac` → `sdp.audiofile`）
- `HKCU\Software\Classes\sdp.audiofile\shell\open\command` = `"...\sdp.exe" "%1"`
- `HKCU\Software\RegisteredApplications` への登録

既定アプリは強制変更しない（WIN-03）。アプリ内のメニューから
`ms-settings:defaultapps` を起動して Windows の設定へ誘導する。
アンインストール時は Inno Setup の `[Registry]` の `uninsdeletekey` で上記を削除する。

---

## 12. パッケージングとインストーラー

- PyInstaller は **onedir** を使う（onefile は起動が遅く、アンチウイルス誤検知も増えるため）。
- spec で `QtQml` / `QtQuick` / `QtNetwork` などの不要な Qt モジュールと翻訳ファイルを除外して
  サイズを削減する（PySide6 + NumPy で 200MB 前後になる見込み。推測）。
- バージョン情報リソースとアイコンを埋め込む。`--selftest` 起動フラグを用意する
  （[testing-strategy.md](./testing-strategy.md) 参照）。
- Inno Setup で per-user インストール（管理者権限不要）、関連付け登録、スタートメニュー登録を行う。
  コード署名は初期スコープ外。

---

## 13. ログとエラー処理

- `logging` で `%LOCALAPPDATA%\sdp\logs\sdp.log` へ出力する
  （RotatingFileHandler、1MB × 5 世代）。`qInstallMessageHandler` で Qt の警告を統合し、
  `sys.excepthook` と Qt スロット内の例外ラッパーで「ログ記録 + エラーダイアログ
  （継続可能なら継続）」を行う。
- 失敗の独立扱い（NF-01）:
  - 再生失敗 → 該当行にエラーを記録して次の曲へ送る（連続 N 曲失敗したら停止する）
  - メタデータ失敗 → ファイル名のみを表示する
  - 解析失敗 → 可視化はプレースホルダ表示にする

  いずれも他の機能へ波及させない。
- 広すぎる例外捕捉で不具合を隠さない。silent fallback を作らない
  （[AGENTS.md](../AGENTS.md)）。
