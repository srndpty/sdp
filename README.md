# sdp (sound player)

Windows 11 向けの個人用ローカル音声プレイヤー。
ローカルの音声ファイルを再生し、DJ ソフトのような追従波形と
foobar2000 のようなリアルタイムスペクトラムを表示することを目標にしている。

## 開発状況

**P1（単曲再生基盤）と P2-C1（プレイリストからの逐次再生）まで完了。**

`uv run python -m sdp` でウィンドウが起動し、次の操作ができる。

単曲再生:

- 「ファイル」→「開く...」で音声ファイルを 1 つ選ぶ
- 再生 / 一時停止 / 停止
- シークバーによる再生位置の変更、現在位置と総時間の表示
- 音量変更とミュート
- 再生状態（再生中 / 一時停止 / 停止）の表示
- 読み込み・再生エラーのステータスバー表示（技術詳細はログファイルへ）

プレイリスト:

- 一覧表示（ファイル名とパス、件数表示）
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

最後の曲が終わっても先頭へは戻らない（繰り返し再生は未実装のため）。
再生中の曲はアプリを閉じると記憶されない。

**未実装**: リピート（全曲 / 1 曲）、シャッフル、
タイトルやアーティストなどのメタデータ表示、再生速度とピッチ補正の操作 UI、
追従波形、スペクトラム、レベルメーター、設定の永続化、
コマンドライン引数でのファイル指定、Windows のファイル関連付け、インストーラー。

保存場所（いずれも `%LOCALAPPDATA%\sdp` 配下）:

- ログ: `logs\sdp.log`（1MB × 5 世代）
- プレイリスト: `playlist.json`

プレイリストファイルが壊れていた場合は、空のプレイリストで起動して
ステータスバーへ通知し、**その起動では上書き保存しない**（元のファイルを残す）。

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

`assets/test_audio/sine440.wav` などのテスト音源で動作を確認できる。
**最初は音量を下げるかミュートにしてから再生すること。**

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

MIT
