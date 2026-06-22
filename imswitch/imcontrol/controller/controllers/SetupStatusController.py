from qtpy import QtCore
from ..basecontrollers import ImConWidgetController

try:
    from thorlabs_apt_device.devices import APTDevice_Motor
    _APT_AVAILABLE = True
except ImportError:
    _APT_AVAILABLE = False


class SetupStatusController(ImConWidgetController):
    """Linked to SetupStatusWidget. Controls flip mirrors, rotation stage and Elliptec slider."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        flipMirrorCOMs = ('COM6', 'COM5', 'COM11')

        self.tiltedCamName = 'Orca'
        self.straightCamName = 'WidefieldCamera'

        self._elliptecSliderManager = self._getOptionalRS232Manager(
            'elliptecSlider', 'Elliptec slider hot keys'
        )
        self._rotationStageManager = self._getOptionalRS232Manager(
            'rotationStage', 'rotation stage controls'
        )
        self.timer = None

        self.flipMirrors = []
        if _APT_AVAILABLE:
            try:
                self.flipMirrors = [APTDevice_Motor(serial_port=port) for port in flipMirrorCOMs]
                self._logger.debug('Initialized flip mirrors in SetupStatusController')
            except Exception:
                self._logger.warning('Could not initialize flip mirrors in SetupStatusController')
                self._widget.setEnabled(False)
        else:
            self._logger.warning(
                'thorlabs_apt_device not installed — flip mirrors disabled in SetupStatusController'
            )
            self._widget.setEnabled(False)

        wideFieldParamDict = {
            'Illumination status': 'Widefield',
            'Detection status': 'Straight',
            'Flip mirror positions': [False, False, True],
            'Hot key': QtCore.Qt.Key_F1,
        }
        lightSheetParamDict = {
            'Illumination status': 'Light sheet',
            'Detection status': 'Tilted',
            'Flip mirror positions': [True, True, False],
            'Hot key': QtCore.Qt.Key_F2,
        }
        wideFieldIlluminationParamDict = {
            'Illumination status': 'Widefield',
            'Detection status': None,
            'Flip mirror positions': [None, False, True],
            'Hot key': QtCore.Qt.Key_F5,
        }
        lightSheetIlluminationParamDict = {
            'Illumination status': 'Light sheet',
            'Detection status': None,
            'Flip mirror positions': [None, True, False],
            'Hot key': QtCore.Qt.Key_F6,
        }
        straightDetectionParamDict = {
            'Illumination status': None,
            'Detection status': 'Straight',
            'Flip mirror positions': [False, None, None],
            'Hot key': QtCore.Qt.Key_F9,
        }
        tiltedDetectionParamDict = {
            'Illumination status': None,
            'Detection status': 'Tilted',
            'Flip mirror positions': [True, None, None],
            'Hot key': QtCore.Qt.Key_F10,
        }

        self.setupConfigs = {
            'Widefield imaging': wideFieldParamDict,
            'Light sheet imaging': lightSheetParamDict,
            'Widefield illumination': wideFieldIlluminationParamDict,
            'Light sheet illumination': lightSheetIlluminationParamDict,
            'Tilted detection': tiltedDetectionParamDict,
            'Straight detection': straightDetectionParamDict,
        }

        self._widget.sigKeyReleased.connect(self.keyReleased)
        self._commChannel.sigSetConfig.connect(
            lambda config_name: self.setConfig(self.setupConfigs[config_name])
        )

        if self._rotationStageManager is not None:
            self._connectRotationStageControls()
        else:
            self._setRotationStageControlsEnabled(False)
            self._widget.currentPosOfRotationStageDisp.setText('Unavailable')

        self.setConfig(self.setupConfigs['Widefield imaging'])

        if self._rotationStageManager is not None:
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self.getPositions)
            self.timer.start(100)

    def _getOptionalRS232Manager(self, managerName, featureLabel):
        rs232sManager = getattr(self._master, 'rs232sManager', None)
        if rs232sManager is None:
            self._logger.warning(
                '%s disabled in SetupStatusController: rs232sManager is unavailable',
                featureLabel,
            )
            return None

        try:
            return rs232sManager[managerName]
        except KeyError:
            self._logger.warning(
                '%s disabled in SetupStatusController: RS232 device "%s" is not configured',
                featureLabel,
                managerName,
            )
            return None

    def _connectRotationStageControls(self):
        self._widget.rotationStagePosEdit.editingFinished.connect(
            self.setRotationStagePosFromEdit
        )
        self._widget.jogStepSizeEdit.editingFinished.connect(
            self.setRotationJogStepSizeFromEdit
        )
        self._widget.jogPositiveButton.clicked.connect(
            lambda: self._rotationStageManager.jog(True)
        )
        self._widget.jogNegativeButton.clicked.connect(
            lambda: self._rotationStageManager.jog(False)
        )

    def _setRotationStageControlsEnabled(self, enabled):
        for attrName in (
            'rotationStageHeader',
            'rotationStagePosLabel',
            'rotationStagePosEdit',
            'jogStepSizeLabel',
            'jogStepSizeEdit',
            'jogPositiveButton',
            'jogNegativeButton',
            'currentPosOfRotationStageLabel',
            'currentPosOfRotationStageDisp',
        ):
            widget = getattr(self._widget, attrName, None)
            if widget is not None and hasattr(widget, 'setEnabled'):
                widget.setEnabled(enabled)

    def setRotationJogStepSizeFromEdit(self):
        if self._rotationStageManager is None:
            return
        newStepSize = self._widget.jogStepSizeEdit.value()
        self._rotationStageManager.setJogDistanceInUnits(newStepSize)

    def setRotationStagePosFromEdit(self):
        if self._rotationStageManager is None:
            return
        newPos = self._widget.rotationStagePosEdit.value()
        self.setRotationStagePos(newPos)

    def setRotationStagePos(self, pos):
        if self._rotationStageManager is None:
            self._logger.warning(
                'Cannot set rotation stage position: rotation stage is not configured'
            )
            return
        self._logger.debug('Setting rotation stage position')
        self._rotationStageManager.moveToInUnits(pos)

    def getPositions(self):
        if self._rotationStageManager is None:
            return
        self._widget.currentPosOfRotationStageDisp.setText(
            str(self._rotationStageManager.getPositionInUnits())
        )

    def setConfig(self, configurationPars: dict):
        try:
            self.setFlipMirrorPositions(configurationPars['Flip mirror positions'])
            ill = configurationPars['Illumination status']
            det = configurationPars['Detection status']
            if ill:
                self._widget.illuminationStatusLabel.setText(ill)
            if det:
                self._widget.detectionStatusLabel.setText(det)
                if det == 'Tilted':
                    self._commChannel.sigSetVisibleLayers.emit((self.tiltedCamName,))
                elif det == 'Straight':
                    self._commChannel.sigSetVisibleLayers.emit((self.straightCamName,))
        except Exception:
            self._logger.warning('Could not set setup to given configuration')

    def setFlipMirrorPositions(self, positionList: list):
        for i, flipMirror in enumerate(self.flipMirrors):
            newPos = positionList[i]
            if newPos is not None:
                flipMirror.move_jog(newPos)

    def keyReleased(self, event):
        if not event.isAutoRepeat():
            self._logger.debug('Key release detected %s' % event.key())
            for key, item in self.setupConfigs.items():
                if event.key() == item['Hot key']:
                    self.setConfig(item)
            if event.key() == QtCore.Qt.Key_1:
                self._moveElliptecSlider(0)
            if event.key() == QtCore.Qt.Key_2:
                self._moveElliptecSlider(1)
            if event.key() == QtCore.Qt.Key_3:
                self._moveElliptecSlider(2)
            if event.key() == QtCore.Qt.Key_4:
                self._moveElliptecSlider(3)

    def _moveElliptecSlider(self, position):
        if self._elliptecSliderManager is None:
            self._logger.warning(
                'Cannot move Elliptec slider: elliptecSlider is not configured'
            )
            return
        self._elliptecSliderManager.moveToPosition(position)

    def closeEvent(self):
        if self.timer is not None:
            self.timer.stop()
        for fm in self.flipMirrors:
            try:
                fm.close()
            except Exception:
                pass
