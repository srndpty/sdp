# sdp 開発計画

Windows 11 向け個人用ローカル音声プレイヤー sdp (sound player) の開発計画。

関連文書:

- [アーキテクチャ](./architecture.md) — 設計の詳細（モジュール、責務、スレッド、可視化）
- [ADR-0001 再生エンジン選定](./adr/0001-playback-engine.md) — Qt Multimedia / mpv / VLC の比較と決定
- [テスト戦略](./testing-strategy.md) — テスト種別、coverage 方針、性能計測
- [開発規約](../AGENTS.md)

---

## 1. 前提と方針の区別

本計画では「確定事項」「推測（妥当な既定値）」「未検証事項」を区別する。
未検証事項は P0（技術検証）で実測し、結果によって設計を変更する。

### 1.1 確定事項

- Python + PySide6 Widgets、uv 管理、日本語 UI、日本語コメント / docstring / 設計文書。
- 対象 OS は Windows 11 のみ。ネットワーク機能・動画は扱わない。
- 配布は exe + インストーラー。既定アプリを強制変更しない。
- UI から QMediaPlayer を直接操作しない。PlaybackController と PlaybackBackend を分離する。
- 汎用プラグイン基盤は作らない。将来必要になるかもしれないという理由だけで抽象化しない。
- 主開発環境は CPython 3.13（`requires-python = ">=3.13,<3.15"`、CI で 3.14 互換も確認）。
- **プレイリストへの同一ファイル重複追加は許可する**（foobar2000 方式。各行は独立した
  エントリ ID を持つ）。
- **ピッチ補正 ON/OFF 切替は必須要件**とする（P3 までに実現）。

### 1.2 推測（既定値として採用。実装前に覆してよい）

| 項目 | 既定値 | 根拠 |
|---|---|---|
| 設定・プレイリスト永続化 | JSON ファイル（レジストリではない） | 可搬性と手編集のしやすさ |
| パッケージング | PyInstaller onedir + Inno Setup（per-user） | 起動速度と AV 誤検知の観点で onefile より有利 |
| 波形キャッシュ形式 | NumPy `.npz`、上限 500MB の LRU | 実装が単純で NumPy 以外の依存が不要 |
| UI状態の保存 | `ui-state.json`（settings.jsonとは別ファイル、schema version 2、v1読込可、整数値で保存） | v2で現在曲の`entry_id`を追加する。v1は現在曲なしで補完し、読み込みだけでは移行保存しない。base64のsaveGeometryは手編集・DPI差・画面外補正に不利なため採らない |
| 設定schema | JSON、schema version 3（v1／v2も読み込み可） | 古いversionに無い項目は既定値で補い、次の変更時に現在のversionで保存する。起動しただけでは書き換えない |
| Repeatの保存表現 | `"off"` / `"all"` / `"one"` の文字列 | core の `RepeatMode` は `auto()` で永続化を意図していないため、保存層で安定した文字列へ写す |
| 再生位置の保存 | 保存しない | 数秒の曲では復元価値が低く、突然の再開は予測しにくい。まず現在曲の選択復元だけでUXを評価する |
| スペクトラム既定値 | 4096 点 FFT、30FPS、96 バンド対数軸、下限 -90dB | 32バンドでは粗すぎたためP5-Aの実測に基づき96バンドへ決定した。CPU負荷は実測0.114ms/フレームで問題なし |
| レベルメーター既定値 | 4096 sample 窓（FFT と共通）、30FPS、下限 -90dB、Peak hold 1.0 秒・減衰 24dB/秒 | 下限と窓長をスペクトラムへ揃えて定数と snapshot 経路を共有した。Peak hold の減衰は tick 数ではなく実経過秒で進める。CPU 負荷は実測 0.040ms/フレーム |
| 描画方式 | 自前 QPainter 描画（PyQtGraph は不採用） | 中央固定スクロール等の要件が特殊で、汎用 API との格闘コストの方が高い |

### 1.3 未検証事項（P0 で検証。結果次第で設計変更）

| # | 未検証事項 | 影響範囲 |
|---|---|---|
| U1 | PySide6 6.10 の `QMediaPlayer` におけるピッチ補正関連 API の存在と、Windows ffmpeg バックエンドでの対応状況 | 再生エンジン選定（最重要） |
| U2 | varispeed（速度連動ピッチ）と time-stretch（ピッチ維持）双方の音質（0.5〜2.0 倍） | 再生エンジン選定 |
| U3 | `QAudioBufferOutput` からの PCM 取得の安定性・オーバーヘッド・接続スレッド・速度変更時の挙動 | スペクトラム設計 |
| U4 | Windows 版 PySide6 同梱 ffmpeg での WAV / MP3 / Vorbis / Opus / FLAC / M4A デコード可否 | 対応形式 |
| U5 | `QAudioDecoder`（オフラインデコード）の形式対応と処理速度 | 波形解析設計 |
| U6 | シーク精度と再生終了通知（`mediaStatusChanged`）の信頼性 | 再生基盤 |
| U7 | PyInstaller 化後の再生、および日本語・空白を含むパスの取り扱い | 配布 |
| U8 | mpv 採用時の PCM 取得手段（libmpv は音声 PCM タップを公開しない前提で、位置駆動可視化により回避できるか） | 代替案の実現性 |

---

## 2. 要件一覧

ID 体系は `<分類>-<連番>`。「フェーズ」は実装マイルストーン（§4）。

### 2.1 再生（PLAY）

| ID | 要件 | フェーズ |
|---|---|---|
| PLAY-01〜08 | 再生 / 一時停止 / 停止 / 前の曲 / 次の曲 / シーク / 音量変更 / ミュート | P1 |
| PLAY-09 | 順次再生・1 曲リピート・全曲リピート・シャッフル | P2 |
| PLAY-10 | 再生時間と総時間の表示 | P1 |
| PLAY-11 | タイトル・アーティスト・アルバム等のメタデータ表示（取得失敗時はファイル名） | P2 |
| PLAY-12 | 対応形式: WAV / MP3 / OGG Vorbis / OGG Opus / FLAC / M4A(AAC)。拡張子だけで対応可否を断定せず、実行環境のデコード対応状況で判定する | P0 / P1 |

### 2.2 プレイリスト（PL）

| ID | 要件 | フェーズ |
|---|---|---|
| PL-01 | 複数音声ファイルのドラッグ＆ドロップ追加（D&D 時の順序を維持） | P2 |
| PL-02 | ファイル選択ダイアログからの複数追加 | P2 |
| PL-03 | プレイリスト内の並べ替え | P2 |
| PL-04 | 選択項目の削除・全消去 | P2 |
| PL-05 | 欠損ファイルの表示（グレー表示、再生時スキップ） | P2 |
| PL-06 | 前回終了時のプレイリスト復元 | P2 / P6 |
| PL-07 | 同一ファイルの重複追加を許可する（エントリ ID で区別） | P2 |
| PL-08 | M3U8 入出力 | 将来（P8 以降） |

### 2.3 再生速度とピッチ（SPD）

| ID | 要件 | フェーズ |
|---|---|---|
| SPD-01 | 再生速度の変更（UI 上の範囲は 0.5〜2.0 倍） | P3 |
| SPD-02 | ピッチを維持した速度変更（time-stretch） | P3 |
| SPD-03 | 速度に応じてピッチも変化する再生（varispeed） | P3 |
| SPD-04 | ピッチ補正 ON/OFF をユーザーが切り替えられる | P3 |
| SPD-05 | 1.0 倍へ戻す操作 | P3 |
| SPD-06 | よく使う速度のプリセット（既定 0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0） | P3 |
| SPD-07 | キーボードショートカット（速度操作を含む全体表を整備） | P3 |

### 2.4 スペクトラム表示（SPEC）

| ID | 要件 | フェーズ |
|---|---|---|
| SPEC-01 | リアルタイム周波数スペクトラム表示（PCM 取得 → 正規化 → mono 化 → Hann 窓 → FFT → 対数周波数軸バンド集約 → attack/release 平滑化） | P5 |
| SPEC-02 | 描画 FPS 制限と、無音時・停止時の自然な減衰 | P5 |
| SPEC-03 | 再生速度変更時にも表示が破綻しない | P5 |
| SPEC-04 | 非表示時・最小化時はWidget単位のFFT・平滑化・描画を停止する。共有PCMタップは固定容量バッファへの受信を継続する | P5 |

### 2.5 追従波形（WAVE）

| ID | 要件 | フェーズ |
|---|---|---|
| WAVE-01 | 再生位置を中央に固定し、左に過去約 30 秒・右に未来約 30 秒を表示してスクロールする | P4 |
| WAVE-02 | 波形のクリックまたはドラッグによるシーク | P4 |
| WAVE-03 | バックグラウンド解析（min/max peak envelope による縮約、表示幅に応じたダウンサンプリング、長時間音声でも UI を停止させない、解析中でも再生可能） | P4 |
| WAVE-04 | キャッシュ（キー: ファイルパス + サイズ + 更新日時 + 解析バージョン、破損・旧バージョンの安全な無効化、容量制限） | P4 |
| WAVE-05 | 解析失敗が再生を妨げない | P4 |

### 2.6 追加ビジュアライザー（VIS）

段階的な追加候補。実装難易度・CPU 負荷・視覚的な面白さの比較で優先順位を決めた。

| ID | 種別 | 実装難易度 | CPU 負荷 | 面白さ | フェーズ |
|---|---|---|---|---|---|
| VIS-01 | Peak / RMS レベルメーター | 低 | 低 | 中 | P5 |
| VIS-02 | オシロスコープ | 低 | 低 | 中 | P5（余力があれば） |
| VIS-03 | スペクトログラム | 中 | 中 | 高 | 将来 |
| VIS-04 | ステレオ・ベクトルスコープ | 中 | 低 | 低 | 将来 |
| VIS-05 | クロマグラム | 高 | 中 | 低 | 将来 |

VIS-01 と VIS-02 はスペクトラムのために作る PCM 供給基盤をそのまま使えるため、
追加コストが小さい。VIS-03 以降は表示バッファやピッチ解析の追加実装が必要になるため
MVP にも初回完成版にも含めない。

### 2.7 Windows 統合（WIN）

| ID | 要件 | フェーズ |
|---|---|---|
| WIN-01 | `sdp.exe` へ 1 個以上のファイルパスを渡して再生できる | P7 |
| WIN-02 | Explorer の「プログラムから開く」候補として登録できる（ProgID と Capabilities をインストーラーで登録し、アンインストール時に削除する） | P7 |
| WIN-03 | 既定アプリを強制変更せず、Windows 設定の「既定のアプリ」を開く導線を設ける | P7 |
| WIN-04 | 既に sdp が起動している場合は、新しいプロセスから既存プロセスへファイルを転送する（単一インスタンス） | P7 |
| WIN-05 | Windows 用 exe とインストーラーの配布 | P7 |

### 2.8 非機能（NF）

| ID | 要件 | フェーズ |
|---|---|---|
| NF-01 | 再生失敗・メタデータ失敗・解析失敗を独立して扱う | P1〜P4 |
| NF-02 | ログとエラー処理（ローテーション、Qt ログ統合、未捕捉例外の記録） | P1 |
| NF-03 | 性能目標（可視化 30FPS 維持、60 分音源の波形解析 30 秒以内、起動 2 秒以内。いずれも目安で P8 に計測） | P8 |
| NF-04 | 日本語・空白・長いパスの取り扱い | P0 以降すべて |

---

## 3. MVP と将来機能の境界

- **最小実用版（MVP）= P3 完了時点**
  再生一式 + プレイリスト（D&D・復元）+ 速度 / ピッチ切替 + キーボードショートカット。
- **sdp らしい初回完成版 = P5 完了時点**
  上記 + 追従波形 + スペクトラム + レベルメーター。
  **P5-B の実装・自動テストと実画面・実音の手動受け入れをもって到達済み。**
- **リリース版 = P8 完了時点**
  上記 + 設定 UI + Windows 統合 + インストーラー + 品質・性能整備。

初期スコープ外（実装しない）:
音楽ライブラリ全体の自動スキャン、ネット配信、動画、CD 再生、VST、高機能イコライザー、
完全なギャップレス再生、クロスフェード、WASAPI 排他、ビットパーフェクト再生、歌詞検索、
音響 AI 分類、プラグイン API。M3U8 入出力（PL-08）は P8 以降の将来機能とする。

---

## 4. マイルストーンと PR 分割

原則として 1 フェーズを 1〜2 個の PR で扱う。各 PR は独立してレビューできる大きさに保つ。
設計の詳細（クラス構成・データフロー）は [architecture.md](./architecture.md) を参照。

### P-1: 開発基盤の初期化（PR#0 — 完了）

- **目的**: git / uv / lint / format / 型検査 / test / coverage / pre-commit / CI / 設計文書の整備。
- **変更ファイル**: `pyproject.toml`、`uv.lock`、`.python-version`、`.gitignore`、
  `.gitattributes`、`.editorconfig`、`.pre-commit-config.yaml`、
  `.github/workflows/ci.yml`、`scripts/{check,fix}.ps1`、`src/sdp/{__init__,__main__}.py`、
  `tests/test_smoke.py`、`AGENTS.md`、`CLAUDE.md`、`README.md`、`docs/**`
- **受け入れ条件**: `scripts/check.ps1` が成功し、`uv run python -m sdp` が正常終了する。
- **テスト**: スモークテスト（import、バージョン取得、エントリポイントの終了コード）。

### P0: 技術検証（PR#1）

- **目的**: 再生エンジンの確定。§1.3 の未検証事項を実機で確認する。
- **変更ファイル**: `spike/p0_*.py`（項目別の検証スクリプト。本体からは独立させ、
  lint / coverage 対象外とする）、`tools/gen_test_audio.py`、`assets/test_audio/`、
  `docs/p0-report.md`（新規）、`docs/adr/0001-playback-engine.md`（結果を反映して状態更新）
- **検証項目**（ユーザー指定 10 項目 + 追加 1 項目）:
  1. WAV / MP3 / OGG Vorbis / OGG Opus / FLAC / M4A の再生
  2. 0.5 / 0.75 / 1.0 / 1.25 / 1.5 / 2.0 倍再生
  3. **ピッチ補正 ON/OFF**（API の存在確認と実際の音響挙動・音質。最優先）
  4. シーク
  5. 再生終了通知
  6. `QAudioBufferOutput` からの PCM 取得（受信スレッドの確認を含む）
  7. PCM からの RMS と FFT 計算
  8. 再生速度変更中の可視化同期
  9. exe 化後の再生
  10. 日本語・空白を含むパスの再生
  11. （追加）`QAudioDecoder` による 6 形式のオフラインデコード（U5）
- **受け入れ条件**: 全項目の結果が `docs/p0-report.md` に記録され、
  ADR-0001 が Accepted になる（または mpv 昇格の判断が下り、mpv で同項目を再検証済み）。
- **テスト**: spike は使い捨ての検証コードのため自動テストの対象外。
  `tools/gen_test_audio.py` のみ簡易確認を行う。

### P1: 再生基盤（PR#2）

- **目的**: 単曲再生の完成と、アーキテクチャ骨格の確立。
- **変更ファイル**: `src/sdp/app.py`、`src/sdp/__main__.py`、
  `src/sdp/core/playback/{backend,qt_backend,controller}.py`、
  `src/sdp/ui/{main_window,player_controls}.py`、
  `src/sdp/services/{logging_setup,settings}.py`（設定は骨格のみ）、`tests/`
- **受け入れ条件**: ファイルダイアログで開いた 1 曲を 再生 / 一時停止 / 停止 / シーク /
  音量 / ミュート でき、時間表示が更新され、曲の終わりで停止する。
  再生エラーはダイアログではなくステータス表示とログに出る。
  UI が再生バックエンドを直接 import していない。
- **テスト**: FakeBackend による PlaybackController の状態遷移テスト、
  `qt_backend` の `audio` マーカー付き実音テスト、ログ設定の単体テスト。

### P2: プレイリストと D&D（PR#3、メタデータを PR#4 に分割してもよい）

- **目的**: PL-01〜07、PLAY-09、PLAY-11。
- **変更ファイル**: `src/sdp/core/playlist/{entry,model,persistence}.py`、
  `src/sdp/core/metadata/reader.py`、`src/sdp/ui/playlist_view.py`、
  `src/sdp/ui/main_window.py`（統合）、`core/playback/controller.py`（曲順・シャッフル拡張）
- **受け入れ条件**: D&D の順序を維持した追加、ダイアログからの複数追加、並べ替え、削除、
  全消去、欠損のグレー表示、重複追加可、終了時保存と起動時復元、
  リピート（1 曲 / 全曲）とシャッフル、メタデータの非同期表示（失敗時はファイル名）。
  1000 曲追加で UI がフリーズしない。
- **テスト**: `QAbstractItemModelTester`、次曲決定ロジックの網羅、永続化の往復、
  メタデータ読取の非同期完了。

### P3: 速度とピッチ（PR#5）— **ここまでで最小実用版**

- **目的**: SPD-01〜07。
- **変更ファイル**: `src/sdp/ui/speed_panel.py`、`src/sdp/ui/shortcuts.py`、
  `core/playback/{backend,qt_backend}.py`（速度・ピッチ補正 API の実装）、
  `services/settings.py`（速度・ピッチ設定の永続化）、`docs/`（ショートカット表）
- **受け入れ条件**: スライダーとプリセットで 0.5〜2.0 倍に変更でき、
  ピッチ補正トグルが即座に反映され、1.0 倍リセットが動作し、
  ショートカットが全て機能する。再生中の速度変更で音の途切れが許容範囲に収まる。
- **テスト**: ショートカットから Controller が呼ばれることの Qt テスト、
  速度・ピッチの `audio` マーカー付きテスト、設定往復の単体テスト。

### P4: 追従波形（PR#6: 解析とキャッシュ、PR#7: ウィジェットとシーク）

- **目的**: WAVE-01〜05。mpv へ切り替えた場合にも可視化の基盤となる中核機能。
- **変更ファイル**: `src/sdp/core/analysis/{waveform,waveform_cache}.py`、
  `src/sdp/core/analysis/waveform_projection.py`、
  `src/sdp/services/waveform_analysis.py`、
  `src/sdp/ui/{waveform_widget,waveform_panel,main_window}.py`
- **受け入れ条件**: 再生位置中央固定で ±30 秒がスクロール表示され、クリック / ドラッグで
  シークでき、60 分音源の解析中も UI 操作でき部分描画される。キャッシュヒット時は即表示。
  更新日時・サイズ・解析バージョンの変化と破損キャッシュで再解析される。
  容量上限を超えると LRU で削除される。解析失敗でも再生が継続する。
- **テスト**: 縮約結果、pixel投影、x→時刻、QPainter描画、click／drag、path／token照合、
  キャッシュ無効化のマトリクス、解析ワーカーのキャンセル、180,000 bucketの表示応答性。

### P5: スペクトラムと追加ビジュアライザー（PR#8: PCM タップとスペクトラム、PR#9: レベルメーター）
— **ここまでで sdp らしい初回完成版**

- **目的**: SPEC-01〜04、VIS-01（余力があれば VIS-02）。
- **変更ファイル**: `src/sdp/core/analysis/{ring_buffer,pcm,spectrum,level}.py`、
  `src/sdp/services/pcm_tap.py`、
  `src/sdp/ui/{spectrum_widget,spectrum_panel,level_meter_widget}.py`
- **進行状況**: P5-A（PCMタップ、リングバッファ、FFT、スペクトラムWidget）と
  P5-B（PcmChunkによるL／R抽出、Peak／RMS、Peak hold、レベルメーターWidget、
  可視化の表示制御と例外境界の分離）はいずれも実装・自動テスト済み
  （実測30.3FPS。詳細は [architecture.md §6](./architecture.md)）。
  **実画面・実音の手動受け入れも完了し、P5は完了**
  （[testing-strategy.md §6.10、§6.11](./testing-strategy.md)）。
  これにより当初計画上の「sdpらしい初回完成版」へ到達した。
  P3・P4-Bの手動受け入れ項目は引き続きリリース前ゲートとして残っている。
- **受け入れ条件**: 30FPS を維持（フレーム時間の 95 パーセンタイルが 33ms 未満）し、
  対数周波数軸のバンド表示、attack/release 平滑化、停止・無音時の減衰が動作する。
  L／R別のPeakとRMSが表示され、Peak holdが一定時間保持されてから実時間基準で減衰する。
  速度変更で表示が破綻しない。非表示・最小化でWidget単位の解析・描画タイマーが停止し、
  共有PCMタップは複数可視化で共用できるよう固定容量バッファへの受信を継続する。
  片方の可視化の失敗が他方と音声再生を止めない。
- **テスト**: `spectrum` と `level` の純粋関数の数値検証（既知の正弦波でピークバンド、
  Peak 0dB、RMS -3.01dB、Peak hold の保持と減衰を確認）、リングバッファの単体テスト、
  1回のQAudioBuffer変換からmono／L／Rを派生させるPCM変換テスト、
  表示 ON/OFF でタイマーが止まる Qt テスト、実音でのL／Rレベル取得。

### P6: 設定・永続化・日常利用 UX（PR#10）

設定schema・ダイアログ・可視化ライフサイクル・ウィンドウ状態が絡むため、
**P6-A（設定画面と可視化ON/OFF）／P6-B（ウィンドウ状態など日常利用状態の復元）／
P6-C（UX仕上げと破損・失敗時の統合確認）**の3つへ分割する。

- **目的**: 設定ダイアログと復元の完成度、細部の使い勝手。
- **変更ファイル**: `src/sdp/services/settings.py`（全項目）、
  `src/sdp/ui/settings_dialog.py`（新規）、`src/sdp/ui/main_window.py`、各ウィジェットの状態復元
- **進行状況**: P6-A／P6-B／P6-Cすべて実装・自動テスト済み。P6-Aで設定schemaをversion 2へ拡張して
  波形・スペクトラム・レベルメーターの表示ON/OFFを追加し、設定ダイアログ
  （Apply／OK／Cancel）と、非表示にした可視化の解析停止を実装した。
  P6-Bでウィンドウ位置・サイズ・最大化・Splitter比率・前回フォルダーを、
  設定とは別の`ui-state.json`（当時schema version 1、P6-Cでversion 2へ拡張）へ
  保存・復元するようにした
  （[architecture.md §9](./architecture.md)）。実画面の手動受け入れは未完了
  （[testing-strategy.md §6.12、§6.13](./testing-strategy.md)）。
  P6-Cで設定schemaをversion 3へ上げて音量・ミュート・リピート・シャッフルを追加し、
  ui-state schemaをversion 2へ上げて現在曲（entry_id）を復元するようにした。
  あわせて3保存ファイルの破損・保存失敗時の通知と、終了処理の例外分離を整えた。
  **P6-Cの実画面手動受け入れが未完了**
  （[testing-strategy.md §6.14](./testing-strategy.md)）。
  再生位置・再生中かどうか・プレイリスト選択行は、判断のうえ**保存しない**こととした
  （理由は [architecture.md §9.6](./architecture.md)）。
- **受け入れ条件**: 速度・ピッチ補正・可視化の表示状態が設定画面から変更でき、
  次回起動時に復元される。可視化を非表示にすると、その可視化固有の解析・描画も止まり、
  共有PCMタップは止めない。ウィンドウ位置・サイズ・最大化・Splitter比率・前回フォルダーが
  復元され、モニター構成が変わってもウィンドウが画面外へ消えない。
  デバウンス保存により異常終了後も直近の状態が残る。
  音量・ミュート・リピート・シャッフルが設定画面から変更でき、次回起動時に復元される。
  前回の曲が選び直されるが**自動再生はしない**。3つの保存ファイルの破損・保存失敗は
  互いに独立で、ユーザーへ短く通知され、再生を妨げない。
  キャッシュ上限・再生デバイスの設定UIはP7以降で採否を判断する。
- **テスト**: 設定の往復とschema version 1→2の移行、ダイアログのApply／OK／Cancel契約、
  可視化ON/OFFで解析回数が増えないことのQtテスト、復元シナリオのapp配線テスト。

**P6完了時点を、パッケージング前の機能完成版とする。**
P7-A（起動引数と単一instance）は実装・自動テスト済みで、
Windows上の手動受け入れを残している。P7-B1（PyInstaller onedirビルドとselftest）、
**P7-B2（配布版の実環境検証とZIPリリース生成）は完了**、
**P7-C（Inno Setupのper-user installerとWindows関連付け）は実装・自動検証済み**で、
いずれも実画面・実音の手動受け入れと、外部配布ライセンスの未解決事項を残している。

### P7: Windows 統合と配布（P7-A: 単一instanceと引数、P7-B: パッケージとインストーラー）

- **目的**: WIN-01〜05。
- **P7-A進捗**: 実装・自動テスト済み、実Windowの手動受け入れ未完了。
  `LaunchRequest`、version付き・256KiB上限のlocal IPC、primary／secondary／stale判定、
  composition構築中の受理queue、初回および実行中のplaylist末尾追加、
  引数なしを含む前面化要求、終了時解放を実装した。
- **P7-B1進捗**: PyInstaller 6 onedir spec、`--selftest`、安全なbuild／smoke script、
  配布layout検査、ライセンス原文収集を実装。
- **P7-B2進捗**: `--codec-test`（配布版の実decode検査）、ZIPリリース生成
  （`scripts/build-release.ps1`）、SHA-256とmanifest、ライセンス資料の機械検査を実装。
  配布版で6形式の実decode、ZIP展開後の起動、read-only配置、repository外GUI起動、
  Defenderスキャン（検出0件）、2回buildの再現性を実測した
  （[architecture.md §12](./architecture.md)、
  [testing-strategy.md §6.17](./testing-strategy.md)）。
  **未完了**: 可聴再生・波形／Spectrum／Peak RMSの実音確認、100%／150% DPI、
  SmartScreen表示、Windows Sandbox等のクリーン環境、Python未導入環境。
  **外部配布ブロッカー**: GPL-3.0-only方針は決定済み。対応source archive、
  Mesa llvmpipeの正確な構成、MSVC runtimeの配布形態
  （[distribution-licenses.md](./distribution-licenses.md)）。
- **P7-C進捗**: Inno Setup 6のper-user installer（`packaging/installer.iss`）、
  `scripts/{build-installer,installer-smoke}.ps1`、自作app icon（`assets/sdp.ico`、
  7解像度）、Windows version resource、スタートメニューと任意のデスクトップ
  ショートカット、「プログラムから開く」登録、7拡張子のProgID（`sdp.AudioFile`）、
  installer manifest、Inno Setup compiler不要の契約検査（`sdp/inno_script.py` と
  `sdp/installer_contract.py`）を実装した。
  upgradeのcleanupは固定AppIdの登録済みinstall先に限定し、旧runtimeは削除ではなく
  `.upgrade-backup`へ退避して、展開失敗・中止時に復元する。
  installer smokeで silent install／install済みselftest・codec test／
  same-version reinstall／**既存sdp.exeを含むdirectoryへの初回installで誤削除しないこと**／
  **cleanup後の展開失敗からの旧版復元**／**起動中のupgrade・uninstallの中止**／uninstall／
  **ユーザーデータ保持**／**UserChoice非変更**の136項目を実測した
  （[architecture.md §11、§12.5](./architecture.md)、
  [testing-strategy.md §6.18](./testing-strategy.md)）。
  **未完了**: UAC非表示・Apps & Features表示・関連付け経由のダブルクリック・
  旧version→新versionのupgrade・DPI・Sandbox／新規ユーザー・SmartScreenの手動確認。
  **ライセンスの未解決事項が残るあいだ、installerは技術検証用に留め、公開可能とは扱わない。**
- **残るreleaseブロッカー**: (1) 対応source archive、Mesa llvmpipeの正確な構成、
  MSVC runtimeの配布形態
  （[distribution-licenses.md](./distribution-licenses.md)）、
  (2) P3・P4-B・P6-C・P7-A・P7-B2・P7-Cの手動受け入れ、
  (3) コード署名なしによるSmartScreen警告の扱い。
- **P7-B/C変更ファイル**: `src/sdp/{inno_script,installer_contract,installer_manifest,
  windows_version}.py`、`src/sdp/__main__.py`（`--selftest`／`--codec-test`）、
  `packaging/{sdp.spec,installer.iss,windows-version-info.txt}`、`assets/sdp.ico`、
  `tools/{installer_contract,installer_manifest,gen_app_icon}.py`、
  `scripts/{build-installer,installer-smoke}.ps1`、
  `docs/testing-strategy.md`（手動チェックリスト）
- **受け入れ条件**: exe へ複数パスを渡して再生でき、二重起動でパスが既存プロセスへ転送され
  前面化（不可の場合はタスクバー点滅）する。インストーラー実行後に「プログラムから開く」へ
  出現し、既定アプリ設定への導線が動作し、アンインストールで登録が消える。
  `--selftest` が成功し、日本語パスでも動作する。
- **テスト**: 単一インスタンスの Qt テスト、`--selftest`、手動チェックリスト全項目。

### P8: 品質・性能・リリース整備（PR#13）

- **目的**: NF-03 の計測と改善、ドキュメント整備、v1.0。
- **変更ファイル**: 計測結果に応じた修正、`README.md`、`CHANGELOG.md`（新規）、
  必要なら `core/playlist/persistence.py`（M3U8 入出力）
- **受け入れ条件**: [testing-strategy.md](./testing-strategy.md) の計測シナリオの結果が
  記録され、目標を満たす（未達項目は既知の制限として明記する）。
  インストーラー配布物のスモークテストに合格する。
- **テスト**: 回帰テスト一式 + パッケージ版チェックリスト。

### 4.1 実装順序と依存関係

```
P-1 ─→ P0 ─→ P1 ─→ P2 ─→ P3（最小実用版）
                            ├─→ P4 ─→ P5（初回完成版）
                            └─→ P6 ─────┐
              P1 ────────────→ P7（完成は P6 の後）─→ P8
```

P4 は技術的には P1 完了後に着手できるが、体験の核である MVP を先に完成させる。
また **P4 を P5 より先に置くのは意図的**で、mpv へ切り替えた場合に
P4 の波形解析基盤がそのままスペクトラムの PCM 供給源になるため、切替コストが最小になる。

---

## 5. リスク一覧と代替策

| # | リスク | 可能性 / 影響 | 代替策 |
|---|---|---|---|
| R1 | ピッチ補正の切替が Windows の Qt Multimedia で不可、または音質が実用に耐えない | 中 / 致命 | mpv を本命へ昇格（判断基準は [ADR-0001](./adr/0001-playback-engine.md)）。可視化は位置駆動方式へ |
| R2 | `QAudioBufferOutput` のオーバーヘッドで音が途切れる | 低〜中 / 高 | PCM タップを OFF にできる設計にする。最悪はスペクトラムも位置駆動化する |
| R3 | `QAudioDecoder` が一部形式（M4A / Opus）を扱えない | 中 / 中 | ffmpeg CLI を同梱し WAV パイプでデコードする（`waveform.py` のデコード層のみ差し替え、インターフェースは不変） |
| R4 | PyInstaller 成果物の肥大化・アンチウイルス誤検知 | 中 / 低 | onedir + 不要 Qt モジュール除外。誤検知は個人利用のため除外設定で許容 |
| R5 | 長時間音源で解析時のメモリが膨張する | 低 / 中 | フル PCM を保持せず逐次 envelope 縮約する（設計済み） |
| R6 | 自前 QPainter 描画の性能不足 | 低 / 低 | QPixmap タイルによる描画キャッシュ。それでも不足なら PyQtGraph を再検討 |
| R7 | Windows のフォアグラウンド制約で前面化が失敗する | 中 / 低 | `QApplication.alert` によるタスクバー点滅へフォールバック（発動条件とログを明記する） |
| R8 | mpv 昇格時に PCM が取得できず可視化仕様が縮小する | 中 / 中 | 位置駆動方式（波形はそのまま、スペクトラムは低レート PCM キャッシュから）を U8 で先行評価 |

---

## 6. 実装開始前に解決すべき事項

### Blocker（P1 の本実装着手前に解決必須）

1. **P0 の実施と再生エンジンの確定**（U1〜U7）。特にピッチ補正の両モードの品質判定。
   mpv へ昇格する場合は ADR の差し替えと U8 の評価まで完了させる。
2. **開発機への ffmpeg CLI 導入**（テスト音源生成に必要。`winget install ffmpeg` を想定）。

### Non-blocker（並行または後日でよい）

- コード署名（SmartScreen 対策）— 個人利用のため見送り可。
- M3U8 入出力の詳細仕様（文字コード、相対パスの扱い）— P8 以降。
- 追加ビジュアライザー（VIS-02 以降）の採否 — P5 終了時に判断。
- アプリアイコンのデザイン — P7 までに用意すれば足りる。
- 前面化に関する Windows のフォアグラウンド制約の実挙動 — P7 で確認し、
  点滅フォールバックで吸収する。
