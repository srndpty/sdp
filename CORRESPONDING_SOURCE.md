# 対応sourceの提供方針

sdpのWindows binaryはGPL-3.0-onlyで配布する。公開するbinaryと同じGitHub Releaseに、
少なくとも次のsource archive、build情報、hashを配置する。

- sdp: binaryと同じtagのrepository source、`uv.lock`、`packaging/sdp.spec`、`scripts/`
- PySide6 / Shiboken: 6.10.3
- Qt: 6.10.3のうち配布するQt module（少なくともqtbaseとqtmultimedia）
- Mutagen: 1.48.1
- FFmpeg: n7.1.3、全third-party attribution、および
  `packaging/licenses-manifest.json`に記録したconfigure内容
- CPython: 3.13.11、およびpython-build-standalone BUILD 20260127のbuild source

GitHub Releaseへbinaryだけを置いてはならない。上記archiveが同じreleaseから取得でき、
hashとversionが配布manifestに一致することを確認してから外部公開する。

現時点では対応source archiveをreleaseへ配置していないため、生成済みZIPとinstallerは
技術検証用であり、外部公開しない。
