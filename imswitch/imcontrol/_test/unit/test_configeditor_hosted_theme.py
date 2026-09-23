"""The config editor keeps its dark look when ImSwitch opens it.

ImSwitch darkens itself with an application style sheet (qdarkstyle) and
leaves the application palette light. The editor read only the palette, so
inside ImSwitch it drew its device cards in the light theme: white cards with
ImSwitch's light text on them. Standalone it themes the whole application and
never saw the difference.
"""

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("qdarkstyle")

from PyQt5.QtWidgets import QApplication, QMainWindow

from imswitch.imcontrol.view.configeditor import editor

DARK_CARD = editor.get_themed_colors(True)["card_bg"]
LIGHT_CARD = editor.get_themed_colors(False)["card_bg"]


class _Window(QMainWindow):
    """The editor window's theming step, without the editor it would theme."""

    _adopt_dark_theme = editor.MainWindow._adopt_dark_theme


@pytest.fixture
def app_style_sheet(qapp):
    """Set an application style sheet for one test, then put the old one back."""
    previous = qapp.styleSheet()
    yield qapp.setStyleSheet
    qapp.setStyleSheet(previous)


@pytest.fixture
def light_palette(qapp):
    if qapp.palette().window().color().lightness() < 128:
        pytest.skip("this test run's application palette is already dark")


@pytest.fixture
def inside_imswitch(app_style_sheet, light_palette, monkeypatch):
    """The application as ImSwitch's prepareApp() leaves it."""
    monkeypatch.setenv("PYQTGRAPH_QT_LIB", "PyQt5")
    from imswitch.imcommon.view.guitools.stylesheet import getBaseStyleSheet
    app_style_sheet(getBaseStyleSheet())


def test_a_style_sheet_dark_host_counts_as_dark(inside_imswitch):
    assert QApplication.instance().palette().window().color().lightness() >= 128
    assert editor.is_dark_mode() is True


def test_a_light_host_stays_light(app_style_sheet, light_palette):
    app_style_sheet("")
    assert editor.is_dark_mode() is False


def test_device_cards_are_dark_inside_imswitch(inside_imswitch):
    card = editor.DeviceCard("lasers", "592(Exc)", {"managerName": "MPBLaserManager"})
    assert DARK_CARD in card.styleSheet()
    assert LIGHT_CARD not in card.styleSheet()


def test_the_window_takes_the_editors_own_theme_inside_imswitch(inside_imswitch):
    window = _Window()
    window._adopt_dark_theme()
    assert window.styleSheet().endswith(editor._DARK_STYLESHEET)
    assert window.styleSheet().startswith(editor._HOSTED_OVERRIDES)
    assert window.palette().window().color().name().upper() == "#2B2B2B"


def test_the_window_leaves_theming_to_the_standalone_application(app_style_sheet):
    app_style_sheet(editor._DARK_STYLESHEET)
    window = _Window()
    window._adopt_dark_theme()
    assert window.styleSheet() == ""


def test_the_window_does_not_darken_a_light_host(app_style_sheet, light_palette):
    app_style_sheet("")
    window = _Window()
    window._adopt_dark_theme()
    assert window.styleSheet() == ""
