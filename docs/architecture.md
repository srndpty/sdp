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
- `audioBufferReceived` 内で重い FFT や描画を行わない。PCM は固定長リングバッファへ渡す
  （P5-A で実装。§6.3）。
- 可視化は固定 FPS のタイマーで最新スナップショットを描画する。
- 非表示のビジュアライザーはWidget単位の重い解析・描画を停止する。共有PCMタップは受信を継続する。
- 再生失敗、解析失敗、メタデータ失敗を独立して扱う。
- god class を避け、UI・再生制御・音声解析・ファイル操作・設定・Windows 統合を分離する。

---

## 2. モジュール構成

```
sdp/
├── pyproject.toml            # uv 管理。ruff / pyright / pytest / coverage 設定を集約
├── src/sdp/
│   ├── __main__.py           # エントリポイント
│   ├── app.py                # 起動順序と依存の手動配線、日本語ロケール
│   ├── launch.py             # LaunchRequestを既存compositionへ適用するadapter
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
│   │       ├── ring_buffer.py    # 固定長 mono float32 PCM リングバッファ（Qt 非依存）
│   │       ├── pcm.py            # QAudioBuffer → mono float32 のQt境界（波形解析と共有）
│   │       ├── spectrum.py       # SpectrumFrame、Hann 窓・rFFT・バンド集約・平滑化（純粋関数）
│   │       ├── waveform.py       # WaveformData、PCM正規化、増分min/max縮約
│   │       ├── waveform_projection.py # 中央固定窓のpixel min/max投影と座標変換
│   │       └── waveform_cache.py # npz キャッシュ、キー生成、LRU 容量管理
│   ├── services/
│   │   ├── launch_request.py # Qt非依存の起動要求とargv変換
│   │   ├── pcm_tap.py        # QAudioBufferOutput → 正規化 → mono 化 → リングバッファ
│   │   ├── waveform_analysis.py # QAudioDecoder workerと現在sourceの解析調停
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
│       ├── waveform_panel.py # 再生・解析Signalと波形Widgetの調停
│       ├── spectrum_widget.py# スペクトラム（QPainter 自前描画）
│       ├── spectrum_panel.py # 再生状態・PCMタップ・スペクトラムWidgetの調停
│       ├── level_meter.py    # Peak / RMS メーター（P5-B で追加）
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
| `QtMultimediaBackend` | QMediaPlayer / QAudioOutput の所有（QAudioBufferOutput は P5 で追加）と、上記インターフェースへの変換。Qt の enum・QUrl・エラーをアプリ内の型へ写す | 曲順ロジック、値の検証 |
| `PlaybackController` | 1 つの source の再生（読み込み・状態・位置・音量・速度）と Backend との境界 | 曲順、プレイリストの知識 |
| `SpeedPanel` | 0.5～2.0倍の速度操作、プリセット、ピッチ補正切替とControllerとの双方向同期 | Backend、プレイリスト、メタデータ、永続化 |
| `ShortcutManager` | WindowShortcutの生成、フォーカスに応じた抑止、Controller群への操作委譲 | Backend、Modelの直接操作、設定保存 |
| `PlaylistPlaybackController` | 「今どの entry を再生中か」の唯一の管理者。順次再生、前後曲、曲終了時の次曲決定、欠損スキップ | デコード、描画、行データの所有 |
| `PlaylistModel` | 行データ、並べ替え、D&D、欠損フラグ、重複許可（entry_id 採番） | 再生状態の所有（現在行は Controller が entry_id で参照する） |
| `MetadataReader` | ワーカーで Mutagen による読み取りを行い、シグナルで Model へ反映する | GUI スレッドでのブロッキング I/O |
| `WaveformData` / `WaveformReducer` | Qt非依存のread-only min/max波形と、全PCMを保持しない増分縮約 | デコード、thread、描画 |
| `WaveformColumns` / `project_waveform` | Qt非依存の60秒表示窓、bucketからpixel列へのpeak再集約、時刻とx座標の変換 | QWidget、QPainter、Controller、cache |
| `WaveformAnalysisService` | 専用QThreadのQAudioDecoder、現在sourceのrequest token、部分／完了／失敗通知、cache I/Oの調停 | QWidget、seek、PlaylistModel、再生状態の変更 |
| `WaveformCache` | キー生成（path + size + mtime + 解析バージョン）、npz の読み書き、破損検出、LRU 削除 | 解析処理そのもの |
| `WaveformWidget` | pixel列のpaletteベースQPainter描画、中央線、drag preview、release時のseek要求 | Controller、解析Service、cache、decoder |
| `WaveformPanel` | Controllerと解析ServiceのSignal接続、path/token照合、表示状態とseek委譲 | 波形投影、cache、Backend、再生順 |
| `PcmTap` | QAudioBuffer の受領と妥当性確認、float32 mono への正規化、リングバッファへの書き込み、sample rate変更検出、source／stop時のclear | FFT、Hann窓、dB変換、QWidget、PlaylistModel、cache、settings、波形解析、シーク |
| `PcmRingBuffer` | 固定容量のmono float32保持、wrap上書き、最新N sampleのread-only snapshot、短時間lock | Qt、全履歴、FFT |
| `SpectrumFrame` / `spectrum.py` | Qt非依存のread-only帯域別dBと、Hann窓・rFFT・dB変換・対数band集約・attack/release平滑化 | QColor、QWidget、PCM取得 |
| `SpectrumWidget` | band別dBのpaletteベースQPainter一括描画、dB基準線、状態文字 | Controller、PcmTap、FFT、平滑化状態、マウス操作 |
| `SpectrumPanel` | Controllerのstate/source監視、QTimer管理、snapshot取得、Processor呼出、Widget反映、visible／最小化での更新制御 | PCM decode、QAudioBuffer、cache、PlaylistModel、波形data、settings、Backend具体型 |
| `SettingsSession` | 速度・ピッチ設定の復元、変更監視、デバウンス／終了時保存 | UI、プレイリスト、P6以降の設定 |
| `SingleInstanceService` | サーバー / クライアントの判定、パス転送、受信通知 | 受け取ったファイルの解釈 |

### 3.1 再生層と UI の契約（P1 で確定した範囲）

実装済みなのは `types.py` / `backend.py` / `controller.py`（P1-A）、
`qt_backend.py`（P1-B）、`app.py` / `ui/main_window.py` / `ui/player_controls.py` /
`services/logging_setup.py`（P1-C）。
曲順・リピート・シャッフルとプレイリストはP2、速度・ピッチの操作UI
（`speed_panel.py`）はP3-A、ショートカットと速度・ピッチ設定永続化はP3-Bで実装済み。
可視化は後続フェーズで実装する。

- **状態とエラーの型**（`types.py`。すべて Qt 非依存）
  - `PlaybackState`: `NO_MEDIA` / `STOPPED` / `PLAYING` / `PAUSED`。
    読み込みの進行状況は `MediaStatus`、失敗は `PlaybackError` として別に扱い、
    状態 enum へ混ぜない。
    `NO_MEDIA` は source が未設定の場合に限る。source 設定後は、`LOADING` / `LOADED` /
    `END_OF_MEDIA` / `INVALID_MEDIA` を含め、再生中でも一時停止中でもなければ `STOPPED` とする。
  - `MediaStatus`: `QMediaPlayer.MediaStatus` の 8 値と 1 対 1。
    値を間引くと Backend が未知値を丸めることになり silent fallback になるため。
  - `PlaybackError`: 不変 dataclass。`code`（アプリ内コード）/
    `message`（ユーザー向け日本語）/ `detail`（ログ向け技術詳細）。
    例外オブジェクトを UI へ渡さず、`detail` をそのまま表示しない。
    Backend は Qt の既知エラーを `RESOURCE_ERROR` / `FORMAT_ERROR` / `NETWORK_ERROR` /
    `ACCESS_DENIED` へ明示的に写像し、未知値だけを `UNKNOWN_ERROR` とする。
- **値検証**（`PlaybackController`）。暗黙の clamp で呼び出し側のバグを隠さない。
  - 欠損ファイル・ディレクトリの指定は**ユーザー入力由来**として `error_occurred` で通知する
    （例外にしない）。失敗した場合は現在の source を変更しない。
  - 負のシーク位置、範囲外・NaN の音量、0 以下・NaN・無限大の再生速度は
    **プログラミングエラー**として `ValueError` を送出する。
  - duration が確定（1 以上）していれば duration 超えの seek を拒否する。
    duration が未確定（0）のあいだは上限を検証せず転送する
    （読み込み直後の位置復元を拒否しないため。実際の可否は Backend に委ねる）。
  - 拡張子や `QMediaFormat` の列挙で対応可否を判定しない（ADR-0001 の制約 3）。
  - 有効な source は `resolve(strict=True)` で絶対パスへ正規化して保持し、Backend へ渡す。
    Controller の `source` は `None` または絶対パスとする。
- **要求値の保持**: `playback_rate` と `pitch_compensation` はユーザーの要求値を真値とする。
  Backend からの読み戻しは float32 精度になりうる（ADR-0001 の制約 2）ため、
  許容誤差（相対 1e-6）内なら要求値を保ったまま再通知しない。
  誤差を超える場合や Backend が設定値を補正した場合のみ Backend の実値を採用する。
  setter 内の同期通知を含め、最後の変更通知と公開プロパティを一致させる。
  Backend の setter が予期せぬ Python 例外を送出した場合は直前の要求値へ戻して再送出する。
  厳密な等値比較は行わない。
- **同じ値の再設定**は Backend を呼ばず通知もしない（no-op）ものとして全設定で統一する。

### 3.2 速度・ピッチ操作UI（P3-A）

- **`SpeedPanel`の責務**: `PlaybackController`だけを受け取り、速度とピッチ補正だけを
  操作・表示する。Backend、`PlaylistModel`、`PlaylistPlaybackController`、metadata、
  settingsは知らない。`MainWindow`は既存のControllerを渡してPlayerControls直下へ配置する。
- **真値**: `PlaybackController.playback_rate`と`pitch_compensation`を唯一の真値とする。
  初期表示と外部変更はControllerの公開property／Signalから反映する。Controllerが保持する
  要求速度はBackendのfloat32読み戻し誤差（相対1e-6以内）では上書きされないため、
  UI表示も1.25が1.249999等へ振動しない。
- **速度範囲**: 製品UIは0.50～2.00倍、標準1.00倍。Controllerの一般契約は
  「正の有限値」のまま狭めない。Sliderは整数50～200を保持し、`rate = value / 100`、
  `value = round(rate * 100)`の一か所で変換する。SpinBoxは小数2桁・0.05刻みで、
  編集途中の不完全な文字列を送らないようkeyboard trackingを無効にする。Controllerが
  UI範囲外の速度を持つ場合はSlider／SpinBoxを無効化して実値を「操作範囲外」と明示し、
  有効なままの1.0倍resetから通常範囲へ復帰できるようにする。暗黙のclampは行わない。
- **双方向同期**: Slider／SpinBoxのユーザー変更を互いへ反映してControllerへ1回だけ送り、
  Controller通知は両Widgetへ反映するだけでsetterへ返送しない。Widget更新は
  `QSignalBlocker`で囲み、同期Signalによる再帰・二重Backend呼出を防ぐ。
- **プリセットとreset**: 0.50 / 0.75 / 1.00 / 1.25 / 1.50 / 2.00を一つの定数で定義する。
  「1.0倍に戻す」は速度だけを1.00へ戻し、ピッチ補正状態は変えない。同値はno-op。
- **ピッチモード**: 補正ONは速度だけを変えて音高をおおむね維持するtime-stretch、
  OFFはレコード回転数変更のように速度と音高が連動するvarispeed。文字とツールチップで
  区別し、色だけに依存しない。切替は再生中も即時反映する。
- **セッション内設定**: sourceなしでも変更できる。速度・ピッチ変更はload／再生状態／positionを
  操作しない。直接load、プレイリスト曲切替、自動次曲、Repeat ONE／ALL、シャッフルでも
  1.00へ戻さず維持する。再起動後も`SettingsSession`が速度とピッチ補正を復元する。

### 3.3 ショートカット（P3-B）

- `ShortcutManager`は`PlaybackController`と`PlaylistPlaybackController`だけを受け取り、
  `QShortcut`をWindowShortcutとして生成する。`MainWindow`はmanagerを保持するだけで、
  個々の操作ロジックを持たない。
- 再生、停止、相対シーク、前後曲、音量、mute、速度、pitch、repeat、shuffleを
  `README.md`の固定表へ割り当てる。連続入力が必要なシーク・音量・速度だけauto repeatを許可する。
- 文字／数値入力、編集可能なComboBox、ボタン上のSpace、モーダル表示中は
  `ShortcutOverride`で明示的に抑止し、通常のWidget操作を奪わない。抑止対象は
  `ShortcutSpec`に登録されたキー組合せだけとし、既存のCtrl+O／Ctrl+Shift+Oや
  編集Widget自身のCtrl+C／Ctrl+Vは通過させる。
- 相対値はControllerの公開propertyから計算する。シークは0とdurationへclampし、
  音量は0.0～1.0、速度は製品UI範囲0.50～2.00を超える要求を送らない。

定義は不変な`ShortcutSpec(action_id, sequence, description, auto_repeat)`として
`ui/shortcuts.py`の一か所へ集約する。

| キー | 操作 | auto repeat |
|---|---|---|
| `Space` | 再生／一時停止 | なし |
| `S` | 停止 | なし |
| `J` / `L` | 10秒戻る／進む | あり |
| `Shift+J` / `Shift+L` | 60秒戻る／進む | あり |
| `Alt+Left` / `Alt+Right` | 前の曲／次の曲 | なし |
| `Ctrl+Up` / `Ctrl+Down` | 音量を0.05上げる／下げる | あり |
| `M` | mute切替 | なし |
| `X` / `C` | 速度を0.05下げる／上げる | あり |
| `Z` | 速度を1.00倍へ戻す | なし |
| `P` | ピッチ補正切替 | なし |
| `R` | リピートモード切替 | なし |
| `Ctrl+H` | シャッフル切替 | なし |

#### QtMultimediaBackend（P1-B）

- **所有**: `QMediaPlayer(parent=self)` と `QAudioOutput(parent=self)` を持ち、
  `setAudioOutput` で結び付ける。Backend の破棄で両方とも破棄される。
  外部へは公開せず、UI と Controller は Qt の型に触れない。
  P5-A で QAudioBufferOutput も所有し `setAudioBufferOutput` で結び付けるが、
  これは `PlaybackBackend` の契約ではなく Qt 固有の補助ポートとして扱う（§6.2）。
- **状態**: Qt は source 未設定でも `StoppedState` を返すため、`NO_MEDIA` と `STOPPED` を
  Qt の値だけでは区別できない。Backend が現在の source を内部で保持して判定する
  （公開契約へ source プロパティは追加しない。公開 source は Controller の責務）。
  `setSource` は `mediaStatusChanged` を同期的に出す一方で `playbackStateChanged` を
  出さないため、`load` で状態を明示的に評価し `NO_MEDIA` → `STOPPED` を 1 回だけ通知する。
  状態の保持と通知は 1 か所へ集約し、同値は重複通知しない。これにより
  **`state` プロパティと最後に通知した状態が常に一致する**。同値抑制は状態のみに適用し、
  位置や duration へは広げない。
- **enum 写像**: `QMediaPlayer.MediaStatus` の 8 値と `QMediaPlayer.PlaybackState` の 3 値を
  明示的な写像表で 1 対 1 に変換する。既知値を既定値へ丸めない。
  写像表の鍵集合が Qt の enum 全値と一致することをテストし、
  Qt の更新で値が増えた場合に失敗して対応漏れを検知する。写像表は製品の公開APIではなく、
  モジュールprivateな実装詳細とする。
- **エラー写像**: `ResourceError` / `FormatError` / `NetworkError` / `AccessDeniedError` を
  対応するコードへ写し、`NoError` からは `PlaybackError` を作らない。
  未知の Qt 値だけを `UNKNOWN_ERROR` とする。`detail` には Qt の enum 名・`errorString`・
  現在の source を入れ、ユーザー向け `message` へは混ぜない。
  通常の再生エラーのログ記録は Controller の責務のため、Backend では重複して記録しない。
- **変換境界の例外**: PySide6 はスロット内の例外を呼び出し元へ伝播させず処理を継続する
  （P0-C で確認）。状態・メディア状況・エラーの各変換では例外を放置せず、
  critical ログと `UNKNOWN_ERROR` の通知という観測可能な失敗へ変換する。
  状態を捏造せず、内部失敗の報告は再入ガードで繰り返さない。
  `try` はQt型の変換と `PlaybackError` の生成だけを囲み、アプリ側Signalの通知は外で行う。
  変換例外のスタックトレースを記録した経路では同じ内部エラーを二重にログへ書かない。
  単純な中継スロット（位置・duration・音量など）へはこの仕組みを広げない。
- **source の差し替え**: 異なるsourceを `load()` すると、再生中・一時停止中を問わず
  `STOPPED` へ1回だけ遷移し、positionは0へ戻る。durationとmedia statusは新しいsourceの
  非同期通知で更新する。先のsourceが読み込み中でも、最後に指定したsourceを有効とする。
  同じsourceを再度 `load()` した場合はQt 6.10.3の実挙動に合わせ、positionだけを0へ戻し、
  読み込み済みのdurationを保持して `LOADING` / `LOADED` を再通知しない。
- **読み込み通知順**: 有効な source は Controller が保持して `source_changed` を通知してから
  Backend の `load()` へ渡す。Backend が読み込み状態・再生状態・エラーを同期通知しても、
  UI は必ず新しい source を先に認識できる。通常の読み込み失敗は Python 例外ではなく
  `media_status_changed(INVALID_MEDIA)` と `error_occurred` で通知する。

#### 配線と UI（P1-C）

- **`app.py` が composition root**。`QtMultimediaBackend` → `PlaybackController` →
  `MainWindow` の順に組み立てる。具体的な Backend を知ってよいのは `app.py` と
  Backend 自身、およびそのテストだけで、`ui/` からの import は AST 検査で禁止している。
  `__main__.py` は `app.run()` を呼ぶだけにし、組み立てを重複させない。
- **寿命**: Backend・Controller・MainWindow はいずれも QObject の親を持たず、
  `run()` が保持する `PlayerComposition` への参照でイベントループ中の寿命を保証する。
  グローバル変数へは置かず、MainWindow に Backend を所有も参照もさせない。
- **`MainWindow`**: レイアウト骨格、「ファイル」メニュー（開く / 終了）、
  現在のファイル名表示（フルパスはツールチップ）、ウィンドウタイトル、ステータスバー。
  再生操作は持たず `PlayerControls` へ委譲する。ファイルダイアログのフィルターは
  ユーザー補助にすぎず、「すべてのファイル」を必ず選べるようにする
  （拡張子や `QMediaFormat` で再生可否を断定しないため）。
- **`PlayerControls`**: 再生 / 一時停止 / 停止、シークバー、現在時間と総時間、
  音量、ミュート、再生状態ラベル。受け取るのは `PlaybackController` だけ。
  状態に応じた表示と活性の更新は 1 つのメソッドへ集約する。
  ミリ秒 → 表示文字列の変換は純粋関数（`m:ss` / `h:mm:ss`、負値は 0 表示）。
- **シークバー**: 値はミリ秒。`duration_changed` で最大値を更新し、0 なら無効化して
  位置を 0 へ戻す。**ドラッグ中は Backend の位置通知でつまみを戻さず**、
  対応する `sliderPressed` から継続している `sliderReleased` の 1 回だけ `seek()` する
  （毎イベント seek しない）。source変更またはduration 0でドラッグを無効化し、
  古いreleaseを新しいsourceへ適用しない。
  `seek()` の `ValueError` は握り潰さず、プログラミングエラーとして表面化させる。
- **音量とミュート**: UI は 0〜100 の整数、Controller へは 0.0〜1.0 の float。
  Controller 由来の更新は `QSignalBlocker` で反映し、フィードバックループを作らない。
- **エラーとログの境界**: UI は `PlaybackError.message` だけをステータスバーへ出す。
  `detail` は画面へ出さない。通常の再生エラーでモーダルダイアログを出さない。
  現在のsourceで具体的な `PlaybackError` を表示した後の `INVALID_MEDIA` は無視し、
  一般メッセージで上書きしない。通知順が逆なら後着の具体的なエラーを表示する。
  source変更時にこの優先状態をリセットし、source解除時は初期メッセージへ戻す。
  技術詳細のログ記録は Controller の責務で、UI では重複して記録しない。
- **状態表示の分担**: 再生状態ラベルは `PlaybackState`（再生中 / 一時停止 / 停止）、
  ステータスバーは `MediaStatus` とエラーという一時的な情報。両者を 1 つの enum へ統合しない。
- **ログ**: `services/logging_setup.py` が `%LOCALAPPDATA%\sdp\logs\sdp.log` へ
  RotatingFileHandler（UTF-8、1MB × 5 世代）を設定し、多重初期化ではハンドラーを増やさない。
  同じ出力先での再設定は既存ハンドラーを再利用し、異なる出力先なら既存ハンドラーを
  ルートロガーから外して閉じた後、新しい出力先のハンドラーへ置換する。
  未捕捉例外は `sys.excepthook` で記録する。Qt の `qInstallMessageHandler` は
  ADR-0001 の制約 11（終了前の解除が必要）と併せて後続で扱う。

---

## 4. シグナルとデータフロー

```
UI 操作 → PlayerControls ──(メソッド呼び出し)──→ PlaybackController ──→ PlaybackBackend
                                                   │ current_entry_changed(entry_id)
Backend ──position_changed / duration_changed──→ Controller ──→ PlayerControls / WaveformPanel
Backend ──media_status_changed(END_OF_MEDIA)──→ Controller（次曲を決定）
Backend ──error_occurred(PlaybackError)──→ Controller（該当エントリにエラーを記録し方針を適用）→ PlaylistModel
QAudioBufferOutput ──audioBufferReceived──→ PcmTap（軽量変換のみ）→ PcmRingBuffer
QTimer(30FPS) → SpectrumWidget: PcmRingBuffer.snapshot() → FFT → 描画
WaveformAnalysisService(worker) ──partial / finished(WaveformData)──→ WaveformPanel ──→ WaveformWidget
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
| WaveformAnalysisService 用 QThread | QAudioDecoder、NumPy縮約、npz cache I/O、LRU | 現在sourceの1ジョブ。source切替時は先行tokenを論理キャンセルする |
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

**P5-A の実装方針**: 上記の実測に依存した設計にはせず、`PcmRingBuffer` は
`threading.Lock` で自己完結して thread-safe にした。ロックは **memcpy の間だけ**保持し、
**FFT 中は保持しない**（`snapshot()` はコピーを返し、解析はロック外で行う）。
受信スレッドが変わっても壊れないため、実測結果は「ワーカースレッド化が不要」という
性能上の根拠として使い、正しさの前提としては使わない。

---

## 6. PCM リングバッファと FFT・レベル・描画更新（P5-A / P5-B で実装）

### 6.1 QAudioBufferOutput の実測結果

P0-C（[p0-report.md §8](./p0-report.md#8-p0-c-qaudiobufferoutput-による-pcm-取得と可視化適合性)）に加え、
P5-A の着手前に pause / stop / 再生中の直接 `setSource` / 終端通知を probe で確認した。

| 項目 | 実測（PySide6 6.10.3 / Qt 6.10.3 / Windows 11） |
|---|---|
| `QAudioBufferOutput` / `setAudioBufferOutput` / `audioBufferOutput` | いずれも利用可能 |
| シグナル | `audioBufferReceived(QAudioBuffer)`（引数 1 つ） |
| 通常の `QAudioOutput` との併用 | 音声出力は継続。`errorOccurred` は 0 件 |
| 受信スレッド | **GUI スレッド**（`MainThread` / `Qt mainThread`） |
| 開始前後の順序 | `setSource` → `LoadingMedia` / `LoadedMedia`（**buffer は届かない**）→ `play()` → `PlayingState` → `BufferingMedia` / `BufferedMedia` → 以後 buffer が届く |
| pause 中 | **buffer は届かない**（一時停止した時点で通知が止まる） |
| stop 時 | **buffer は届かない**。`StoppedState` + `LoadedMedia` になる |
| 再生中の直接 `setSource` | `StoppedState` → `LoadedMedia` → `LoadingMedia` → `LoadedMedia` → `play()` で新 format の buffer だけが届く（**旧 format の残留 0 件**） |
| `playbackRate` 変更 | format は不変（Float / 44100Hz / 2ch、frameCount も同じ）。変わるのは通知間隔だけ |
| pitch compensation 切替 | format は不変 |
| SampleFormat | **WAV は Int16、MP3 は Float**（P0-C の 6 形式実測と一致。`UInt8` / `Int32` は未観測） |
| channel count / sample rate | 2ch / 44100Hz（Opus のみ 48000Hz） |
| byteCount と duration | Int16 stereo は 1frame 4byte。1 buffer は 4096frame ≒ 92.9ms（コーデックにより 47〜4608frame と一定でない） |
| **終端の空 buffer** | **`EndOfMedia` の直前に「format 未設定（`Unknown` / rate 0 / ch 0）で `constData()` が `None`」の buffer が 1 件届く**（P5-A で新たに判明） |

終端の空 buffer は既定値へ丸めず、無効 buffer として件数を数えて捨てる。

### 6.2 PlaybackBackend との境界

- **`PlaybackBackend` の一般インターフェースへ PCM シグナルや Qt の PCM 型を追加しない。**
  mpv へ差し替えた場合に持ち込めないため、PCM 取得は **Qt Multimedia 固有の補助ポート**として扱う。
- `QtMultimediaBackend` が `QAudioBufferOutput(parent=self)` を所有し、`setAudioBufferOutput`
  で `QMediaPlayer` へ結び付ける。format を指定しないため、デコード直後のネイティブ形式が届く
  （再サンプリングを挟まない）。通常の `QAudioOutput` はそのまま維持する。
- 公開するのは `audio_buffer_output` という狭い property だけで、**composition root と
  `PcmTap` 以外は参照しない**。UI 層からの `QAudioBufferOutput` / `QAudioBuffer` /
  `QtMultimediaBackend` の import は AST 検査で禁止している。
- `FakePlaybackBackend` へ QAudioBuffer 概念を導入しない（テストで検査する）。
- 接続は `app.py` の 1 行（`pcm_tap.connect_audio_buffer_output(backend.audio_buffer_output)`）だけ。

### 6.3 PcmTap（音声コールバック）

`services/pcm_tap.py`。`PlaybackController` と `PcmRingBuffer` だけを受け取る QObject。

コールバック（`handle_audio_buffer`）で行ってよいのは次だけとする。

1. buffer の妥当性確認（sample rate / channel count / sample format / `constData()`）
2. bytes 化（`core/analysis/pcm.py`。波形解析の QAudioDecoder 経路と**同じ変換を再利用**する）。
   **bytes 化は 1 buffer につき 1 回だけ**で、mono と L / R を同じ bytes から派生させる
3. NumPy による正規化と mono / 左 / 右の派生（`waveform.pcm_bytes_to_channels` を再利用）
4. mono / 左 / 右の 3 本のリングバッファへの追記
5. 軽量な `sample_rate_changed` / `channel_count_changed` の通知

**行わない**: FFT、band 集約、**Peak / RMS、dBFS 変換、Peak hold**、QTimer、描画、
status bar 更新、ファイル I/O、Model / Controller 操作、
大量のログ出力（無効 buffer のログは 100 件ごとに 1 回へ間引く）。

#### PcmChunk（P5-B）

`core/analysis/pcm.py` の frozen dataclass。`mono` / `left` / `right`（いずれも 1 次元
float32・同じ長さ・read-only）と `sample_rate` / `channel_count` を持つ。

- **1 回の bytes 化と 1 つの `(frame数, channel数)` 配列から 3 本を派生させる。**
  mono 化してから別経路で再び bytes 化して L / R を取り出すことはしない。
- mono 入力（1ch）では `left` と `right` を `mono` と同値へ複製する。
- 2ch 以上では `left` = channel 0、`right` = channel 1。**3ch 以上の残り channel は
  L / R には使わず**、`mono` の全 channel 平均にだけ含める（多 channel の個別表示は行わない）。
- 有限性・-1〜1・shape 一致を検証し、呼び出し側の配列をコピーして read-only 化する。
  QAudioBuffer / QAudioFormat / memoryview は保持しない。
- `audio_buffer_to_mono()` は `PcmChunk.mono` を返す**互換 wrapper**として残し、
  波形解析（`WaveformAnalysisService`）は従来どおり mono だけを使う。
- 非有限値と範囲外値の寄せ方は mono と各 channel で独立に行う。mono は従来どおり
  「channel 平均のあとに寄せる」ため、P5-A までの波形解析の値と一致する。

- **QAudioBuffer とその memory view をコールバックの外へ持ち出さない。** 必要な PCM だけを
  新しい float32 配列へコピーする（保持していないことをテストで検査する）。
- PySide6 はスロット内の例外を呼び出し元へ伝播させないため（P0-C）、**例外を外へ漏らさず**、
  無効 buffer・未対応 format は件数を数えて捨てる。再生エラーへは変換しない。
  予期しない例外は初回だけtracebackを記録し、継続時は100件ごとの警告へ間引く。
- **clear 契約**: `source_changed` で即時 clear。`state_changed` が `STOPPED` / `NO_MEDIA` に
  なったら clear（`END_OF_MEDIA` から次曲へ進む場合も前曲 PCM を持ち越さない）。
  **`PAUSED` では保持する**（最後のフレームを静止表示するため）。
- **format 変更時**: sample rate が変わったら、新しい rate に合う容量へ**作り直してから**
  追記する。旧 format のサンプルを新 format へ混ぜない。
- **可視化snapshotの原子性**: PcmTap全体の短時間lockで、format更新とmono／L／Rへのappendを
  1単位として保護する。`snapshot_visualization()`も同じlock内でsample rate／channel countと
  3配列をコピーし、異なるbuffer世代やformatを混在させない。lockはコピー後に解放し、
  FFT・Peak／RMS・描画中は保持しない。個々の`PcmRingBuffer`のthread-safe契約も維持する。
- **shutdown契約**: shutdownは終端操作とする。接続とController監視を解除してPCMをclearし、
  queue済みbufferや直接slot呼出を含む以後の入力を無視する。shutdown後の再接続は禁止する。

### 6.4 PcmRingBuffer

`core/analysis/ring_buffer.py`。Qt 非依存。

- **容量は固定**。標準契約は `round(sample_rate × 2.0)`（48kHz で 96,000 sample = 384KB、
  44.1kHz で 88,200 sample = 345KB）で、FFT 長を下限とする。sample rate が判明する前は
  48kHz を既定値として構築し、最大想定 192kHz へ固定して巨大化させない。
- dtype は float32、1 次元。満杯時は古いサンプルを上書きし、**全履歴は保持しない**。
  1 回の `append` が容量を超える場合は末尾 capacity 分だけを保持する。
- `append` ごとの `np.concatenate` や sample 単位の Python ループを使わない
  （事前確保した配列への 2 分割スライス代入だけで wrap を処理する）。
- NaN / inf は保持しない（有限性の検査に失敗したときだけ 0 と ±1 へ寄せる）。
- **`snapshot(n)` は常に長さ `n` の独立した read-only 配列**を返す。保持数が `n` 未満のときは
  **左側を 0 で埋める**（無効フレームにはしない。起動直後や曲頭でも FFT の shape が安定する）。
- **thread-safe**。`threading.Lock` を **memcpy の間だけ**保持し、**FFT 中は保持しない**。
  受信が GUI スレッドである実測（§5）は性能上の根拠として使い、正しさの前提にはしない。

**P5-B では同じ契約のリングバッファを 3 本持つ**（mono / 左 / 右）。stereo な 2 次元
バッファを導入して `PcmRingBuffer` の契約を複雑化させない。3 本でも 48kHz × 2 秒 ×
float32 で合計約 1.1MB（実測 1,125KB = 96,000 sample × 3 × 4byte）であり、固定容量で十分小さい。

- 公開 API は `snapshot(n)`（mono の互換 API）、`snapshot_mono(n)`、`snapshot_stereo(n)`と、
  format＋mono／L／Rを同一世代で返す`snapshot_visualization()`。
  `snapshot_stereo`と統合snapshotは左右で**別の**read-only配列を返す。
- sample rate 変更・**channel count 変更**・source 変更・stop では**3 本すべて**を
  clear / 作り直す（旧 format や旧 channel layout のサンプルを混ぜない）。
- Panelは1 tickにつき`snapshot_visualization()`を1回だけ呼び、formatと3配列をまとめて取得する。

### 6.5 SpectrumFrame と FFT 設定

`core/analysis/spectrum.py`（すべて純粋関数・Qt 非依存で単体テストできる）。

`SpectrumFrame` は frozen dataclass で `frequencies_hz` と `levels_db` だけを持ち、
shape 一致・float32・1 次元・有限性・周波数の非負と昇順・0dB 以下を検証してから
コピーして read-only 化する。QColor 等の描画情報は持たない。
0〜1 の正規化済み表示値は field に持たず、描画側が dB 範囲から変換する。

初期設定は 1 か所の定数へ集約する。

```python
FFT_SIZE = 4096
SPECTRUM_BAND_COUNT = 96
SPECTRUM_MIN_HZ = 30.0
SPECTRUM_MAX_HZ = 20_000.0
SPECTRUM_DB_FLOOR = -90.0
SPECTRUM_FPS = 30
SPECTRUM_TIMER_INTERVAL_MS = 33
SPECTRUM_ATTACK = 0.65
SPECTRUM_RELEASE = 0.15
```

処理順は次のとおり。

1. 最新 `FFT_SIZE` sample を取得（長い場合は最新側、短い場合は**左 0 padding**）
2. DC offset 除去（平均を引く。直流は表示帯域を持ち上げない）
3. Hann 窓（`np.hanning`）
4. `np.fft.rfft` → 振幅
5. **窓による振幅補正**: `2.0 / window.sum()`。Hann のコヒーレントゲイン 0.5 と実正弦波の
   正負対を合わせて補正するため、**0dBFS の正弦波が約 0dB になる**
   （実測: 100Hz で -1.2dB、1kHz / 10kHz で -0.6dB。band と bin の量子化による差であり、
   -6dB や +6dB のような系統的なずれはない）。振幅 0.5 の正弦波は約 -6dB。
   音響測定器としての校正精度までは要求しない。
6. dB 変換: `magnitude = max(magnitude, 1e-12)` としてから `20 * log10`。log(0) と 0 除算を避ける
7. floor clamp: `clip(db, SPECTRUM_DB_FLOOR, 0.0)`
8. band 集約（§6.6）
9. 時間平滑化（§6.7）

**上限周波数は Nyquist 以下へ制限する**（`effective_max_hz = min(SPECTRUM_MAX_HZ, sample_rate / 2)`）。
有効帯域が `SPECTRUM_MIN_HZ` 以下になる極端な低 sample rate では、band を捏造せず
**空フレーム**を返す。

### 6.6 対数周波数バンド

線形 bin をそのまま横軸へ描かず、**30Hz〜min(20kHz, Nyquist) を 96 band** へ集約する。
境界は `np.logspace`、band の代表周波数は境界の**幾何平均**。

- 対象 bin がある band は、視認性を優先して **band 内の最大 dB** を採る。
- 対象 bin が無い band（4096 点 / 48kHz では分解能 11.7Hz のため、主に 120Hz 未満）は、
  **最大値の複製ではなく `np.interp` による補間**で埋める。同じ bin のピーク値が
  複数 band へ複製されて不自然に広がるのを避けるため。

### 6.7 時間平滑化（SpectrumProcessor）

生の FFT をそのまま描くとちらつくため、attack と release を分けて平滑化する。

```text
coefficient = attack if 新値 > 前回値 else release
表示値 = 前回値 + coefficient × (新値 - 前回値)
```

係数は 0 より大きく 1 以下（1 で即時追従）。既定は attack 0.65 / release 0.15 で、
立ち上がりが速く減衰が滑らかになる。無音が続けば floor へ収束する。

`SpectrumProcessor` が前フレームと sample rate を保持し、
**sample rate 変更・source 変更・停止で `reset()`** する。
**QWidget へ平滑化状態を持たせない。**

### 6.8 pause / stop / source 変更の契約

スペクトラムとレベルメーターで同じ契約とする。

| 状態 | 挙動 |
|---|---|
| `PLAYING` | 約 30FPS で最新 PCM を解析・描画する（FFT と Peak / RMS の両方） |
| `PAUSED` | **最後のフレームを静止表示**し、新しい解析を止める（タイマー停止）。**Peak hold の時間も進めない**（タイマー停止時に経過時間の時計を無効化する）。再開で追従を再開 |
| `STOPPED` / `NO_MEDIA` | リングバッファ 3 本 clear、SpectrumProcessor / LevelProcessor reset、旧フレーム破棄、タイマー停止、プレースホルダー表示 |

`END_OF_MEDIA` から次曲へ進む場合も、前曲の PCM とフレームを次曲の表示へ持ち越さない。

source 変更時は `PlaybackController.source_changed` を PcmTap と SpectrumPanel の**両方**が
監視し、リングバッファ 3 本 clear / format 状態 reset / 両 Processor reset（Peak hold 破棄）/
旧フレーム破棄を即時行う。stateが`PLAYING`のままでも旧フレームを必ず捨て、新 source の最初の PCM が
届くまではプレースホルダーを表示する。
**波形解析サービスとは完全に独立**（相互に参照しない）。

### 6.9 SpectrumWidget

`QWidget` を直接継承し、**QPainter で一括描画**する。objectName は `spectrumWidget`、
accessibleName は `スペクトラム`。minimumHeight 130、sizePolicy は Expanding / Fixed。
**マウス操作もフォーカスも持たない**（`NoFocus`。シークは波形側の責務）。

- **96 個の子 Widget や QGraphicsItem を作らない。** 1 回の paintEvent で全 bar を描く。
- pixel 幅より band 数が多い場合は、隣接 band の最大値へ**間引く**（bar 数 ≤ min(band 数, 幅)）。
- palette ベースで描く（固定 RGB に依存しない）: 背景 `Base`、grid `Mid`、bar `Highlight`、
  文字 `Text` / `PlaceholderText`。状態を色だけで伝えず、-20 / -40 / -60 / -80dB の
  基準線と短い日本語の状態文字を併用する。
- 状態文字は Widget 内の 1 か所へ QPainter で描く（別 QLabel の表示切替で Panel 高を変動させない）。

### 6.9.1 Peak / RMS レベル（P5-B）

`core/analysis/level.py`（すべて Qt 非依存で単体テストできる）。

**このメーターは出力音量計ではない。** `QAudioBufferOutput` が渡すのは
速度・ピッチ処理と**音量・ミュートを適用する前**のデコード済み PCM なので
（[p0-report.md](./p0-report.md) §8.6、§8.7）、表示しているのは**音源信号のレベル**である。
音量 0 でもミュート中でもメーターは振れる。
LUFS、true peak、inter-sample peak、K 特性、ラウドネス正規化は扱わない。

設定は 1 か所の定数へ集約する。

```python
LEVEL_DB_FLOOR = -90.0  # スペクトラムの下限と揃える
PEAK_HOLD_SECONDS = 1.0
PEAK_HOLD_RELEASE_DB_PER_SECOND = 24.0
LEVEL_WINDOW_SIZE = 4096  # FFT 長と同じ。48kHz で約 85ms
```

- **Peak**: `max(abs(samples))`。空入力は 0。dBFS は `20 * log10(max(peak, 1e-12))` を
  floor〜0dB へ clamp する（1.0 が 0dB、0.5 が約 -6.02dB、0 は floor）。
- **RMS**: `sqrt(mean(samples ** 2))`。float32 の二乗和で精度が落ちるため **float64 へ昇格**し、
  **入力配列は変更しない**（read-only の snapshot をそのまま渡せる）。振幅 1.0 の正弦波は
  約 -3.01dB、振幅 0.5 の正弦波は約 -9.03dB。RMS は通常 Peak 以下になる。
- **窓長は毎 tick 4096 sample だけ**。リングバッファ 2 秒分すべてを毎 tick 計算しない。
- NaN / inf を含む PCM は floor へ黙って丸めず明示的に失敗させる
  （リングバッファ側で既に寄せているため、通常は届かない）。

`StereoLevelFrame` は frozen dataclass で、左右の Peak / RMS / Peak hold の **6 つの
dBFS 値だけ**を持つ。bool を数値として受理せず、有限性・floor 以上 0dB 以下・
`RMS ≦ Peak ≦ Peak hold`（丸め誤差の範囲）を検証する。QColor や時刻オブジェクト、
NumPy 配列は持たない。0〜1 の正規化済み表示値も持たず、描画側が dB 範囲から変換する。

`LevelProcessor` が Peak hold の状態を持つ。

- **hold は左右で独立**に保持・減衰させる（片 ch の Peak 更新が他 ch の hold を延命しない）。
- 現在 Peak が hold 以上なら**即時追従**し、保持時間を測り直す。
- 保持時間（既定 1.0 秒）を過ぎた**ぶんだけ** `24.0 dB/秒` で減衰させる。減衰量は
  経過秒に比例するため、**タイマー FPS の揺れやこま落ちで減衰速度が変わらない**
  （1 tick で 2 秒進めた場合と 2 tick で 1 秒ずつ進めた場合が同じ結果になる）。
- 減衰中も**現在 Peak と floor より下へは落とさない**。減衰線が現在Peakへ追いついた場合は、
  そのPeakを新しいholdとして保持時間を測り直す。
- `reset()` で hold と経過時間を捨てる（stop / source 変更 / format 変更）。
- **`QElapsedTimer` は Panel 側が持ち**、Processor へは経過秒だけを渡す（純粋関数に近い状態機械）。
  pause 中は `process` を呼ばないため、hold の時間も進まない。

### 6.9.2 LevelMeterWidget（P5-B）

`QWidget` を直接継承し、**QPainter で一括描画**する。objectName は `levelMeterWidget`、
accessibleName は `レベルメーター`。minimumHeight 78（プレイリストを圧迫しないよう
スペクトラムより低く抑える）、sizePolicy は Expanding / Fixed。
**マウス操作もフォーカスも持たない**（`NoFocus`）。

- L / R の**横長バー 2 本**を 1 回の paintEvent で描く。チャンネルや目盛ごとに子 Widget を作らない。
- **Peak と RMS を色だけで区別しない**: RMS は塗りつぶしバー、Peak は細い縦線（1px）、
  Peak hold は太い短線（3px）。-60 / -40 / -20 / -6dB の基準線と数値目盛も併記する。
- palette ベースで描く（固定 RGB に依存しない）: 背景 `Base`、目盛と枠 `Mid`、
  RMS `Highlight`、Peak と L / R ラベル `Text`、Peak hold `Link`、状態文字 `PlaceholderText`。
- floor 以下の値は描かない（無音では枠と目盛だけになる）。
- 状態文字（source なし / 停止中 / PCM 待機中 / 失敗）は Widget 内の 1 か所へ QPainter で描く。

### 6.10 SpectrumPanel とタイマー

`SpectrumPanel(playback: PlaybackController, pcm_tap: PcmTap)` だけを受け取る。
**スペクトラムとレベルメーターの両方を持ち**、同じ PcmTap と同じ tick を共有する
（`SpectrumWidget` と `LevelMeterWidget` を縦に並べる）。Panel を可視化ごとに分けて
MainWindow の依存を増やしたり、汎用オーディオグラフやイベントバスを導入したりはしない。

1 tick の処理順は次のとおり。

1. sample rate／channel count／mono（FFT長4096）／L／R（Level窓長4096）の統合snapshotを**1回**取得
2. 実経過秒を `QElapsedTimer.restart()` で取り出す
3. FFT + band 集約 + 平滑化を**最大 1 回**
4. Peak / RMS + Peak hold を**最大 1 回**
5. 2 つの Widget を更新する

- 間隔は約 33ms。**`Qt.TimerType.PreciseTimer` を指定する**（既定の CoarseTimer は Windows の
  15.6ms 粒度のため実測 21FPS 程度に落ちたが、PreciseTimer では実測 30.3FPS を得た）。
- **タイマー 1 tick で FFT と Level 計算は各最大 1 回。** 処理中の再入は専用フラグで防ぐ。
- タイマーを動かす条件は「`PLAYING` かつ Widget 表示中かつ top-level window が最小化されていない」。
  それ以外では停止する（SPEC-04）。最小化では子へ hideEvent が来ないため、
  `showEvent` で top-level window へ event filter を入れて `WindowStateChange` を監視する。
  `PcmTap`はP5-B以降の複数可視化で共用するため可視性では切断せず、固定容量リングバッファへの
  受信を継続する。停止するのはWidget固有のFFT・平滑化・描画だけとする。
- タイマー停止後に古い timeout が処理されても安全に何もしない。
- shutdownは終端操作とし、Signalとevent filterを解除する。以後のsource／state／sample rate通知、
  show／最小化復帰、古いtimeoutのいずれでもタイマーや表示状態を再開・変更しない。
- `QApplication.processEvents()` はタイマーハンドラー内で呼ばない。
- 最初の PCM が届く前（sample rate が 0）は FFT せずプレースホルダーのまま待つ。
- **解析の失敗は再生を妨げない。** 例外はログへ残してプレースホルダー表示へ切り替える。
  Controller へは何も要求しない。
- **例外境界は解析ごとに独立させる**（P5-B）。
  - FFT 失敗: スペクトラムだけ失敗表示。**レベルメーターは更新を続ける**
  - Level 失敗: レベルメーターだけ失敗表示。**スペクトラムは更新を続ける**
  - 共通の snapshot 失敗: 両方を失敗表示にしてタイマーを止める
  - **両方が失敗したときだけ**タイマーを止める（ログの大量出力を避ける）。
    source 変更で失敗状態から復帰できる
- Peak hold の減衰はタイマー回数ではなく `QElapsedTimer` の実経過秒で進める。
  タイマーを止めるとき（pause / 非表示 / 最小化 / 停止）は時計を無効化し、
  **止まっていた実時間を減衰へ数えない**。

### 6.11 MainWindow と app.py

`MainWindow` は PlayerControls → SpeedPanel → WaveformPanel → **SpectrumPanel
（スペクトラム + レベルメーター）** → PlaylistView の順に配置するだけで、
FFT・Peak / RMS・Peak hold・タイマー・リングバッファ・LevelProcessor を持たない。
レベルメーターは `SpectrumPanel` の中へ入れるため、MainWindow の依存も既定高も増やさない。
可視化が 2 つに増えた分だけ既定ウィンドウ高を 540 → 700 へ広げ、プレイリスト領域を残す。
`MainWindow` へ具体 Backend は渡さない（`PcmTap` は既存の `WaveformAnalysisService` と
同じく composition 済みサービスとして受け取る）。

`app.py`（composition root）が `QtMultimediaBackend` → `PlaybackController` →
`PcmTap` → `QAudioBufferOutput` への接続 → `MainWindow` の順に組み立て、
`PlayerComposition` が `pcm_tap` を保持する。**`build_player()` だけではタイマーを開始しない。**
終了時は可視化を先に止める（`spectrum_panel.shutdown()` → `pcm_tap.shutdown()` →
`waveform_analysis.shutdown()` → `metadata_reader.shutdown()` → settings / playlist 保存）。
破棄済み QObject へシグナルが飛ばないようにするため、この順序を変えない。

### 6.12 変えていない契約

`PlaybackBackend` のインターフェース、`PlaybackController`（PCM 配列を持たせない）、
`WaveformAnalysisService`、波形キャッシュ schema、`settings.json`、`playlist.json`、
`PlaylistModel`、`MetadataReader` はいずれも変更していない。
`core/analysis/pcm.py` は波形解析にあった QAudioBuffer 変換を**移設して共有した**もので、
振る舞いは同じ（channel count の検査だけ追加した）。

P5-B で Peak / RMS レベルメーターを追加したが、`PlaybackBackend` / `PlaybackController` /
`FakePlaybackBackend` / 各 schema はいずれも変更していない。`PcmTap` に増えたのは
L / R リングバッファと `snapshot_stereo` / `channel_count` / `channel_count_changed` だけで、
音声コールバックへ FFT・Peak / RMS・描画は持ち込んでいない。
`pcm_bytes_to_mono()` の mono 値も P5-A と同じ（channel 平均後に寄せる）。

**P5-B 完了時点が、当初計画上の「sdp らしい初回完成版」である**
（[development-plan.md §5](./development-plan.md)）。ただし P3〜P5 の実画面・実マウス・
実音による手動受け入れはリリース前ゲートとして残る。

## 7. 波形解析・縮約・キャッシュ

- **データ型**: `WaveformData`は1次元float32の`minimum`／`maximum`、実bucket幅、
  duration、completeを持つ。shape、有限性、-1～1、min≦maxを検証し、入力をコピーして
  read-only化する。QAudioBuffer、path、例外は保持しない。
- **PCM正規化**: QAudioFormatのUInt8／Int16／Int32／FloatをNumPyで[-1, 1]へ正規化し、
  interleaved channelをframeごとに平均してmono化する。frame途中のbytes、channel／sample rate
  不正、未知formatは明示的に失敗する。Windows上の実測ではWAVがInt16、MP3がFloatだった。
- **増分縮約**: 基準bucketは20ms。`frames_per_bucket = max(1, round(sample_rate * 0.020))`
  とし、実bucket幅はframe数／sample rateから算出する。`WaveformReducer`は完成bucketの
  min/maxと1bucket未満の端数だけを保持し、chunk境界を跨ぐ端数を次chunkへ引き継ぐ。
  完了snapshotだけが最後の不完全bucketを含む。60分では約180,000 bucket、配列本体は約1.44MB。
- **thread境界**: `_WaveformWorker`を専用QThreadへmoveし、そのthread上でQAudioDecoderを生成する。
  bufferReadyごとにbytes化、正規化、縮約を行い、GUIへは不変なWaveformDataだけをqueued Signalで
  返す。最初の完成bucket、その後1024bucket増加または250ms経過でpartialを通知し、完了時は
  必ず最終結果を通知する。GUI threadではdecode、全sample縮約、cache走査、解析中のstatを行わない。
- **現在sourceとcancel**: `WaveformAnalysisService`はPlaybackControllerの`source`と
  `source_changed`だけを使用する。GUI側は単調増加token・path・現在sourceだけを照合し、source変更時は
  workerの開始を待たず直ちに`analysis_cleared`を通知する。size／mtimeの再確認はworkerがpartial・完了・
  cache境界で行い、現在sourceが解析中に変化した場合は専用の`analysis_failed`で必ず要求を終端する。
  source変更とshutdownではthread-safeなcancel状態を即時設定してQAudioDecoderへstopを要求し、workerが
  cancel処理を終えたtokenはregistryから回収する。解析失敗はPlaybackControllerの状態や再生エラーへ
  混ぜない。request生成前のpath事前確認に失敗した場合もtokenを先に採番し、同じpath/tokenで
  `analysis_started`、`analysis_failed`の順に通知してUIの解析状態を必ず終端する。
- **cache key**: 解決済み絶対path、size、mtime_ns、analysis version 1、20ms bucket、mono format
  version 1をcanonical JSONからSHA-256化する。ファイル名はhashだけで、生pathを含めない。
- **npz schema**: minimum、maximum、bucket_duration_ms、duration_ms、analysis_version、
  format_version、file_size、file_mtime_ns、completeを保存する。`allow_pickle=False`で読み、
  必須field、scalar型、dtype、shape、有限性、範囲、complete、key属性を再検証する。
- **保存と破損**: `%LOCALAPPDATA%\sdp\cache\waveforms`へ同一directoryの一時ファイルを
  flush／fsyncしてから`os.replace`する。部分結果は保存しない。zip破損や検証不一致はログへ残し、
  破損cacheを削除してmissとして再解析する。cache失敗は再生や解析済み結果を失敗扱いにしない。
- **LRU**: hit時にcacheファイルのmtimeを利用時刻として更新する。保存後にworker threadで`.npz`だけを
  走査し、mtime・名前の決定的順で500MB以下まで削除する。temp／他形式／現在保存したcacheは削除せず、
  個別削除失敗はログに残して後続を処理する。
- **ライフサイクル**: `build_player()`はserviceを保持するだけでthreadを開始しない。`run()`は
  metadata開始後にserviceをstartし、終了時はmetadataより先にserviceをshutdownする。shutdownは
  token無効化、decoder停止要求、thread quit、明示timeout付きwaitを行う。timeout時は警告後も
  `QThread.terminate()`を使わず終了まで待ち、実行中QThreadを所有したままQObjectを破棄しない。
  Qt内部decodeや注入された同期処理が戻らない場合の厳密な終了期限は保証しない。厳密な期限が
  必要になった場合は解析を終了可能な子プロセスへ隔離する。
- **表示投影**: `project_waveform()`は現在位置を中心とする固定60秒窓だけを、Widget幅と同数の
  read-only `WaveformColumns`へ再集約する。1pixelに複数bucketが重なる場合はminimumの最小と
  maximumの最大を採用し、peakを平均で失わない。全trackを走査せず、表示窓と交差する約3,000
  bucketとpixel列だけを処理する。音源先頭より前、末尾より後、partialの未解析範囲は
  `valid=False`として空白にし、表示窓を音源側へ寄せないため現在位置線は常に中央となる。
- **描画**: `WaveformWidget`はQPaletteのBase／Mid／Text／Highlight／Link／PlaceholderTextを
  使い、各有効pixelにつき最大1本の縦線、振幅0線、中央の現在位置線、drag previewと時刻を
  QPainterで描く。同一data・中心位置・幅の投影はWidget内で再利用する。
- **表示調停**: `WaveformPanel`はControllerのsource／position／durationとServiceの
  started／partial／finished／failed／clearedをWidgetへ接続する。startedでactive path/tokenを
  記録し、一致しない結果は無視する。source変更とclearでは旧波形・active token・dragを即時解除する。
  sourceなし、解析中、部分表示、完了、失敗をWidget内の1か所だけに短い日本語で表示し、生のdecoder
  errorは表示しない。波形がない状態では中央、partial表示中は左上へ描き、別QLabelの表示切替による
  Panel高の変動を作らない。
- **duration**: シーク上限は正の`PlaybackController.duration_ms`を優先し、それが未確定の場合だけ
  completeな`WaveformData.duration_ms`を使う。partialのdurationは総時間として扱わない。
  描画のbucket時刻は常に`WaveformData.bucket_duration_ms`から求め、Controller durationへ引き延ばさない。
- **マウスシーク**: xは固定表示窓の`center - 30秒 + x / width * 60秒`へ写し、0～durationへ
  clampしてhalf-upで丸める。左press時の中心位置をdrag終了まで固定し、move中はpreviewだけを更新、
  releaseで1回だけController.seekへ委譲する。source変更、clear、hide、disableでdragを取り消す。
- **composition**: MainWindowはPlayerControls、SpeedPanel、WaveformPanel、PlaylistViewの順に配置する。
  `app.py`がP4-Aと同じWaveformAnalysisServiceをPanelへ渡し、start／shutdown順序は変更しない。
- P4は解析・cache・追従表示・クリック／ドラッグseekまで実装済み。P5-AでPCM tapとスペクトラムを
  追加した（§6。波形解析とは独立）。P5-Bでレベルメーターを追加する。

---

## 8. プレイリストモデル

- `QAbstractTableModel` を使う。列は 状態アイコン / タイトル / アーティスト / アルバム / 長さ。
  `Qt.UserRole` で entry_id とパスを保持する。
- D&D は外部から `text/uri-list` を受理し、受理順をそのまま表示順とする（PL-01）。
  内部の並べ替えは `moveRows` を用いる。
- 欠損は追加時と復元時に存在チェックを行って `missing` フラグを立て、
  `QStyledItemDelegate` でグレー描画し、再生時はスキップする（PL-05）。
- **重複追加は許可する**（PL-07）。「現在再生中」の表示・波形・メタデータはすべて
  entry_id 単位で扱うため、同一パスの行が複数あっても破綻しない。
- 妥当性は `QAbstractItemModelTester` で常時検証する。
- 永続化は `playlist.json`。
  M3U8 入出力は将来 `persistence.py` に追加する（`#EXTM3U` / `#EXTINF`、UTF-8）。

### 8.1 P2-A〜P2-C1 で確定した契約

実装済みなのは `entry.py` / `model.py` / `persistence.py`（P2-A）、
`ui/playlist_view.py` / `services/playlist_session.py` / Model の D&D（P2-B）、
`playback_controller.py` と前後曲 UI（P2-C1）、
リピート・シャッフル・再生履歴（P2-C2）、
Mutagen による非同期メタデータ取得と表示（P2-D）。
これで P2 は完了。次は P3（再生速度とピッチの操作 UI）。

- **`PlaylistEntry`**: 不変 dataclass。`entry_id`（`str`）/ `path`（絶対 `Path`）/
  `file_status`（`AVAILABLE` / `MISSING`）だけを持つ。
  直接構築を含むすべての生成時にファイル状態を検査し、`file_status` は呼び出し側から
  指定できない。
  「現在再生中」「選択中」やメタデータの状態は持たない
  （現在曲は PlaybackController が entry_id で管理する。メタデータ状態は P2-D で判断）。
- **`entry_id`**: `uuid4().hex` の文字列。行番号でも `hash(path)` でもなく、
  同じパスを複数回追加しても別 ID になり、保存・復元をまたいで安定する。空文字は拒否する。
- **パスの正規化**: `entry.normalize_path()` が唯一の正規化地点で、
  `expanduser().resolve(strict=False)` で絶対パスへ統一する。
  存在しないファイルも復元・保持する必要があるため `strict=True` にしない。
  相対パスを作業ディレクトリ依存のまま保持せず、絶対パス以外は dataclass が拒否する。
  拡張子や音声形式で拒否しない（対応可否は再生時に判定する。ADR-0001 の制約 3）。
- **欠損**: 追加時と復元時に存在チェックし、`refresh_file_status()` で再確認できる。
  欠損行は削除せず保持する（PL-05）。ファイル状態は**永続化しない**。
  復元時にファイルシステムから判定し直すのが常に正しいため。
- **`PlaylistModel`**: 行データ、追加・挿入・削除・移動、entry_id → 行の索引、
  欠損状態のみ。現在再生中のエントリ、再生位置、リピート、シャッフル、保存先パス、
  自動保存、メタデータワーカーは持たない。
  内部リストは公開せず `entries()` は `tuple` を返す。
  役割別 role は `ENTRY_ID_ROLE` / `PATH_ROLE` / `FILE_STATUS_ROLE`（`Qt.UserRole` 起点）で、
  `roleNames()` はそれぞれ `entryId` / `path` / `fileStatus` という安定名を返す。
  entry_id の重複は暗黙に採番し直さず `ValueError` で拒否する。
  範囲外の行指定は `IndexError`、`removeRows` / `moveRows` の不正引数は `False`。
#### プレイリスト UI と永続化（P2-B）

実装済みなのは `ui/playlist_view.py` と `services/playlist_session.py`、
および Model の D&D。プレイリストからの逐次再生・現在曲・前後曲・
リピート・シャッフルはP2-C、メタデータはP2-Dで実装済み。

- **`PlaylistView`**: 受け取るのは `PlaylistModel` だけ。表示、ファイル追加、
  削除、全消去、選択に応じたボタン活性、短いステータスメッセージの要求
  （`message_requested`）まで。再生操作は持たず、`playlist.json` も知らない。
- **`MainWindow`**: `PlaybackController` と `PlaylistModel` だけを受け取り、
  `PlayerControls` と `PlaylistView` を QSplitter へ配置して
  `message_requested` をステータスバーへ流すだけ。追加・削除・D&D の処理は持たない。
  単曲用の「開く...」と、プレイリストの「プレイリストに追加...」は別操作として共存する。
- **テーブル設定**: 行単位・複数選択、編集不可、ドラッグ有効、外部ドロップ受理、
  ドロップ位置表示、`dragDropOverwriteMode(False)`（行と行の「間」へ落とす）。
  列ヘッダーのクリックによるソートは無効のままにする（プレイリスト順と表示順が
  ずれると「次の曲」の意味が壊れるため。`QSortFilterProxyModel` も使わない）。
- **内部 D&D**: 専用 MIME `application/x-sdp-playlist-entry-ids` に entry_id の
  JSON 配列を入れる。パスは重複しうるし行番号はドラッグ中に変わるため、
  行の安定した同一性である entry_id を運ぶ。移動は最終的に `moveRows` で行い、
  `beginResetModel` による並べ替えはしない。
  **非連続の複数行ドラッグは P2-B では対応せず、安全に拒否する**
  （途中行を巻き込まない移動の意味を一意に決められないため）。
  移動範囲の内部・直後へのドロップも拒否する（no-op のため）。
  `PlaylistTableView` はドラッグを CopyAction として実行し、Model が受理する
  DropAction も CopyAction だけに限定する。これにより外部ドラッグ元へ Move 成功を返して
  元ファイルを削除される危険を避け、
  Qt が移動後に元行を自動削除（`clearOrRemove`）するのを防ぐ。
  Model は内部 MIME を受け取った時点で常に移動として扱う。
- **外部 D&D**: `text/uri-list` を受理し、URL の順序を表示順にする。
  ローカルファイル URL だけを `Path` へ変換し、ディレクトリと非ローカル URL は
  無視する（再帰追加もしない）。拡張子では判定せず、内容も開かない。
  有効なファイルが 0 件ならドロップを拒否する。
  ドロップ直前に消えていたファイルは欠損エントリとして追加する（行を消さない契約に従う）。
  `canDropMimeData` はドラッグ中に何度も呼ばれるため軽い判定に留め、
  ディレクトリ判定などのファイルシステムアクセスは `dropMimeData` で行う。
  不正な内部 MIME や非連続選択の警告も確定 Drop 時だけ記録し、可否照会では記録しない。
- **ドロップ位置**: `PlaylistModel.drop_row()` の 1 か所で決める。
  有効な parent → その行の前、行と行の間 → その位置、`row < 0`（末尾より下・空） → 末尾。
- **欠損表示**: `PlaylistView` の `MissingEntryDelegate` が `FILE_STATUS_ROLE` を読み、
  現在の QPalette の Disabled/Text でグレー描画する（固定 RGB を埋め込まないため、
  ライト / ダークどちらでも読める）。コア Model に色を持たせない。
  欠損行も選択・削除・並べ替えができ、ツールチップでパスを確認できる。
  disabled item にはしない（表示上のグレー化と再生可否は別の話。
  再生時のスキップは P2-C）。
- **削除**: 非連続選択に対応する。降順で 1 行ずつ消すのではなく、連続範囲へまとめて
  下側の範囲から `removeRows` する（行番号のずれを防ぐ）。
  削除後は次の行、末尾を消した場合は新しい末尾を選ぶ。選択が無ければ何もしない。
- **全消去**: 非空のときだけ有効。確認ダイアログでキャンセルされたら変更せず、
  確認されたら `clear()` を 1 回だけ呼ぶ。ディスク上のファイルは削除しない。
- **永続化サービス**: `services/playlist_session.py` が保存先の保持、
  `load_into(model)` / `save_from(model)` を担当する。Model に save/load は持たせず、
  UI からは import しない（AST テストで担保）。保存先は
  `%LOCALAPPDATA%\sdp\playlist.json`（規則は `services/user_paths.py` へ集約）。
  組み立てと復元・保存の呼び出しは composition root（`app.py`）が行う。
- **起動時復元**: ファイルが無ければ初回起動として空で始める。正常な空の保存ファイルも
  Model 全体の置換として扱い、既存の行を残さない。
  順序・entry_id・重複行・日本語パスを維持し、欠損状態は復元時に再評価する。
- **破損時の上書き防止**: JSON構造の破損、非UTF-8、読み込み I/O エラーでは、
  技術詳細をログへ残し、
  空のモデルで起動してステータスへ短いメッセージを出したうえで、
  **その起動中の保存を無効化する**（`is_save_enabled` が False）。
  空のプレイリストを保存して既存ファイルを壊さないための、
  「復元失敗」と「正常な空プレイリスト」の区別。
#### プレイリストからの逐次再生（P2-C1）

- **責務の分離**: `PlaybackController` は 1 つの source だけを扱い、プレイリストを
  保持しない。曲順と現在 entry は `PlaylistPlaybackController` が持つ。
  `PlaylistModel` も現在再生状態を持たない。
  依存の向きは PlaylistPlaybackController → PlaybackController → PlaybackBackend、
  および PlaylistPlaybackController → PlaylistModel。
- **`current_entry_id`**: 「現在の source がどの entry から読み込まれたか」であり、
  選択行ではない。プレイリスト未再生・「開く...」での直接読み込み・現在 entry の削除・
  全消去・source 解除では `None`。行番号や `QModelIndex` は公開の識別子にしない。
- **パスからの逆引きをしない**: 同じパスの行が複数あるため、`source == entry.path` では
  現在 entry を決められない。`play_entry` が load する直前に entry_id を控え、
  `source_changed` を受けた時点でその id を関連付ける。
  控えが無い（＝自分が読み込んでいない）source 変更では関連付けを解除する。
  これにより「開く...」で同じパスを直接開いた場合も確実に解除される。
- **`play_entry`**: 行の検索 → その entry だけファイル状態を再確認
  （`PlaylistModel.refresh_entry_status`。1000 件を毎回走査しない）→ 欠損なら
  再生せずメッセージだけ出す → `load` → `play` → 現在 entry を関連付け → 前後曲の可否を更新。
  戻り値の `True` は読み込み・再生要求を発行できたことを表し、実際のデコード・再生開始は
  状態・エラーシグナルで非同期に通知される。
  **ユーザーが明示的に選んだ欠損行では、勝手に別の曲へ移動しない。**
  デコード失敗は PlaybackController の既存エラー処理に任せ、
  **再生エラーを理由に自動で次曲へ飛ばさず、現在entryも維持する**
  （無限スキップとエラーの隠蔽を避ける）。
- **探索規則**: 表示中の行順をそのまま再生順とする（ソート用 Proxy は使わない）。
  次は現在行の後ろ、前は現在行の前を順に見て、最初の再生可能な entry を選ぶ。
  現在 entry が無ければ、次は先頭から・前は末尾から探す。
  探索は最大でも行数で終わり、候補が無ければ `False`。
  **P2-C1 では折り返さない**（Repeat ALL は P2-C2）。
- **欠損スキップ**: 次の曲・前の曲・自動次曲では欠損を飛ばし、見つけた欠損は
  Model へ反映してグレー表示を更新する。**直接指定した行ではスキップしない。**
  探索時の存在確認とloadの間に候補が消えた場合も、そのentryを欠損へ更新して
  同じ方向の次候補まで探索を続ける。欠損と断定できない拒否・再生エラーでは続けない。
  前後曲の可否判定は保持済みの状態だけで行い、モデル変更のたびに全行を stat しない
  （実際の可否は探索時に確定し、その結果で可否が更新される）。
- **`END_OF_MEDIA` の防御**: 現在 entry があるときだけ自動次曲する
  （「開く...」の単曲が終わってもプレイリストへ移らない）。
  source変更ごとに世代を進め、positionが正になるまでその世代を未開始として扱う。
  `END_OF_MEDIA` は開始済みのsource世代ごとに1回だけ消費し、sourceが変わるまで
  消費済み状態を解除しない。受信時の世代・entry_id・sourceを控え、イベントループの
  次のターンでもすべて一致するときだけ進める。position/duration比率による推測は行わない。
- **末尾到達**: 次の候補が無ければ新しい load を行わず、`current_entry_id` は
  最後の entry のまま保つ（`None` にしない）。ステータスへ通知だけ出す。
- **Model 変更時**: 並べ替えでは entry_id で追跡するため現在 entry を維持し、
  前後曲の可否だけ再計算する。現在 entry 以外の削除でも維持する。
  **現在 entry の削除・全消去では関連付けだけ解除し、再生中の音声は止めない**
  （`stop()` を呼ばない）。以後 `END_OF_MEDIA` が来ても自動次曲しない。
- **UI**: `PlaylistView` は `PlaylistModel` だけを受け取り、行の実行を
  `entry_activated(entry_id)` で外へ出す（`activated` のみを使う。`doubleClicked` も
  繋ぐと 1 操作で 2 回通知されるため）。欠損行でも通知はし、可否の判断はしない。
  現在 entry は `set_current_entry_id` で受け取り、**Model ではなく View の delegate が保持**して
  太字で描く（Model は再生状態を持たない。並べ替えても entry_id で追う）。
  Model のリセットではなく再描画だけで反映する。
  `PlayerControls` は前後曲ボタンを持つが `previous_requested` / `next_requested` を
  出すだけで、活性は `set_playlist_navigation_available` で外から設定される。
  `MainWindow` は配線後にControllerの現在値を一度反映するため、復元済みModelでも
  起動直後から前後曲ボタンが正しく活性化される。次曲探索や欠損スキップの判断は持たない。
- **永続化しないもの**: `current_entry_id`、現在行、再生位置、前後曲の履歴、
  リピート設定、シャッフル設定、シャッフルの履歴とサイクル、
  メタデータ（タイトル・アーティスト・アルバム・長さ・読み取り状態）。
  復元の必要性は P6 以降で判断する。

### 8.2 メタデータ（P2-D）

- **`TrackMetadata`**（`core/metadata/types.py`。Qt 非依存の不変 dataclass）:
  `title` / `artist` / `album` / `duration_ms`（いずれも省略可）。
  entry_id・path・読み込み状態・エラー文字列・UI の表現は持たない。
  Mutagen のオブジェクトはワーカースレッドの外へ出さない。
- **`MetadataStatus`**: `NOT_REQUESTED` →（要求）→ `LOADING` →
  `LOADED` または `FAILED`。ファイルの欠損は `FileStatus` が表すので
  ここへ `MISSING` は入れない。**タグが 1 件も無くても `LOADED`**（失敗ではない）。
  欠損になったら値を捨てて `NOT_REQUESTED` へ戻し、復活したら読み直せるようにする。
- **`PlaylistEntry`**: `metadata`（不変値）と `metadata_status` を持つだけで、
  読み取りはしない。`LOADED` のときだけ値を持ち、それ以外は `None`。
  メタデータ更新で entry_id・path・file_status は変えず、内部限定のcloneは
  `__post_init__`を通さないためGUIスレッドでファイル状態を再調査しない。
- **タグの正規化**: easy tags の複数値に備え、title / album は最初の非空値、
  artist は非空値を順序どおり `/` で結合（1 件なら区切りなし）。前後の空白は除去し、
  空だけなら `None`。文字列でない値は無理に文字列化せず無視する。
- **長さ**: `info.length`（秒）を `round(seconds * 1000)` でミリ秒へ。
  取得できない・NaN・inf・負値は `None`（0:00 と偽らない）。
  長さが取れないだけでタグは捨てない。**再生中の `PlaybackController.duration_ms`
  とは別物**で、同期させない。
- **`MetadataReader`**: Model の既存・追加・欠損復活エントリを読み取り対象にし、
  **専用 QThreadPool**（最大 4、`idealThreadCount` を考慮）へ `QRunnable` を投入する。
  UI・再生制御・永続化は知らない。**Mutagen を GUI スレッドで呼ばない。**
  ワーカーは Model にも QWidget にも触れず、読み取って結果を返すだけ。
  未対応形式・破損・I/O失敗は通常の読取失敗として扱う。属性取得や抽出ロジックの
  予期しない例外は純粋関数で変換せず、ワーカーで traceback 付きのエラーログへ残す。
  いずれの例外もスレッド外へは漏らさない（`BaseException` は捕まえない）。
- **古い結果の防止**: 要求ごとに単調増加のトークンを付け、結果には
  entry_id・path・token を含める。反映前に「shutdown していない」「最新トークン」
  「entry がまだある」「path が一致」「欠損していない」「`LOADING` である」を確認する。
  **パスから別 entry へ結果を流用しない。** 削除・欠損・reset・適用不能で不要になった
  tokenは回収し、古い結果で新しいtokenを消さない。破棄は通常運転で起こるためdebugログのみ。
- **反映は GUI スレッド**: ワーカーの結果シグナルは自動的にキュー接続で GUI スレッドへ渡る。
  Model の更新は entry_id 単位（`mark_metadata_loading` / `apply_metadata` /
  `mark_metadata_failed` / `clear_metadata`）で、`beginResetModel` は使わない。
- **shutdown（協調的停止）**: 新規要求を止め、トークンを無効化し、未開始タスクを
  `clear()`し、`shutdown()`内では最大3秒だけ待つ。実行中の同期I/Oは強制終了せず、
  結果を無視する論理キャンセルとする。ただしQThreadPool破棄は実行中タスクを待つため、
  ネットワークドライブや故障媒体でI/O自体が戻らない場合、**プロセス終了の3秒上限は
  保証しない**。厳密な終了期限が必要になった場合は読取を終了可能な子プロセスへ隔離する。
- **列と role**: タイトル / アーティスト / アルバム / 長さ / パスの 5 列
  （`Column.NAME` は `TITLE` の別名として残す）。
  role は `TITLE_ROLE` / `ARTIST_ROLE` / `ALBUM_ROLE` / `DURATION_MS_ROLE` /
  `METADATA_STATUS_ROLE` で、表示文字列ではなく意味上の値を返す。
- **タイトルのフォールバック**: 表示は `metadata.title` → ファイル名 → パス全体の順。
  未要求・読み取り中・失敗のいずれでも常にファイル名が出るので、
  追加直後から曲を識別できる（「読み込み中...」でタイトルを置き換えない）。
- **`dataChanged` の範囲**: `LOADING` は状態 role だけ、`LOADED` / `FAILED` は
  タイトルから長さまでの列を表示 role と各メタデータ role で通知する。
  ファイル状態の変化ではメタデータ破棄も伴うため、タイトルからパスまでを
  file status・全メタデータ・表示・ツールチップroleで通知する。値が変わらなければ通知しない。
- **再生制御との分離**: `PlaylistPlaybackController` は `dataChanged` の roles を見て、
  **メタデータだけの変化では何もしない**（曲順・履歴・可否・現在 entry を触らない）。
  roles が空なら従来どおり可否を計算し直す。
- **非同期更新列のリサイズ**: `ResizeToContents` は `dataChanged` のたびに全行を
  走査するため、メタデータ列では使わない（1000 件で O(n^2) 相当になる）。
- **1000 件のスレッド境界**: 追加もスケジュールも GUI スレッドで完結し（同期読取をしない）、
  読み取りだけがワーカーで並列に走る。GUI のタイマーとボタンは動き続ける。

#### リピート・シャッフル・再生履歴（P2-C2）

- **`RepeatMode`**（`core/playlist/types.py`。UI からも参照するので Controller 内部へ
  閉じ込めない）: `OFF` / `ALL` / `ONE`。ボタンは OFF → ALL → ONE → OFF の順で回す。
  表示文字列は UI 側の責務。不正な値は既定へ丸めず `TypeError`。
  シャッフル設定も厳密な `bool` だけを受理し、文字列・整数・`None`は拒否する。
- **Repeat ALL（シャッフル OFF）**: 行順の探索候補に折り返し分を足す。
  末尾の次は先頭、先頭の前は末尾。候補の最後に現在 entry 自身も含めるため、
  利用可能な行が 1 件だけなら同じ曲を繰り返せる。欠損は従来どおり飛ばす。
- **Repeat ONE**: **自動の曲終わり（`END_OF_MEDIA`）にだけ効く。**
  手動の「次の曲」「前の曲」は妨げず、通常どおり移動する。
  繰り返しは `seek(0)` ではなく現在 entry を読み込み直す
  （Backend の再開挙動へ依存せず、source 世代を新しくして古い通知と区別するため）。
  履歴とサイクルの消化状態は進めない。`current_entry_id` が `None`
  （「開く...」の単曲）のときは何もしない。
- **シャッフルは再生順だけの状態**。Model の行順・`PlaylistEntry`・`playlist.json` の
  保存順は変えない。対象は Path ではなく **entry_id** なので、同じパスの 3 行は
  3 つの独立した候補になる。
- **乱数**: `PlaylistPlaybackController(..., rng=random.Random(seed))` で注入できる。
  本番は引数なし（新しい `random.Random()`）。モジュールのグローバル `random` へ
  依存せず、固定 seed も入れない。決定的な検証は単体テストで行う。
- **履歴と cursor**: 実際に通った entry_id の列と、現在位置を指す cursor を持つ。
  「前の曲」は**再抽選ではなく履歴を戻る**。戻ったあとの「次の曲」は、
  まず履歴の未来側へ進み、使い切ってから新しい候補を選ぶ。
  未来履歴が残っている状態で**別entry**を直接再生すると、未来履歴を切り捨てて
  末尾へ追加する。現在entryと同じentry_idの再実行では履歴を一切変更せず、
  次の曲で元の未来履歴へ戻れる。
  再生できなかった entry は履歴へ入れない。履歴は公開しない。
- **サイクル**: 現在サイクルで再生済みの entry_id を集合で持つ。
  Repeat OFF は全候補を消化したら終了（新しいサイクルを始めない）。
  Repeat ALL は消化後に訪問済みを空にして新サイクルを始め、
  **候補が 2 件以上あるならサイクル境界で直前の曲を選び直さない**
  （1 件だけなら選び直してよい）。
- **欠損と TOCTOU**: 候補選択時に 1 件ずつ状態を確認し、消えていれば Model へ反映して
  次の候補を試す。候補は有限なので探索も有限で終わる。
  デコード失敗（同期的に欠損と断定できない失敗）では次候補へ移らず、
  プレイリスト末尾のメッセージも表示しない。内部の再生結果は終了判定まで保持し、
  公開操作の境界だけで `bool` へ変換する。
- **Model 変更時**: 並べ替え・追加では履歴とサイクルを保つ（追加分は未訪問候補になる）。
  現在 entry 以外の削除では、消えた entry を履歴から取り除き、cursor が現在 entry を
  指し続けるよう旧cursor以前に残った要素数から新cursorを算出する。同じentry_idが
  複数サイクルの履歴に存在しても最初の出現位置へ戻さない。訪問済み集合から除くのは
  Modelから削除されたentry_idだけで、未来履歴から外れた訪問済みentryは維持する。
  **現在 entry の削除・全消去・「開く...」による直接読み込みでは、
  履歴とサイクルを捨てて関連付けを解除する**（音声は止めない。リピート・シャッフルの
  設定値そのものは維持する）。
- **ナビゲーション可否**: 保持済みの欠損状態と履歴・候補だけで算出し、
  ファイルシステムを全件走査しない。シャッフル OFF + ALL なら利用可能な行が
  1 件でもあれば前後とも可。シャッフル ON では、previous は cursor より前に
  利用可能な履歴がある場合のみ、next は未来履歴・未訪問候補・（ALL なら）利用可能候補の
  いずれかがある場合。Repeat ONE は手動の可否を変えない。

- **終了時保存**: `app.exec()` の戻り後に `entry_id` / `path` / 順序 / 重複行だけを保存する。
  選択行・スクロール位置・現在曲・再生位置・音量・ミュート・欠損状態・
  メタデータは保存しない。保存の失敗はログへ残すだけにして終了処理を止めない
  （ウィンドウが閉じた後でユーザーへ提示できないため）。

- **永続化**: `schema_version` 付きの JSON（`entry_id` と `path` の配列、UTF-8）。
  `schema_version` は bool や float を受理せず、現在値と一致する厳密な整数だけを受理する。
  同じディレクトリの一時ファイルへ書いてから `os.replace` でアトミックに置き換える。
  保存前に空 ID・ID 重複・相対パスを検証し、不整合があれば一時ファイルも既存ファイルも
  変更せず `ValueError` にする。
  壊れたデータ（JSON 不正、未対応バージョン、型不一致、空の `entry_id`、ID 重複）は
  黙って解釈せず `PlaylistFileError` にする。未知のキーは無視する。
  ファイルが無い場合は初回起動の正常状態として空リストを返す。
  保存先の決定・保存タイミング・自動保存は呼び出し側（P2-C 以降）の責務。

---

## 9. 設定保存と設定画面

### 9.1 保存する設定と schema（P3-B / P6-A）

- 保存先は`%LOCALAPPDATA%\sdp\settings.json`。**現在の schema version は 2**。
  保存するのは次の5項目だけで、音量、mute、repeat、shuffle、現在曲、再生位置、
  ウィンドウ状態などは暗黙に保存しない（ウィンドウ状態はP6-Bの対象）。

  | キー | 追加 | 内容 |
  |---|---|---|
  | `playback_rate` | v1 | 0.50〜2.00 の再生速度 |
  | `pitch_compensation` | v1 | ピッチ補正のON/OFF |
  | `waveform_visible` | v2 | 波形の表示ON/OFF |
  | `spectrum_visible` | v2 | スペクトラムの表示ON/OFF |
  | `level_meter_visible` | v2 | Peak／RMSレベルメーターの表示ON/OFF |
  | `volume` | v3 | 0.0〜1.0 の音量 |
  | `muted` | v3 | ミュートのON/OFF |
  | `repeat_mode` | v3 | `"off"` / `"all"` / `"one"` |
  | `shuffle_enabled` | v3 | シャッフルのON/OFF |

- 可視化の色、バンド数、FPS、窓長、Peak hold時間・減衰速度、ショートカット、
  再生デバイス、キャッシュ容量は**保存対象へ入れない**（P6後半以降）。
- Qt非依存の`AppSettings`と純粋なload／saveを`services/settings.py`へ置く。
  UI範囲外、NaN／inf、型不一致、非UTF-8、不正JSON、未知versionは復元エラーとする。
  bool欄は`0`／`1`／文字列を受理しない。
- 読み込み時は未知のキーを無視し、**欠落した既知キーは既定値で補い、値が不正な
  既知キーは明示的に失敗させる**（両者を同じ扱いにしない）。

**Repeat の保存表現**: core の `RepeatMode` は `auto()` の値を持ち永続化を意図していない
ため、保存層は安定した文字列を持つ別enum `RepeatModeSetting`（`off` / `all` / `one`）へ
写す。core enumの定義順や実装が変わってもファイル互換を壊さないためで、対応表に無い値は
既定値へ丸めず失敗させる。依存方向は services → core の一方向のままとする。

### 9.2 schema version の移行

- **version 1・2・3 のいずれも有効な入力として読み込む。** 古いversionに無い項目は
  既定値で補う（可視化は表示ON、音量1.0、ミュートOFF、Repeat OFF、ShuffleOFF）。
- **古いversionでは、後のversionのキーが混入していても未知キーとして無視する**
  （v1のファイルへ手で書いた `volume` をv1の意味へ影響させない）。
- **読み込みだけではファイルを書き換えない。** 古いversionで起動しても、次に
  設定が変わって通常の保存契機が来たときに初めて現在のversionとして保存する。
- 未知の将来versionは上書きせず、復元エラーとして扱う
  （その起動では保存を無効化し、元ファイルを保護する）。

### 9.3 適用の調停（AppSettingsController）

`services/settings.py`。適用済み設定のsnapshotを1か所で持つ小さなQObject。
**実効値の持ち主は2つある**ため、両方を調停する。

| 設定 | 実効値を持つ層 |
|---|---|
| 再生速度・ピッチ補正・音量・ミュート | `PlaybackController` |
| Repeat・Shuffle | `PlaylistPlaybackController` |
| 可視化の表示ON/OFF | 保存層のsnapshotだけ（適用先はMainWindowの配置責務） |

- `apply(settings)` は検証してから**差分のある項目だけ**各Controllerへ適用する。適用中の
  Controller echoは集約し、全setter成功後に実効値を読み戻してsnapshotを1回だけ公開する。
  途中で失敗した場合は**変更済みのControllerをすべて**直前値へ可能な限り戻し、
  未適用snapshotを公開・保存しない（rollback自体の失敗はログへ残し、再生は止めない）。
  同値なら通知しない。
- SpeedPanel・PlayerControls・ショートカット経由で各Controllerが直接変更された場合も
  snapshotへ追従させ、保存対象を1か所に保つ（可視化設定は再生操作で失われない）。
- shutdownは終端操作とし、以後のController通知を無視して`apply()`も拒否する。
- JSON読み書き、QDialog、Backend具体型、PCM解析、FFT／レベル計算、プレイリストの
  曲順操作は持たない。`SettingsSession`はこのsnapshotだけを見て保存する。

### 9.4 設定ダイアログ（P6-A）

`ui/settings_dialog.py`。`QDialog` + `QDialogButtonBox`（OK / キャンセル / 適用）。
objectNameは`settingsDialog`、各入力は`settingsPlaybackRateSpinBox`、
`settingsPitchCompensationCheckBox`、`settingsWaveformVisibleCheckBox`、
`settingsSpectrumVisibleCheckBox`、`settingsLevelMeterVisibleCheckBox`。

| 操作 | 意味 |
|---|---|
| 開いた時点 | 現在の**適用済み設定snapshot**を各入力へ反映する |
| Apply | 検証 → 適用要求 → **成功通知後だけ**適用済みsnapshot更新。閉じない |
| OK | Applyと同じ処理が成功した場合だけ閉じる。失敗時は入力を維持してエラー表示 |
| Cancel / Esc | **Apply後の変更は戻さず**、未適用の編集だけを破棄して閉じる |

- ダイアログは設定ファイルもschema versionもPlaybackControllerも知らない。
  `settings_requested` で要求を出し、MainWindowから`mark_applied`／`show_apply_error`で
  結果だけを受け取る。適用そのものは調停サービスが行う。
- 通常操作ではWidget制約により不正値にならないが、プログラム経由で不正値が入った
  場合は適用せず、ダイアログ内へ短いエラーを表示する（例外を外へ漏らさない）。
  不正なままOKしても閉じない。
- MainWindowはツールメニューの「設定...」でダイアログを開き、**同時に2つ開かない**
  （開いていれば入力を再設定せず前面へ出し、未適用編集を維持する）。開いている間の
  外部設定変更は編集中入力へ自動反映しない。JSON処理、schema version分岐、保存タイマー、
  設定項目ごとの巨大な分岐はMainWindowへ持ち込まない。

### 9.5 可視化の表示ON/OFF（P6-A）

**隠すだけでなく、その可視化固有の解析を止める。** ただし共有PCMタップは停止も
切断もしない（複数の可視化が共用しているため。§6.10 / SPEC-04と同じ方針）。

| 設定 | 非表示時の挙動 |
|---|---|
| `waveform_visible` | `WaveformPanel`を隠し、**位置追従の描画更新を止める**。解析結果とキャッシュは捨てず、バックグラウンド解析の可否は既存契約のまま。再表示時に現在sourceの位置・長さへ復帰する（非表示中のsource変更でも旧sourceを表示しない） |
| `spectrum_visible` | `SpectrumWidget`を隠し、**mono snapshotとFFTを行わない**（frame数0で要求し、コピーもしない）。平滑化履歴と旧フレームは破棄する |
| `level_meter_visible` | `LevelMeterWidget`を隠し、**L／R snapshotとPeak／RMSを行わない**。Peak holdも破棄する |
| 両方OFF | `SpectrumPanel`ごと畳み、**タイマーを停止**する。PCMタップは固定容量bufferへの受信を継続する |

- 再表示時は最新PCMから表示を再開する。**非表示だった実時間はPeak holdの減衰へ
  加算しない**（タイマー停止時に経過時間の時計を無効化する）。
- 非表示→再表示だけでは解析の失敗状態を消さない（復帰はsource変更・format変更のまま）。
  非表示中は解析しないため、新たな失敗もログも生まれない。

### 9.6 UI状態（P6-B / P6-C）

設定画面で**明示的に変更する** ``AppSettings``（settings.json）と、使っているうちに
**自然に変わる**ウィンドウ状態は別ファイル・別責務にする。同じ値を両方へ保存しない。

| 項目 | ファイル | 変わり方 |
|---|---|---|
| 速度・ピッチ・可視化の表示ON/OFF | `settings.json`（schema v2） | 設定画面から明示的に |
| ウィンドウ位置・サイズ・最大化・Splitter比率・前回フォルダー・現在曲 | `ui-state.json`（schema v2） | 日常操作で自動的に |

保存形式は Qt の ``saveGeometry()`` / ``QByteArray`` の base64 ではなく、
**意味の明確な整数値**とする（手編集・デバッグ・DPIやQtバージョン差の吸収・
画面外補正・破損箇所の個別検証がしやすい）。

```json
{
  "schema_version": 2,
  "window": {"x": 120, "y": 80, "width": 960, "height": 760, "maximized": false},
  "main_splitter": {"player": 360, "playlist": 400},
  "last_open_directory": "C:\Music",
  "current_playlist_entry_id": "..."
}
```

#### 現在曲（P6-C）

- 保存するのは**entry_id**で、行番号もパスも保存しない。並べ替えても、同じパスの
  重複行があっても、同じentryへ戻れる。
- `ui_state.py` は PlaylistModel を知らない（実在確認をしない）。照合と復元は
  `PlaylistUiStateSource`（services）が行い、`UiStateSession` も MainWindow も
  entry_id の照合を持たない。`PlaybackController` へ entry_id を持たせない。
- 復元は `PlaylistPlaybackController.select_entry_by_id()` で**選ぶだけ**。
  sourceは読み込むが `play()` は呼ばないため、**自動再生しない・位置は0のまま**。
- entry_id が存在しない・欠損している場合は復元をあきらめるだけで、ui-state全体を
  破損扱いにしない。次の保存で自然に取り除かれる（現在曲が削除・全消去された場合も
  `current_entry_id` が `None` になり、保存対象から消える）。
- 現在曲の変更もUI状態の保存契機にする（`PlaylistUiStateSource` が
  `current_entry_changed` を購読して通知する）。
- v1のui-stateには現在曲が無いためNoneで補い、読み込んだだけではv2へ書き換えない。

#### 再生位置を保存しない理由（P6-C）

- 数秒の曲やSEでは復元価値が低い。
- 前回終了位置から突然再開するのは予測しにくい。
- 音源が更新された場合、その位置がまだ妥当かの判定が必要になる。
- duration不明・ライブ音源・壊れたファイルの扱いが増える。
- 定期保存の頻度が上がる（位置は常に変化するため）。
- まず**現在曲の選択復元だけ**でUXを評価する。再生中かどうかも保存しない。

- `services/ui_state.py`（**Qt非依存**）: 値オブジェクト（`WindowState` /
  `SplitterState` / `UiState` / `ScreenRect`）、JSON解析とschema検証、アトミック保存、
  画面外補正とSplitter再配分の純粋関数。MainWindow・QSplitter・QFileDialog・
  PlaybackController・PlaylistModel・AppSettingsControllerを参照しない。
- `services/ui_state_session.py`（Qt調停）: 復元の適用、変更のデバウンス保存、
  終了時flush。Windowへは `capture_ui_state` / `restore_ui_state` /
  `connect_ui_state_changed` の小さな契約（Protocol）だけを要求し、ui層をimportしない。
- 値の契約: boolをint欄で受理しない、width／heightは正、splitterは0以上かつ合計が正、
  パスは**絶対パスのみ**。未知キーは無視し、既知キーの欠落は「未保存」、
  既知キーの不正値は復元失敗とする。

#### ウィンドウgeometry

- 保存するのは**normal状態**のx・y・width・heightと`maximized`。
  最大化中は `normalGeometry()` を使い、**最大化された画面全体のサイズを
  normal sizeとして保存しない**。
- **最小化状態は保存しない。** 最小化中に終了しても、次回はnormalまたはmaximizedで開く
  （`windowState()` の Maximized フラグだけを見る）。
- 復元は「normal geometryを設定 → 必要なら最大化」の順。逆にするとnormal geometryを失う。

#### 画面外補正（マルチモニター）

`fit_window_state(state, screens, minimum_size=...)` を純粋関数として持ち、
`QScreen.availableGeometry()` を `ScreenRect` へ写して渡す（テストから注入できる）。

- **負のx／yを一律に拒否しない。** 左・上に置かれたモニターでは正当な値。
- タイトルバー相当の帯が最も広く重なる画面を復元先とする。タイトル帯がどの画面にも
  重ならない場合は、ウィンドウ矩形との交差面積が最大の画面を使う。
- 選んだ画面でタイトルバー相当の帯（上端24px・幅80px以上）が見えていれば位置を保つ。
- どの画面とも重ならない（モニターを外した・解像度が変わった）場合は
  **primary screenの中央**へ戻す。
- サイズは**選んだscreen**のavailable size以内へclampし、`minimumSizeHint()` を下回らせない。
  サイズを縮めた場合は位置も補正し、矩形全体をそのscreen内へ収める。
- 画面情報が取れない場合は位置に触れず、サイズの下限だけを保証する。

#### Splitter

- 保存は上下の絶対サイズだが、**復元は比率**として現在の利用可能高さへ再配分する
  （`distribute_splitter_sizes`）。前回と違うウィンドウ高さでも見た目の比率が保たれる。
- 片側が完全に潰れないよう最小60px（総量が小さい場合は総量の半分）を確保する。
- 可視化の表示ON/OFFでplayer側の最小高さが変わるため、**Splitterの復元は
  AppSettingsの可視化適用のあと**に行う。表示でレイアウトが確定してから
  最初の `showEvent` でもう一度比率を当て直す。

#### 前回フォルダー

- 「開く...」の初期ディレクトリに使う。**ファイルを選んだときだけ**その親フォルダーを
  保存対象へ反映し、**Cancelでは変更しない**。プレイリストへのD&Dでも変更しない
  （追加操作であって「開く」操作ではないため）。
- 保存するのはファイルパスではなくディレクトリで、相対パスは保持しない。
- 読み込み時にもダイアログを開く直前にも存在確認をしない。切断済みネットワークドライブ等
  への同期I/OでGUIを止めないため、保存値をそのまま `QFileDialog` へ渡す。Qtが初期位置へ
  アクセスするときの待ち時間まではアプリ側で保証しない。

#### 保存契機と障害の独立性

- 監視するのはmove／resize／WindowStateChange／splitterMoved／前回フォルダー変更。
  MainWindowはこれらを `ui_state_changed` の1本へまとめて通知する。
- **移動・リサイズのたびには書き込まない。** 1.2秒のデバウンスでまとめ、
  最大化・復元の連続イベントでも最終snapshotだけを保存する。終了時は必ずflushし、
  変更がなければ書き換えない。**復元の適用中は通知しない**（restoreを保存契機にしない）。
- 破損時は既定状態で起動して短い復元失敗メッセージを出し、その起動では保存を無効化して
  元ファイルを守る。`settings.json` / `playlist.json` / `ui-state.json` の
  **保存可否と障害は互いに独立**とする。

### 9.6.1 保存・復元失敗のユーザー通知（P6-C）

`services/save_status.py`。カテゴリ（設定／プレイリスト／ウィンドウ状態）と、
メッセージ整形の純粋関数、通知抑制の小さなQObjectだけを持つ。

- **復元失敗**は起動時に1文へまとめる（例:「設定とプレイリストの復元に失敗しました。
  既定状態で起動します。」）。カテゴリは重複を除いて既定順へ揃え、
  **生の例外文もファイルパスも出さない**（詳細はログだけ）。
- **保存失敗**は終了を待たずステータスバーへ短く出す（例:「設定を保存できませんでした。」）。
  デバウンス保存はタイマーごとに失敗し得るため、**状態が変わったときだけ**通知する
  （失敗→成功で「保存しました」を1回）。modal dialogは出さず、再生は妨げない。
- プレイリストも追加・削除・移動・全置換を1.5秒でデバウンス保存する。メタデータや
  欠損表示だけの更新は永続化内容を変えないため保存契機にしない。
- 終了時の保存失敗はウィンドウが閉じた後でユーザーへ提示できないため、ログだけへ残す。

### 9.7 起動と保存のライフサイクル

- 順序は「設定読込 → `AppSettings`生成 → Controllerへ速度・ピッチ適用 →
  プレイリスト復元 → MainWindow構築 → **可視化表示設定の適用** →
  **UI状態読込とgeometry／Splitter／前回フォルダーの復元** → Window表示 → 各監視開始」。
  可視化が一瞬見えてから消えるフリッカーを避けるため、表示設定はWindow表示**前**に反映する。
- `build_player()`はMainWindow構築前に設定をControllerへ適用し、構築だけでは監視を開始しない。
  **起動時の適用をユーザー変更として保存しない。**
  `run()`が全構築後に監視を開始し、変更から1.5秒のデバウンスと正常終了時のflushで保存する。
  変更がないApplyではファイルを書き換えない。
- UI状態の監視は**Window表示後**に開始する（表示で生じるmove／resizeを
  ユーザー変更として保存しないため）。
- 終了順は「単一instance IPC → SpectrumPanel → PcmTap → 波形解析 → MetadataReader → **UI状態flush** →
  設定flush → プレイリスト保存 → 各session stop → AppSettingsController shutdown」。
  **Windowが破棄された後にgeometryを取得しない**（破棄済みならUI状態の保存を諦めて
  終了処理を止めない）。
- 終了処理は `app.shutdown()` がカテゴリごとに独立して実行する。**1カテゴリの例外で
  後続を飛ばさない**（各段をtry/exceptで囲み、失敗はログへ残して次へ進む）。
  保存APIの`False`は「変更なし」と内部で記録済みの失敗を区別しないため、終了段階の
  失敗判定には使わない。大きなライフサイクル基盤は作らず、順序と例外分離だけをここで守る。
- 起動時の現在曲復元は「QApplication作成 → 単一instance判定 → 設定適用 →
  プレイリスト復元 → MainWindow構築 → 可視化適用 → UI状態復元
  （geometry・Splitter・前回フォルダー・現在曲）→ 起動引数追加 → Window表示 →
  IPC受信開始 → 状態監視開始」の
  順で行う。**復元しただけでは再生しない。**
- 異常終了ではデバウンス済みの直近状態までが残る。crash handlerや強制終了時の
  完全な保証は行わない。
- 書き込みは同一ディレクトリの一時ファイルへUTF-8で書き、flush／fsync後に`os.replace`で
  アトミックに置き換える。復元失敗時は既定値で起動して通知し、その起動では設定保存を
  無効化して破損ファイルを保護する。設定とプレイリストの障害・保存可否は互いに独立し、
  両方の復元に失敗した場合は両メッセージを結合して表示する。
- デバウンスタイマーによる保存が一時的に失敗した場合は5秒後に1回だけ自動再試行する。
  新たな設定変更は通常の1.5秒デバウンスを再開し、終了時の明示的なflushは再試行タイマーを
  作らない。

---

## 10. 単一インスタンス

### 10.1 起動要求

`services.launch_request.LaunchRequest` はQt非依存のimmutableな値オブジェクトで、
絶対パスのtuple、無視した引数、前面化意図の`activate_window`を持つ。
OSが分割済みの`argv`を受け取るため、
引用符の再解釈は行わない。相対パスは`QApplication`構築前に取得した起動時の
current directory基準で絶対化し、指定順と重複を保つ。

起動引数解析で`resolve()`、`stat()`、`is_dir()`などのファイルシステムI/Oを行わない。
既存の`PlaylistModel.add_paths()`契約と合わせ、欠損パス、未知拡張子、
ディレクトリも受理する。ファイルでないpathはプレイリスト側で欠損として扱う。
Pathに変換できない引数だけを無視する。

### 10.2 IPCと競合防止

- `SingleInstanceService` は`QLocalServer` / `QLocalSocket` / `QLockFile`だけを使い、
  QWidget、PlaylistModel、PlaybackController、保存JSONを参照しない。
- server名はユーザー、home、Windows domain、session識別子をSHA-256で短縮した
  `sdp-<24 hex>`。Qtの`UserAccessOption`も指定し、別ユーザーsessionや別アプリとの衝突を避ける。
- wire formatは`4-byte big-endian payload length + UTF-8 JSON`。JSONは
  `{"version": 1, "paths": [...], "ignored_arguments": [...], "activate_window": true}`とする。payload上限は
  **256KiB**で、受信側と送信側の両方で拒否する。受信bufferは部分受信を蓄積し、
  1socket上の連続messageを個別に処理する。不正JSONと未知versionはログに残して無視する。
- primaryは専用`QThread`でlistenとframe検証を開始し、composition構築中の要求も
  内部queueへ受理する。Windowとhandlerの準備後に`start_delivery()`でGUI通知を開始する。
- primaryが検証済み要求を内部queueへ追加した後に1-byteの受理確認を返す。
  ACKが保証するのは**primary queueへの受理**であり、playlist適用やWindow前面化の成功ではない。
  secondaryはこれを待ってから終了するため、単なるsocket接続を転送成功と誤判定しない。
- 接続できなければ排他lockを試み、所有できたprocessだけがlistenする。
  lock取得後に残っているendpoint、またはQtがPID消滅等でstaleと確認したlockだけを除去する。
  既存instanceが疑われるのに転送できない場合は二重起動せず、終了コード2で終了する。

### 10.3 起動・適用・終了順序

1. 起動時current directoryを保持し、`argv`を`LaunchRequest`へ変換する。
2. `QApplication`を作成し、`PlayerComposition`より先に単一instanceを判定する。
3. secondaryは受理確認後にevent loopもcompositionも作らず終了する。
4. primaryは設定と保存済みplaylistを復元し、初回起動引数をその末尾へ追加する。
   追加だけで自動再生はしない。
5. primaryのIPC threadはlisten直後から要求をqueueへ受理し、composition構築中でもACKを返す。
6. Window表示後に受信Signalを`LaunchRequestHandler`へ接続し、`start_delivery()`でqueueを1回だけ適用する。
7. 転送要求は同じWindowとPlaylistModelの末尾へ追加する。`activate_window=True`ならpathの有無にかかわらず、
   最小化flagだけを外し、`show()` / `raise_()` / `activateWindow()`を試みる。
   最大化flagは維持し、OS制約でactiveにできなければ`QApplication.alert()`を要求する。
8. shutdownの最初にSignalを切断し、socket、server thread、endpoint、lockを解放する。
   その失敗で他の保存・解放カテゴリを飛ばさない。

---

## 11. Windows ファイル関連付け（P7-C で実装）

インストーラー（Inno Setup、per-user）で登録する。**書き込み先は HKCU だけ**で、
HKLM へは一切書かない。ProgID は形式ごとに分けず `sdp.AudioFile` の 1 つにまとめる
（sdp は 7 形式をすべて同じ「音声ファイル」として開くため、形式別に分ける利点がない）。

| キー | 内容 | uninstall |
|---|---|---|
| `HKCU\Software\Classes\sdp.AudioFile` | 表示名、`DefaultIcon`＝`sdp.exe,0`、`shell\open\command`＝`"<install>\sdp.exe" "%1"`、`FriendlyAppName` | `uninsdeletekey`（自分のキーごと削除） |
| `HKCU\Software\Classes\Applications\sdp.exe` | `FriendlyAppName`、`DefaultIcon`、`shell\open\command`、`SupportedTypes`（7 拡張子） | `uninsdeletekey` |
| `HKCU\Software\Classes\<ext>\OpenWithProgids` | 値名 `sdp.AudioFile` | `uninsdeletevalue`（**自分の値だけ**。他アプリの登録を壊さない） |
| `HKCU\Software\sdp\Capabilities` | ApplicationName / ApplicationDescription と FileAssociations（7 拡張子 → `sdp.AudioFile`） | `uninsdeletekey` |
| `HKCU\Software\RegisteredApplications` | 値名 `sdp` → `Software\sdp\Capabilities` | `uninsdeletevalue` |

対象拡張子は `.wav` `.mp3` `.flac` `.ogg` `.opus` `.m4a` `.aac` の 7 種類。
一覧の source of truth は `packaging/installer.iss` で、installer manifest も
`sdp/inno_script.py` で読み取って生成する（二重管理しない）。

**既定アプリは強制変更しない**（WIN-03）。Windows 10/11 では `UserChoice` を
インストーラーから正当に書き換える手段が無く、hash を偽装する行為は取らない。
sdp が行うのは「プログラムから開く」候補への登録と Capabilities の宣言までで、
既定にするかどうかは利用者が Windows の設定から選ぶ。アプリ内のメニューからは
`ms-settings:defaultapps` を起動して設定へ誘導する。
`ChangesAssociations=yes` により、登録・削除後にシェルへ通知する。

複数ファイルを Explorer から開くと Windows が複数プロセスを起動し得るが、
P7-A の単一 instance 転送により既存 Window のプレイリストへ追加される（§10）。

---

## 12. パッケージングとインストーラー

### 12.1 P7-B1 onedir配布物

- PyInstaller 6の**onedir**を使い、`packaging/sdp.spec`を唯一のビルド定義とする。
- entry pointは`src/sdp/__main__.py`、windowed（consoleなし）、UPXなしとする。
- `sdp.exe`とPython、PySide6、Qt plugin、NumPy、Mutagen等は`dist/sdp`と
  `dist/sdp/_internal`へ配置する。Qt DLLやpluginを手作業でコピーしない。
- `sdp`のpackage metadataを同梱し、`sdp.__version__`は`pyproject.toml`のversionを使う。
- `resources.py`は開発時にrepository root、frozen時にexe directoryと`_internal`を区別する。
  ユーザーデータは従来どおり`%LOCALAPPDATA%/sdp`であり、exe隣へ保存しない。
- テスト音源、pytest、Ruff、Pyright等の開発物は同梱しない。
- 配布物にsdpのMIT License、第三者通知、wheelから収集したライセンス原文を含める。

PySide6 6.10.3の標準hookによるP7-B1実測では、`ffmpegmediaplugin.dll`と
`windowsmediaplugin.dll`、FFmpegのavcodec／avformat／avutil／swresample／swscale DLLを
収集した。Qt 6の既定はFFmpeg backendで、Windows Media Foundation backendはQt 6.10から
非推奨である。sdp自身は外部FFmpeg CLIを同梱・起動しない。各形式は実際のbackend、codec、
hardware driver等にも左右されるため、selftestの無音WAV decode成功を全codec対応の証明にしない。
（[Qt Multimedia backend](https://doc.qt.io/qt-6/qtmultimedia-index.html)）

`--selftest`は通常起動と独立したCLI modeで、`PlayerComposition`、Window、音声再生、
単一instance IPCを開始しない。Qt Widgets／Network／Multimediaの必須classを構築し、
ログ保存先とQt temp directoryへの書き込みを確認する。さらに標準ライブラリで短い無音PCM WAVを
Qt tempへ生成し、FFmpeg backendを明示した`QAudioDecoder`で最低1bufferを実decodeして削除する。
成功0、依存または書き込み失敗1、不正引数2を返す。これは音を出さずにplugin loadを診断するが、
実音や圧縮形式を含む全codecの受け入れは代替しない。

layout検査はFFmpeg／Windows media plugin、FFmpeg runtime DLL、VC Runtimeに加え、宣言した
project／Python／PySide6／NumPy／Mutagen／PyInstallerのライセンス文書を必須にする。specも
必須ライセンス原文をwheelから検出できない場合はbuildを失敗させ、欠落した配布物を作らない。

layout検査は、利用者がZIP展開直後に読める位置（`sdp.exe`と同じ階層）の`LICENSE`と
`THIRD_PARTY_NOTICES.txt`も必須にする。specはCOLLECT後にこの2つを配布物ルートへ複製する
（PyInstallerのdatasは`_internal`へ入るため）。原文一式は`_internal/licenses/`に残す。

### 12.2 配布版のdecode検査（P7-B2）

`--selftest`が「Qt依存と書き込み先が揃っているか」を見るのに対し、`--codec-test`は
**指定された音源を実際にPCMへdecodeできるか**だけを見る。CLIは独立モードとし、
`--selftest`との併用と、pathを伴わない指定を拒否する。

- 成功条件: source設定成功、decoderのerrorなし、期間内のfinished、有効bufferが1件以上、
  frame数・sample rate・channel数がいずれも正。**metadataが読めただけでは成功にしない。**
- Windowを表示せず、音を鳴らさず、単一instance IPCを開始せず、settings／playlist／
  ui-state／波形cacheを作らない。
- 一部が失敗しても全件を試し、形式ごとの可否を1回で把握できるようにする。終了コードは
  成功`0`、decode失敗`1`、CLI不正`2`。
- **検査用の音源は製品配布物へ同梱しない。** 呼び出し側がpathを渡す
  （`scripts/build-release.ps1`は`assets/test_audio`から渡し、無い形式は警告して
  「未検証」と記録する）。

### 12.3 ZIPリリースの生成（P7-B2）

`scripts/build-release.ps1`が build → layout → package smoke → ライセンス資料検査 →
ZIP → **展開後の layout・selftest・codec test** → SHA-256 → manifest を通しで行う。
失敗時は`release/`へ不完全なarchiveを残さない。ZIPは`sdp/`単一rootで、
展開直後にそのまま実行できる構成にする。

`sdp/release_manifest.py`（Qt非依存）がmanifestを組み立てる。

- key順を固定し、**timestampを持たない**（同一入力なら同一JSON）。
- ファイル列挙はPOSIX相対path昇順でlocaleに依存させない。
- `content_sha256`は「相対pathと中身」だけから決まるため、mtimeやZIPの圧縮方法に
  左右されない。再現性の確認にはこれを使う。
- username・home・repositoryの絶対pathを含めない（`find_local_paths`で機械検査する）。
- pluginはload-bearingなもの（platform／multimedia）だけを記録し、DLLを全列挙しない。

**再現性の範囲**: 同一commit・同一環境で2回buildすると、ファイル集合・ファイル数・
合計サイズ・runtime version・plugin一覧・`_internal`配下の全ファイル内容は一致する。
一方で`sdp.exe`と`_internal/base_library.zip`はPyInstallerが埋め込むbuild時刻等により
bit単位では一致せず、その結果ZIP自体のSHA-256も一致しない。bit-for-bitの再現は
P7-B2の要件にしない。

### 12.4 ライセンス資料の検査（P7-B2）

`packaging/licenses-manifest.json`へ「配布物に実際に含まれるコンポーネント」と
「同梱している原文」を宣言し、`sdp/license_audit.py`が2種類を区別して報告する。

- **error**: 宣言した原文が配布物に無い（機械的な不備。build-releaseを失敗させる）
- **unresolved**: 原文追加や配布形態の判断が残っている（人が決める事項）

未解決が残るあいだは「外部配布可能」と結論づけない。詳細と根拠は
[distribution-licenses.md](./distribution-licenses.md)。

### 12.5 Windows installer（P7-C）

#### 責務の境界

- **onedir配布物**（`dist/sdp`）が「動くもの」を作り、**installer**は
  「それをWindowsへ安全に置く・更新する・消す」だけを担う。installerは
  ファイル構成を組み替えず、`dist/sdp`をそのまま`{app}`へ展開する。
- `scripts/build-installer.ps1`は**未検証のsource treeを詰めない**。
  P7-B2の`build-release.ps1`を通し、`dist/sdp`のcontent hashが
  release manifestの`contents.content_sha256`と一致することを確かめてからcompileする
  （ZIP配布物とinstaller入力が同一内容であることの担保）。
- `packaging/installer.iss`がinstaller仕様のsource of truthで、Inno Setup GUIの
  ローカル状態には依存しない。versionと入力配布物は`/D`で外部注入し、
  未定義なら`#error`でcompileを止める。

#### scope とユーザーデータ

- per-user。`PrivilegesRequired=lowest`、`PrivilegesRequiredOverridesAllowed=`（空）で
  コマンドラインからの昇格指定も許さない。install先は`{localappdata}\Programs\sdp`。
- **install先（`%LOCALAPPDATA%\Programs\sdp`）とユーザーデータ
  （`%LOCALAPPDATA%\sdp`）は別directory**。installerはユーザーデータへ書かず、
  uninstallでも消さない。install先へはsettings／playlist／ui-state／cacheを置かない。
- AppIdはversionを含まない固定GUID。upgradeで同じ登録を引き継ぐ。

#### upgrade

- ファイル展開の直前（`CurStepChanged(ssInstall)`）に、旧runtimeを同一volume上の
  `{app}\.upgrade-backup`へ移動する。対象は`{app}\_internal`と、`unins*`以外の
  直下ファイル。onedirの単純上書きでは旧runtime DLLや不要になったpluginが残るため。
  **アンインストーラーは消さない。**
- cleanup対象は、固定AppIdのHKCU uninstall登録が存在し、その
  `Inno Setup: App Path`と現在の`{app}`が一致し、かつ`{app}\sdp.exe`が存在する場合だけ。
  初回installで利用者が既存directoryを指定し、偶然`sdp.exe`があっても旧sdpと誤認しない。
- 展開が成功して`ssPostInstall`へ進んだらbackupを削除する。展開中の失敗や中止で
  `DeinitializeSetup`へ到達した場合は、backupから旧runtimeを復元して、旧版の起動可能性を
  できるだけ保つ。
- 旧runtimeの退避、rollback前の部分展開ファイル削除、backupからの復元が失敗した場合は
  ログへ原因を残し、ファイル展開前ならinstallを中止する。削除失敗を無視して新旧runtimeを
  混在させない。

#### 起動中の install / uninstall

- 起動中のsdpを**無断で強制終了しない**。起動中なら install も uninstall も中止する。
- 判定は`FileUseState`（`CreateFileW`をGENERIC_WRITE・共有なしで開く）で行う。
  実行中のexeはimage sectionのため書き込みで開けない。
  **読み取りで開く判定は使えない**（実行中でも読み取りは成功し、
  `FILE_SHARE_DELETE`により削除すら通る。実測で確認済み）。
  `CloseHandle`は無効handleでも成功を返すため、成否判定には使わず
  `INVALID_HANDLE_VALUE`と直接比較する（同じく実測で確認済み）。
- open失敗時は`GetLastError()`を見て、`ERROR_SHARING_VIOLATION`／
  `ERROR_LOCK_VIOLATION`だけを「実行中」とする。ACL、read-only属性、
  セキュリティ製品、I/O errorなどの別理由では「アクセスできないため中止」として
  実行中とは別の案内を出す。
- 判定は`InitializeSetup`で行う。`CloseApplications=yes`のRestart Managerは
  **silent実行時に既定でアプリを閉じてしまい、それは`PrepareToInstall`より前に起きる**。
  `InitializeSetup`はRestart Managerより前に走る唯一の入口である。
  `PrepareToInstall`はウィザード操作中に起動された場合の二段目の防波堤として残す。
- `/FORCECLOSEAPPLICATIONS`は使わない。

#### version と icon

- versionのsourceは`pyproject.toml`ひとつ。`sdp/windows_version.py`が
  semantic versionをWindowsの4要素整数へ変換する（`0.0.1`→`(0, 0, 1, 0)`。
  第4要素はbuild番号用に予約し常に0）。pre-release識別子は数値へ反映せず、
  `FileVersion`／`ProductVersion`の文字列にだけ残す（Windowsの数値比較で
  pre-releaseを正式版より小さく見せる方法が無いため）。解釈できないversionは例外。
- specは`packaging/windows-version-info.txt`をbuild時に展開して
  `build/`（git管理外）へ書き、`EXE(version=...)`へ渡す。
- `assets/sdp.ico`は7解像度を持つ自作アイコン（`tools/gen_app_icon.py`が
  標準ライブラリだけで生成。第三者素材なし、MIT）。exe・installer・uninstaller・
  ショートカット・Apps & Featuresの表示に使う。

#### 検査

- `sdp/inno_script.py`（Inno Setup scriptの限定parser）と
  `sdp/installer_contract.py`が、compilerなしでも契約を検査する。完全な
  Inno言語のparserではなく、section／`#define`／`key=value`／`Name: Value`と
  引用符だけを扱う。
- installer manifestは`sdp/installer_manifest.py`が組み立てる。ZIPのmanifestと
  同じ方針で、timestampを持たず、絶対path・username・build hostを含めない。
  `distribution: technical-verification-only`を明示的に記録する。

**ライセンスの未解決事項が残っている間は、installerも技術検証用に留め、
公開可能な配布物として扱わない。** コード署名も行っていない。

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
