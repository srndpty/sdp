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
│   │       ├── waveform.py       # WaveformData、PCM正規化、増分min/max縮約
│   │       ├── waveform_projection.py # 中央固定窓のpixel min/max投影と座標変換
│   │       └── waveform_cache.py # npz キャッシュ、キー生成、LRU 容量管理
│   ├── services/
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
| `PcmTap` / `PcmRingBuffer` | QAudioBuffer の受領、float32 mono への正規化、リングバッファへの書き込み、スナップショット読み出し | FFT、描画 |
| 各可視化ウィジェット | 固定 FPS タイマーでスナップショットを取得し、`spectrum.py` の純粋関数を通して描画する。hide / 最小化でタイマー停止と PCM タップ解除 | PCM 取得の詳細 |
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
  QAudioBufferOutput は接続しない（PcmTap とリングバッファは P5 の責務）。
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
  混ぜない。
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
  sourceなし、解析中、部分表示、完了、失敗を短い日本語表示で区別し、生のdecoder errorは表示しない。
- **duration**: シーク上限は正の`PlaybackController.duration_ms`を優先し、それが未確定の場合だけ
  completeな`WaveformData.duration_ms`を使う。partialのdurationは総時間として扱わない。
  描画のbucket時刻は常に`WaveformData.bucket_duration_ms`から求め、Controller durationへ引き延ばさない。
- **マウスシーク**: xは固定表示窓の`center - 30秒 + x / width * 60秒`へ写し、0～durationへ
  clampしてhalf-upで丸める。左press時の中心位置をdrag終了まで固定し、move中はpreviewだけを更新、
  releaseで1回だけController.seekへ委譲する。source変更、clear、hide、disableでdragを取り消す。
- **composition**: MainWindowはPlayerControls、SpeedPanel、WaveformPanel、PlaylistViewの順に配置する。
  `app.py`がP4-Aと同じWaveformAnalysisServiceをPanelへ渡し、start／shutdown順序は変更しない。
- P4は解析・cache・追従表示・クリック／ドラッグseekまで実装済み。P5でPCM tap、スペクトラム、
  レベルメーターを追加する。

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

## 9. 設定保存

- P3-Bの保存先は`%LOCALAPPDATA%\sdp\settings.json`。schema version 1では
  `playback_rate`と`pitch_compensation`だけを保存する。音量、mute、repeat、shuffle、
  現在曲、再生位置、ウィンドウ状態などはP3-Bの対象外で、暗黙に保存しない。
- Qt非依存の`AppSettings`と純粋なload／saveを`services/settings.py`へ置く。
  UI範囲外、NaN／inf、型不一致、非UTF-8、不正JSON、未知versionは復元エラーとする。
- 読み込み時は未知のキーを無視し、欠落キーは既定値で補う。
  実際の旧バージョンが生まれるまで migration を作り込まない（[AGENTS.md](../AGENTS.md) の方針）。
- `build_player()`はMainWindow構築前に設定をControllerへ適用し、構築だけでは監視を開始しない。
  `run()`が全構築後に監視を開始し、変更から1.5秒のデバウンスと正常終了時のflushで保存する。
- 書き込みは同一ディレクトリの一時ファイルへUTF-8で書き、flush／fsync後に`os.replace`で
  アトミックに置き換える。復元失敗時は既定値で起動して通知し、その起動では設定保存を
  無効化して破損ファイルを保護する。設定とプレイリストの障害・保存可否は互いに独立し、
  両方の復元に失敗した場合は両メッセージを結合して表示する。
- デバウンスタイマーによる保存が一時的に失敗した場合は5秒後に1回だけ自動再試行する。
  新たな設定変更は通常の1.5秒デバウンスを再開し、終了時の明示的なflushは再試行タイマーを
  作らない。

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
