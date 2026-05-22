from qtpy import QtCore, QtWidgets


class BetterDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    """ QDoubleSpinBox that can optionally ignore mouse scroll wheel changes. """

    def __init__(self, *args, allowScrollChanges=True, **kwargs):
        super().__init__(*args, **kwargs)
        self._allowScrollChanges = allowScrollChanges
        self.installEventFilter(self)

    def eventFilter(self, source, event):
        if not self._allowScrollChanges and source is self and event.type() == QtCore.QEvent.Wheel:
            return True
        return False
