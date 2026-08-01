"""法的告知ダイアログの表示契約を検証する。"""

from pathlib import Path

import pytest
from PySide6.QtWidgets import QDialogButtonBox, QPlainTextEdit
from pytestqt.qtbot import QtBot

from sdp.ui.legal_dialog import LegalDocumentDialog, load_legal_document


def test_dialog_is_read_only_and_closable(qtbot: QtBot) -> None:
    """法的文書は編集できず、明示した閉じるボタンで閉じられる。"""
    dialog = LegalDocumentDialog("ライセンス", "本文")
    qtbot.addWidget(dialog)
    dialog.show()

    viewer = dialog.findChild(QPlainTextEdit, "legalDocumentText")
    buttons = dialog.findChild(QDialogButtonBox, "legalDocumentButtons")
    assert viewer is not None
    assert buttons is not None
    assert viewer.isReadOnly()
    assert viewer.toPlainText() == "本文"
    close_button = buttons.button(QDialogButtonBox.StandardButton.Close)
    assert close_button is not None
    close_button.click()
    assert not dialog.isVisible()


@pytest.mark.parametrize("name", ["LICENSE", "THIRD_PARTY_NOTICES.txt", "CORRESPONDING_SOURCE.md"])
def test_packaged_legal_documents_are_readable(name: str) -> None:
    """GUIから表示する3文書が開発実行時にもUTF-8で読める。"""
    assert load_legal_document(name).strip()


def test_unknown_document_is_rejected() -> None:
    """任意pathを法的文書viewerから読ませない。"""
    with pytest.raises(ValueError, match="許可していない"):
        load_legal_document(str(Path("..") / "secret.txt"))
