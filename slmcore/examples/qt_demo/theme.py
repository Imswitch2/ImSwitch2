"""Application-level styling for the standalone Qt demo.

The reusable :mod:`slmcore.qt` widgets deliberately do not own an application
style. The demo uses the same QDarkStyle base as ImSwitch so the reference host
presents the widgets in a familiar dark Qt theme while ``slmcore.qt`` remains
fully host-themeable.
"""

from __future__ import annotations

import qdarkstyle
from qtpy import API_NAME,QtWidgets


# Keep demo-specific styling intentionally minimal. Generic widget appearance
# (buttons, check boxes, editors, tabs, menus, disabled states, etc.) should come
# from QDarkStyle rather than being reimplemented here.
_DEMO_STYLESHEET = """
QScrollArea {
    border: none;
}
"""


def apply_demo_theme(app: QtWidgets.QApplication) -> None:
    """Apply the standalone demo's ImSwitch-like dark application theme."""
    qt_api = str(API_NAME).lower()
    stylesheet = qdarkstyle.load_stylesheet(qt_api=qt_api)
    app.setStyleSheet(stylesheet + "\n" + _DEMO_STYLESHEET)
