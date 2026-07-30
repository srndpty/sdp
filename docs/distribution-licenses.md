# 配布物のライセンス状況（P7-B2 時点）

Windows onedir 配布物（`dist/sdp` と ZIP release）へ**実際に含まれている**
コンポーネントと、外部配布に必要な資料の状態をまとめる。

**この文書は法務判断ではない。** 確認できた事実と、まだ決めていないことを分けて記録し、
未解決が残っているあいだは「外部配布可能」とは扱わない。機械的な検査は
[`packaging/licenses-manifest.json`](../packaging/licenses-manifest.json) と
`uv run python tools/license_audit.py dist/sdp` が行う。

## 調査に使った事実

| 対象 | 確認方法 | 結果 |
|---|---|---|
| PySide6 / shiboken6 のライセンス表明 | wheel metadata（`importlib.metadata`） | `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` |
| PySide6 wheel が同梱する原文 | `*.dist-info/licenses/` の列挙 | `LicenseRef-Qt-Commercial.txt` **のみ** |
| FFmpeg の版と build 構成 | 同梱 DLL 内の configuration 文字列を実測 | n7.1.3 / `--enable-gpl`・`--enable-nonfree`・`--enable-version3` **なし** |
| Mutagen のライセンス | 同梱 `COPYING` と `mutagen/__init__.py` の冒頭 | GPL v2 **or later** |
| PyInstaller bootloader | 同梱 `COPYING.txt` と wheel metadata | GPL-2.0-or-later **＋ bootloader 例外** |
| 同梱 DLL の一覧 | `dist/sdp/_internal` の実列挙 | Qt 6、FFmpeg、OpenSSL 3、MSVC ランタイム、Python |

## 分類

### 解決済み

- **sdp 本体**: MIT。`LICENSE` を配布物ルートと `_internal/` の両方へ同梱。
- **CPython**: PSF-2.0。`_internal/licenses/Python/LICENSE.txt`。
- **NumPy**: BSD-3-Clause（同梱コンポーネントの原文込み）。`_internal/licenses/numpy/`。
- **PyInstaller bootloader**: GPL-2.0-or-later ＋ bootloader 例外。例外により、生成した
  実行ファイルの配布条件は制約されない。`_internal/licenses/pyinstaller/COPYING.txt`。

### 文書追加で解決可能

- **PySide6 / Qt（LGPL で配布する場合）**: wheel が同梱するのは商用ライセンス参照文だけで、
  **LGPL-3.0 の原文が配布物に無い**。LGPL-3.0 と（LGPLv3 が参照する）GPL-3.0 の原文、
  Qt の著作権表示、ライブラリのソース入手方法の提示が必要。
- **FFmpeg**: LGPL-2.1-or-later 相当の構成であることは実測できたが、**LGPL 原文と
  ソース入手方法の提示が無い**。
- **OpenSSL 3**: Apache-2.0 の原文が無い。sdp は TLS を使わないため、
  `libssl` / `libcrypto` と関連 plugin を配布物から除外する選択肢もある。

これらの原文は本作業のオフライン環境では取得できなかったため、**追加していない**。
「同梱済み」と誤認しないよう、`licenses-manifest.json` では `shipped_texts` を空にしている。

### 配布形態の判断が必要

- **Qt の配布形態**: LGPL-3.0 で配布するのか、商用ライセンスを取得するのかを先に決める。
  LGPL を選ぶ場合、onedir 配布は Qt DLL を差し替え可能な形（動的リンク）で同梱しているため
  再リンク要件には適合しやすいが、原文・著作権表示・ソース提供の手当てが要る。
- **MSVC ランタイム**: Visual Studio の再頒布条件に従う。GPL 系と組み合わせる場合は
  system library 例外の該当性も確認が要る。

### 外部専門家確認推奨

- **Mutagen（GPL-2.0-or-later）**: 「or later」であるため GPL-3.0 として扱えば、
  MIT・LGPL-3.0・PSF・BSD とは互換になる。ただしその場合、**配布物全体を GPL-3.0 の
  条件で頒布する**ことになり、ソース提供義務の範囲を判断する必要がある。
  回避したい場合は、メタデータ読み取りを GPL でない実装へ置き換える選択肢がある。
- **Qt Virtual Keyboard**: sdp は使用していないが `PySide6-Addons` 経由で同梱される。
  Qt のオープンソース提供では GPL-3.0-only の可能性があり、ライセンス面と配布サイズの
  両方の理由から、除外できるかを検討する。

## 現時点の結論

**外部公開可能とは判断できない。** 少なくとも次が未了である。

1. Qt / PySide6 のライセンス形態の決定と、LGPL-3.0・GPL-3.0 原文の同梱
2. FFmpeg の LGPL 原文とソース入手方法の提示
3. OpenSSL の Apache-2.0 原文の同梱、または OpenSSL 自体の除外
4. Mutagen の GPL 波及範囲の判断

P7-C（インストーラーと関連付け）へ進むこと自体は技術的に可能だが、上記が解決するまでは
**インストーラーも技術検証用**と位置づけ、公開配布物として扱わない。

## 検査の実行

```powershell
uv run python tools/license_audit.py dist/sdp
```

- 宣言した原文が配布物に無い場合は **exit 1**（機械的な不備）。
- 未解決事項が残る場合は一覧を表示し、「外部配布可能とは判断できない」旨を明示する。
- `scripts/build-release.ps1` はこの検査を必ず通す。
