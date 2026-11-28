import re
import threading
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
from imswitch.imcontrol.model.devices.graph import (
    DeviceDependencySpec, DeviceDescriptorSpec, DeviceRelationKind,
    DeviceSection, HardwareDeviceId,
)
from imswitch.imcontrol.model.devices.status import DeviceId


class UploadJob:
    """An ordered batch of board commands handed to the uploader thread.

    ``commands`` is a list of ``(command, pauseAfterS)``. ``onFinished(ok,
    message)`` is called once on the thread that owns the manager (the GUI
    thread), whatever happened: every command written, a write that raised,
    or the job cancelled before it got to the next command.
    """

    def __init__(self, commands, onFinished=None, label=''):
        self.commands = list(commands)
        self.onFinished = onFinished
        self.label = label
        self._cancelled = threading.Event()

    def cancel(self):
        """Do not send any command not yet written. A command already inside
        the serial write cannot be recalled."""
        self._cancelled.set()

    @property
    def cancelled(self):
        return self._cancelled.is_set()


class _CommandUploader(Worker):
    """Writes ``UploadJob`` batches to the board, one job at a time, in the
    order they were requested.

    It lives on its own thread so that a long batch (a raster scan uploads
    ~20 PARAMETER lines with a 50 ms pause after each) and any wait on the
    serial I/O lock never run on the GUI thread. One thread, one queue: the
    board still sees exactly the order of the requests.
    """

    sigJobDone = Signal(object, bool, str)

    def __init__(self, write, onJobFinished=None):
        super().__init__()
        self.__logger = initLogger(self)
        self._write = write
        self._onJobFinished = onJobFinished

    def runJob(self, job):
        ok, message = True, ''
        try:
            for command, pauseS in job.commands:
                if job.cancelled:
                    ok, message = False, 'cancelled before all commands were sent'
                    break
                self._write(command)
                if pauseS:
                    time.sleep(pauseS)
        except Exception as exc:
            ok, message = False, f'{type(exc).__name__}: {exc}'
            self.__logger.error(
                'TriggerScope upload %r failed: %s', job.label, message)
        finally:
            if self._onJobFinished is not None:
                self._onJobFinished()
        self.sigJobDone.emit(job, ok, message)


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
    #: GUI thread -> uploader thread (queued, so strictly in request order).
    _sigUploadRequested = Signal(object)

    #: Longest the GUI thread waits for the monitor/uploader threads to stop.
    SHUTDOWN_WAIT_MS = 5000

    def __init__(self, setupInfo, rs232sManager):
        super().__init__()
        self.__logger = initLogger(self)

        info = setupInfo.triggerScope
        self._rs232Name = str(info.rs232device)
        self._rs232manager = rs232sManager[info.rs232device]
        self._rs232manager.setTimeout(100000)
        # Ordered off-GUI-thread writer (created on first use).
        self._uploadLock = threading.Lock()
        self._uploadsOutstanding = 0
        self._uploader = None
        self._uploadThread = None
        self._stuckThreads = []
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
        """ Defensive cleanup — real lifecycle must be explicit via finalize(). """
        try:
            self.finalize()
        except Exception:
            pass
        if hasattr(super(), '__del__'):
            super().__del__()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    def getDeviceDescriptorSpec(self):
        return DeviceDescriptorSpec(
            hardware_id=HardwareDeviceId('infrastructure', 'triggerscope'),
            display_name='TriggerScope',
            category='infrastructure',
            section=DeviceSection.INFRASTRUCTURE,
            dependencies=(
                DeviceDependencySpec(
                    DeviceRelationKind.USES_TRANSPORT,
                    target=DeviceId('rs232', self._rs232Name),
                    label=self._rs232Name,
                ),
            ),
        )

    @property
    def deviceInfo(self):
        """ Read-only view of the device registry built from SetupInfo. """
        return self._deviceInfo

    # ------------------------------------------------------------------
    # Board primitives
    # ------------------------------------------------------------------

    def setParameter(self, parameterName, parameterValue):
        """ Send a PARAMETER,name,value command and wait for board to process.

        Blocks the calling thread for ``setParSleepTime``; scan uploads go
        through :meth:`uploadCommands` instead so the GUI thread never sleeps.
        """
        command, pauseS = self.parameterCommand(parameterName, parameterValue)
        self.send(command)
        time.sleep(pauseS)

    def parameterCommand(self, parameterName, parameterValue):
        """ ``(command, pauseAfterS)`` of one PARAMETER line. """
        return (f'PARAMETER,{parameterName},{parameterValue}\n',
                self.setParSleepTime)

    def send(self, command):
        """ Write a raw command string to the TriggerScope serial port.

        While an upload is queued or running the command joins the same queue,
        so it reaches the board after the upload, exactly as it did when the
        upload held the calling thread; otherwise it is written directly.
        """
        with self._uploadLock:
            if self._uploadsOutstanding:
                self._enqueueLocked(UploadJob([(command, 0)], label='send'))
                return
        self._rs232manager.write(command)

    def uploadCommands(self, commands, onFinished=None, label=''):
        """ Write ``[(command, pauseAfterS), ...]`` in order on the uploader
        thread and return at once.

        ``onFinished(ok, message)`` runs later on the manager's thread. The
        returned :class:`UploadJob` can be cancelled; a cancelled job sends no
        further command. Jobs run one after another in request order.
        """
        job = UploadJob(commands, onFinished, label)
        with self._uploadLock:
            self._enqueueLocked(job)
        return job

    def _enqueueLocked(self, job):
        self._ensureUploader()
        self._uploadsOutstanding += 1
        self._sigUploadRequested.emit(job)

    def _ensureUploader(self):
        if self._uploader is not None:
            return
        self._uploader = _CommandUploader(
            self._rs232manager.write, self._jobWritten)
        self._uploadThread = Thread()
        self._uploader.moveToThread(self._uploadThread)
        self._sigUploadRequested.connect(self._uploader.runJob)
        self._uploader.sigJobDone.connect(self._onJobDone)
        self._uploadThread.start()

    def _jobWritten(self):
        # Runs on the uploader thread once a job has written its last command
        # (or stopped): from here a direct write cannot overtake it.
        with self._uploadLock:
            self._uploadsOutstanding -= 1

    def _onJobDone(self, job, ok, message):
        if not ok and job.label != 'send':
            self.__logger.error(
                'TriggerScope upload %r did not complete: %s', job.label, message)
        if job.onFinished is not None:
            try:
                job.onFinished(ok, message)
            except Exception:
                self.__logger.error(
                    'TriggerScope upload completion handler failed',
                    exc_info=True)

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

    def finalize(self):
        """ Idempotent shutdown: stop and wait for the serial monitor thread.
        Safe to call multiple times and safe if the monitor was never started. """
        uploadThread = getattr(self, '_uploadThread', None)
        if uploadThread is not None:
            self._uploadThread = None
            uploadThread.quit()
            if not uploadThread.wait(self.SHUTDOWN_WAIT_MS):
                self.__logger.warning(
                    'TriggerScope uploader thread did not stop within %d ms',
                    self.SHUTDOWN_WAIT_MS)
                # A QThread destroyed while still running aborts the process;
                # keep it alive until the blocked write returns and it exits.
                self._stuckThreads.append(uploadThread)
        if hasattr(self, '_monitoring') and self._monitoring:
            if hasattr(self, '_thread'):
                self._thread.quit()
                # Bounded: the GUI thread must not hang on a stuck port.
                if self._thread.wait(self.SHUTDOWN_WAIT_MS) is False:
                    self.__logger.warning(
                        'TriggerScope serial monitor did not stop within '
                        '%d ms; closing the port anyway', self.SHUTDOWN_WAIT_MS)
            self._monitoring = False

    def closeMonitor(self):
        """ Legacy method — now delegates to finalize() for proper stop+wait. """
        self.finalize()

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _unknownMsg(self, msg):
        # repr() so any stray/invisible control characters (e.g. a trailing
        # '\r') are visible in the log and don't masquerade as a clean string.
        self.__logger.debug(f'[TriggerScope serial] {msg!r}')


class SerialMonitor(Worker):
    """ Background worker that polls the TriggerScope serial port for status
    messages (``"Scan done"``, ``"MSG<n>"``, and anything else). """

    sigScanDone = Signal()
    sigTrigMSG = Signal(int)
    sigUnknownMessage = Signal(str)

    def __init__(self, rs232Manager, updatePeriod):
        super().__init__()
        self.__logger = initLogger(self)
        self._rs232Manager = rs232Manager
        self._updatePeriod = updatePeriod
        self._vtimer = None
        # Bytes of a line that has begun and not ended yet (readWaiting path).
        self._partial = ''
        # Diagnostic: throttle repeated read-timeout log lines so a quiet port
        # doesn't spam the log, while still proving the monitor thread is alive.
        self._emptyReads = 0

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
            # The port's read() waits for the configured termination and
            # RS232Manager holds its I/O lock through the wait, so every DAC,
            # TTL and PARAMETER write waited for the port's timeout (100 s
            # here) whenever the termination did not come: on an idle port,
            # and -- the Snouty rig, 2026-10-20 -- after every reply of a
            # board that ends its lines with CR LF on a port configured for
            # CR, which left an LF behind each time. Take what has arrived,
            # no more, and split the lines here on CR or LF: nothing can make
            # that wait. A backend that cannot do it (None) is read as before.
            chunk = self._rs232Manager.readWaiting()
            if chunk is None:
                msg = self._rs232Manager.read()
                lines = [] if msg is None else [msg]
            else:
                self._partial += chunk
                *lines, self._partial = re.split(r'[\r\n]', self._partial)
        except (OSError, AttributeError):
            # The serial resource was closed during application shutdown while
            # a timer callback was still pending. Stop the timer and exit
            # quietly — this is expected during teardown, not a real error.
            if self._vtimer is not None:
                self._vtimer.stop()
            return
        except (VisaIOError, InvalidSession, SerialException, TypeError) as exc:
            # A read timeout (the read() path only): normal on an idle port
            # there. Log it occasionally so we can confirm the monitor thread
            # is alive but receiving nothing — which would point at a
            # recv_termination mismatch rather than a code bug.
            self._emptyReads += 1
            if self._emptyReads <= 3 or self._emptyReads % 20 == 0:
                self.__logger.debug(
                    '[TriggerScope RX] read() returned nothing (%s); '
                    'empty-read count=%d',
                    type(exc).__name__, self._emptyReads,
                )
            lines = []

        for msg in lines:
            # Diagnostic: log exactly what came off the wire, with repr() so any
            # invisible terminators (\r, \n) are visible. This is how we tell
            # what the firmware actually sends at end-of-scan.
            self.__logger.debug('[TriggerScope RX] %r', msg)
            # Normalise whitespace before matching: an unstripped "Scan done\r"
            # fails the exact match below, so scanDone() never fires and the
            # scan/laser widgets stay stuck (laser left armed). strip() also
            # prevents the MSG branch from doing int("5\r") -> ValueError.
            msg = msg.strip()
            if not msg:
                continue
            if msg == 'Scan done':
                self.sigScanDone.emit()
            elif msg[:3] == 'MSG' and msg[3:].isdigit():
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
