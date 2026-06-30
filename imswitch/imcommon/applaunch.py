import logging
import os
import sys
import traceback

from qtpy import QtCore, QtGui, QtWidgets

from .model import dirtools, pythontools, initLogger
from .view.guitools import getBaseStyleSheet


def prepareApp(scale=None):
    """ This function must be called before any views are created.

    Args:
        scale: Optional factor to scale the entire UI by (e.g. 0.8 for 80%).
            If None, the ``IMSWITCH_UI_SCALE`` environment variable is used
            instead; if that is also unset, an explicitly-set ``QT_SCALE_FACTOR``
            is left untouched and the UI renders at its native size.
    """

    # Initialize exception handling
    pythontools.installExceptHook()

    # Set logging levels
    logging.getLogger('pyvisa').setLevel(logging.WARNING)

    # Apply UI scaling before the QApplication is created (must precede it to take effect).
    # Precedence: explicit `scale` arg > IMSWITCH_UI_SCALE env var > existing QT_SCALE_FACTOR.
    if scale is None:
        scale = os.environ.get('IMSWITCH_UI_SCALE')
    if scale is not None:
        try:
            scaleFactor = float(scale)
        except (TypeError, ValueError):
            initLogger('prepareApp').warning(f'Ignoring invalid UI scale {scale!r}')
        else:
            if scaleFactor > 0:
                os.environ['QT_SCALE_FACTOR'] = repr(scaleFactor)
            else:
                initLogger('prepareApp').warning(f'Ignoring non-positive UI scale {scaleFactor}')

    # Create app
    os.environ['IMSWITCH_FULL_APP'] = '1'  # Indicator that non-plugin version of ImSwitch is used
    os.environ['PYQTGRAPH_QT_LIB'] = 'PyQt5'  # Force Qt to use PyQt5
    os.environ['HDF5_USE_FILE_LOCKING'] = 'FALSE'  # Force HDF5 to not lock files
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)  # Fixes Napari issues
    QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_DisableHighDpiScaling, True) # proper scaling on Mac?
    #QtWidgets.QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
    app = QtWidgets.QApplication([])
    app.setWindowIcon(QtGui.QIcon(os.path.join(dirtools.DataFileDirs.Root, 'icon.png')))
    app.setStyleSheet(getBaseStyleSheet())
    return app


def launchApp(app, mainView, moduleMainControllers):
    """ Launches the app. The program will exit when the app is exited. """

    logger = initLogger('launchApp')

    # Show app
    mainView.showMaximized()
    mainView.show()
    exitCode = app.exec_()

    # Clean up
    for controller in moduleMainControllers:
        try:
            controller.closeEvent()
        except Exception:
            logger.error(f'Error closing {type(controller).__name__}')
            logger.error(traceback.format_exc())

    # Exit
    sys.exit(exitCode)


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
