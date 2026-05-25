from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
MAIN_VIEW_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'ImConMainView.py'
SCAN_BASE_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'ScanWidgetBase.py'
SCAN_ADVANCED_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'ScanWidgetAdvanced.py'
SCAN_ADVANCED_CONTROLLER_PATH = (
    ROOT / 'imswitch' / 'imcontrol' / 'controller' / 'controllers' / 'ScanControllerAdvanced.py'
)
LASER_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'LaserWidget.py'
RECORDING_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'RecordingWidget.py'
POSITIONER_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'PositionerWidget.py'
BEAD_REC_WIDGET_PATH = ROOT / 'imswitch' / 'imcontrol' / 'view' / 'widgets' / 'BeadRecWidget.py'


def test_main_view_keeps_direct_dock_widget_insertion():
    source = MAIN_VIEW_PATH.read_text()

    assert 'def _addScrollableWidgetToDock(self, dock, widget):' not in source
    assert 'class _DockScrollArea(QtWidgets.QScrollArea):' not in source
    assert 'self._addScrollableWidgetToDock(self.docks[widgetKey], self.widgets[widgetKey])' not in source
    assert 'self.docks[widgetKey].addWidget(self.widgets[widgetKey])' in source
    assert "self.docks['Image'].addWidget(self.widgets['Image'])" in source


def test_scan_widget_base_does_not_force_minimum_width_or_hide_horizontal_scrollbar():
    source = SCAN_BASE_PATH.read_text()

    assert 'self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.scrollArea.setMinimumWidth(width)' not in source
    assert 'self.setMinimumWidth(width)' not in source
    assert 'QtCore.Qt.ScrollBarAlwaysOff' not in source


def test_laser_widget_does_not_force_minimum_width_or_hide_horizontal_scrollbar():
    source = LASER_WIDGET_PATH.read_text()

    assert 'self.scrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.scrollArea.setMinimumWidth(width)' not in source
    assert 'self.setMinimumWidth(width)' not in source
    assert 'QtCore.Qt.ScrollBarAlwaysOff' not in source


def test_recording_widget_uses_internal_scroll_area():
    source = RECORDING_WIDGET_PATH.read_text()

    assert 'self.scrollArea = QtWidgets.QScrollArea()' in source
    assert 'self.scrollArea.setWidget(self.recGridContainer)' in source
    assert 'self.scrollArea.setWidgetResizable(True)' in source
    assert 'layout.addWidget(self.scrollArea)' in source
    assert 'self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.setMinimumSize(0, 0)' in source


def test_positioner_widget_uses_internal_scroll_area():
    source = POSITIONER_WIDGET_PATH.read_text()

    assert 'self.gridContainer = QtWidgets.QWidget()' in source
    assert 'self.gridContainer.setLayout(self.grid)' in source
    assert 'self.scrollArea = QtWidgets.QScrollArea()' in source
    assert 'self.scrollArea.setWidget(self.gridContainer)' in source
    assert 'self.scrollArea.setWidgetResizable(True)' in source
    assert 'self.scrollArea.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.setMinimumSize(0, 0)' in source


def test_bead_rec_widget_has_responsive_list_and_status_controls():
    source = BEAD_REC_WIDGET_PATH.read_text()

    assert 'listPanel.setMaximumWidth(100)' not in source
    assert 'listPanel.setMinimumWidth(140)' in source
    assert 'self.cwidget.setMinimumSize(0, 0)' in source
    assert 'self.imageListWidget.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)' in source
    assert 'self.statusLabel = QtWidgets.QLabel("Idle")' in source
    assert 'self.progressBar = QtWidgets.QProgressBar()' in source
    assert 'def setStatusText(self, text):' in source
    assert 'def updateProgress(self, current, total):' in source
    assert 'return name' in source
    assert 'self.setMinimumSize(0, 0)' in source


def test_advanced_scan_centers_allow_negative_positions():
    source = SCAN_ADVANCED_PATH.read_text()

    assert 'centerPar.setRange(-1e9, 1e9)' in source


def test_advanced_scan_has_no_partial_bead_rec_controls():
    widget_source = SCAN_ADVANCED_PATH.read_text()
    controller_source = SCAN_ADVANCED_CONTROLLER_PATH.read_text()

    assert 'sigUpdateBeadRecCenter' not in widget_source
    assert 'sigShowBeadRecCenterCross' not in widget_source
    assert 'sigAutoAxialToggled' not in widget_source
    assert '_showBeadCenterBox' not in widget_source
    assert '_beadCenterXEdit' not in widget_source
    assert '_beadCenterYEdit' not in widget_source
    assert '_emitBeadRecCenter' not in widget_source
    assert 'beadRecWorkflow.update_bead_rec_center' not in controller_source
    assert 'beadRecWorkflow.show_bead_rec_center_cross' not in controller_source
    assert 'beadRecWorkflow.set_auto_axial' not in controller_source
