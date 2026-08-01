# 対応sourceの提供方針

sdpのWindows binaryはGPL-3.0-onlyで配布する。公開するbinaryと同じGitHub Releaseに、
少なくとも次のsource archive、build情報、hashを配置する。

- sdp: binaryと同じtagのrepository source、`uv.lock`、`packaging/sdp.spec`、`scripts/`
- PySide6 / Shiboken: 6.10.3
- Qt: 6.10.3のうち配布するQt module（少なくともqtbaseとqtmultimedia）
- Mutagen: 1.48.1
- FFmpeg: n7.1.3、全third-party attribution、および
  `packaging/licenses-manifest.json`に記録したconfigure内容

CPython、NumPy、libffi等のpermissive componentは、GPL/LGPLの対応source archiveとは
分けて扱う。配布manifestにversion、upstream、binary hash、ライセンス／copyright noticeを
記録する。sourceも同じreleaseへ置く場合は法的最低限より厳しい自主ルールとして明記する。

NumPyのWindows wheelは、delvewheelにより一部pydの`MSVCP140.dll` importが
ハッシュ付き私有名へ変更されている。sdpのpackage buildはRuntime DLLを同梱しないため、
`src/sdp/package_runtime.py`でそのimport名を標準の`MSVCP140.dll`へ戻す。同じ長さの
PE文字列領域をNUL paddingしており、処理前wheel、変換実装、lockfile、生成物hashを
対応source一式へ含める。実行環境ではMicrosoft Visual C++ v14 Redistributable x64を使う。

PyPI wheelを単に同versionのupstream tagへ対応すると推定しない。build-infoには少なくとも
PySide6／Shiboken wheel filenameとSHA-256、Qt DLLのfile versionとSHA-256、source tag、
build configurationまたはSBOM、適用patchの有無と対応根拠を含める。

GitHub Releaseへbinaryだけを置いてはならない。上記archiveが同じreleaseから取得でき、
hashとversionが配布manifestに一致することを確認してから外部公開する。

現時点では対応source archiveをreleaseへ配置していないため、生成済みZIPとinstallerは
技術検証用であり、外部公開しない。
