import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from .DataEditDialog import DataEditDialog
from .guitools import BetterPushButton


class _ElidingLabel(QtWidgets.QLabel):
    """A QLabel whose contents never impose a horizontal minimum width.

    File and dataset names can be arbitrarily long.  A normal QLabel feeds the
    full text width into its parent layout's minimum-size calculation; because
    Current data shares a dock column with the other left-hand panels, loading
    one long filename can otherwise make the entire column impossible to
    shrink again.
    """

    def __init__(self, text='', *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fullText = ''
        self.setMinimumWidth(0)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Ignored,
            QtWidgets.QSizePolicy.Preferred,
        )
        self.setText(text)

    def setText(self, text):
        self._fullText = str(text)
        self.setToolTip(self._fullText)
        self._updateElidedText()

    def fullText(self):
        return self._fullText

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._updateElidedText()

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(0)
        return hint

    def _updateElidedText(self):
        width = max(0, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(
            self._fullText,
            QtCore.Qt.ElideMiddle,
            width,
        )
        super().setText(shown)


class DataFrame(QtWidgets.QFrame):
    """Frame for showing and examining the raw data"""

    # Signals
    sigShowMeanClicked = QtCore.Signal()
    sigAdjustDataClicked = QtCore.Signal()
    sigUnloadDataClicked = QtCore.Signal()
    sigOpenDataClicked = QtCore.Signal()
    sigProcessCurrentClicked = QtCore.Signal()
    sigFrameNumberChanged = QtCore.Signal(int)
    sigFrameSliderChanged = QtCore.Signal(int)

    # Methods
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Image Widget
        imageWidget = pg.GraphicsLayoutWidget()
        self.imgVb = imageWidget.addViewBox(row=0, col=0)
        self.imgVb.setMouseMode(pg.ViewBox.PanMode)
        self.img = pg.ImageItem(axisOrder='row-major')
        self.img.setTransform(self.img.transform().translate(-0.5, -0.5))
        self.imgVb.addItem(self.img)
        self.imgVb.setAspectLocked(True)
        self.imgHist = pg.HistogramLUTItem(image=self.img)
        imageWidget.addItem(self.imgHist, row=0, col=1)

        self.openDataBtn = BetterPushButton('Open data…')
        self.openDataBtn.setToolTip('Open a data source as Current Data')
        self.openDataBtn.clicked.connect(self.sigOpenDataClicked)

        self.processCurrentBtn = BetterPushButton('Process current')
        self.processCurrentBtn.setToolTip('Process the currently inspected data with the active reconstructor')
        self.processCurrentBtn.clicked.connect(self.sigProcessCurrentClicked)
        self.openDataBtn.hide()
        self.processCurrentBtn.hide()

        self.showMeanBtn = BetterPushButton()
        self.showMeanBtn.setText('Show mean image')
        self.showMeanBtn.clicked.connect(self.sigShowMeanClicked)

        self.adjustDataBtn = BetterPushButton()
        self.adjustDataBtn.setText('Adjust/compl. data')
        self.adjustDataBtn.clicked.connect(self.sigAdjustDataClicked)

        self.unloadDataBtn = BetterPushButton()
        self.unloadDataBtn.setText('Unload data')
        self.unloadDataBtn.clicked.connect(self.sigUnloadDataClicked)

        frameLabel = QtWidgets.QLabel('Frame # ')
        self.frameNum = QtWidgets.QLineEdit('0')
        self.frameNum.textChanged.connect(lambda x: (x.isdigit() and
                                                     self.setCurrentFrame(int(x)) and
                                                     self.sigFrameNumberChanged.emit(int(x))))
        self.frameNum.setFixedWidth(45)

        self.dataName = _ElidingLabel('File:')
        self.datasetName = _ElidingLabel('Dataset:')
        numFramesLabel = QtWidgets.QLabel('No. frames:')
        self.numFrames = QtWidgets.QLabel('')

        self.slider = QtWidgets.QSlider(QtCore.Qt.Horizontal, self)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.setTickInterval(5)
        self.slider.setSingleStep(1)
        self.slider.valueChanged[int].connect(self.setCurrentFrame)
        self.slider.valueChanged[int].connect(self.sigFrameSliderChanged)

        self.patternScatter = pg.ScatterPlotItem()
        self.patternScatter.setData(
            pos=[[0, 0], [10, 10], [20, 20], [30, 30], [40, 40]],
            pen=pg.mkPen(color=(255, 0, 0), width=0.5,
                         style=QtCore.Qt.SolidLine, antialias=True),
            brush=pg.mkBrush(color=(255, 0, 0), antialias=True), size=1,
            pxMode=False)

        self.detectionScatter = pg.ScatterPlotItem()
        self.detectionScatter.setData(
            pos=[[0, 0]],
            pen=pg.mkPen(color=(255, 50, 50), width=1.5,
                         style=QtCore.Qt.SolidLine, antialias=True),
            brush=None, size=8,
            symbol='o',
            pxMode=False)

        self.editWdw = DataEditDialog(self)

        layout = QtWidgets.QGridLayout()
        self.setLayout(layout)

        actionRow = QtWidgets.QHBoxLayout()
        actionRow.setContentsMargins(0, 0, 0, 0)
        actionRow.setSpacing(4)
        actionRow.addWidget(self.openDataBtn)
        actionRow.addWidget(self.processCurrentBtn)
        actionRow.addStretch(1)
        layout.addLayout(actionRow, 0, 0, 1, 6)
        layout.addWidget(self.dataName, 1, 0, 1, 4)
        layout.addWidget(self.datasetName, 1, 4, 1, 2)
        layout.addWidget(self.showMeanBtn, 2, 0)
        layout.addWidget(self.slider, 2, 1, 1, 3)
        layout.addWidget(frameLabel, 2, 4)
        layout.addWidget(self.frameNum, 2, 5)
        layout.addWidget(self.adjustDataBtn, 3, 0)
        layout.addWidget(self.unloadDataBtn, 3, 1)
        layout.addWidget(numFramesLabel, 3, 4)
        layout.addWidget(self.numFrames, 3, 5)
        layout.addWidget(imageWidget, 4, 0, 1, -1)

        self._showPattern = False
        self._showDetectionPreview = False

    def setShowPattern(self, value):
        self._showPattern = value

        if value:
            self.imgVb.addItem(self.patternScatter)
        else:
            self.imgVb.removeItem(self.patternScatter)

    def setShowDetectionPreview(self, value):
        # Idempotent: visibility may be re-asserted (e.g. cleared again when
        # the active reconstructor switches away), and ViewBox.removeItem on
        # an item that was never added raises.
        value = bool(value)
        if value == self._showDetectionPreview:
            return
        self._showDetectionPreview = value

        if value:
            self.imgVb.addItem(self.detectionScatter)
        else:
            self.imgVb.removeItem(self.detectionScatter)

    def showEditWindow(self):
        self.editWdw.show()

    def setImage(self, im, autoLevels):
        self.img.setImage(im, autoLevels)

    def setPatternGridData(self, x, y):
        self.patternScatter.setData(x, y)

    def setDetectionPreviewData(self, x, y):
        self.detectionScatter.setData(x, y)

    def setDetectionPreviewStyle(self, marker):
        """``"point"``: a dot on each found focus, as the pattern grid is
        drawn; ``"circle"``: an open ring around each candidate."""
        if str(marker) == "point":
            self.detectionScatter.setPen(None)
            self.detectionScatter.setBrush(pg.mkBrush(color=(255, 50, 50), antialias=True))
            self.detectionScatter.setSize(1)
        else:
            self.detectionScatter.setPen(pg.mkPen(color=(255, 50, 50), width=1.5,
                                                  style=QtCore.Qt.SolidLine, antialias=True))
            self.detectionScatter.setBrush(None)
            self.detectionScatter.setSize(8)

    def setCurrentFrame(self, value):
        self.frameNum.setText(str(value))
        self.slider.setValue(value)

    def setNumFrames(self, value):
        """Set the frame range for new data without navigating.

        Lowering the maximum below the slider's position clamps it, and the
        clamp emits ``valueChanged`` like a user drag -- which read a plane of
        the new data behind a preview that had just decided to show nothing,
        decoding a non-lazy source whole, and replaced the mean on screen with
        whatever frame the clamp landed on. The controls are updated silently
        and agree with each other afterwards.
        """
        self.numFrames.setText(str(value))
        with QtCore.QSignalBlocker(self.slider), QtCore.QSignalBlocker(self.frameNum):
            self.slider.setMaximum(value - 1 if value > 0 else 0)
            self.frameNum.setText(str(self.slider.value()))

    def setDataName(self, value):
        self.dataName.setText(f'File: {value}')

    def setDatasetName(self, value):
        self.datasetName.setText(f'Dataset: {value}')

    def setProcessCurrentVisible(self, visible: bool) -> None:
        self.processCurrentBtn.setVisible(bool(visible))

    def setProcessCurrentEnabled(self, enabled: bool) -> None:
        self.processCurrentBtn.setEnabled(bool(enabled))

    def setImageControlsEnabled(self, enabled):
        """Disable pixel-only actions while a metadata source is current."""
        enabled = bool(enabled)
        self.showMeanBtn.setEnabled(enabled)
        self.adjustDataBtn.setEnabled(enabled)
        self.slider.setEnabled(enabled)
        self.frameNum.setEnabled(enabled)


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
