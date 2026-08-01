# 配布物のライセンス状況

Windows onedir配布物、ZIP release、installerへ実際に含めるcomponentと、
外部公開までの条件を記録する。この文書は法務判断ではなく、確認済みの事実と
release gateを管理するための資料である。

## 採用方針

- sdp本体は **GPL-3.0-only** とする。
- PySide6 / Shiboken / Qtはopen sourceのGPL-3.0-onlyを選択する。
- MutagenのGPL-2.0-or-laterはversion 3を選択し、配布物全体をGPL-3.0-onlyとして扱う。
- GPL-onlyかどうかにかかわらず、sdpが使用しないruntimeは配布しない。
- 対応sourceを用意できないbinaryは外部公開しない。

## 解決した事項

- **sdp本体**: `LICENSE`と`pyproject.toml`をGPL-3.0-onlyへ変更し、配布物のrootと
  `_internal/`へGPLv3原文を同梱する。旧MIT版の表示は`LICENSES/MIT.txt`へ保存する。
- **Mutagen 1.48.1**: upstreamのGPL-2.0-or-laterからversion 3を選択したため、sdpとの
  license互換性に関する判断は解決した。upstreamのCOPYINGとGPLv3原文を同梱する。
- **Qt Virtual Keyboard**: Qt 6.10のopen source提供ではGPL-3.0-onlyだが、sdpは使用しない。
  `Qt6VirtualKeyboard.dll`と`qtvirtualkeyboardplugin.dll`をPyInstaller収集後に除外し、
  package layout検査で再混入を失敗させる。
- **OpenSSL**: sdpはTLSを使わない。`qopensslbackend.dll`、`libssl-3-x64.dll`、
  `libcrypto-3-x64.dll`を除外し、再混入をpackage layout検査で失敗させる。
- **Mesa llvmpipe**: software OpenGL fallbackを必須要件としないため、由来を確定できない
  `opengl32sw.dll`を除外する。package layout検査で再混入を失敗させる。
- **MSVC／Universal CRT**: `VCRUNTIME140*.dll`、`MSVCP140*.dll`、`concrt140*.dll`、
  `ucrtbase.dll`、`api-ms-win-*.dll`を除外する。Windows 11標準のUniversal CRTと、利用者が
  別途導入するMicrosoft Visual C++ v14 Redistributable x64を前提条件にする。NumPyのpydは
  ハッシュ付きMSVCP import名を標準の`MSVCP140.dll`へ戻し、処理前wheel、変換実装、lockfile、
  生成物hashを対応source資料へ含める。
  installerはHKLMを読み取って導入済みか確認し、不足時は公式ページを案内して中止する。
  Redistributable自体は同梱・自動実行せず、per-user／UACなしの契約を維持する。
- **libffi 3.4.6**: uvが使用するpython-build-standaloneの`BUILD=20260127`に対応する
  build sourceでversionと原文を確認し、原文を同梱する。
- **原文**: GPL-3.0、LGPL-3.0、LGPL-2.1、Apache-2.0、libffiの原文を
  `packaging/license-texts/`でsource管理し、wheelの内容に依存せず同梱する。
- **CPython / NumPy / PyInstaller bootloader**: 各配布元の原文を同梱する。
  PyInstaller bootloaderはbootloader exception付きである。

## 残っている外部公開blocker

### 1. 対応source archive

GPL/LGPL componentの対応sourceを、binaryと同じGitHub Releaseから取得可能にする必要がある。
対象versionと必要物は`CORRESPONDING_SOURCE.md`に固定した。外部公開可能とする判断基準は次の通り。

- sdp、PySide6 / Shiboken 6.10.3、Qt 6.10.3、Mutagen 1.48.1、FFmpeg n7.1.3の
  source archiveがreleaseにある
- FFmpeg archiveにLGPLだけでなくBSD／ISC／MIT／MPL-2.0等の全third-party attributionがある
- sdpのbuild script、lockfile、PyInstaller spec、FFmpeg configure内容を取得できる
- source archiveのSHA-256とcomponent versionをrelease manifestで検査できる
- PySide6／Shiboken wheel、Qt DLL、source tag、SBOM／build configuration、patchの対応根拠を
  `build-info.json`で照合できる
- binaryだけのreleaseをCIが拒否する

単にupstream URLを記載しただけでは完了扱いにしない。

## 現在の公開可否

**まだ外部公開可能とは判断しない。** GPL-3.0-onlyの選択、主要原文の同梱、未使用の
Qt Virtual Keyboard／OpenSSL／Mesa／MSVC runtime除外、libffiの特定までは完了したが、
対応source archiveとbinaryとの対応根拠が残る。
ZIPとinstallerは引き続き技術検証用とする。

## 機械検査

```powershell
uv run python tools/license_audit.py dist/sdp --fail-on-unclassified
uv run python tools/license_audit.py dist/sdp --fail-on-unclassified --fail-on-unresolved
```

通常buildは宣言した原文の欠落と未分類runtimeで失敗する。後者の`--fail-on-unresolved`は
外部公開gateで使い、残課題が0件になるまで失敗する。
