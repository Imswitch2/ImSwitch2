import sys

import numpy as np
from qtpy import QtCore, QtGui
from qtpy.QtWidgets import QLabel, QApplication

from imswitch.imcommon.framework import SignalInterface
from imswitch.imcommon.model import initLogger
from imswitch.imcontrol.model.devices.status import (
    DeviceConnectionState, DeviceManagerStatusMixin,
)


class HamamatsuSLMdviManager(DeviceManagerStatusMixin, SignalInterface):
    """Manager for communication with Hamamatsu SLM with dvi connection"""
    
    requires_device_connection: bool = False

    def __init__(self, slmInfo, slmName, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__logger = initLogger(self)

        self.slmName = slmName
        self.slmInfo = slmInfo
        self.width = slmInfo.width
        self.height = slmInfo.height
        self.__logger.debug(self.slmInfo)
        self.pixel_size = slmInfo.pixelSize

        self.preferredMonitor = slmInfo.monitorIdx

        self.mockermode = False
        if slmInfo.managerProperties is not None:
            self.mockermode = slmInfo.managerProperties.get("mockermode", False)

        if self.mockermode:
            self.__logger.info(
                f"SLM Manager {self.slmName} running in MOCKER MODE. No actual connection to SLM will be made.")
            self._setMockActive("DVI SLM mock mode configured")

        # prepare the qwidget

        self.SLMQLabel = QLabel()
        self.imgArr = np.random.randint(1, 250, size=(self.width, self.height), dtype=np.uint8)

        if self.init_slm_window():
            self._setConnectionState(
                DeviceConnectionState.NOT_APPLICABLE,
                summary="DVI SLM physical connection cannot be verified",
                details=(
                    f"Display output available at monitor index {self.preferredMonitor}; "
                    f"active geometry {self.width} x {self.height}"
                ),
            )

    def init_slm_window(self) -> bool:
        """Show the SLM QLabel full screen on the configured monitor.

        Returns False, without a window, when that monitor does not exist (the
        SLM display is unplugged or switched off, or the setup names the wrong
        index). The SLM is then reported as not found and ImSwitch starts
        without it instead of failing to load; patterns are computed and shown
        in the panel but reach no display.
        """

        app = QApplication.instance()

        screens = list(app.screens())

        if not 0 <= self.preferredMonitor < len(screens):
            available = ", ".join(
                f"{i}: {s.name()} {s.geometry().width()}x{s.geometry().height()}"
                for i, s in enumerate(screens)
            ) or "none"
            message = (
                f"SLM {self.slmName}: monitorIdx {self.preferredMonitor} does not exist "
                f"({len(screens)} display(s) connected: {available}). "
                f"Connect the SLM display, or set monitorIdx in the setup file, "
                f"and restart ImSwitch."
            )
            if self.mockermode:
                # Mock mode needs no display; keep the mock status.
                self.__logger.info(message)
            else:
                self.__logger.warning(message)
                self._setDeviceNotFound(message, summary="SLM display not connected")
            self._windowShown = False
            return False

        screen = screens[self.preferredMonitor]
        geo = screen.geometry()

        self.width = geo.width()
        self.height = geo.height()

        self.__logger.debug(f"Screen geometry: {self.width}x{self.height}")

        self.SLMQLabel.setWindowFlags(QtCore.Qt.Window)
        self.SLMQLabel.setWindowTitle("SLM display")

        self.SLMQLabel.setGeometry(geo)
        self.SLMQLabel.move(geo.topLeft())

        self.SLMQLabel.setScaledContents(False)
        self.SLMQLabel.setAlignment(QtCore.Qt.AlignCenter)

        self.SLMQLabel.showFullScreen()
        self._windowShown = True
        return True

    def upload_pattern(self, pattern, slot_no=0):
        """ Upload pattern to SLM """

        if self.mockermode:
            self.__logger.info("Mocker mode - not uploading pattern to device.")
            return

        if not self._windowShown:
            return

        pix = self._array_to_qpixmap(pattern)
        self.SLMQLabel.setPixmap(pix)

    def finalize(self):
        pass

    def connect_to_device(self):
        pass

    def close_device(self):
        pass


    def _array_to_qpixmap(self, arr2d: np.ndarray) -> QtGui.QPixmap:
        """
        Converts a numpy array to RGB888 QPixmap.
        """
        array = np.ascontiguousarray(arr2d, dtype=np.uint8)

        w, h = array.shape

        if h != self.height or w != self.width:
            self.__logger.warning(f"Pattern shape {array.shape} does not match SLM {self.width} x {self.height} shape")

            import skimage.transform
            array = skimage.transform.resize(array, (self.height, self.width), order=0, preserve_range=True)

            h = self.height
            w = self.width

        rgb = np.repeat(array[:, :, None], 3, axis=2)

        rgb = np.ascontiguousarray(rgb)

        qimg = QtGui.QImage(rgb.data, w, h, w * 3, QtGui.QImage.Format_RGB888).copy()

        return QtGui.QPixmap.fromImage(qimg)








