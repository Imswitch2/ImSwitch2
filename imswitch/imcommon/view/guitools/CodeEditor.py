"""A Python code editor shared by ImScripting and ImProcess.

``PythonCodeEditor`` is QScintilla with the Python lexer (line numbers,
indentation guides, auto-indent) when QScintilla is installed, and a plain
monospaced text box otherwise. Both expose ``text()``, ``setText()``,
``selectedText()`` and ``textChanged``, which is all a caller needs.
"""

from qtpy import QtGui, QtWidgets

try:
    # ``import a.b as c`` rather than ``from a import b``: only the former
    # honours ``sys.modules["PyQt5.Qsci"] = None`` once the module has been
    # imported elsewhere, which is how a missing QScintilla is simulated.
    import PyQt5.Qsci as Qsci
except ImportError:
    Qsci = None

#: Whether the editor below is the QScintilla one.
QSCI_AVAILABLE = Qsci is not None


def _fixed_font(point_size: int = 11) -> QtGui.QFont:
    font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    font.setPointSize(point_size)
    return font


if QSCI_AVAILABLE:

    class PythonCodeEditor(Qsci.QsciScintilla):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

            self.setMargins(1)
            self.setMarginWidth(0, '00000000')
            self.setMarginType(0, Qsci.QsciScintilla.NumberMargin)

            self.setTabWidth(4)
            self.setIndentationGuides(True)
            self.setAutoIndent(True)

            self.setScrollWidth(1)
            self.setScrollWidthTracking(True)

            font = _fixed_font()

            lexer = Qsci.QsciLexerPython()
            lexer.setFont(font)
            lexer.setDefaultFont(font)
            self.setLexer(lexer)

else:

    class PythonCodeEditor(QtWidgets.QPlainTextEdit):
        """The fallback: a monospaced plain text box with four-space tabs."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.setFont(_fixed_font())
            self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
            self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)

        def text(self) -> str:
            return self.toPlainText()

        def setText(self, text: str) -> None:
            self.setPlainText(text)

        def selectedText(self) -> str:
            # Qt separates the paragraphs of a selection with U+2029.
            return self.textCursor().selectedText().replace(" ", "\n")


__all__ = ["PythonCodeEditor", "QSCI_AVAILABLE"]
