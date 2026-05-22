"""Inspired from EtMonalisaWidget"""

import os

from imswitch.imcommon.model import initLogger
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import dirtools
from imswitch.imcontrol.view import guitools
from imswitch.imcommon.view.guitools import naparitools
from .basewidgets import Widget

_etSnoutyDir = 'C:/Users/Snouty/imcontrol_etsnouty'


class EtSnoutyWidget(Widget):
    """Widget for controlling the EtSnouty implementation."""

    sigSavePipelineClicked = QtCore.Signal()
    sigLoadPipelineClicked = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        self.__logger = initLogger(self, instanceName='EtSnoutyWidget')
        super().__init__(*args, **kwargs)

        self.analysisDir = os.path.join(_etSnoutyDir, 'analysis_pipelines')
        self.transformDir = os.path.join(_etSnoutyDir, 'transform_pipelines')

        if not os.path.exists(self.analysisDir):
            os.makedirs(self.analysisDir)
        if not os.path.exists(self.transformDir):
            os.makedirs(self.transformDir)

        # scatter plot of detected event coordinates in the image viewer
        self.eventScatterPlot = naparitools.VispyScatterVisual(color='red', symbol='x')
        self.eventScatterPlot.hide()

        # analysis pipeline dropdown
        self.analysisPipelines = []
        self.analysisPipelinePar = QtWidgets.QComboBox()
        for pipeline in os.listdir(self.analysisDir):
            if os.path.isfile(os.path.join(self.analysisDir, pipeline)):
                self.analysisPipelines.append(pipeline.split('.')[0])
        self.analysisPipelinePar.addItems(self.analysisPipelines)
        self.analysisPipelinePar.setCurrentIndex(0)

        self.__paramsExclude = ['img', 'prev_frames', 'binary_mask', 'exinfo', 'testmode']

        # fast detector dropdown
        self.fastImgDetectors = []
        self.fastImgDetectorsPar = QtWidgets.QComboBox()
        self.fastImgDetectorsPar_label = QtWidgets.QLabel('Fast detector')
        self.fastImgDetectorsPar_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom
        )

        # fast laser dropdown + power field
        self.fastImgLasers = []
        self.fastImgLasersPar = QtWidgets.QComboBox()
        self.fastImgLasersPar_label = QtWidgets.QLabel('Fast laser, power(mW)')
        self.fastImgLasersPar_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom
        )
        self.fastImgLasersPower_edit = QtWidgets.QLineEdit(str(30))

        # experiment mode dropdown
        self.experimentModes = ['Experiment', 'TestVisualize', 'TestValidate']
        self.experimentModesPar = QtWidgets.QComboBox()
        self.experimentModesPar_label = QtWidgets.QLabel('Experiment mode')
        self.experimentModesPar_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignCenter
        )
        self.experimentModesPar.addItems(self.experimentModes)
        self.experimentModesPar.setCurrentIndex(0)

        self.param_names = []
        self.param_edits = []

        self.savePipelineParamsBtn = guitools.BetterPushButton('Save pipeline parameters')
        self.loadPipelineParamsBtn = guitools.BetterPushButton('Load pipeline parameters')

        self.initiateButton = guitools.BetterPushButton('Initiate etSnouty')
        self.initiateButton.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding
        )
        self.loadPipelineButton = guitools.BetterPushButton('Load pipeline')

        self.recordBinaryMaskButton = guitools.BetterPushButton('Record binary mask')
        self.loadBinaryMaskButton = guitools.BetterPushButton('Load binary mask')
        self.clearBinaryMaskButton = guitools.BetterPushButton('Clear binary mask')
        self.showBinaryMaskButton = guitools.BetterPushButton('Show binary mask')

        self.setUpdatePeriodCheck = QtWidgets.QCheckBox('Use Widefield camera frameRate')
        self.TestModeCheck = QtWidgets.QCheckBox('Test Mode')
        self.setBusyFalseButton = guitools.BetterPushButton('Unlock softlock')
        self.endlessScanCheck = QtWidgets.QCheckBox('Endless')

        self.update_period_label = QtWidgets.QLabel('Update period (ms)')
        self.update_period_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignBottom)
        self.update_period_edit = QtWidgets.QLineEdit(str(100))

        self.analysisHelpWidget = AnalysisWidget(*args, **kwargs)

        self.grid = QtWidgets.QGridLayout()
        self.setLayout(self.grid)

        currentRow = 0
        self.grid.addWidget(self.initiateButton, currentRow, 0)
        self.grid.addWidget(self.endlessScanCheck, currentRow, 1)
        self.grid.addWidget(self.experimentModesPar_label, currentRow, 2)
        self.grid.addWidget(self.experimentModesPar, currentRow, 3)
        self.grid.addWidget(self.setBusyFalseButton, currentRow, 4)

        currentRow += 1
        self.grid.addWidget(self.loadPipelineButton, currentRow, 0)
        self.grid.addWidget(self.analysisPipelinePar, currentRow, 1)
        self.grid.addWidget(self.loadBinaryMaskButton, currentRow, 3)
        self.grid.addWidget(self.recordBinaryMaskButton, currentRow, 4)

        currentRow += 1
        self.grid.addWidget(self.loadPipelineParamsBtn, currentRow, 0)
        self.grid.addWidget(self.savePipelineParamsBtn, currentRow, 1)
        self.grid.addWidget(self.clearBinaryMaskButton, currentRow, 3)
        self.grid.addWidget(self.showBinaryMaskButton, currentRow, 4)

        currentRow += 1
        self.grid.addWidget(self.update_period_label, currentRow, 2)
        self.grid.addWidget(self.update_period_edit, currentRow, 3)
        self.grid.addWidget(self.setUpdatePeriodCheck, currentRow, 4)

        currentRow += 1
        self.grid.addWidget(self.fastImgDetectorsPar_label, currentRow, 2)
        self.grid.addWidget(self.fastImgDetectorsPar, currentRow, 3)
        self.grid.addWidget(self.TestModeCheck, currentRow, 4)

        currentRow += 1
        self.grid.addWidget(self.fastImgLasersPar_label, currentRow, 2)
        self.grid.addWidget(self.fastImgLasersPar, currentRow, 3)
        self.grid.addWidget(self.fastImgLasersPower_edit, currentRow, 4)

        self.savePipelineParamsBtn.clicked.connect(self.sigSavePipelineClicked)
        self.loadPipelineParamsBtn.clicked.connect(self.sigLoadPipelineClicked)

    def initParamFields(self, parameters: dict):
        """Initialize widget parameter fields for the loaded pipeline."""
        for param in self.param_names:
            self.grid.removeWidget(param)
            param.deleteLater()
        for param in self.param_edits:
            self.grid.removeWidget(param)
            param.deleteLater()

        currentRow = 3
        self.param_names = []
        self.param_edits = []
        for pipeline_param_name, pipeline_param_val in parameters.items():
            if pipeline_param_name not in self.__paramsExclude:
                param_name = QtWidgets.QLabel(f'{pipeline_param_name}')
                param_value = (
                    pipeline_param_val.default
                    if pipeline_param_val.default is not pipeline_param_val.empty
                    else 0
                )
                param_edit = QtWidgets.QLineEdit(str(param_value))
                self.grid.addWidget(param_name, currentRow, 0)
                self.grid.addWidget(param_edit, currentRow, 1)
                self.param_names.append(param_name)
                self.param_edits.append(param_edit)
                currentRow += 1

    def setFastDetectorList(self, detectorNames):
        """Populate the fast-detector combobox."""
        for detectorName in detectorNames:
            self.fastImgDetectors.append(detectorName)
        self.fastImgDetectorsPar.addItems(self.fastImgDetectors)
        self.fastImgDetectorsPar.setCurrentIndex(0)

    def setFastLaserList(self, laserNames):
        """Populate the fast-laser combobox."""
        for laserName in laserNames:
            self.fastImgLasers.append(laserName)
        self.fastImgLasersPar.addItems(self.fastImgLasers)
        self.fastImgLasersPar.setCurrentIndex(0)

    def setEventScatterData(self, x, y):
        self.eventScatterPlot.setData(x=x, y=y)

    def setEventScatterVisible(self, visible):
        pass

    def getEventScatterPlot(self):
        return self.eventScatterPlot

    def launchHelpWidget(self, widget, init=True):
        if init:
            widget.show()
        else:
            widget.hide()


class AnalysisWidget(Widget):
    """Pop-up widget for live analysis images or binary masks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.imgVbWidget = pg.GraphicsLayoutWidget()
        self.imgVb = self.imgVbWidget.addViewBox(row=1, col=1)

        self.img = pg.ImageItem(axisOrder='row-major')
        self.img.translate(-0.5, -0.5)

        self.scatter = pg.ScatterPlotItem()

        self.imgVb.addItem(self.img)
        self.imgVb.setAspectLocked(True)
        self.imgVb.addItem(self.scatter)

        self.info_label = QtWidgets.QLabel('<image info>')

        self.grid = QtWidgets.QGridLayout()
        self.setLayout(self.grid)
        self.grid.addWidget(self.info_label, 0, 0)
        self.grid.addWidget(self.imgVbWidget, 1, 0)
