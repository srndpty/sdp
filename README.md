# sdp (sound player)

Windows 11 向けの個人用ローカル音声プレイヤー。
ローカルの音声ファイルを再生し、DJ ソフトのような追従波形と
foobar2000 のようなリアルタイムスペクトラムを表示することを目標にしている。

## 開発状況

**開発初期段階（P-1: 開発基盤の初期化）。再生機能はまだ実装されていない。**

現時点のリポジトリに含まれるのは、開発環境・品質チェック基盤・設計文書のみ。
`uv run python -m sdp` を実行しても、開発中である旨を表示して終了する。

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
