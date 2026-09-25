"""Tools > Memory limits…: the per-machine memory settings, edited in place.

The three limits live in ``imcontrol_options.json`` (``Options.memory``)
because they describe the computer, not the microscope; see
:mod:`imswitch.imcommon.model.memory_limits`. This dialog shows what is in
force, lets it be changed, and hands the values to the controller, which
saves them and adopts them at once -- no restart. What each number bounds is
said next to it, so nobody has to open the docs to decide.
"""

from qtpy import QtCore, QtWidgets

from imswitch.imcommon.model import memory_limits

#: (option field, label, what it bounds). Order is the order shown.
FIELDS = (
    ('writerQueueMB', 'Recording writer queue',
     'Backlog the recording writer may hold before acquisition waits for the '
     'disk. Waiting is graceful: frames meanwhile queue at the detector.'),
    ('perDetectorQueueMB', 'Each detector queue',
     'Backlog any one detector queue may hold for one consumer (a recording, '
     'BeadRec, a workflow) before that consumer\'s data is declared '
     'incomplete. One delivery larger than this is still accepted.'),
    ('processingWorkingSetMB', 'ImProcess working set',
     'Memory ImProcess may spend on work nobody asked for: contrast sampling '
     'and the mean preview on load. Above it the preview shows the first '
     'plane and large opens are announced first.'),
)

#: Largest value offered, in MiB (1 TiB).
MAX_MIB = 1024 * 1024


class MemoryLimitsDialog(QtWidgets.QDialog):
    """Edit the three memory limits; the controller saves and applies them."""

    #: ({option field: MiB}) -- the operator asked to save these values.
    sigSaveRequested = QtCore.Signal(dict)

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self.setWindowTitle('Memory limits')
        self.setModal(True)

        from imswitch.imcontrol.model.Options import MemoryOptions
        self._defaults = {field: int(getattr(MemoryOptions(), field))
                          for field, _label, _help in FIELDS}

        intro = QtWidgets.QLabel(
            'How much memory ImSwitch may use on this computer for buffering '
            'and automatic work. Stored in imcontrol_options.json, not in the '
            'setup file, and applied as soon as it is saved. None of these '
            'caps the whole application: camera drivers keep their own '
            'buffers, and datasets are as large as the data.'
        )
        intro.setWordWrap(True)

        form = QtWidgets.QFormLayout()
        self._spinBoxes = {}
        for field, label, help_text in FIELDS:
            spin = QtWidgets.QSpinBox()
            spin.setRange(1, MAX_MIB)
            spin.setSuffix(' MiB')
            spin.setSingleStep(64)
            spin.setToolTip(help_text)
            spin.setObjectName(field)
            self._spinBoxes[field] = spin
            hint = QtWidgets.QLabel(
                f'{help_text} Default {self._defaults[field]} MiB.'
            )
            hint.setWordWrap(True)
            hint.setEnabled(False)
            column = QtWidgets.QVBoxLayout()
            column.addWidget(spin)
            column.addWidget(hint)
            form.addRow(f'{label}:', column)

        self.statusLabel = QtWidgets.QLabel()
        self.statusLabel.setWordWrap(True)

        self.buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel,
            QtCore.Qt.Horizontal, self,
        )
        self.restoreButton = self.buttons.addButton(
            'Restore defaults', QtWidgets.QDialogButtonBox.ResetRole
        )
        self.restoreButton.clicked.connect(self.restoreDefaults)
        self.buttons.accepted.connect(self._requestSave)
        self.buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.statusLabel)
        layout.addWidget(self.buttons)
        self.setLayout(layout)
        self.resize(560, 0)

    # -- values ---------------------------------------------------------------

    def setValues(self, memoryOptions) -> None:
        """Show what the options file holds.

        A value the file holds but cannot be honoured (``"lots"``, ``2.5``) is
        shown as the default that is in force instead, and said so: saving
        then replaces it.
        """
        unusable = []
        for field, label, _help in FIELDS:
            raw = getattr(memoryOptions, field, None) if memoryOptions is not None else None
            mib = memory_limits.wholeMib(raw)
            if mib is None:
                if raw is not None:
                    unusable.append(f'{label} ({raw!r})')
                mib = self._defaults[field]
            self._spinBoxes[field].setValue(min(int(mib), MAX_MIB))
        self.setStatus(
            'Not usable in the options file, so the default is in force and '
            'shown: ' + ', '.join(unusable) + '.' if unusable else ''
        )

    def values(self) -> dict:
        return {field: int(spin.value()) for field, spin in self._spinBoxes.items()}

    def restoreDefaults(self) -> None:
        for field, spin in self._spinBoxes.items():
            spin.setValue(self._defaults[field])
        self.setStatus('Defaults restored; Save to keep them.')

    def setStatus(self, text: str) -> None:
        self.statusLabel.setText(str(text or ''))
        self.statusLabel.setVisible(bool(text))

    def _requestSave(self) -> None:
        # Not accept(): the controller closes the dialog once the values are
        # saved, and leaves it open with the reason when they cannot be.
        self.sigSaveRequested.emit(self.values())
