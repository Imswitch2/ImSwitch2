import time

import numpy as np
try:
    from pyvisa.errors import VisaIOError, InvalidSession
except ImportError:
    # pyvisa is an optional, hardware-only dependency. Provide fallbacks so this
    # module can be imported without it (e.g. in CI or hardware-free installs).
    # The TriggerScope cannot operate without pyvisa anyway, so these stand-ins
    # are never actually raised in that case — they only keep the ``except``
    # clause below valid.
    class VisaIOError(Exception):
        pass

    class InvalidSession(Exception):
        pass
from serial.serialutil import SerialException

from imswitch.imcommon.framework import Signal, SignalInterface, Thread, Timer, Worker
from imswitch.imcommon.model import initLogger


class TriggerScopeManager(SignalInterface):
    """ Board-level manager for TriggerScope DAQ hardware.

    Handles the serial connection, device registry, and raw DAC/TTL primitives.
    Scan sequence logic lives in ScanManagerTriggerScope.

    Reads ``setupInfo.triggerScope.rs232device`` to identify the RS232 channel.
    Scans ``setupInfo.getAllDevices()`` to build an internal registry mapping
    device names to their DAC channel numbers and TTL line numbers.
    """

    sigScanDone = Signal()
    sigScanStarted = Signal()

    def __init__(self, setupInfo, rs232sManager):
        super().__init__()
        self.__logger = initLogger(self)

        info = setupInfo.triggerScope
        self._rs232manager = rs232sManager[info.rs232device]
        self._rs232manager.setTimeout(100000)
        self.send('*')

        # Build device registry: name -> {DACChannel, TTLLine, MinV, MaxV}
        self._deviceInfo = {}
        for targetName, targetInfo in setupInfo.getAllDevices().items():
            analogChannel = targetInfo.getAnalogChannel()
            digitalLine = targetInfo.getDigitalLine()
            if analogChannel is not None:
                parts = analogChannel.split('/')
                if len(parts) == 2:
                    dev, chan = parts
                    if dev == 'Triggerscope' and chan.startswith('DAC'):
                        chanNr = chan[3:]  # "DAC7" -> "7", handles >=10 correctly
                        self._deviceInfo[targetName] = {
                            'DACChannel': chanNr,
                            'MinV': targetInfo.managerProperties.get('minVolt', -10),
                            'MaxV': targetInfo.managerProperties.get('maxVolt', 10),
                        }
            if digitalLine is not None:
                parts = digitalLine.split('/')
                if len(parts) == 2:
                    dev, line = parts
                    if dev == 'Triggerscope' and line.startswith('TTL'):
                        lineNr = line[3:]  # "TTL7" -> "7"
                        if targetName in self._deviceInfo:
                            self._deviceInfo[targetName]['TTLLine'] = lineNr
                        else:
                            self._deviceInfo[targetName] = {'TTLLine': lineNr}

        self.setParSleepTime = 0.05

        # Background serial monitor watching for "Scan done" messages
        self._serialMonitor = SerialMonitor(self._rs232manager, updatePeriod=5)
        self._thread = Thread()
        self._serialMonitor.moveToThread(self._thread)
        self._thread.started.connect(self._serialMonitor.run)
        self._thread.finished.connect(self._serialMonitor.stop)
        self._thread.start()
        self._monitoring = True

        self._serialMonitor.sigScanDone.connect(self.sigScanDone)
        self._serialMonitor.sigUnknownMessage.connect(self._unknownMsg)

        self.__logger.info('TriggerScopeManager ready, %d devices registered',
                           len(self._deviceInfo))

    def __del__(self):
        self._thread.quit()
        self._thread.wait()
        if hasattr(super(), '__del__'):
            super().__del__()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def deviceInfo(self):
        """ Read-only view of the device registry built from SetupInfo. """
        return self._deviceInfo

    # ------------------------------------------------------------------
    # Board primitives
    # ------------------------------------------------------------------

    def setParameter(self, parameterName, parameterValue):
        """ Send a PARAMETER,name,value command and wait for board to process. """
        self.send(f'PARAMETER,{parameterName},{parameterValue}\n')
        time.sleep(self.setParSleepTime)

    def send(self, command):
        """ Write a raw command string to the TriggerScope serial port. """
        self._rs232manager.write(command)

    def setDigital(self, target, enable):
        """ Set a TTL line high (True) or low (False) for a named device. """
        val = '1' if enable else '0'
        line = self._deviceInfo[target]['TTLLine']
        self.send(f'TTL{line},{val}')

    def setAnalog(self, target, voltage):
        """ Set the DAC voltage for a named device.  Clamps to configured range. """
        info = self._deviceInfo[target]
        if info['MinV'] <= voltage <= info['MaxV']:
            self.send(f'DAC{info["DACChannel"]},{voltage}')
        else:
            self.__logger.warning(
                f'Voltage {voltage} V outside [{info["MinV"]}, {info["MaxV"]}] V'
                f' for target "{target}" — command ignored'
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeMonitor(self):
        if self._monitoring:
            self._thread.quit()
            self._monitoring = False

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _unknownMsg(self, msg):
        self.__logger.info(f'[TriggerScope serial] {msg}')


class SerialMonitor(Worker):
    """ Background worker that polls the TriggerScope serial port for status
    messages (``"Scan done"``, ``"MSG<n>"``, and anything else). """

    sigScanDone = Signal()
    sigTrigMSG = Signal(int)
    sigUnknownMessage = Signal(str)

    def __init__(self, rs232Manager, updatePeriod):
        super().__init__()
        self._rs232Manager = rs232Manager
        self._updatePeriod = updatePeriod
        self._vtimer = None

    def run(self):
        self._vtimer = Timer()
        self._vtimer.timeout.connect(self.checkSerial)
        self._vtimer.start(self._updatePeriod)

    def stop(self):
        if self._vtimer is not None:
            self._vtimer.stop()
        self._rs232Manager.finalize()

    def checkSerial(self):
        try:
            msg = self._rs232Manager.read(termination='\r\n')
        except (VisaIOError, InvalidSession, SerialException, TypeError):
            msg = None

        if msg is not None:
            if msg == 'Scan done':
                self.sigScanDone.emit()
            elif msg[:3] == 'MSG':
                self.sigTrigMSG.emit(int(msg[3:]))
            else:
                self.sigUnknownMessage.emit(msg)


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
