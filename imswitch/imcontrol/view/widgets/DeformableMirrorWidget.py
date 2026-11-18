import numpy as np
import pyqtgraph as pg
from qtpy import QtCore, QtWidgets

from .basewidgets import Widget


class DeformableMirrorWidget(Widget):
    """ The deformable mirrors of the setup: per mirror, its last command as
    bars over its actuators, the flat as dots, and buttons to send the flat,
    zeros, one actuator poke or a sum of modes. """

    sigFlatten = QtCore.Signal(str)
    sigZero = QtCore.Signal(str)
    sigReset = QtCore.Signal(str)
    sigSaveAsFlat = QtCore.Signal(str)
    sigPoke = QtCore.Signal(str, int, float)          # name, actuator, value
    sigApplyModes = QtCore.Signal(str, object)        # name, {mode: amplitude}
    sigLoadCommand = QtCore.Signal(str, str)          # name, path
    sigSaveCommand = QtCore.Signal(str, str)          # name, path

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._mirrors = {}
        self._layout = QtWidgets.QVBoxLayout()
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(8)
        self._layout.addStretch(1)
        self.setLayout(self._layout)

    def addMirror(self, name, actuatorCount, maxCommand, modeNumbers):
        box = QtWidgets.QGroupBox(name)
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(6, 6, 6, 6)

        status = QtWidgets.QLabel('')
        status.setWordWrap(True)
        grid.addWidget(status, 0, 0, 1, 4)

        plot = pg.PlotWidget()
        plot.setMinimumHeight(110)
        plot.setMenuEnabled(False)
        plot.setMouseEnabled(x=False, y=False)
        plot.hideButtons()
        plot.setLabel('bottom', 'Actuator')
        plot.setYRange(-maxCommand, maxCommand, padding=0.05)
        plot.setXRange(-0.5, actuatorCount - 0.5, padding=0)
        bars = pg.BarGraphItem(x=np.arange(actuatorCount), height=np.zeros(actuatorCount),
                               width=0.8, brush=pg.mkBrush(70, 130, 200))
        plot.addItem(bars)
        flatDots = pg.ScatterPlotItem(size=4, brush=pg.mkBrush(230, 160, 40))
        plot.addItem(flatDots)
        grid.addWidget(plot, 1, 0, 1, 4)

        summary = QtWidgets.QLabel('')
        grid.addWidget(summary, 2, 0, 1, 4)

        buttons = {}
        for column, (key, text, tip, signal) in enumerate((
                ('flat', 'Flat', 'Send the flat command', self.sigFlatten),
                ('zero', 'Zero', 'Send all zeros (not the flat)', self.sigZero),
                ('reset', 'Reset', "The driver's reset: all actuators to zero", self.sigReset),
                ('saveFlat', 'Use as flat', 'Make the current command the flat '
                 '(in memory; Save... writes it to a file)', self.sigSaveAsFlat))):
            button = QtWidgets.QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, s=signal: s.emit(name))
            grid.addWidget(button, 3, column)
            buttons[key] = button

        load = QtWidgets.QPushButton('Load...')
        load.setToolTip('Send a command read from a file (.csv, .txt, .npy, .xlsx)')
        load.clicked.connect(lambda: self._askLoad(name))
        save = QtWidgets.QPushButton('Save...')
        save.setToolTip('Write the current command to a file (.csv or .npy)')
        save.clicked.connect(lambda: self._askSave(name))
        grid.addWidget(load, 4, 0, 1, 2)
        grid.addWidget(save, 4, 2, 1, 2)
        buttons['load'], buttons['save'] = load, save

        pokeRow = QtWidgets.QHBoxLayout()
        pokeRow.addWidget(QtWidgets.QLabel('Actuator'))
        actuator = QtWidgets.QSpinBox()
        actuator.setRange(0, max(actuatorCount - 1, 0))
        pokeRow.addWidget(actuator)
        pokeRow.addWidget(QtWidgets.QLabel('push'))
        value = QtWidgets.QDoubleSpinBox()
        value.setRange(-maxCommand, maxCommand)
        value.setDecimals(3)
        value.setSingleStep(0.01)
        value.setValue(min(0.1, maxCommand))
        pokeRow.addWidget(value)
        poke = QtWidgets.QPushButton('Poke')
        poke.setToolTip('Send the flat with this push added to one actuator')
        poke.clicked.connect(lambda: self.sigPoke.emit(name, actuator.value(), value.value()))
        pokeRow.addWidget(poke)
        buttons['poke'] = poke
        grid.addLayout(pokeRow, 5, 0, 1, 4)

        modeSpins = {}
        if modeNumbers:
            modesBox = QtWidgets.QGroupBox('Modes (amplitude per unit row of the mode matrix)')
            modesLayout = QtWidgets.QGridLayout(modesBox)
            perRow = 4
            for index, mode in enumerate(modeNumbers):
                spin = QtWidgets.QDoubleSpinBox()
                spin.setRange(-100.0, 100.0)
                spin.setDecimals(3)
                spin.setSingleStep(0.01)
                row, column = divmod(index, perRow)
                modesLayout.addWidget(QtWidgets.QLabel(f'{mode}'), row, 2 * column,
                                      alignment=QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
                modesLayout.addWidget(spin, row, 2 * column + 1)
                modeSpins[mode] = spin
            apply = QtWidgets.QPushButton('Apply modes')
            apply.setToolTip('Send the flat plus these modes')
            apply.clicked.connect(lambda: self.sigApplyModes.emit(
                name, {mode: spin.value() for mode, spin in modeSpins.items()}))
            clear = QtWidgets.QPushButton('Clear')
            clear.clicked.connect(lambda: [spin.setValue(0.0) for spin in modeSpins.values()])
            last = (len(modeNumbers) - 1) // perRow + 1
            modesLayout.addWidget(apply, last, 0, 1, 4)
            modesLayout.addWidget(clear, last, 4, 1, 4)
            buttons['modes'] = apply
            grid.addWidget(modesBox, 6, 0, 1, 4)

        message = QtWidgets.QLabel('')
        message.setWordWrap(True)
        grid.addWidget(message, 7, 0, 1, 4)
        for button in buttons.values():
            button.setProperty('baseTip', button.toolTip())

        self._layout.insertWidget(self._layout.count() - 1, box)
        self._mirrors[name] = dict(box=box, status=status, bars=bars, flat=flatDots,
                                   summary=summary, buttons=buttons, modes=modeSpins,
                                   message=message, maxCommand=float(maxCommand))

    def setStatus(self, name, text):
        self._mirrors[name]['status'].setText(text)

    def setCommand(self, name, command, flat):
        mirror = self._mirrors[name]
        command = np.asarray(command, dtype=float)
        flat = np.asarray(flat, dtype=float)
        mirror['bars'].setOpts(height=command)
        mirror['flat'].setData(x=np.arange(flat.size), y=flat)
        if command.size:
            peak = float(np.max(np.abs(command)))
            fromFlat = float(np.sqrt(np.mean((command - flat) ** 2)))
            mirror['summary'].setText(
                f'Largest |value| {peak:.3f} of {mirror["maxCommand"]:g};  '
                f'RMS from the flat {fromFlat:.4f}')

    def setModeValues(self, name, amplitudes):
        for mode, spin in self._mirrors[name]['modes'].items():
            spin.blockSignals(True)
            spin.setValue(float(amplitudes.get(mode, 0.0)))
            spin.blockSignals(False)

    def setMirrorUsable(self, name, usable, reason=''):
        mirror = self._mirrors[name]
        for button in mirror['buttons'].values():
            button.setEnabled(usable)
            button.setToolTip(button.property('baseTip') if usable else reason)

    def showMessage(self, name, text, error=False):
        label = self._mirrors[name]['message']
        label.setStyleSheet('color: #c62828;' if error else '')
        label.setText(text)

    def _askLoad(self, name):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, f'Send a command to {name}', '',
            'Mirror commands (*.csv *.txt *.npy *.xlsx);;All files (*)')
        if path:
            self.sigLoadCommand.emit(name, path)

    def _askSave(self, name):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, f'Save the command of {name}', f'{name}_command.csv',
            'CSV (*.csv);;NumPy (*.npy)')
        if path:
            self.sigSaveCommand.emit(name, path)


# Copyright (C) 2020-2026 ImSwitch developers
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
