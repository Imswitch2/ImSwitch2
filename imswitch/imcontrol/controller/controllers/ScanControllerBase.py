import traceback
import configparser

from ast import literal_eval
from typing import Dict, Any

from ..basecontrollers import SuperScanController
from imswitch.imcontrol.model import getWidgetStatePersistence


class ScanControllerBase(SuperScanController):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._widget.initControls(
            self.positioners.keys(),
            self.TTLDevices.keys(),
            self._master.scanManager.TTLTimeUnits
        )

        self.updatePixels()
        self.updateScanStageAttrs()
        self.updateScanTTLAttrs()

        # Connect ScanWidget signals
        self._widget.sigContLaserPulsesToggled.connect(self.setContLaserPulses)
        
        # Register for widget state persistence
        getWidgetStatePersistence().register('ScanController', self)

    def setParameters(self):
        self.settingParameters = True
        try:
            for i in range(len(self._analogParameterDict['target_device'])):
                positionerName = self._analogParameterDict['target_device'][i]
                self._widget.setScanDim(i, positionerName)
                self._widget.setScanSize(positionerName,
                                         self._analogParameterDict['axis_length'][i])
                self._widget.setScanStepSize(positionerName,
                                             self._analogParameterDict['axis_step_size'][i])
                self._widget.setScanCenterPos(positionerName,
                                              self._analogParameterDict['axis_centerpos'][i])

            setTTLDevices = []
            for i in range(len(self._digitalParameterDict['target_device'])):
                deviceName = self._digitalParameterDict['target_device'][i]
                self._widget.setTTLStarts(deviceName, self._digitalParameterDict['TTL_start'][i])
                self._widget.setTTLEnds(deviceName, self._digitalParameterDict['TTL_end'][i])
                setTTLDevices.append(deviceName)

            for deviceName in self.TTLDevices:
                if deviceName not in setTTLDevices:
                    self._widget.unsetTTL(deviceName)

            self._widget.setSeqTimePar(self._digitalParameterDict['sequence_time'])
        finally:
            self.settingParameters = False
            self.plotSignalGraph()

    def runScanAdvanced(self, *, recalculateSignals=True, isNonFinalPartOfSequence=False,
                        sigScanStartingEmitted):
        """ Runs a scan with the set scanning parameters. """
        try:
            self._widget.setScanButtonChecked(True)
            self.isRunning = True

            if recalculateSignals or self.signalDict is None or self.scanInfoDict is None:
                self.getParameters()
                try:
                    self.signalDict, self.scanInfoDict = self._master.scanManager.makeFullScan(
                        self._analogParameterDict, self._digitalParameterDict,
                        staticPositioner=self._widget.isContLaserMode()
                    )
                except TypeError:
                    self._logger.error(traceback.format_exc())
                    self.isRunning = False
                    return

            self.doingNonFinalPartOfSequence = isNonFinalPartOfSequence

            if not sigScanStartingEmitted:
                self.emitScanSignal(self._commChannel.sigScanStarting)
            # set positions of scanners not in scan from centerpos
            for index, positionerName in enumerate(self._analogParameterDict['target_device']):
                if positionerName not in self._positionersScan:
                    position = self._analogParameterDict['axis_centerpos'][index]
                    self._master.positionersManager[positionerName].setPosition(position, 0)
                    self._logger.debug(f'set {positionerName} center to {position} before scan')
            # run scan
            self._master.nidaqManager.runScan(self.signalDict, self.scanInfoDict)
        except Exception:
            self._logger.error(traceback.format_exc())
            self.isRunning = False

    def scanDone(self):
        self.isRunning = False

        if not self._widget.isContLaserMode() and not self._widget.repeatEnabled():
            self.emitScanSignal(self._commChannel.sigScanDone)
            if not self.doingNonFinalPartOfSequence:
                self._widget.setScanButtonChecked(False)
                self.emitScanSignal(self._commChannel.sigScanEnded)
        else:
            self.runScanAdvanced(sigScanStartingEmitted=True)

    def getParameters(self):
        if self.settingParameters:
            return
        self._analogParameterDict['target_device'] = []
        self._analogParameterDict['axis_length'] = []
        self._analogParameterDict['axis_step_size'] = []
        self._analogParameterDict['axis_centerpos'] = []
        self._analogParameterDict['axis_startpos'] = []
        self._positionersScan = []
        for i in range(len(self.positioners)):
            self._positionersScan.append(self._widget.getScanDim(i))
        for positionerName in self._positionersScan:
            if positionerName != 'None':
                size = self._widget.getScanSize(positionerName)
                stepSize = self._widget.getScanStepSize(positionerName)
                center = self._widget.getScanCenterPos(positionerName)
                start = list(self._master.positionersManager[positionerName].position.values())
                self._analogParameterDict['target_device'].append(positionerName)
                self._analogParameterDict['axis_length'].append(size)
                self._analogParameterDict['axis_step_size'].append(stepSize)
                self._analogParameterDict['axis_centerpos'].append(center)
                self._analogParameterDict['axis_startpos'].append(start)
        for positionerName in self.positioners:
            if positionerName not in self._positionersScan:
                size = 1.0
                stepSize = 1.0
                center = self._widget.getScanCenterPos(positionerName)
                start = [0]
                self._analogParameterDict['target_device'].append(positionerName)
                self._analogParameterDict['axis_length'].append(size)
                self._analogParameterDict['axis_step_size'].append(stepSize)
                self._analogParameterDict['axis_centerpos'].append(center)
                self._analogParameterDict['axis_startpos'].append(start)

        self._digitalParameterDict['target_device'] = []
        self._digitalParameterDict['TTL_start'] = []
        self._digitalParameterDict['TTL_end'] = []
        for deviceName, _ in self.TTLDevices.items():
            if not self._widget.getTTLIncluded(deviceName):
                continue

            self._digitalParameterDict['target_device'].append(deviceName)
            self._digitalParameterDict['TTL_start'].append(self._widget.getTTLStarts(deviceName))
            self._digitalParameterDict['TTL_end'].append(self._widget.getTTLEnds(deviceName))

        self._digitalParameterDict['sequence_time'] = self._widget.getSeqTimePar()
        self._analogParameterDict['sequence_time'] = self._widget.getSeqTimePar()

    def setContLaserPulses(self, isContLaserPulses):
        for i in range(len(self.positioners)):
            positionerName = self._widget.scanPar['scanDim' + str(i)].currentText()
            self._widget.setScanDimEnabled(i, not isContLaserPulses)
            self._widget.setScanSizeEnabled(positionerName, not isContLaserPulses)
            self._widget.setScanStepSizeEnabled(positionerName, not isContLaserPulses)
            self._widget.setScanCenterPosEnabled(positionerName, not isContLaserPulses)

    def updatePixels(self):
        self.getParameters()
        for index, positionerName in enumerate(self.positioners):
            if float(self._analogParameterDict['axis_step_size'][index]) != 0:
                pixels = round(float(self._analogParameterDict['axis_length'][index]) /
                               float(self._analogParameterDict['axis_step_size'][index]))
                self._widget.setScanPixels(positionerName, pixels)

    def emitScanSignal(self, signal, *args):
        if not self._widget.isContLaserMode():  # Cont. laser pulses mode is not a real scan
            signal.emit(*args)

    def saveScanParamsToFile(self, filePath: str) -> None:
        """ Saves the set scanning parameters to the specified file. """
        self.getParameters()
        config = configparser.ConfigParser()
        config.optionxform = str

        config['analogParameterDict'] = self._analogParameterDict
        config['digitalParameterDict'] = self._digitalParameterDict
        config['Modes'] = {'scan_or_not': self._widget.isScanMode()}

        with open(filePath, 'w') as configfile:
            config.write(configfile)

    def loadScanParamsFromFile(self, filePath: str) -> None:
        """ Loads scanning parameters from the specified file. """
        config = configparser.ConfigParser()
        config.optionxform = str
        config.read(filePath)

        for key in self._analogParameterDict:
            self._analogParameterDict[key] = literal_eval(
                config._sections['analogParameterDict'][key]
            )

        for key in self._digitalParameterDict:
            self._digitalParameterDict[key] = literal_eval(
                config._sections['digitalParameterDict'][key]
            )

        scanOrNot = (config._sections['Modes']['scan_or_not'] == 'True')
        if scanOrNot:
            self._widget.setScanMode()
        else:
            self._widget.setContLaserMode()

        self.setParameters()

    # Widget State Persistence Interface
    
    def getWidgetState(self) -> Dict[str, Any]:
        """
        Get current scan settings state for persistence.
        
        Returns scan parameters like size, step size, center position, and safe TTL settings.
        Does NOT include scan running state or cont laser mode for safety.
        
        Returns:
            Dict with structure:
            {
                'positioners': {
                    positionerName: {
                        'size': float,
                        'step_size': float,
                        'center_pos': float
                    }
                },
                'ttl_devices': {
                    deviceName: {
                        'included': bool,
                        'start': value,
                        'end': value
                    }
                },
                'sequence_time': value
            }
        """
        state = {
            'positioners': {},
            'ttl_devices': {},
            'sequence_time': None
        }
        
        try:
            # Save positioner settings
            for positionerName in self.positioners.keys():
                try:
                    state['positioners'][positionerName] = {
                        'size': self._widget.getScanSize(positionerName),
                        'step_size': self._widget.getScanStepSize(positionerName),
                        'center_pos': self._widget.getScanCenterPos(positionerName)
                    }
                except Exception as e:
                    self._logger.debug(
                        f'Could not save settings for positioner {positionerName}: {e}'
                    )
            
            # Save TTL device settings (safe settings only, not trigger commands)
            for deviceName in self.TTLDevices.keys():
                try:
                    state['ttl_devices'][deviceName] = {
                        'included': self._widget.getTTLIncluded(deviceName),
                        'start': self._widget.getTTLStarts(deviceName),
                        'end': self._widget.getTTLEnds(deviceName)
                    }
                except Exception as e:
                    self._logger.debug(
                        f'Could not save settings for TTL device {deviceName}: {e}'
                    )
            
            # Save sequence time
            try:
                state['sequence_time'] = self._widget.getSeqTimePar()
            except Exception as e:
                self._logger.debug(f'Could not save sequence time: {e}')
            
            # Scan mode radio (True = Scan, False = Cont. Laser Pulses)
            try:
                state['scan_mode'] = self._widget.scanRadio.isChecked()
            except Exception as e:
                self._logger.debug(f'Could not save scan mode: {e}')
            
            # Repeat checkbox
            try:
                state['repeat'] = self._widget.repeatBox.isChecked()
            except Exception as e:
                self._logger.debug(f'Could not save repeat setting: {e}')
            
            # Dimension combo selections: index → positioner name string
            state['scan_dims'] = {}
            for i in range(2):  # ScanWidgetBase exposes getScanDim(index) for 0 and 1
                try:
                    state['scan_dims'][str(i)] = self._widget.getScanDim(i)
                except Exception:
                    pass
        
        except Exception as e:
            self._logger.error(f'Failed to save scan settings state: {e}')
        
        return state
    
    def setWidgetState(self, state: Dict[str, Any]) -> None:
        """
        Restore scan settings state from persistence.
        
        SAFETY: Does NOT start scans or trigger hardware. Only restores:
        - Scan size, step size, and center position for positioners
        - TTL timing parameters (not triggering)
        - Sequence time
        
        Args:
            state: Dict returned by getWidgetState()
        """
        try:
            # Restore positioner settings
            positioners_state = state.get('positioners', {})
            for positionerName, positioner_state in positioners_state.items():
                if positionerName not in self.positioners:
                    self._logger.debug(
                        f'Skipping state for non-existent positioner: {positionerName}'
                    )
                    continue
                
                try:
                    if 'size' in positioner_state:
                        self._widget.setScanSize(positionerName, positioner_state['size'])
                    
                    if 'step_size' in positioner_state:
                        self._widget.setScanStepSize(positionerName, positioner_state['step_size'])
                    
                    if 'center_pos' in positioner_state:
                        self._widget.setScanCenterPos(positionerName, positioner_state['center_pos'])
                
                except Exception as e:
                    self._logger.warning(
                        f'Failed to restore settings for positioner {positionerName}: {e}'
                    )
            
            # Restore TTL device settings
            ttl_devices_state = state.get('ttl_devices', {})
            for deviceName, device_state in ttl_devices_state.items():
                if deviceName not in self.TTLDevices:
                    self._logger.debug(
                        f'Skipping state for non-existent TTL device: {deviceName}'
                    )
                    continue
                
                try:
                    if 'start' in device_state:
                        self._widget.setTTLStarts(deviceName, device_state['start'])
                    
                    if 'end' in device_state:
                        self._widget.setTTLEnds(deviceName, device_state['end'])
                    
                    # Note: We restore 'included' state but this doesn't trigger anything
                    # It just sets the checkbox state for user reference
                    if 'included' in device_state and hasattr(self._widget, 'setTTLIncluded'):
                        self._widget.setTTLIncluded(deviceName, device_state['included'])
                
                except Exception as e:
                    self._logger.warning(
                        f'Failed to restore settings for TTL device {deviceName}: {e}'
                    )
            
            # Restore sequence time
            if 'sequence_time' in state and state['sequence_time'] is not None:
                try:
                    self._widget.setSeqTimePar(state['sequence_time'])
                except Exception as e:
                    self._logger.debug(f'Could not restore sequence time: {e}')
            
            # Restore scan mode (Scan vs Cont. Laser Pulses)
            try:
                if state.get('scan_mode', True):
                    self._widget.setScanMode()  # sets scanRadio checked
                else:
                    self._widget.setContLaserMode()  # sets contLaserPulsesRadio checked
            except Exception as e:
                self._logger.debug(f'Could not restore scan mode: {e}')
            
            # Restore repeat checkbox
            try:
                self._widget.setRepeatEnabled(state.get('repeat', False))
            except Exception as e:
                self._logger.debug(f'Could not restore repeat setting: {e}')
            
            # Restore dimension combo selections
            for i_str, posName in state.get('scan_dims', {}).items():
                try:
                    self._widget.setScanDim(int(i_str), posName)
                except Exception:
                    pass
            
            self._logger.info('Scan settings state restored successfully')
            
        except Exception as e:
            self._logger.error(f'Failed to restore scan settings state: {e}')
    
    def getStateSchemaVersion(self) -> int:
        """Return schema version for state compatibility checking."""
        return 1


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
