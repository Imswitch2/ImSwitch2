import threading

import numpy as np
from qtpy import QtCore

from imswitch.imcommon.model import APIExport
from imswitch.imcontrol.model.managers import LeasePurpose
from ..basecontrollers import ImConWidgetController

_Z_AXIS = 'Z'
_SETTLE_S = 0.15
_CLOSE_JOIN_TIMEOUT_S = 2.0
# How long a starting scan waits for autofocus to let go of the Z axis. Bounded
# because blocking a scan indefinitely on a wedged positioner call is worse than
# the brief overlap it prevents.
_SCAN_HANDOFF_TIMEOUT_S = 2.0


class AutofocusController(ImConWidgetController):
    """Linked to AutofocusWidget."""

    sigFocusDone = QtCore.Signal(float)  # optimal z position
    sigFocusStopped = QtCore.Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._focusAcqHandle = None
        self._focusThread = None
        self._focusCancel = threading.Event()
        self._focusLeaseLock = threading.Lock()
        self._closed = False
        self._focusing = False
        # Depth-counted like the focus lock's: workflows publish these signals
        # themselves, so a duplicate start must not be cleared by a single end.
        self._scanDepth = 0
        self._scanActive = False
        self._yieldActuator = False

        if self._setupInfo.autofocus is None:
            return

        self.camera = self._setupInfo.autofocus.camera
        self.positioner = self._setupInfo.autofocus.positioner

        # FOCUS lease instead of reaching past the DetectorsManager to the
        # sub-manager: the camera is now refcounted, so an unrelated global
        # stop can no longer disarm it underneath us. FOCUS leases are
        # excluded from the user-visible acquisition signals.
        self._focusAcqHandle = self._master.detectorsManager.acquire(
            [self.camera], LeasePurpose.FOCUS
        )

        self._widget.focusButton.clicked.connect(self._onFocusButton)
        self.sigFocusDone.connect(self._onFocusDone)
        self.sigFocusStopped.connect(self._onFocusStopped)

        self._commChannel.sigScanStarting.connect(self._onScanStarting)
        self._commChannel.sigScanEnded.connect(self._onScanEnded)

    def _onScanStarting(self):
        """A scan is about to take the hardware; hand the Z axis over.

        This is a handoff, not a request. Setting the cancel event and
        returning let the scan start while the worker was still mid-sweep --
        and its ``finally`` restores the starting Z, so cancelling could itself
        issue a move *after* the waveform was running. The worker is therefore
        told to abandon the axis where it stands, and this waits for it to
        actually leave before the scan proceeds.
        """
        if self.__dict__.get('_closed', False):
            return
        self._scanDepth = self.__dict__.get('_scanDepth', 0) + 1
        self._scanActive = True
        if not self.__dict__.get('_focusing', False):
            return

        self._logger.warning('Autofocus cancelled: a scan took the Z axis.')
        # Suppress the restore-to-start move: the scan owns the axis from here,
        # and its own return-to-center defines the final position.
        self._yieldActuator = True
        cancel = self.__dict__.get('_focusCancel')
        if cancel is not None:
            cancel.set()

        worker = self.__dict__.get('_focusThread')
        if worker is None or worker is threading.current_thread():
            return
        worker.join(_SCAN_HANDOFF_TIMEOUT_S)
        if worker.is_alive():
            # Bounded on purpose: blocking the scan indefinitely on a wedged
            # positioner call would be worse than the overlap it prevents.
            self._logger.error(
                'Autofocus did not release the Z axis within '
                f'{_SCAN_HANDOFF_TIMEOUT_S:g} s; the scan is starting anyway '
                'and the two may briefly drive it together.'
            )

    def _onScanEnded(self):
        if self.__dict__.get('_closed', False):
            return
        depth = self.__dict__.get('_scanDepth', 0)
        if depth <= 0:
            # Never clear on an end we did not see a start for.
            return
        self._scanDepth = depth - 1
        self._scanActive = self._scanDepth > 0

    def closeEvent(self) -> bool:
        self._closed = True

        comm = self.__dict__.get('_commChannel')
        if comm is not None:
            for signalName, slot in (
                ('sigScanStarting', self._onScanStarting),
                ('sigScanEnded', self._onScanEnded),
            ):
                signal = getattr(comm, signalName, None)
                if signal is not None:
                    try:
                        signal.disconnect(slot)
                    except Exception:
                        pass

        cancel = self.__dict__.get('_focusCancel')
        if cancel is not None:
            cancel.set()

        focusThread = self.__dict__.get('_focusThread')
        if focusThread is not None and focusThread is not threading.current_thread():
            focusThread.join(_CLOSE_JOIN_TIMEOUT_S)

        # Never disarm the camera while the worker may still be reading it.
        # If a backend call is blocked beyond the bounded GUI-close wait, the
        # worker releases the handle itself from its finally block.
        if focusThread is None or not focusThread.is_alive():
            self._releaseFocusLease()
        else:
            self._logger.warning(
                'Autofocus is still stopping; its camera lease will be '
                'released by the worker when the active hardware call returns.'
            )
        super().closeEvent()
        return self.shutdownComplete()

    def shutdownComplete(self) -> bool:
        """Return whether the autofocus worker and camera lease are gone."""
        focusThread = self.__dict__.get('_focusThread')
        return (
            (focusThread is None or not focusThread.is_alive())
            and self.__dict__.get('_focusAcqHandle') is None
            and not self.__dict__.get('_focusing', False)
        )

    def _releaseFocusLease(self):
        lock = self.__dict__.get('_focusLeaseLock')
        if lock is None:
            lock = threading.Lock()
            self._focusLeaseLock = lock
        with lock:
            handle = self.__dict__.get('_focusAcqHandle')
            if handle is None:
                return
            try:
                self._master.detectorsManager.release(handle)
            except Exception as e:
                self._logger.error(
                    f'Failed to release autofocus detector lease: {e}',
                    exc_info=True,
                )
            finally:
                self._focusAcqHandle = None

    def _onFocusButton(self):
        values = self._validateFocusInputs(
            self._widget.zStepRangeEdit.text(),
            self._widget.zStepSizeEdit.text(),
        )
        if values is None:
            self._onFocusStopped()
            return
        rangez, resolutionz = values
        self.autoFocus(rangez, resolutionz)

    def _validateFocusInputs(self, rangez, resolutionz):
        try:
            rangez = float(rangez)
            resolutionz = float(resolutionz)
        except (TypeError, ValueError):
            self._logger.warning(
                'Autofocus range and step size must be finite positive numbers.'
            )
            return None
        if (
            not np.isfinite(rangez)
            or not np.isfinite(resolutionz)
            or rangez <= 0
            or resolutionz <= 0
        ):
            self._logger.warning(
                'Autofocus range and step size must be finite positive numbers.'
            )
            return None
        return rangez, resolutionz

    @APIExport(runOnUIThread=True)
    def autoFocus(self, rangez: float = 100.0, resolutionz: float = 10.0) -> None:
        """Start an autofocus scan asynchronously.

        Scans +-rangez/2 around the current Z in steps of resolutionz,
        fits a parabola to the gradient-variance focus metric, and moves
        to the estimated peak.
        """
        values = self._validateFocusInputs(rangez, resolutionz)
        if values is None:
            if not self.__dict__.get('_closed', False):
                self._onFocusStopped()
            return
        rangez, resolutionz = values
        if self._focusing or self.__dict__.get('_closed', False):
            return
        if self.__dict__.get('_scanActive', False):
            # Autofocus sweeps Z from a worker thread. On a rig where it
            # targets the same actuator a scan is driving, that is the same
            # conflict the focus lock yields for.
            self._logger.warning(
                'Autofocus cannot start while a scan is running; it would '
                'drive the Z axis against the scan waveform.'
            )
            if not self.__dict__.get('_closed', False):
                self._onFocusStopped()
            return
        self._focusCancel.clear()
        self._yieldActuator = False
        self._focusing = True
        self._widget.focusButton.setText('Focusing...')
        self._widget.focusButton.setEnabled(False)

        self._focusThread = threading.Thread(
            target=self._runFocus,
            args=(rangez, resolutionz),
            daemon=True,
        )
        try:
            self._focusThread.start()
        except Exception as e:
            self._focusing = False
            self._logger.error(
                f'Autofocus worker could not start: {e}', exc_info=True
            )
            self._onFocusStopped()

    def _runFocus(self, rangez: float, resolutionz: float) -> None:
        positioner = None
        current_z = None
        completed = False
        try:
            positioner = self._master.positionersManager[self.positioner]
            detector = self._master.detectorsManager[self.camera]

            current_z = positioner.position[_Z_AXIS]
            n_intervals = max(2, int(np.ceil(rangez / resolutionz)))
            z_positions = np.linspace(
                current_z - rangez / 2,
                current_z + rangez / 2,
                n_intervals + 1,
            ).tolist()

            focus_vals = []
            for z in z_positions:
                if self._focusCancel.is_set():
                    return
                positioner.setPosition(z, _Z_AXIS)
                if self._focusCancel.wait(_SETTLE_S):
                    return
                img = detector.getLatestFrameShared().astype(np.float64)
                gx = np.diff(img, axis=1)
                gy = np.diff(img, axis=0)
                focus_vals.append(float(np.var(gx) + np.var(gy)))

            if self._focusCancel.is_set():
                return
            coeffs = np.polyfit(z_positions, focus_vals, 2)
            if coeffs[0] < 0:
                z_focus = float(-coeffs[1] / (2 * coeffs[0]))
                z_focus = float(np.clip(z_focus, min(z_positions), max(z_positions)))
            else:
                z_focus = z_positions[int(np.argmax(focus_vals))]

            if self._focusCancel.is_set():
                return
            positioner.setPosition(z_focus, _Z_AXIS)
            if self._focusCancel.is_set():
                return
            completed = True
            self._logger.debug(f'Autofocus: optimal Z = {z_focus:.2f}')

            if not self._closed:
                QtCore.QMetaObject.invokeMethod(
                    self, '_updatePlot',
                    QtCore.Qt.QueuedConnection,
                    QtCore.Q_ARG('PyQt_PyObject', z_positions),
                    QtCore.Q_ARG('PyQt_PyObject', focus_vals),
                )
                self.sigFocusDone.emit(z_focus)

        except Exception as e:
            self._logger.error(f'Autofocus failed: {e}', exc_info=True)
        finally:
            if self.__dict__.get('_yieldActuator', False):
                # A scan took the axis. Restoring our starting Z here would be
                # a move issued into a running waveform.
                self._logger.warning(
                    'Autofocus left the Z axis where the scan found it; its '
                    'starting position was not restored.'
                )
            elif (
                not completed
                and positioner is not None
                and current_z is not None
            ):
                try:
                    positioner.setPosition(current_z, _Z_AXIS)
                except Exception as e:
                    self._logger.error(
                        f'Autofocus could not restore the starting Z position: {e}',
                        exc_info=True,
                    )
            self._focusing = False
            if self._closed:
                self._releaseFocusLease()
            else:
                try:
                    self.sigFocusStopped.emit()
                except RuntimeError:
                    # Bare controller shells used by tests are not always
                    # initialized as QObjects. Production controllers are.
                    pass

    @QtCore.Slot(object, object)
    def _updatePlot(self, z_positions, focus_vals):
        self._widget.focusPlotCurve.setData(z_positions, focus_vals)

    @QtCore.Slot(float)
    def _onFocusDone(self, z_focus: float):
        if self._closed:
            return
        self._onFocusStopped()

    @QtCore.Slot()
    def _onFocusStopped(self):
        if self.__dict__.get('_closed', False):
            return
        self._widget.focusButton.setText('Autofocus')
        self._widget.focusButton.setEnabled(True)
        self._widget.focusButton.setChecked(False)


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
