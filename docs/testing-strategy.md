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
| `analysis/spectrum.py` | 既知の正弦波を入力してピークバンドの位置を確認。窓関数・dB 変換・平滑化の数値検証 |
| `analysis/ring_buffer.py` | 折り返し、スナップショット、満杯時の上書き |
| `analysis/waveform.py` の縮約 | 合成 PCM から生成した envelope の min/max の正当性 |
| `analysis/waveform_cache.py` | キー照合（更新日時・サイズ・解析バージョンの差異で無効化）、破損 npz、LRU 削除 |
| `playlist` のロジック | 次曲決定（順次 / 1 曲リピート / 全曲リピート / シャッフルの網羅）、欠損スキップ、重複 entry_id |
| `services/settings.py` | 往復、欠落キーの既定値補完、未知キーの無視、アトミック書き込み |
| `playlist/persistence.py` | `playlist.json` の往復（将来は M3U8 も） |

## 3. Qt 統合テスト（pytest-qt、`QT_QPA_PLATFORM=offscreen`）

| 対象 | 検証内容 |
|---|---|
| `PlaylistModel` | `QAbstractItemModelTester`、URL ドロップの MIME 処理、`moveRows` |
| `PlaybackController` | **FakeBackend**（`IPlaybackBackend` のテストダブル）を使い、状態遷移・曲終了時の次曲送り・エラー時の方針を `qtbot.waitSignal` で検証 |
| `QtMultimediaBackend` | Qt enum 写像の完全性（値が増えたら失敗する）、エラー変換、故障注入による変換失敗・再入ガード、状態通知の重複抑制、音を鳴らさない load・source差し替え・再ロード。所有する QMediaPlayer / QAudioOutput は `findChildren` で取得し、テストのために公開 API を増やさない |
| `SingleInstanceService` | 同一プロセス内でサーバーとクライアントを往復させる |
| `MetadataReader` | 実ファイルに対する非同期完了シグナル |
| 可視化ウィジェット | 表示 ON/OFF でタイマーと PCM タップが停止すること（SPEC-04） |

## 4. 実音再生テスト（`audio` マーカー。ローカル手動実行のみ）

置き場所は `tests/audio/`。再生前に必ず音量を 0.0 にして可聴音を出さない。
待機は `qtbot.waitUntil` などで行い、必ずタイムアウトを明示して無期限待機を作らない。

- ロード → 再生 → `mediaStatusChanged`（`END_OF_MEDIA`）の確認
- 一時停止・再開、シーク、停止
- 再生速度変更、ピッチ補正の切り替え

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
