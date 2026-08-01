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
- **libffi 3.4.6**: uvが使用するpython-build-standaloneの`BUILD=20260127`に対応する
  build sourceでversionと原文を確認し、原文を同梱する。
- **Mesaの既知の表示**: Qt公式third-party attributionにあるBrian Paul、Khronos Group、
  yohhoyのcopyrightとMIT／Boost Software License 1.0本文を同梱する。binaryの厳密な
  構成特定は未完了のため、Mesa自体はblockerのままとする。
- **原文**: GPL-3.0、LGPL-3.0、LGPL-2.1、Apache-2.0、libffiの原文を
  `packaging/license-texts/`でsource管理し、wheelの内容に依存せず同梱する。
- **CPython / NumPy / PyInstaller bootloader**: 各配布元の原文を同梱する。
  PyInstaller bootloaderはbootloader exception付きである。

## 残っている外部公開blocker

### 1. 対応source archive

GPL/LGPL componentの対応sourceを、binaryと同じGitHub Releaseから取得可能にする必要がある。
対象versionと必要物は`CORRESPONDING_SOURCE.md`に固定した。外部公開可能とする判断基準は次の通り。

- sdp、PySide6 / Shiboken 6.10.3、Qt 6.10.3、Mutagen 1.48.1、FFmpeg n7.1.3、
  CPython 3.13.11 / python-build-standalone BUILD 20260127のsource archiveがreleaseにある
- FFmpeg archiveにLGPLだけでなくBSD／ISC／MIT／MPL-2.0等の全third-party attributionがある
- sdpのbuild script、lockfile、PyInstaller spec、FFmpeg configure内容を取得できる
- source archiveのSHA-256とcomponent versionをrelease manifestで検査できる
- binaryだけのreleaseをCIが拒否する

単にupstream URLを記載しただけでは完了扱いにしない。

### 2. Mesa llvmpipe

PySide6 wheelの`opengl32sw.dll`はQtのsoftware OpenGL fallbackである。Qt公式のattributionでは
Mesa / LLVM由来のMIT・Boost系条件が示され、既知のcopyright・license本文は同梱したが、
同梱binaryの正確なversion・patch・全componentをwheelだけから確定できていない。

判断基準は、PySide6 6.10.3 binaryに対応するQt SBOMまたはbuild記録からversion・componentを
特定し、そのすべてのcopyright・license原文を同梱すること。特定できない場合は
`opengl32sw.dll`を除外し、software OpenGL fallbackなしで対応環境を明記して実機検証する。

### 3. Microsoft Visual C++ Runtime

現在のonedirには`VCRUNTIME140*.dll`等が含まれる。GPLのSystem Library例外とMicrosoftの
再頒布条件を同時に満たす配布形態を確定していない。

判断基準は、次のいずれかを専門家確認を含めて確定すること。

- runtime DLLをsdp配布物から除外し、Microsoft Visual C++ Redistributableを前提条件として
  clean Windows環境でinstall・起動・再生を確認する
- runtimeを同梱できる明確な根拠と適用条件を文書化する

## 現在の公開可否

**まだ外部公開可能とは判断しない。** GPL-3.0-onlyの選択、主要原文の同梱、未使用の
Qt Virtual Keyboard / OpenSSL除外、libffiの特定までは完了したが、上記3 blockerが残る。
ZIPとinstallerは引き続き技術検証用とする。

## 機械検査

```powershell
uv run python tools/license_audit.py dist/sdp --fail-on-unclassified
uv run python tools/license_audit.py dist/sdp --fail-on-unclassified --fail-on-unresolved
```

通常buildは宣言した原文の欠落と未分類runtimeで失敗する。後者の`--fail-on-unresolved`は
外部公開gateで使い、残課題が0件になるまで失敗する。
