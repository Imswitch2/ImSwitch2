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

        self._elliptecSliderManager = self._master.rs232sManager['elliptecSlider']
        self._rotationStageManager = self._master.rs232sManager['rotationStage']

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

        self._widget.sigKeyReleased.connect(self._commChannel.sigKeyReleased)
        self._commChannel.sigKeyReleased.connect(self.keyReleased)
        self._commChannel.sigSetConfig.connect(
            lambda config_name: self.setConfig(self.setupConfigs[config_name])
        )

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

        self.setConfig(self.setupConfigs['Widefield imaging'])

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.getPositions)
        self.timer.start(100)

    def setRotationJogStepSizeFromEdit(self):
        newStepSize = self._widget.jogStepSizeEdit.value()
        self._rotationStageManager.setJogDistanceInUnits(newStepSize)

    def setRotationStagePosFromEdit(self):
        newPos = self._widget.rotationStagePosEdit.value()
        self.setRotationStagePos(newPos)

    def setRotationStagePos(self, pos):
        self._logger.debug('Setting rotation stage position')
        self._rotationStageManager.moveToInUnits(pos)

    def getPositions(self):
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
                self._elliptecSliderManager.moveToPosition(0)
            if event.key() == QtCore.Qt.Key_2:
                self._elliptecSliderManager.moveToPosition(1)
            if event.key() == QtCore.Qt.Key_3:
                self._elliptecSliderManager.moveToPosition(2)
            if event.key() == QtCore.Qt.Key_4:
                self._elliptecSliderManager.moveToPosition(3)

    def closeEvent(self):
        self.timer.stop()
        for fm in self.flipMirrors:
            try:
                fm.close()
            except Exception:
                pass
