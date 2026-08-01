"""GPL告知と配布文書を表示する読み取り専用ダイアログ。"""

from pathlib import Path

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPlainTextEdit, QVBoxLayout, QWidget

from sdp import __version__
from sdp.resources import resource_path

ABOUT_NOTICE = f"""sdp {__version__}
Copyright (C) 2026 sdp contributors

このプログラムはフリーソフトウェアです。
GNU General Public License version 3の条件で再配布・変更できます。

このプログラムには、法律で認められる範囲で一切の保証がありません。

ライセンス本文: LICENSE
第三者ライセンス: THIRD_PARTY_NOTICES.txt
対応ソース: CORRESPONDING_SOURCE.md
"""

LEGAL_DOCUMENTS = frozenset({"LICENSE", "THIRD_PARTY_NOTICES.txt", "CORRESPONDING_SOURCE.md"})


def load_legal_document(name: str) -> str:
    """許可した配布文書をUTF-8で読み込む。"""
    if name not in LEGAL_DOCUMENTS:
        raise ValueError(f"表示を許可していない法的文書です: {name}")
    return resource_path(Path(name)).read_text(encoding="utf-8")


class LegalDocumentDialog(QDialog):
    """法的告知を選択・編集なしで表示する。"""

    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("legalDocumentDialog")
        self.setWindowTitle(title)
        self.setAccessibleName(title)
        self.setModal(False)
        self.resize(720, 560)

        viewer = QPlainTextEdit(self)
        viewer.setObjectName("legalDocumentText")
        viewer.setAccessibleName(f"{title}の本文")
        viewer.setReadOnly(True)
        viewer.setPlainText(text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.setObjectName("legalDocumentButtons")
        close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
        close_button.setText("閉じる")
        close_button.setObjectName("legalDocumentCloseButton")
        close_button.setAccessibleName("閉じる")
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(viewer)
        layout.addWidget(buttons)

    @property
    def text(self) -> str:
        """表示中の文書本文を返す。"""
        viewer = self.findChild(QPlainTextEdit, "legalDocumentText")
        return "" if viewer is None else viewer.toPlainText()
