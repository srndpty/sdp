# sdp テスト戦略

テストの種別、coverage 方針、性能計測、手動チェックリストをまとめる。
各マイルストーンで何をテストするかは [開発計画 §4](./development-plan.md#4-マイルストーンと-pr-分割)、
テスト対象の設計は [アーキテクチャ](./architecture.md) を参照する。

## 0. 方針

- 純粋ロジック（FFT、波形縮約、次曲決定、キャッシュキー、設定）は Qt から分離し、
  GUI も音声デバイスも不要な単体テストで厚く検証する。
- Qt に依存する部分は pytest-qt によるオフスクリーン統合テストで検証する。
- 実際の音声デバイスを使う再生テストは `audio` マーカーを付け、ローカルの手動実行に限定する。
- **意味のないテストで coverage の数値だけを上げない。**
  外部音声デバイス、Qt Multimedia の実音再生、PyInstaller 成果物は
  通常の単体テスト coverage の対象に含めない。

## 1. テスト用音源

`assets/test_audio/` に自作音源を同梱する（著作権上の問題がないため）。

- 440Hz 正弦波、スイープ、無音。各 3 秒、対象 6 形式すべて。
- 日本語と空白を含むファイル名の版も用意する（NF-04）。
- 生成は `tools/gen_test_audio.py`（WAV は NumPy で生成し、他形式は ffmpeg CLI で変換）。
  **開発機に ffmpeg CLI が必要**（開発計画の Blocker 参照）。

## 2. 単体テスト（pytest。GUI・音声デバイス不要）

| 対象 | 検証内容 |
|---|---|
| `analysis/spectrum.py` | SpectrumFrameの不変性／shape一致／dtype／周波数の昇順と非負／dBの有限性と0dB以下、左0 paddingとFFT_SIZE超過時の最新sample採用、Hann窓によるリーク抑制、rFFT周波数軸、100Hz／1kHz／10kHzの正弦波ピークバンド（許容幅付き）、0dBFSが約0dB・振幅0.5が約-6dBになる振幅補正、無音とDCのfloor収束、clipping入力の0dB clamp、epsilonによるlog(0)防御、96band・対数境界・幾何平均の代表周波数、無bin bandの補間（ピークを複製しない）、30Hz未満と20kHz超の除外、Nyquist制限、低sample rate、有効帯域なしの空フレーム、attack／releaseの係数検証とreset、sample rate変更でのreset、入力配列を変更しないこと |
| `analysis/ring_buffer.py` | capacity検証、空snapshot、capacity未満／ちょうど／超過、1回のappendがcapacity超過、wrap前／後／複数wrap、最新N sample、Nが保持量超過、N=0、clear、set_capacityでの作り直し、dtype float32と1次元の検証、NaN／inf防御、snapshotのread-only性と非共有性、旧snapshotの不変性、大量appendでの容量固定、sample単位ループやconcatenateを使わない構造、barrierで開始を揃えたthread競合（固定sleepを使わない） |
| `analysis/pcm.py` | QAudioBufferからのUInt8／Int16／Int32／Float、mono／stereo／3ch、無音、clipping、NaN／inf、空PCM、frame境界不正、sample rate／channel count不正、未対応format、`constData()` が None、byteCount不一致。handler内でbytesへコピーし、QAudioBufferのviewを保持しないこと。Qtが不正なQAudioFormatをリセットしてしまう条件はstub bufferで検証する |
| `analysis/pcm.py` の `PcmChunk`（P5-B） | mono入力で`left == right == mono`、stereoでch0／ch1、3ch以上でleft=ch0・right=ch1・mono=全ch平均、4形式すべてでのL／R取り出し、3配列のread-only性・float32・1次元・長さ一致、buffer破棄後も読めること（非共有）、sample rate／channel countの公開、空PCM、frame境界不正、channelごとのNaN／inf／範囲外の寄せ、長さ違い・非有限・範囲外・不正formatの拒否、呼び出し側配列のコピー、既存`audio_buffer_to_mono`との一致（互換wrapper） |
| `analysis/level.py`（P5-B） | StereoLevelFrameの不変性／bool拒否／有限性／floor〜0dB／`RMS ≦ Peak ≦ Peak hold`、無音・空入力・定値1.0（0dB）・定値0.5（約-6.02dB）・正弦波1.0のPeak約0dBとRMS約-3.01dB・正弦波0.5のRMS約-9.03dB、絶対値によるPeak、clipping入力の0dB clamp、floor clamp、epsilonによるlog(0)防御、RMS≦Peak、200万sampleでの精度（float64昇格）、入力配列を変更しないこと、NaN／inf／dtype／次元の拒否。Peak holdは即時上昇・保持時間中の維持・保持後の減衰・elapsedへの比例（tick数に依存しない）・現在Peak未満へ落ちないこと・floor未満へ落ちないこと・左右独立・process未呼出時の不変・reset・負／非有限／boolのelapsed拒否・不正な設定値の拒否 |
| `analysis/waveform.py` | WaveformDataのread-only／shape／dtype／有限性／範囲検証、UInt8／Int16／Int32／Float正規化、stereo mono化、frame境界、無音／正弦波／clipping、増分chunk境界、端数bucket、1sample追加、旧snapshot不変、60分相当18万bucket |
| `analysis/waveform_cache.py` | path／size／mtime／analysis version／bucket／format versionのkey無効化matrix、SHA-256名、日本語path、float32往復、必須field／dtype／shape／NaN／inf／min>max／duration／completeの破損matrix、allow_pickle=False、write／fsync／replace失敗、temp回収、hit時刻、決定的500MB LRU、個別削除失敗継続 |
| `metadata/reader.py` の純粋読取 | タグあり（MP3 / FLAC）・タグなし・壊れたファイル・未対応形式・欠損・ディレクトリ。日本語と空白、複数アーティストの結合、空文字の `None` 化、長さの丸めと NaN / inf / 負値の防御、長さ不明でもタグを捨てないこと、既知の解析・I/O失敗だけを`MetadataReadError`へ正規化し、属性取得などの予期しない例外は変換せず伝播すること、読み取りで元ファイルを書き換えないこと。タグ付きファイルはテスト音源を tmp_path へ複製して Mutagen 自身で書き込む（外部プロセスを起動しない） |
| `metadata/types.py` | `MetadataStatus` の値、`TrackMetadata` の不変性、長さの表示整形 |
| `playlist/entry.py` | entry_id の一意性と復元時の保持、パスの絶対化（相対・`~`・日本語・空白）、直接構築を含む欠損の検出と再確認、不変性 |
| `PlaylistPlaybackController` | FakeBackend + 実 PlaybackController + 実 PlaylistModel で、現在 entry の管理（重複パスを entry_id で区別、直接 load による解除）、次 / 前 / 自動次曲の欠損スキップ（存在確認直後に消えるTOCTOUを含む）、末尾で折り返さないこと、source世代ごとの`END_OF_MEDIA`一度だけ消費・遅延重複・手動切替への防御、Model の並べ替え / 削除 / 全消去中の current 追跡（削除で stop しないこと）、再生エラーで自動スキップしないこと |
| リピート | OFF / ALL / ONE の組合せ、ALL の前後折り返しと自動次曲、1 件だけの再実行、ONE が**手動 next/previous を妨げない**こと、ONE が直接単曲へ適用されないこと、モード変更で再生へ触れないこと、不正値の `TypeError` |
| シャッフル | seed 付き `random.Random` を注入して決定性を確保。厳密なbool設定、1 サイクル内で重複しないこと、同じパスの異なる entry_id がそれぞれ候補になること、Repeat ALL の新サイクルとサイクル境界での同曲回避、欠損と TOCTOU のスキップ、同期的な再生拒否を末尾到達と区別すること、全候補欠損でも有限で終わること、Model の行順が変わらないこと |
| シャッフル履歴 | previous が**再抽選ではなく履歴を戻る**こと、previous 後の next が未来履歴を優先すること、別entry直接再生による未来履歴のtruncateと現在entry再実行時の維持、複数サイクルで重複するentry_idを含む履歴のcursor保持、履歴外の訪問済み状態の維持、削除・欠損した履歴要素のスキップ、entry 追加・削除・移動時の整合性、current 削除／全消去／直接 load での履歴クリア、OFF→ON での新セッション、1000 件での実用的な所要時間 |
| `playlist` のロジック | 次曲決定（順次 / 1 曲リピート / 全曲リピート / シャッフルの網羅）、欠損スキップ、重複 entry_id |
| `services/logging_setup.py` | ログファイルの UTF-8 出力、多重初期化でハンドラーが増えないこと、出力先変更時のハンドラー置換、ローテーション設定、出力先の決定、未捕捉例外フックの記録と多重インストールの抑止 |
| `ui/player_controls.py` の時間整形 | `m:ss` / `h:mm:ss` の境界値と負値の扱い |
| `ui/speed_panel.py` | Slider整数値とrateの境界変換、Slider／SpinBoxの双方向同期、Controller同期Signalへの耐性、float32読み戻し時の要求表示維持、6プリセット、1.0倍reset、pitch補正ON/OFF、sourceなし操作、load／transport／seekを呼ばないこと |
| `services/settings.py` | UTF-8往復、保存キー限定（速度・ピッチ・可視化3項目）、欠落キーの既定値補完、未知キーの無視、version／型／0.5～2.0／NaN／inf検証、bool欄の厳密判定（0／1／文字列を拒否）、非UTF-8・不正JSON、fsync／replace失敗時の既存ファイル保持と一時ファイル回収。**schema version 1→2の移行**（v1の可視化設定を表示ONで補完し、同名v2キーがfalse／不正型でも未知キーとして無視すること、読み込みだけではファイルを書き換えないこと、変更後はv2で保存されること、v2の3項目の独立往復と厳密検証、未知versionの拒否）、`validate_settings`が適用前検証としてboolと値域を拒否すること |
| `services/ui_state.py`（P6-B） | UiStateの既定値と不変性、schema version 1の往復、window／splitter／last_open_directoryの欠落を「未保存」として扱うこと、未知キーの無視、未知schema versionの拒否、boolをint欄で拒否、width／height=0や負値の拒否、負座標の受理、maximizedの厳密bool、splitterの0以上と合計0拒否、絶対パスのみ受理（相対パス拒否・Unicode／空白パス）、存在しないフォルダーでも読み込みを失敗させないこと、非UTF-8・不正JSON・非objectルート、atomic save（fsync／replace／json.dump失敗時の既存ファイル保持と一時ファイル回収）。**画面補正**は整数矩形を注入して、単一画面内・左/上モニターの負座標・一部だけ画面内・完全画面外のprimary中央復帰・タイトル帯／矩形の重なりによる所属画面選択・大小が異なるsecondary基準のサイズclamp・最小サイズ・極端な座標・screenなしのfallback・最大化フラグの保持を検証する。**Splitter**は比率での再配分、合計の一致、片側が潰れないこと、総量が小さい場合の下限、総量0での保存値維持を検証する |
| `playlist/persistence.py` | `playlist.json` の往復（順序・entry_id・日本語パス）、未作成時の空リスト、ファイル状態を保存しないこと、アトミック書き込みと失敗時の既存ファイル保持、保存前のID重複検証、schema version の厳密な整数判定、非UTF-8を含む破損データごとの明示的エラー、未知キーの無視（将来は M3U8 も） |

## 3. Qt 統合テスト（pytest-qt、`QT_QPA_PLATFORM=offscreen`）

| 対象 | 検証内容 |
|---|---|
| `PlaylistModel` | 全テストで `QAbstractItemModelTester`（Fatal）を取り付ける。一括追加・指定位置への挿入・削除・全消去・`moveRows`（前後・複数行・不正引数）、entry_id の索引追随、role ごとの `data` / `headerData` と安定した `roleNames`、外部URLと内部MIMEのCopyAction限定D&D、可否照会で警告しないこと、欠損の再確認と `dataChanged` の範囲、1000 件の一括追加が単一の `rowsInserted` 通知になること |
| `PlaybackController` | **FakeBackend**（`IPlaybackBackend` のテストダブル）を使い、状態遷移・曲終了時の次曲送り・エラー時の方針を `qtbot.waitSignal` で検証 |
| `QtMultimediaBackend` | Qt enum 写像の完全性（値が増えたら失敗する）、エラー変換、故障注入による変換失敗・再入ガード、状態通知の重複抑制、音を鳴らさない load・source差し替え・再ロード。所有する QMediaPlayer / QAudioOutput は `findChildren` で取得し、テストのために公開 API を増やさない |
| `SingleInstanceService` | 同一プロセス内でサーバーとクライアントを往復させる |
| `PlayerControls` | **FakeBackend + 実 PlaybackController** で、状態ごとのボタン活性、シーク（ドラッグ中の非同期更新の抑止、有効なpress/releaseでの1回だけのseek、source・duration変更による古い操作の取消）、音量・ミュートの往復とフィードバックループの不在を検証する。子ウィジェットは `objectName` で取得する |
| `MainWindow`（UI状態） | captureがnormal geometryとSplitterサイズを返すこと、**最大化中でもnormal geometryを返すこと**、最小化状態を保存しないこと、restoreでgeometryと最大化を適用しnormal復元では既存の最大化／最小化を解除すること、画面外の保存値を画面内へ戻すこと、Splitterの往復と現在Window高さへの適応、可視化が全ON／全OFFのどちらでも保存比率を許容誤差内で復元すること、前回フォルダーを同期I/Oなしでファイルダイアログへ渡すこと、ファイル選択で親フォルダーを更新し**Cancelでは更新しないこと**、D&Dで更新しないこと、相対パスの無視、Unicodeパス、move／resizeの通知、**復元適用が通知しないこと**、JSON・schema version・保存先・保存タイマーを持たないことを検証する |
| `MainWindow` | `QFileDialog.getOpenFileName` を差し替え、キャンセル / 選択、ファイル名とタイトルの更新、`MediaStatus` とエラー表示（具体的エラーの優先、`detail` を出さないこと）、source解除、終了アクションを検証する。**設定アクションでダイアログが開くこと、二重に開かないこと、閉じた後に再度開けること、開き直しで最新設定が入ること、ダイアログの要求が調停サービス経由でControllerと各Panelへ届くこと、JSON・schema version・保存タイマーを持たないこと、復元済み表示設定をWindow表示前に反映すること、3つの可視化を個別にON/OFFできること、波形非表示中は位置追従を止め再表示で現在位置へ復帰すること**を検証する |
| `app.py` の配線（UI状態） | PlayerCompositionがUiStateSessionを保持しbuild時に監視しないこと、既定パスが`%LOCALAPPDATA%\sdp\ui-state.json`で設定ファイルと別であること、Window表示前にgeometryと前回フォルダーが復元されること、可視化の表示設定を適用したあとにSplitterが復元されること、復元だけではファイルを書き換えないこと、終了時flushで保存されること、破損時に既定位置で起動し元ファイルを上書きしないこと、ui-state／settings／playlistの障害と保存可否が互いに独立であること、3つのschemaが混ざらないこと、`run()`がstopより前にflushすること、UI層がui-stateのJSON I/Oを持たないことを検証する |
| `app.py` の配線（設定） | AppSettingsControllerを保持しSettingsSessionと同じsnapshotを使うこと、保存済み表示設定をWindow表示前に反映すること、version 1設定から正常起動し起動だけでは書き換えないこと、初期読込で保存が走らないこと、設定ダイアログの変更がControllerと各Panelへ届くこと、終了時flushでversion 2として保存されること、設定保存失敗がプレイリスト保存と再生を妨げないこと、UI層が設定JSONを読み書きしないことを検証する |
| `app.py` の配線 | Backend → Controller → PlaylistModel → 永続化サービス → MainWindow を組み立て、復元済みModelの前後曲可否をWindow構築時に反映できること。イベントループは起動しない。PcmTapを保持しBackendのQAudioBufferOutputへ接続されていること、SpectrumPanelが同じPcmTapを使うこと、**LevelMeterWidgetが1つでSpectrumPanel内にあり同じPcmTapを共有すること、mono／L／Rの3本が同じ固定容量であること、本番配線のPCM通知で3本が埋まること、MainWindowがLevelProcessorやリングバッファを持たないこと、UI層がQAudioBuffer系を参照しないこと**、SpectrumWidgetとWaveformWidgetが1つずつ共存すること、buildだけではタイマーを開始しないこと、source変更でPCMがclearされること、shutdownでタイマーとPCM受信が残らないこと、PlaybackBackend IF・FakeBackend・settings／playlist／波形cache schemaが不変であること |
| `ShortcutManager` | 実QTestキー入力で全割当、auto repeat設定、相対値の境界、sourceなし、入力Widget・ボタンSpace・modalでの抑止、管理外のCtrl+O／Ctrl+Shift+O／Ctrl+C／Ctrl+Vの通過、QObject削除後の安全性を検証する |
| `UiStateSession`（P6-B） | 未作成時の既定状態復元、保存済み状態のWindowへの適用、**復元が保存契機にならないこと**、build時に監視を始めないこと、start冪等、move／resize／最大化／Splitter／前回フォルダーそれぞれのデバウンス保存、連続変更で最終snapshotだけを保存すること、同値なら書き換えないこと、flushの即時保存とtimer停止、一時失敗後の1回自動再試行、破損時の保存無効化と元ファイル保護、Window破棄後のflushが例外を出さないこと、stop冪等とSignal解除、stop後にschedule_saveしないこと、QObject削除でtimerが残らないこと、ui-state破損時もsettingsが保存でき・settings破損時もui-stateが保存できることを検証する |
| `SettingsSession` | MainWindow構築前のController適用、build時未開始、start冪等、1.5秒相当のデバウンス、連続変更の最終snapshot、終了時flush、一時失敗後の1回自動再試行、破損時保存無効化、QObject削除時timer停止を検証する。可視化設定の変更もデバウンス保存対象になること、復元した表示設定がstart前にsnapshotへ入り保存契機にならないこと、version 1ファイルが起動では書き換わらず次の変更でversion 2になることを検証する |
| `AppSettingsController` | Controller現在値と可視化既定からの初期snapshot、applyでの差分適用と1回だけの通知、通知slot内でController実効値とsnapshotが一致すること、rate／pitch同時変更でも通知1回、Backendの実効読戻し採用、2つ目のsetter失敗時のrollbackと未適用snapshot・保存通知の抑止、同値applyでの無通知、表示ON/OFFだけの変更でControllerのsetterを呼ばないこと、SpeedPanel／ショートカット経由の変更をsnapshotへ取り込むこと（可視化設定を失わないこと）、不正値の拒否と既存設定の保持、shutdownの冪等性・監視解除・apply拒否を検証する |
| `SettingsDialog` | objectName／accessibleName、速度入力の範囲・刻み・小数桁、OK／Cancel／Applyの存在、開いた時点の適用済み設定表示、編集だけでは要求しないこと、Applyで閉じずに要求すること、OKで要求して成功時だけ閉じること、調停側が成功通知しなければ適用済み扱いにせずエラーを表示すること、Cancel／EscがApply後の変更を戻さず未適用編集だけ破棄すること、同値Applyでも1回だけ要求すること、`set_settings`での再反映、開いたダイアログの再前面化で全入力の未適用編集を維持すること、プログラム経由の不正値を適用せず短いエラーを出すこと（不正・適用失敗のままOKで閉じないこと）、成功時のエラー消去、JSON・schema version・PlaybackControllerを参照しないこと、破棄後にcallbackを残さないことを検証する |
| プレイリストの永続化ライフサイクル | 保存先の決定、ファイル未作成での空起動、順序・entry_id・重複行・日本語パス・欠損行の復元、並べ替え / 削除 / 全消去後の保存、読み書き I/O エラーのログ記録。**破損ファイルではクラッシュせず空で起動し、その起動の保存を無効化して既存ファイルを上書きしないこと** |
| UI 層の依存 | `src/sdp/ui/` 配下を再帰走査し、`qt_backend` / `QMediaPlayer` / `QAudioOutput` / `QAudioBufferOutput` / `QAudioBuffer` / `QAudioDecoder` 自身またはその配下をimportしていないことを標準ライブラリの `ast` で検査する（親モジュール経由も完全修飾して判定し、新しい依存は追加しない） |
| `MetadataReader` | 実ファイルに対する非同期完了、GUIスレッドでの反映、entry_id・token・path照合、削除・欠損・不適用結果のtoken回収、shutdown後の論理キャンセル、専用poolの並列上限。実行中の同期I/Oを強制停止できないことは協調的shutdownの制約として扱う |
| `WaveformAnalysisService` | fake decode境界でstart冪等、source監視、source変更時の即時clear、事前確認失敗でも同一tokenのstarted→failedとなること、partial／完了／失敗、cache hit／破損miss、request tokenと回収、source切替cancel、stale結果・旧cache保存防止、解析中のsize／mtime変更を終端失敗にすること、読取中削除、GUI thread受信、stat再確認とLRUがworker側であること、縮約中のGUI heartbeat、60分相当stream、timeout後もthread終了まで待つshutdown／QObject削除を検証する |
| 実`QAudioDecoder` | 音声出力deviceを使わず、WAVとMP3のPCM、実duration、有限min/max、partial、decoderのthread affinity、不正ファイル、decoder生成後のsource切替cancel、shutdown後のthread終了を通常CIで検証する |
| `WaveformColumns` / 投影 | 配列の不変性、固定60秒窓、0幅・空・無音、1対1、複数bucketのpixel集約、1bucketの複数pixel展開、peak保持、先頭／末尾空白、partial未解析範囲、x→時刻とclamp、180,000 bucketから固定幅だけを生成することを純粋テストする |
| `WaveformWidget` | QPainter描画、palette変更、resizeと投影cache、線数がpixel幅以下、source／durationなし、左右クリック、中央・端・音源端のclamp、drag move中の非通知、固定中心のpreview、release時1回、clear／source変更／hide／disableでの取消をQTestで検証する |
| `WaveformPanel` | sourceなし、started／partial／finished／cache hit／failed／cleared、状態文言がWidget内の1か所だけであること、path・token不一致の無視、生error非表示、position追従、Controller duration優先とcomplete fallback、partial duration非採用、解析中・失敗後のseek、source切替中のdrag取消をFakeBackendで検証する。さらに実Serviceをstartし、Controller→request→worker→公開Signal→Panel→Widgetの正常decode、cache hit、事前確認失敗、decode失敗、A→B切替を統合テストする |
| 波形描画性能 | 180,000 bucketを800／1,920／3,840pxへ投影し、出力・描画線が幅以下であること、QTimerによる100回のposition更新と別QTimerのGUI heartbeatが通常event loop上で共に進むことを厳格な時間上限なしで確認する |
| `PcmTap` | sourceなし、有効buffer受信、sample rate公開と重複通知の抑制、リングバッファへのappendとmono化、**mono／L／R 3本への原子的append・format＋3配列を同一世代で返す統合snapshot・writer threadのformat切替途中を観測しないこと・`snapshot_mono`／`snapshot_stereo`・左右が混ざらないこと・mono入力の左右複製・channel count公開と重複通知の抑制・channel count変更での3本再構築・sample rate変更での3本再構築・source／stopでの3本clear・pauseでの3本保持・大量append後も3本の容量が固定・コールバック内でPeak／RMSを呼ばないこと**、sample rate変更でのclearと容量再構築、source変更／stopでのclear、pauseでの保持、無効buffer（終端の空buffer）の破棄と件数計上、未対応formatの無視、通常失敗と予期しない例外のログ間引き、コールバックから例外を漏らさないこと、PlaybackControllerを操作しないこと、FFT・QWidget・PlaylistModelを参照しないこと、QAudioBufferを保持しないこと、コールバック実行threadの記録、実QAudioBufferOutputでの接続／二重接続／切断、shutdownの冪等性と終端性（queue済み／直接入力の無視、再接続拒否）、破棄後のシグナルで落ちないこと。PCMは公開スロット `handle_audio_buffer` から注入する |
| `SpectrumWidget` | objectName／accessibleName／minimumHeight／sizePolicy、初期プレースホルダー、empty／silence／96bandフレームの描画、resizeと幅0、bar数がband数とpixel幅以下であること、band毎の子Widgetを作らないこと、palette変更での再描画、db_floorの0以上／NaN／inf／bool拒否、clear_frameでの旧フレーム破棄、pause相当の再描画でのフレーム保持、マウス操作とフォーカスを持たないこと、破棄後の安全性。pixel完全一致は検証しない |
| `LevelMeterWidget` | objectName／accessibleName／minimumHeight（70〜100px）／sizePolicy／NoFocus、初期プレースホルダー、状態文字がWidget内の1か所だけであること、無音フレーム・左右で異なるフレームの描画、RMSバー／Peak線／Peak hold線の本数、floor以下を描かないこと、LevelProcessor出力の描画、極端な幅・高さでのresize、palette変更での再描画、db_floorの変更と0以上／NaN／inf／boolの拒否、clear_frameでの旧レベル破棄、pause相当の再描画でのフレーム保持、チャンネルごとの子Widgetを作らないこと、マウス操作とフォーカスを持たないこと、破棄後の安全性。pixel完全一致は検証しない |
| `SpectrumPanel`（表示ON/OFF） | 既定は両方表示であること、スペクトラム非表示でFFTとmono snapshotが止まりレベルは続くこと、レベル非表示でPeak／RMSとstereo snapshotが止まりスペクトラムは続くこと、非表示側へはframe数0でPCMを要求すること、両方非表示でタイマーが止まりPanelが畳まれてもPCMタップは受信を続けること、再表示で解析が再開すること、pause中・最小化中の表示ONではタイマーを開始しないこと、非表示だった実時間をPeak hold減衰へ加算しないこと、表示切替だけでは失敗状態を消さないこと、shutdown後の表示変更で再開しないことを検証する |
| `SpectrumPanel` | ControllerとPcmTapだけを受け取ること、SpectrumWidgetとLevelMeterWidgetが1つずつ同一Panel内で共存すること、1tickで統合snapshot 1回・FFTとLevelが各最大1回、PLAYINGで両方更新（L／R一致・RMS≦Peak≦hold）、左右非対称PCMでのレベル差、PAUSEDで両方静止（Level計算も止まる）、STOPPED／source変更で両方即時clear、sample rate／channel count変更で両Processor reset（Peak hold破棄）、PCM到着前は両方プレースホルダー、hidden／最小化でFFTとLevelが止まり共有PCM受信は継続、**FFT失敗でもLevelが継続すること・Level失敗でもSpectrumが継続すること・両方失敗でだけタイマーが止まること・共通snapshot失敗で両方止まること・いずれもControllerへ何も要求せず再生を止めないこと**、Widgetが1つ、sourceなし／load直後／PAUSED／STOPPED／NO_MEDIAでタイマー停止、PLAYINGで開始、pausedでの最終フレーム保持とFFT停止、stop／PLAYINGを維持したsource変更でのフレーム即時reset、sample rate変更でのprocessor reset、1tickでsnapshot1回・FFT最大1回、Widgetのフレーム更新、PCM到着前はFFTしないこと、hidden／最小化でWidget固有タイマーを止めつつ共有PCM受信は継続すること、再表示／復帰での再開、再入防止、停止後の古いtimeoutの安全性、shutdown後はSignal・表示イベントでも再開・変更しない終端性、FFT失敗で再生を変更せずControllerへ何も要求しないこと、失敗後もsource変更で復帰できること、波形解析・PlaylistModel・cacheを参照しないこと。固定sleepを使わず状態変化とqtbotの待機で判定する |
| スペクトラム・レベル性能 | 音声コールバック内でFFTもPeak／RMSも呼ばないこと、コールバックがリングバッファ追記だけで3本の容量を増やさないこと、リングバッファ合計メモリが固定であること、タイマー1tickで統合snapshot・FFT・Level計算が各1回を超えないこと、Peak＋RMSの所要時間の記録、連続更新中も別QTimerのGUI heartbeatが進むこと、幅を変えてもbar数が有界であること、コールバックとFFT＋band集約の所要時間の記録（**CIへ厳格な時間上限は設けない**） |
| 可視化ウィジェットのライフサイクル | 表示 ON/OFF と最小化でWidget固有タイマーが停止し、共有PCMタップは固定容量で受信を継続すること（SPEC-04） |

## 4. 実音再生テスト（`audio` マーカー。ローカル手動実行のみ）

置き場所は `tests/audio/`。再生前に必ず音量を 0.0 にして可聴音を出さない。
待機は `qtbot.waitUntil` などで行い、必ずタイムアウトを明示して無期限待機を作らない。

- ロード → 再生 → `mediaStatusChanged`（`END_OF_MEDIA`）の確認
- プレイリストから 2 曲以上を続けて再生し、曲の終わりで自動的に次曲へ進むこと。
  途中の欠損エントリを飛ばすこと。最後の曲の後に先頭へ戻らないこと
- Repeat ONE で同じ entry を読み込み直して再生し、1 回の終了で多重に読み込まないこと
- Repeat ALL で最後の曲の後に先頭へ折り返すこと
- 一時停止・再開、シーク、停止
- 再生速度変更、ピッチ補正の切り替え
- 再生中に1.50→0.75→1.00倍へ変更し、source・PLAYING・position前進・errorなしを確認する
- pitch補正ON/OFFの反復、直接load・次曲・Repeat ONE後の速度／pitch維持を確認する
- wall clock比や周波数の再収録による音質自動判定は行わない
- **実 `QAudioBufferOutput` からの PCM 取得**（`tests/audio/test_pcm_spectrum.py`）:
  WAV と MP3 での buffer 受信、sample rate と channel count、mono float32 の有限性、
  PCM タップを付けても音声出力と position が継続し `errorOccurred` が出ないこと、
  440Hz 音源のピークが 440Hz 付近になること、コールバック実行 thread の実測、
  playbackRate 0.75 / 1.5 と pitch 補正 ON/OFF での format 不変、
  stop・曲切替・終端での PCM clear、SpectrumPanel が実再生に追従して pause で静止すること、
  再生中 shutdown の安全性。環境依存の skip は入れず、通常の `audio` マーカーで実行する
- **実音での L / R レベル**（P5-B、同じファイル）: WAV と MP3 での L / R Peak / RMS 取得、
  左右同振幅のスイープ音源で L と R がおおむね一致すること、`sine440`（L=0.5 / R=0.25）で
  **L > R** となり Peak が約 -6.02dB / -12.04dB になること、正弦波の RMS が Peak - 約 3.01dB に
  なること、**音量 0.0 とミュート中でもメーターが振れること**（出力音量計ではないため）、
  playbackRate 0.75 / 2.0 でも更新が続くこと、stop で 3 本の buffer が空になること、
  Panel がスペクトラムとレベルの両方で実再生へ追従し pause で静止・stop で消えること、
  曲切替で前曲の Peak hold を引き継がないこと

形式ごとの対応可否は P0-A で 6 形式すべて実測済みのため、ここでは全形式を再検証せず、
WAV と代表的な圧縮形式 1 つに留める。

音声デバイスを前提とするため CI では実行しない（`pytest -m "not audio"`）。

```powershell
uv run pytest -m audio
```

## 5. UI テスト

- pytest-qt の `qtbot` で主要操作を検証する
  （ボタン → Controller の呼び出し、ショートカット、シークバー）。
- フルの E2E GUI 自動化は個人開発では費用対効果が合わないため行わない。
  代わりに §7 の手動チェックリストで補う。

## 6. パッケージ版スモークテスト

- `sdp.exe --selftest`: オフスクリーンで各形式を 1 秒ずつデコード再生し、
  終了コード 0 / 1 を返す。CI ではなくリリース前のローカル実行を想定する。
- 手動チェックリスト（§7）。

## 6.1 P1 手動スモーク（`uv run python -m sdp`）

GUI と実音を伴うため自動化しない。**最初は音量を下げるかミュートで確認する。**

- [ ] アプリが起動する
- [ ] 「開く...」から WAV を選べる
- [ ] ファイル名表示とウィンドウタイトルが更新される
- [ ] 再生 / 一時停止 / 再開 / 停止
- [ ] シーク（つまみを離した時点で位置が変わる）
- [ ] 音量変更とミュート
- [ ] 最後まで再生して「再生終了」が表示される
- [ ] MP3 を開いて再生できる
- [ ] 日本語・空白を含むパスを開いて再生できる
- [ ] 壊れたファイル・音声でないファイルを選んでもクラッシュせず、エラーが表示される
- [ ] ウィンドウを閉じて正常終了する
- [ ] `%LOCALAPPDATA%\sdp\logs\sdp.log` が生成される

## 6.2 P2-B 手動スモーク（プレイリストと D&D）

D&D は実際のマウス操作でしか確認できないため自動化しない。

- [ ] 「ファイルを追加...」で複数選択し、選択順のまま追加される
- [ ] 同じファイルを 2 回追加でき、2 行として並ぶ
- [ ] Explorer から 3 件以上を D&D で追加でき、元の順序が保たれる
- [ ] 日本語・空白を含むパスを D&D できる
- [ ] ディレクトリを D&D しても追加されない
- [ ] 行を上方向・下方向へ D&D で移動できる
- [ ] 連続した複数行をまとめて D&D で移動できる
- [ ] 並べ替え後に行が重複も消失もしない
- [ ] 非連続の複数行を D&D しても壊れない（移動しないだけ）
- [ ] 複数選択した行をまとめて削除できる
- [ ] 全消去のキャンセルで消えない / 確認で消える
- [ ] 見つからないファイルがグレー表示になり、行は残る
- [ ] アプリを終了して再起動すると、順序と重複行が復元される
- [ ] `%LOCALAPPDATA%\sdp\playlist.json` の内容が想定どおり
- [ ] 壊れた `playlist.json` で起動してもクラッシュせず、終了しても上書きされない
- [ ] 単曲再生（開く / 再生 / シーク等）が従来どおり動く

## 6.3 P2-C1 手動スモーク（プレイリストからの再生）

- [ ] 保存済みプレイリストからの起動直後に「前の曲」「次の曲」が使える
- [ ] 行のダブルクリックで再生が始まる
- [ ] Enter でも再生が始まる
- [ ] 再生中の行が強調表示される
- [ ] 同じファイルを 2 行追加し、それぞれ別の行として再生できる
- [ ] 「次の曲」「前の曲」ボタンが効く
- [ ] 欠損行を直接ダブルクリックするとエラー表示だけで、別の曲へ移らない
- [ ] 「次の曲」で欠損行を飛ばす
- [ ] 曲の終わりで自動的に次の曲へ進む
- [ ] 次曲開始直後に不自然にもう1曲飛ばない
- [ ] 最後の曲が終わっても先頭へ戻らない
- [ ] 再生中の行を D&D で動かしても強調が追従する
- [ ] 再生中でない行を削除しても再生が続く
- [ ] 再生中の行を削除しても音は続き、強調だけ消える。その後は自動次曲しない
- [ ] 「開く...」で単曲を直接開くと強調が消える
- [ ] 終了・再起動でプレイリストの順序は復元され、再生中の曲は復元されない

## 6.4 P2-C2 手動スモーク（リピート・シャッフル）

- [ ] リピートボタンが オフ → 全曲 → 1曲 → オフ と切り替わる
- [ ] Repeat OFF では最後の曲で止まる
- [ ] Repeat ALL では末尾から先頭へ戻る / 先頭で「前の曲」を押すと末尾へ行く
- [ ] Repeat ONE で同じ曲を繰り返す
- [ ] Repeat ONE 中でも「次の曲」で次へ進む
- [ ] 「開く...」した単曲は Repeat ONE でも繰り返さない
- [ ] シャッフル ON でプレイリストの表示順が変わらない
- [ ] 1 サイクル内で同じ曲を重複再生しない（同じパスの重複行は別々に再生される）
- [ ] 「前の曲」で実際に再生した順を戻り、その後の「次の曲」で履歴を進む
- [ ] previous 後に現在行を再実行しても、「次の曲」で元の未来履歴へ戻る
- [ ] 戻った状態で別の行を直接再生すると、それ以降の履歴が捨てられる
- [ ] Repeat OFF では全曲消化後に終了、Repeat ALL では次のサイクルへ進む
- [ ] サイクル境界で直前の曲がすぐに再生されない
- [ ] シャッフル OFF で行順のナビゲーションへ戻る
- [ ] シャッフル中の曲追加が後続の候補になる
- [ ] 履歴中の曲を削除してもクラッシュしない
- [ ] Repeat ALLの2サイクル目で別の履歴曲を削除しても、previousが直前の実再生曲へ戻る
- [ ] 現在曲を削除すると音は続き、履歴と強調が解除される
- [ ] 再起動でリピートは オフ、シャッフルは OFF へ戻る

## 6.5 P2-D 手動スモーク（メタデータ）

- [ ] タグ付き MP3 のタイトル・アーティスト・アルバムが表示される
- [ ] タグ付き FLAC でも表示される
- [ ] タグなし WAV はファイル名のまま
- [ ] 再生時間が表示される
- [ ] 日本語タグ・空白を含むタグが崩れない
- [ ] 同じファイルを複数行追加しても、各行へ正しく入る
- [ ] 読み取り中に行を D&D しても壊れない
- [ ] 読み取り中に行を削除してもクラッシュしない
- [ ] 壊れたファイルはファイル名へフォールバックし、再生も他の操作も妨げない
- [ ] 欠損ファイルはグレーかつファイル名表示。復活後に再読取される
- [ ] 1000 件追加中もウィンドウを操作できる（CPU とスレッド数が暴走しない）
- [ ] 追加・削除・D&D・前後曲・リピート・シャッフルが従来どおり
- [ ] 通常のローカルファイルでは終了処理が速やかに完了する
- [ ] ブロックした同期I/Oではプロセス終了期限を保証しない既知制約がログ・文書と一致する
- [ ] 再起動後にメタデータが非同期で再表示される
- [ ] `playlist.json` にメタデータが入っていない

## 6.6 P3-A 手動スモーク（速度とピッチ）

実画面・実マウス・低音量で行う。プログラムからの`setValue`や`click`だけで代替しない。

- [ ] SpeedPanelがプレイリストを過度に圧迫せず、sourceなしでも操作できる
- [ ] Sliderと数値入力が同期し、0.50～2.00を超えない
- [ ] 6プリセットと「1.0倍に戻す」が正しく反映され、UIが振動・再帰しない
- [ ] pitch補正ON（time-stretch）で0.75 / 1.25 / 1.50 / 2.00倍を聴き、テンポだけが変わり音高がおおむね維持される
- [ ] pitch補正OFF（varispeed）で0.75倍は音高が下がり、1.25倍以上では音高が上がる
- [ ] 再生中にON→OFF→ON、0.5→2.0→1.0を操作して再生・positionが継続する
- [ ] 次／前／自動次曲／Repeat ONE／Repeat ALL／シャッフル／直接「開く...」で設定を維持する
- [ ] sourceが再読み込みされず、先頭へ戻らず、クラッシュや長時間停止がない
- [ ] 音途切れ、長時間の無音、time-stretchの明確な破綻を主観評価して記録する

## 6.7 P3-B 手動スモーク（ショートカットと設定復元）

実ウィンドウをアクティブにして行い、P3-Aの聴感確認と合わせてMVP受け入れとする。

- [ ] READMEの全ショートカットが動作し、J/L、音量、速度の長押しだけが連続動作する
- [ ] 数値入力中の文字キー、ボタン上のSpace、ファイル選択／確認dialog中のキー操作を奪わない
- [ ] 速度とピッチを変更し、約1.5秒後の`settings.json`がその2項目だけを含む
- [ ] 変更直後に終了しても値が保存され、再起動後のSpeedPanelへ復元される
- [ ] 音量、mute、repeat、shuffle、現在曲、再生位置は再起動後に復元されない
- [ ] 破損した`settings.json`で既定値起動・通知・元ファイル保護が行われる

## 6.8 P4-A 手動スモーク（波形解析基盤）

P4-Aには表示Widgetがないため、ログと`%LOCALAPPDATA%\sdp\cache\waveforms`を観測する。

- [ ] WAV／圧縮音源を開くと再生を妨げずcacheが生成される
- [ ] 同じ音源の再loadはcache hitになり、mtime／size変更後は再解析される
- [ ] cacheを壊してもクラッシュせず再解析・置換される
- [ ] 解析中のsource切替で旧結果が公開・保存されない
- [ ] 解析中も再生、シーク、速度、pitch、プレイリスト操作が応答する
- [ ] 終了時にwaveformAnalysisThreadが残らない
- [ ] cacheは500MB上限のLRU対象となり、音源directoryへファイルを作らない

## 6.9 P4-B 手動スモーク（追従波形とマウスシーク）

- [ ] sourceなし、解析中、partial、完了、cache hit、解析失敗の各表示が判別できる
- [ ] 現在位置線が中央に固定され、先頭・末尾の範囲外は空白になる
- [ ] 再生・一時停止・0.5／1.0／2.0倍・seek後も波形が位置へ追従する
- [ ] 左クリックで1回seekし、右・中央クリックではseekしない
- [ ] drag中はpreviewだけが動き、releaseで1回だけseekする
- [ ] drag中の曲切替、連続次曲、Repeat ONE／ALL、shuffleで新曲を誤seekしない
- [ ] WAV／MP3／30～60分音源と100%／可能なら150%表示倍率でresize・操作が重くならない
- [ ] 解析失敗中も再生UI、プレイリスト、速度・ピッチ操作を継続できる

## 6.10 P5-A 手動スモーク（PCMタップとリアルタイムスペクトラム）

実画面・実マウス・実音で行う。自動テストでは代替できない。**P5完了時に実施済み**。

表示:

- [ ] sourceなしでプレースホルダーが出る
- [ ] 再生開始でスペクトラムが動く
- [ ] 一時停止で静止し、再開で動き出す
- [ ] 停止でスペクトラムが消える（無音表示へ戻る）
- [ ] WAV と MP3 の両方で動く
- [ ] 日本語・空白を含むパスで動く
- [ ] ウィンドウresize、100%表示倍率、可能なら150%でも崩れない
- [ ] 波形とスペクトラムがプレイリスト領域を過度に圧迫しない

周波数:

- [ ] 正弦波音源（100Hz / 1kHz / 10kHz）でピーク位置がおおむね正しい
- [ ] 通常の音楽で低音が左、高音が右に出る
- [ ] 無音でfloorへ下がる
- [ ] attackが速くreleaseが滑らかで、不自然な激しいちらつきがない

再生状態:

- [ ] play / pause / stop / seek / 次曲 / 前曲
- [ ] Repeat ONE / Repeat ALL / shuffle
- [ ] 0.5 / 1.0 / 2.0倍、pitch補正 ON / OFF
- [ ] いずれでもクラッシュせず、前曲のPCMを次曲の表示へ持ち越さない

負荷:

- [ ] 10分以上の連続再生でUI操作が重くならない
- [ ] CPU使用率が暴走しない（タスクマネージャで目視）
- [ ] メモリが増加し続けない
- [ ] 最小化中に更新が止まり、復帰後に再開する

なお、可聴音を伴わない範囲での実測は済んでいる（30FPS を50秒継続して 30.3FPS、
プロセス CPU 8.6%、RSS は 113MB で横ばい、PCM 破棄 0 件、リングバッファ 345KB 固定）。
上記は「実際に目と耳で確認する」項目として残す。

## 6.11 P5-B 手動スモーク（Peak／RMSレベルメーター）

実画面・実音で行う。自動テストでは代替できない。**P5完了時に実施済み**。

表示:

- [ ] sourceなしでプレースホルダーが出る
- [ ] 再生でL／Rのバーが動く
- [ ] Peakが瞬時に反応する
- [ ] RMSがPeakより低く、動きが滑らか
- [ ] Peak holdが約1秒残り、その後自然に下がる
- [ ] pauseで静止する（Peak holdも下がらない）
- [ ] stopで消える（プレースホルダーへ戻る）
- [ ] 曲切替で前曲のPeak holdが残らない
- [ ] mono音源でL／Rが同じ振れ方になる
- [ ] stereo音源（`sine440`はL=0.5 / R=0.25）で左右差が見える
- [ ] RMS・Peak・Peak holdが色だけでなく形（塗り／細線／太線）で区別できる

信号位置（出力音量計ではないこと）:

- [ ] 音量0でもメーターが振れる
- [ ] ミュート中でもメーターが振れる
- [ ] varispeed（pitch補正OFF）でも取得PCM自体の振幅を表示する
- [ ] READMEの説明と画面の挙動が一致する

レイアウト:

- [ ] 波形・スペクトラム・レベルメーター・プレイリストが同時に見える
- [ ] プレイリストが極端に狭くならない
- [ ] 100%と（可能なら）150%表示倍率で崩れない

長時間・負荷:

- [ ] 10分以上の連続再生でCPUが暴走しない
- [ ] メモリが増加し続けない
- [ ] 最小化でタイマーが止まり、復帰で再開する
- [ ] source連続切替、Repeat ONE／ALL、shuffle、0.5／1.0／2.0倍、pitch ON／OFFでクラッシュしない

なお、可聴音を伴わない範囲での実測は済んでいる（30FPSを60秒継続して実測30.2〜30.3FPS、
PCMコールバック平均0.372ms／最大1.571ms、Peak＋RMS平均0.040ms、プロセスCPU 5.8%、
RSSは60.9→65.5MBでt=30s以降横ばい、リングバッファ3本合計1,034KB固定、PCM破棄0件）。
上記は「実際に目と耳で確認する」項目として残す。

## 6.12 P6-A 手動スモーク（設定画面と可視化ON/OFF）

実画面で行う。自動テストでは代替できないため**リリース前ゲートとして残す**。

- [ ] 「ツール」→「設定...」で設定画面が開き、複数回開閉できる
- [ ] 開いている状態でもう一度開くと、新しい窓ではなく前面へ出る
- [ ] Applyで閉じずに反映され、OKで反映して閉じる
- [ ] Apply後にCancelしても、適用済みの変更が戻らない
- [ ] Applyしていない編集はCancel／Escで破棄される
- [ ] 波形・スペクトラム・レベルメーターを個別にON/OFFできる
- [ ] 3つすべてOFFでもプレイリストと再生操作が崩れない
- [ ] 3つすべてONへ戻すと、現在再生中の曲へ追従して表示が再開する
- [ ] 非表示にするとCPU負荷が下がる（タスクマネージャで目視）
- [ ] 再起動後に設定（速度・ピッチ・表示ON/OFF）が復元される
- [ ] 旧version 1の`settings.json`から正常起動し、可視化はすべて表示になる
- [ ] version 1のファイルは起動しただけでは書き換わらず、設定変更後にversion 2になる
- [ ] 破損した`settings.json`では既定値で起動し、元ファイルを上書きしない
- [ ] 100%／可能なら150%表示倍率でダイアログが崩れない
- [ ] Tab順が自然で、キーボードだけで操作でき、EscでCancelできる

## 6.13 P6-B 手動スモーク（ウィンドウ状態と前回フォルダー）

実画面で行う。マルチモニターと解像度変更は自動テストで代替できないため
**リリース前ゲートとして残す**。

- [ ] ウィンドウを移動して終了し、次回起動で同じ位置に出る
- [ ] サイズを変えて終了し、次回起動で同じサイズになる
- [ ] 最大化して終了し、次回起動でも最大化で出る
- [ ] 最大化を解除すると、最大化前のサイズに戻る
- [ ] 最小化したまま終了しても、次回は最小化では起動しない
- [ ] スプリッターの比率を変えて終了し、次回起動で復元される
- [ ] 可視化が全ONのときも全OFFのときもスプリッターが復元され、プレイリストが潰れない
- [ ] 「開く...」でファイルを選ぶと、次回そのフォルダーから始まる
- [ ] ダイアログをキャンセルしても前回フォルダーが変わらない
- [ ] プレイリストへのD&Dでは前回フォルダーが変わらない
- [ ] 日本語・空白を含むフォルダーでも前回フォルダーが復元される
- [ ] secondary monitorへ移動して終了し、次回同じモニターで出る
- [ ] 大小の異なるmonitor間で、保存サイズが復元先monitor内へ収まる
- [ ] secondary monitorを外した状態で起動しても画面外へ消えない
- [ ] 解像度を下げた状態で起動しても画面外へ消えず、サイズが画面内へ収まる
- [ ] 切断済みネットワークフォルダーを前回位置にしても、「開く...」の前処理で固まらない
- [ ] 100%／可能なら150%表示倍率で崩れない
- [ ] `ui-state.json`を壊すと既定位置で起動し、通知が出て**元ファイルが上書きされない**
- [ ] `ui-state.json`が壊れていても設定とプレイリストは保存される
- [ ] 移動やリサイズを連打しても、書き込みが連続して発生しない（終了直前の状態が残る）

## 7. 手動チェックリスト（リリース前）

- [ ] ファイルの D&D 追加で順序が維持される
- [ ] Explorer の「プログラムから開く」に sdp が出現する
- [ ] 関連付けから起動して再生できる
- [ ] 起動中に別ファイルを開くと既存プロセスへ転送され、ウィンドウが前面化する
      （前面化できない場合はタスクバーが点滅する）
- [ ] 日本語・空白を含むパスで再生できる
- [ ] 既定のアプリ設定への導線が動作する
- [ ] アンインストール後にレジストリの登録が残っていない
- [ ] インストール直後の初回起動でクラッシュしない

## 8. 性能計測方法（NF-03）

`--perf-log` フラグでログへ出力する。

- 可視化のフレーム時間（`QElapsedTimer`、95 パーセンタイル）
- `audioBufferReceived` の処理時間
- 波形解析のスループット（音源の分数 / 実時間秒）

P5-A / P5-B のローカル実測（PySide6 6.10.3 / Windows 11。CI では上限を課さない）:

| 項目 | 実測 |
|---|---|
| PCM コールバック 1 回（P5-A: mono 1 本） | 平均 0.056ms / 最大 0.374ms |
| PCM コールバック 1 回（P5-B: mono + L / R 3 本、単体計測） | 平均 0.122ms / 最大 0.435ms |
| PCM コールバック 1 回（P5-B: 30FPS 更新と同時に 60 秒） | 平均 0.372ms / 最大 1.571ms（33msのフレーム予算を十分下回る） |
| Peak + RMS（L / R 各 4096 sample）+ Peak hold | 平均 0.040ms / 最大 0.165ms |
| 1 tick 合計（FFT + Level） | 約 0.17ms（33ms を十分下回る） |
| 30FPS を 60 秒継続（P5-B） | 実測 30.2〜30.3FPS（FFT 1,815 回 = Level 1,815 回 = snapshot 回数） |
| PCM 通知頻度（P5-B、1.0 倍） | 10.6 件/秒 / 破棄 0 件 |
| プロセス CPU（P5-B、offscreen 60 秒） | 3.5s / 60.0s = 5.8% |
| RSS（P5-B、60 秒） | 60.9MB → 65.5MB（t=30s 以降は増加なし） |
| GUI heartbeat（別 QTimer、33ms 指定） | 21.3 回/秒で進み続ける（CoarseTimer のため 30 は超えない） |
| リングバッファ 3 本合計 | 48kHz で 96,000 sample × 3 = 1,125KB / 44.1kHz で 1,034KB 固定 |
| 4096 点 FFT + 96band 集約 | 平均 0.114ms / 最大 0.441ms |
| 30FPS を 50 秒継続 | 実測 30.3FPS（FFT 1,514 回 = snapshot 回数と一致） |
| PCM 通知頻度（1.0 倍） | 10.8 件/秒（P0-C の 11.2 件/秒と整合） |
| プロセス CPU | 8.6%（Qt のデコードと音声出力、波形パネル、実描画を含む） |
| RSS | 104MB → 113MB で横ばい（t=35s 以降は増加なし） |
| リングバッファ | 44.1kHz で 88,200 sample = 345KB 固定 |
| 破棄した PCM buffer | 0 件 |

タイマー種別の比較も実測した。既定の CoarseTimer では 33ms 指定でも約 21FPS まで落ちるため、
`Qt.TimerType.PreciseTimer` を指定している。

psutil などの依存は追加しない。CPU 使用率はタスクマネージャの目視と
チェックリストへの記録で足りる（個人開発の現実性を優先する）。

計測シナリオ:

1. 再生 + 全可視化 ON で 10 分間
2. 60 分の FLAC の波形解析
3. 1000 曲のプレイリスト復元
4. 起動時間

目標値（いずれも目安）: 可視化のフレーム時間 95 パーセンタイルが 33ms 未満、
60 分音源の解析 30 秒以内、起動 2 秒以内。未達の項目は既知の制限として記録する。

## 9. CI と coverage

CI（GitHub Actions、Windows のみ）の構成は `.github/workflows/ci.yml` を参照。
ローカルと CI で同じ `scripts/check.ps1` を実行する。

- coverage は branch coverage を有効化し、対象は `src/sdp` のみ。
  `spike/` とテストコードは対象に含めない。
- 下限は **80%**。XML（`coverage.xml`）を生成し、CI では artifact として保存する。
- 下限は「意味のあるテストの結果として満たす」ものとし、
  数値合わせのためのテストは書かない。
