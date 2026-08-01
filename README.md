# sdp (sound player)

Windows 11 向けの個人用ローカル音声プレイヤー。
ローカルの音声ファイルを再生し、DJ ソフトのような追従波形と
foobar2000 のようなリアルタイムスペクトラムを表示することを目標にしている。

## 開発状況

**P1（単曲再生基盤）とP2（プレイリストとメタデータ）は完了。**
P3（速度・ピッチ、ショートカット、設定永続化）は実装・自動テスト済み。
実画面・実マウス・聴感による手動受け入れが終わればMVP完了となる。
P4（波形解析・キャッシュ・追従波形）は実装・自動テスト済み。
**P5（リアルタイムスペクトラムとPeak／RMSレベルメーター）は、実画面・実音の
手動受け入れまで完了。** これにより当初計画上の「sdpらしい初回完成版」へ到達した。

P6（設定画面、可視化の表示ON/OFF、ウィンドウ状態と前回フォルダーの復元、
再生設定と現在曲の復元、保存・復元失敗時のUX）は実装・自動テスト済み。
P6-A／P6-Bの実画面受け入れは完了しており、**P6-Cの実画面受け入れが未完了**で
リリース前ゲートとして残っている。

P7-A（起動引数と単一instance）は実装・自動テスト済み。
実際のPowerShell間転送とWindowsの前面化制約については手動受け入れが未完了。
P7-B1（PyInstaller onedir配布ビルド）とP7-B2（配布版の実環境検証とZIPリリース生成）は
実装・自動検証済み。配布版で6形式の実decode、ZIP展開後の起動、read-only配置、
Defenderスキャンまで確認した。実音・実画面の受け入れ（可聴再生、150%DPI、SmartScreen、
クリーン環境）と外部配布ライセンスの未解決事項は残っており、
**外部公開可能な配布物とは扱わない**（[docs/distribution-licenses.md](./docs/distribution-licenses.md)）。

P7-C（Inno Setupのper-userインストーラーとWindows関連付け）は実装・自動検証済み。
silent install／same-version reinstall／upgrade失敗時の旧版復元／起動中の
upgrade・uninstall中止／uninstallまでを実プロファイル上で通しで確認した
（[installer smoke](#installerの動作確認)）。
インストーラーも**ライセンスの未解決事項が残るあいだは技術検証用**であり、
公開配布物として扱わない。

`uv run python -m sdp` でウィンドウが起動し、次の操作ができる。

単曲再生:

- 「ファイル」→「開く...」で音声ファイルを 1 つ選ぶ
- 再生 / 一時停止 / 停止
- シークバーによる再生位置の変更、現在位置と総時間の表示
- 音量変更とミュート
- 再生状態（再生中 / 一時停止 / 停止）の表示
- 読み込み・再生エラーのステータスバー表示（技術詳細はログファイルへ）

プレイリスト:

- 一覧表示（タイトル / アーティスト / アルバム / 長さ / パス、件数表示）
- タグの非同期読み取り（[Mutagen](https://mutagen.readthedocs.io/) を使用）。
  追加した直後はファイル名が表示され、読み取りが終わるとタイトル等へ更新される。
  タグが無い・読み取れない場合はファイル名のまま表示し、再生や他の操作は妨げない
- 「ファイルを追加...」からの複数ファイル追加（選択順を維持）
- Explorer などからの複数ファイルのドラッグ＆ドロップ追加（元の順序を維持）
- 同じファイルの重複追加
- 行のドラッグ＆ドロップによる並べ替え（連続した複数行に対応）
- 複数選択した項目の削除、全項目の消去（確認あり）
- 見つからないファイルのグレー表示（行は削除しない）
- 終了時の自動保存と、次回起動時の自動復元（順序・重複行を維持）

プレイリストからの再生:

- 行のダブルクリックまたは Enter で再生
- 再生中の行の強調表示
- 「前の曲」「次の曲」
- 曲の終わりで自動的に次の曲へ進む
- 見つからないファイルは曲送りのときだけ自動で飛ばす
  （直接選んだ場合は飛ばさず、エラーを表示する）
- リピート: オフ / 全曲 / 1 曲（ボタンで切り替え）
- シャッフル（プレイリストの表示順は変えず、再生順だけをランダムにする）
- シャッフル中の「前の曲」は、実際に再生した履歴をさかのぼる
  （ランダムに選び直さない）。戻ったあとの「次の曲」は履歴を進む

リピートが「オフ」のときは、最後の曲が終わるとそこで止まる。
リピート・シャッフル・前回の曲は次回起動時に復元する（下の「保存される項目」を参照）。
**再生位置と「再生中だったか」は保存しない**ため、復元された曲は選択されるだけで
自動再生はしない。

メタデータは `playlist.json` へ保存せず、起動のたびに非同期で読み直す。

再生速度とピッチ:

- 0.50～2.00倍のスライダーと数値入力（再生中も即時反映）
- 0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0倍のプリセット
- 「1.0倍に戻す」操作
- ピッチ維持（time-stretch）とピッチ連動（varispeed）の即時切り替え
- source未選択時にも設定でき、直接「開く...」・プレイリスト曲切替・リピート後も
  現在値を維持
- 速度とピッチ補正は変更から約1.5秒後と正常終了時に保存し、次回起動時に復元

キーボードショートカット:

| キー | 操作 |
|---|---|
| `Space` | 再生 / 一時停止 |
| `S` | 停止 |
| `J` / `L` | 10秒戻る / 進む |
| `Shift+J` / `Shift+L` | 60秒戻る / 進む |
| `Alt+Left` / `Alt+Right` | 前の曲 / 次の曲 |
| `Ctrl+Up` / `Ctrl+Down` | 音量を0.05上げる / 下げる |
| `M` | ミュート切替 |
| `X` / `C` | 速度を0.05下げる / 上げる |
| `Z` | 速度を1.00倍へ戻す |
| `P` | ピッチ補正切替 |
| `R` | リピートモード切替 |
| `Ctrl+H` | シャッフル切替 |

ショートカットはウィンドウがアクティブなときだけ動作する。文字入力欄・数値入力欄・
編集可能なComboBoxへの入力、ボタン上の`Space`、モーダルダイアログ表示中は操作を奪わない。

波形解析基盤:

- sourceを専用QThreadのQAudioDecoderでバックグラウンド解析
- PCMをmono float32へ変換し、20ms単位のmin/maxへ全PCMを保持せず増分縮約
- 解析途中の部分結果を通知し、source変更時は古い解析結果を論理キャンセル
- path・size・mtime・解析version・bucket設定によるnpzキャッシュ無効化
- キャッシュ破損時は削除して再解析し、解析失敗でも音声再生は継続
- 最終利用時刻による500MB上限のLRU削除
- 再生位置を中央に固定し、前後30秒（合計60秒）を追従表示
- 解析途中は取得済みの部分波形を表示し、cache hit時は保存済み波形を表示
- 波形上の左クリックでシーク
- ドラッグ中は候補時刻だけをプレビューし、release時に1回だけシーク
- source切替時は旧波形とドラッグ操作を即時解除
- 解析失敗時も短いプレースホルダーだけを表示し、音声再生は継続

リアルタイムスペクトラム:

- `QAudioBufferOutput` による再生中PCMの取得（**音源を再デコードしない**）
- 1回のPCM変換からmono・左・右を派生させ、mono／L／R各1本の固定長リングバッファへ保持
  （48kHzで2秒相当を3本で約1.1MB。sample rate・channel数の判明時に作り直す）
- Hann窓と4096点FFT（rFFT）による振幅算出と窓補正
- 30Hz〜min(20kHz, Nyquist)を対数96バンドへ集約し、dB（下限-90dB）で表示
- attack／releaseを分けた時間平滑化（立ち上がりが速く、減衰は滑らか）
- 約30FPSでの更新（実測30.3FPS）
- 一時停止中は最後のフレームを静止表示し、新しいFFTを止める
- 停止・曲切替では旧PCMと旧フレームを即時破棄する
- ウィジェット非表示時とウィンドウ最小化中はFFT・平滑化・描画タイマーを停止し、復帰で再開する
- 共有PCMタップは可視性にかかわらず接続を維持し、固定容量リングバッファだけを更新する
- スペクトラムの失敗は音声再生を妨げない

Peak／RMSレベルメーター:

- 左右チャンネル別のPeakレベルとRMSレベル（dBFS、下限-90dB）
- RMSは塗りつぶしバー、Peakは細い縦線、Peak holdは太い短線で表示（色だけに頼らない）
- Peak holdは約1秒保持したあと24dB/秒で減衰する。減衰は描画FPSではなく**実時間基準**
- mono音源では左右を同じ値で表示する（3ch以上の音源では先頭2chをL／Rとして扱う）
- 一時停止中は最後の表示を保持し、Peak holdの時間も進めない
- 停止・曲切替では表示とPeak holdを即時破棄する
- スペクトラムと同じ再生中PCMを、format＋mono／L／Rが一貫した統合snapshotとして約30FPSで共有する
- レベルの失敗はスペクトラムと音声再生を妨げない（逆も同様）

スペクトラムとレベルメーターは**再生中のPCMから生成する**。音源を独立したデコーダーで
再デコードせず、波形キャッシュとも独立している。なお `QAudioBufferOutput` が渡すのは
**速度・ピッチ処理と音量・ミュートを適用する前**のデコード済みPCMのため、
ピッチ補正OFF（varispeed）で2倍再生しても表示上のピークは元の周波数のままになり、
ミュート中でもスペクトラムは振れる（[docs/p0-report.md](./docs/p0-report.md) §8.6、§8.7）。

**レベルメーターは出力音量計ではない。** 表示しているのは音量・ミュートを適用する前の
**入力信号（音源）のレベル**なので、音量を0にしてもミュート中でもメーターは振れる。
出力音量の変化はメーターへ反映されない。

設定画面:

- 「ツール」→「設定...」で開く（同時に2つは開かない）
- 再生速度（0.50〜2.00、0.05刻み）とピッチ補正
- 音量（％）とミュート
- リピート（オフ／全曲／1曲）とシャッフル
- 波形／スペクトラム／Peak／RMSレベルメーターの表示ON/OFF
- 「適用」は閉じずに反映、「OK」は反映して閉じる、「キャンセル」（Esc）は
  **適用済みの変更は戻さず**、未適用の編集だけを破棄する
- 開いている設定画面を再度呼んだ場合は、未適用の入力を維持したまま前面へ出す
- 適用に失敗した場合は入力を維持してエラーを表示し、OKでも閉じない
- 設定は変更から約1.5秒後と正常終了時に保存し、次回起動時に復元する

可視化を非表示にすると、隠すだけでなくその可視化の解析も止まる。

- 波形OFF: 位置追従の描画更新を停止（解析キャッシュは残す）
- スペクトラムOFF: FFTとmono PCMの取得を停止
- レベルメーターOFF: Peak／RMS計算とL／R PCMの取得を停止
- 3つともOFF: 可視化のタイマーを停止（再生中PCMの受信だけは継続する）

再表示すると、その時点の最新PCMから表示を再開する。

前回終了時の状態:

- 再生速度・ピッチ補正・音量・ミュート・リピート・シャッフルを復元する
- 前回選んでいた曲を選び直す（**自動再生はしない**。再生位置は先頭から）
- 曲が削除されていた場合はエラーにせず、曲を選ばずに起動する
- 同じファイルを複数行追加していても、前回と同じ行を復元する（並べ替え後も同じ）
- **再生位置と「再生中だったか」は保存しない**。数秒の曲では復元価値が低く、
  前回位置から突然再開するのは予測しにくいため、まず曲の選択だけを復元する

ウィンドウ状態の復元:

- 終了時のウィンドウ位置・サイズ・最大化状態を次回起動時に復元する
  （最小化状態は保存しない。最小化中に終了しても次回は通常表示か最大化で開く）
- 上下スプリッター（プレイヤー／プレイリスト）の比率を復元する。
  ウィンドウの高さが前回と違っても比率を保って再配分する
- 「開く...」で最後に使ったフォルダーから次回もダイアログが開く
  （キャンセルした場合と、プレイリストへのドラッグ＆ドロップでは変わらない）
- モニターを外した・解像度が変わった場合でもウィンドウが画面外へ消えない
  （どの画面にも掛からない位置なら主画面の中央へ戻す。マルチモニターの負座標は保つ）
- これらは設定画面から変更する項目ではないため、`settings.json` とは別の
  `ui-state.json` へ保存する（意識して選ぶ設定と、自然に変わる状態を混ぜない）

起動引数と単一instance:

- コマンドラインで1件以上のファイルを指定すると、保存済みプレイリストの末尾へ追加する
- 相対パスは起動したPowerShellのcurrent directoryを基準にする。順序と重複は維持する
- 既にsdpが起動中なら既存Windowとプレイリストへ転送し、2つ目のprocessは常駐しない
- 引数なしで2回目を起動した場合も、既存Windowを前面化する要求として扱う
- 転送を受けたWindowは最小化を解除して前面化を試みる。最大化状態は維持する
- 引数解析中に存在確認をせず、ディレクトリ、欠損パス、未知拡張子も
  既存のプレイリスト追加契約どおり行として受理し、ファイルでない場合は欠損表示にする

**未実装**: LUFS／true peak などのラウドネス表示、スペクトログラム、ウォーターフォール、
カバーアート、メタデータの編集、オンラインからの情報取得、
プリセット編集、独立したピッチシフト、波形ズーム、ステレオ別波形、
可視化の色設定・バンド数設定・FPS設定・Peak hold時間設定、多チャンネルの個別表示、
ショートカット編集、再生デバイス選択、キャッシュ容量設定、設定のインポート／エクスポート、
設定の検索、再生位置の復元、プレイリストの選択行の復元、OpenGL描画、M3U8 入出力、
コード署名。

### 保存される項目

「アプリを閉じても覚えている値」の一覧。ここが唯一の一覧で、schemaと手動テストの
記載もこれへ合わせる。

| 値 | 保存先 | schema |
|---|---|---|
| プレイリストの並び（順序・重複・entry_id） | `playlist.json` | version 1 |
| 再生速度、ピッチ補正 | `settings.json` | version 1 以降 |
| 波形・スペクトラム・レベルメーターの表示ON/OFF | `settings.json` | version 2 以降 |
| 音量、ミュート、リピート、シャッフル | `settings.json` | version 3 |
| ウィンドウ位置・サイズ・最大化、スプリッター比率、前回フォルダー | `ui-state.json` | version 1 以降 |
| 前回の曲（`entry_id`） | `ui-state.json` | version 2 |

**保存しない**: 再生位置、「再生中だったか」、プレイリストの選択行、
メタデータ（起動のたびに読み直す）、ファイルの欠損状態（起動後に確認し直す）。

保存場所（いずれも `%LOCALAPPDATA%\sdp` 配下）:

- ログ: `logs\sdp.log`（1MB × 5 世代）
- プレイリスト: `playlist.json`（曲の並びだけ。メタデータや再生状態は保存しない）
- 設定: `settings.json`（再生速度、ピッチ補正、音量、ミュート、リピート、シャッフル、
  可視化3種類の表示ON/OFFだけ。schema version 3。旧version 1／2のファイルも読み込め、
  次の変更時にversion 3で保存する）
- ウィンドウ状態: `ui-state.json`（位置・サイズ・最大化・スプリッター比率・前回フォルダー・
  前回の曲だけ。schema version 2。削除すると既定の位置とサイズで起動する）
- 波形キャッシュ: `cache\waveforms\*.npz`（削除しても音源から再生成される）

波形キャッシュは元の音声ファイルと別のアプリデータ領域へ保存し、音声ファイル自体は
変更しない。

3つの保存ファイル（`settings.json` / `playlist.json` / `ui-state.json`）のどれかが
壊れていた場合は、そのファイルだけ既定状態で起動してステータスバーへ短く通知し、
**その起動では上書き保存しない**（元のファイルを残す）。複数が壊れていても
「設定とプレイリストの復元に失敗しました。既定状態で起動します。」のように
1文へまとめて表示し、技術的な詳細はログファイルへ残す。
3つのファイルの保存可否と障害は互いに影響しない。

保存に失敗した場合も、再生を止めずステータスバーへ短く知らせる
（「設定を保存できませんでした。」など）。同じ失敗を出し続けることはなく、
保存できるようになった時点で1回だけ復旧を知らせる。

実装の進め方は [docs/development-plan.md](./docs/development-plan.md) を参照。

## 必要環境

- Windows 11
- [uv](https://docs.astral.sh/uv/)（Python 本体は uv が用意する）
- 主開発環境は CPython 3.13（CI で CPython 3.14 の互換性も確認している）

## セットアップ

```powershell
uv sync
```

開発用ツール（Ruff、Pyright、pytest など）を含めて同期する場合:

```powershell
uv sync --all-groups
```

## 実行

```powershell
uv run python -m sdp
```

起動時にファイルをプレイリスト末尾へ追加する例:

```powershell
uv run python -m sdp ".\assets\test_audio\sine440.wav"
uv run python -m sdp "C:\Music\日本語 曲.mp3" "C:\Music\second.flac"
```

既にsdpが起動中でも同じコマンドを使える。要求は既存instanceへ転送され、
送信側は受理確認後に終了する。同名instanceに接続できるのに転送できない場合は、
二重起動せず終了コード2を返す。

`assets/test_audio/sine440.wav` などのテスト音源で動作を確認できる。
**最初は音量を下げるかミュートにしてから再生すること。**

## Windows配布ビルド（P7-B1）

PyInstaller 6の`onedir`形式で、consoleを表示しない`dist\sdp\sdp.exe`を作る。
Pythonやuvをインストールしていない環境でも、このディレクトリ全体を維持すれば起動できる。
`sdp.exe`だけを`_internal`から切り離してコピーしてはならない。

実行環境はWindows 11 x64と、Microsoft Visual C++ v14 Redistributable x64を必要とする。
sdpはMSVC／Universal CRTのDLLを同梱しない。`VCRUNTIME140.dll`または`MSVCP140.dll`が
見つからないと表示される場合は、[Microsoft公式の最新サポート対象Visual C++
Redistributable](https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist)を
インストールする。installerは導入済みかを確認するだけで、Redistributableの同梱・
自動install・UAC昇格は行わない。

```powershell
pwsh -File scripts/build-package.ps1
pwsh -File scripts/package-smoke.ps1
pwsh -File scripts/package-gui-smoke.ps1
```

ビルドスクリプトは以前の`build`／`dist`を安全確認後に削除し、
`packaging\sdp.spec`から常にクリーンビルドする。スモークは配布物を一時ディレクトリへ
コピーし、制限した`PATH`と隔離した`LOCALAPPDATA`で次を実行する。

```powershell
dist\sdp\sdp.exe --selftest
```

`--selftest`はWindowを表示せず、Qt Widgets／Network／Multimediaのobject構築、ログ・一時領域の
書き込みに加え、その場で生成した短い無音PCM WAVをFFmpeg backendの`QAudioDecoder`で実decode
する。音声出力、単一instance server、設定・playlist・ui-state・波形cacheの作成は行わず、
一時WAVも必ず削除する。終了コードは成功`0`、依存／書き込み失敗`1`、不正なCLI指定`2`である。
ファイルpathとの併用は不正指定として扱う。

配布物ルート（`sdp.exe`と同じ階層）へ`LICENSE`と`THIRD_PARTY_NOTICES.txt`を置き、
各wheelが提供するライセンス原文を`_internal\licenses`以下へ収録する。
**外部配布のライセンス条件はまだ解決していない。** 何が揃っていて何が未解決かは
[docs/distribution-licenses.md](./docs/distribution-licenses.md)にまとめ、
`uv run python tools/license_audit.py dist\sdp`で機械的に検査できる。

## ZIPリリースの作成（P7-B2）

```powershell
pwsh -File scripts/build-release.ps1
```

build → layout検査 → package smoke → ライセンス資料検査 → ZIP作成 →
**別の一時ディレクトリへ展開して layout・selftest・codec test を再実行** →
SHA-256とmanifest生成、までを1コマンドで行う。途中で失敗した場合、不完全なarchiveを
`release\`へ残さない。生成物は次の3つ。

```text
release/
├─ sdp-0.0.1-windows-x64.zip
├─ sdp-0.0.1-windows-x64.zip.sha256
└─ sdp-0.0.1-windows-x64.manifest.json
```

ZIPは`sdp/`という単一のフォルダーを含む。**フォルダーごと展開して使うこと**
（`sdp.exe`だけを取り出すと`_internal`が無く起動しない）。manifestにはversion、
architecture、ファイル数、内容のSHA-256、runtime version、同梱pluginを記録する。
ユーザー名やビルド環境の絶対pathは含めない。

## 配布版のdecode検査（--codec-test）

```powershell
dist\sdp\sdp.exe --codec-test "C:\path\song.wav" "C:\path\song.mp3"
```

指定した音源を実際に`QAudioDecoder`でPCMへ展開し、**bufferが1件以上・frame数・
sample rate・channel数がすべて正**であることを確認する。metadataが読めただけでは
成功にしない。Windowも音も出さず、単一instance IPCも設定ファイルも作らない。
1件でも失敗すると終了コード`1`になり、一部が失敗しても残りは必ず検査する。
検査用の音源は配布物へ同梱しないため、pathの指定は必須である。

この環境（Windows 11 build 26200 / PySide6 6.10.3 / FFmpeg n7.1.3）では、配布版で
**WAV・MP3・FLAC・Ogg Vorbis・Opus・M4A(AAC)の6形式すべてが実decodeに成功**した。
可聴再生・音声デバイス依存の挙動は別途手動で確認する。

PySide6 6.10.3の標準hookによる実測では、QtのFFmpeg backendとWindows Media Foundation
backend、FFmpeg runtime DLLが配布物へ入る。Qt 6ではFFmpeg backendが既定で、Windows
backendは6.10から非推奨である。sdpが別途FFmpeg CLIを起動することはない。形式ごとの対応は
backendに含まれるcodec、Windows、driver等でも変わるため、WAV／MP3／FLAC／Ogg Vorbis／
Opus／M4A／AACは対象Windows環境で実音確認する。
（[Qt Multimediaのbackend説明](https://doc.qt.io/qt-6/qtmultimedia-index.html)、
[Windows固有事項](https://doc.qt.io/qt-6/qtmultimedia-windows.html)）

## Windowsインストーラーの作成（P7-C）

```powershell
pwsh -File scripts/build-installer.ps1
```

[Inno Setup 6.3以降](https://jrsoftware.org/isinfo.php)が必要
（`winget install --id JRSoftware.InnoSetup -e`）。PATHに無い場合は
`-InnoSetupCompiler <ISCC.exeのpath>` か環境変数`INNO_SETUP_COMPILER`で指定する。
未導入なら対処方法つきのエラーで停止する。

ZIPリリース生成 → **ZIP配布物とinstaller入力の内容一致検証** → layout検査 →
ライセンス資料検査 → installer契約検査 → selftest・codec test →
version resource確認 → compile → SHA-256とmanifest、までを1コマンドで行う。
途中で失敗した場合、前回の正常なinstallerを`release\`から消さない。

```text
release/
├─ sdp-0.0.1-windows-x64.zip                        （P7-B2）
├─ sdp-0.0.1-windows-x64.zip.sha256
├─ sdp-0.0.1-windows-x64.manifest.json
├─ sdp-0.0.1-windows-x64-setup.exe                  （P7-C）
├─ sdp-0.0.1-windows-x64-setup.exe.sha256
└─ sdp-0.0.1-windows-x64-installer.manifest.json
```

**このインストーラーは技術検証用である。** コード署名を行っていないため、
ダウンロードして実行するとSmartScreenの警告が出る想定であり、
同梱ライブラリのライセンス条件も未解決である。外部公開・再頒布はしない
（[docs/distribution-licenses.md](./docs/distribution-licenses.md)）。

### インストールされるもの

| 項目 | 内容 |
|---|---|
| 方式 | per-user（管理者権限・UAC昇格なし。Program Filesへ書かない） |
| インストール先 | `%LOCALAPPDATA%\Programs\sdp` |
| ユーザーデータ | `%LOCALAPPDATA%\sdp`（**インストール先とは別。上書きも削除もしない**） |
| スタートメニュー | `sdp`（標準で作成） |
| デスクトップ | 任意（インストーラーのチェックボックス。既定はオフ） |
| レジストリ | HKCUのみ（`Software\Classes\sdp.AudioFile`、`Software\Classes\Applications\sdp.exe`、各拡張子の`OpenWithProgids`、`Software\sdp\Capabilities`、`Software\RegisteredApplications`） |
| 関連付け候補 | `.wav` `.mp3` `.flac` `.ogg` `.opus` `.m4a` `.aac`（ProgIDは`sdp.AudioFile`の1つ） |

### 既定のアプリは変更しない

Windows 10/11では、既定のアプリ（`UserChoice`）をインストーラーから正しく変更できない。
sdpは**「プログラムから開く」の候補として登録するだけ**で、`UserChoice`には一切触れない。
既定にしたい場合は、利用者がWindowsの設定（`ms-settings:defaultapps`）から選ぶ。
既存の関連付けを奪うことはない。

### アンインストール

「アプリと機能」または`%LOCALAPPDATA%\Programs\sdp\unins000.exe`から実行する。
削除されるのはインストールしたファイル、ショートカット、インストーラーが作成した
HKCUの登録、アンインストーラー自身だけである。

**設定・プレイリスト・UI状態・波形キャッシュ・ログは削除しない。**
再インストールするとそのまま引き継がれる。手動で消す場合は次を削除する。

```text
%LOCALAPPDATA%\sdp
```

sdpの起動中はインストールもアンインストールも中止される（無断で強制終了しない）。
sdpを終了してからやり直すこと。ファイルの権限やセキュリティ製品のブロックで
`sdp.exe`へアクセスできない場合も、「実行中」ではなく別のエラーとして中止する。

### 更新（upgrade）

同じインストール先へ上書きすると、古いランタイムを消してから新しいものを展開する
（onedirの単純上書きでは不要になったDLLやプラグインが残るため）。

- 掃除の対象は、**このインストーラーが登録した既存インストール先だけ**である。
  初回インストール時にインストール先を変更し、そこに偶然`sdp.exe`があっても、
  無関係なファイルは削除しない。
- 古いランタイムは削除ではなく`.upgrade-backup`へ退避する。展開が成功したら破棄し、
  途中で失敗・中止された場合は元へ戻す。**更新に失敗しても、以前のsdpは起動できる。**
- 設定・プレイリスト・UI状態・キャッシュは更新の影響を受けない。

### installerの動作確認

```powershell
pwsh -File scripts/installer-smoke.ps1 -ConfirmProfileChanges
```

既存sdp.exeを含むディレクトリへの初回install（誤削除しないこと）→ silent install →
install済みexeのselftestとcodec test → same-version reinstall →
更新失敗時の旧版復元 → 起動中のupgrade・uninstallが中止されること → uninstall →
ユーザーデータ保持、までを自動で確認する（136項目）。
**実行ユーザーのプロファイル（`%LOCALAPPDATA%`、HKCU、
スタートメニュー）を実際に変更する**ため`-ConfirmProfileChanges`を必須にしており、
CIからは実行しない。可能ならWindows Sandboxか検証用の新規Windowsユーザーで実行する。

Inno Setup compilerが無くても、installer scriptの契約（per-user、HKCUのみ、
UserChoice非変更、対象7拡張子、uninstall時のユーザーデータ保持など）は
pytestと次のコマンドで検査できる。

```powershell
uv run python tools/installer_contract.py
```

### アプリアイコン

`assets/sdp.ico`（16／24／32／48／64／128／256px）はフォントやクリップアート等の
第三者素材を使わず、`tools/gen_app_icon.py`の図形描画だけで生成した自作物で、
sdp本体と同じGPL-3.0-onlyで扱う。再生成する場合のみ次を実行する（追加依存は不要）。

```powershell
uv run python tools/gen_app_icon.py
```

## 開発コマンド

自動修正（Ruff の lint 自動修正 + フォーマット）:

```powershell
pwsh -File scripts/fix.ps1
```

品質チェック（CI と同じ内容: lockfile 整合性 / format check / lint / Pyright /
テスト / coverage 下限）:

```powershell
pwsh -File scripts/check.ps1
```

### pre-commit

commit 時に高速な検査（空白除去、末尾改行、YAML/TOML 検証、Ruff）を実行する。
初回のみ以下でフックをインストールする。

```powershell
uv run pre-commit install
```

全ファイルに対して手動実行する場合:

```powershell
uv run pre-commit run --all-files
```

Pyright と pytest 全件は pre-commit では実行しない。`scripts/check.ps1` と CI で実行する。

## テスト

```powershell
uv run pytest
```

実際の音声デバイスや実音再生を必要とするテストには `audio` マーカーを付ける。
これらはローカルでの手動実行のみを想定しており、**通常の CI からは除外される**
（`pytest -m "not audio"`）。coverage の下限は 80% とし、
`audio` テスト・PyInstaller 成果物・実音再生は coverage の対象に含めない。

`audio` マーカーのテストを含めて実行する場合:

```powershell
uv run pytest -m audio
```

### テスト音源

`assets/test_audio/` に自己作成のテスト音源（正弦波・スイープ・無音を全対応形式で用意）を
コミットしてある。通常はそのまま使えるため、再生成は不要。

再生成する場合のみ **FFmpeg CLI（`ffmpeg` と `ffprobe`）が別途必要**で、
`winget install --id Gyan.FFmpeg -e` などで導入し PATH へ通しておく。

```powershell
uv run python tools/gen_test_audio.py
```

FFmpeg CLI はテスト音源生成用の開発ツールであり、sdp 本体からは実行せず、
依存関係にも配布物にも含めない。Qt Multimedia が内部で使う FFmpeg バックエンドとは別物である。

## 設計文書

- [開発計画](./docs/development-plan.md) — 要件一覧、MVP 境界、マイルストーン、PR 分割
- [アーキテクチャ](./docs/architecture.md) — モジュール構成、責務、スレッド境界、可視化設計
- [ADR-0001 再生エンジン選定](./docs/adr/0001-playback-engine.md) — Qt Multimedia / mpv / VLC の比較
- [テスト戦略](./docs/testing-strategy.md) — テスト種別、性能計測、手動チェックリスト
- [開発規約](./AGENTS.md)

## ライセンス

sdpのcombined workはGPL-3.0-onlyで配布する。
過去にMIT Licenseで公開された部分は、元の条件でも引き続き利用できる。
[LICENSES/MIT.txt](./LICENSES/MIT.txt)とgit履歴を参照する。

配布物に含まれる第三者componentと、対応source公開前の残課題は
[docs/distribution-licenses.md](./docs/distribution-licenses.md)を参照する。
