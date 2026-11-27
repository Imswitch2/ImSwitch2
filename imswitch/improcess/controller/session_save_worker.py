"""Background persistence worker for Processing Session auto-saves."""

from __future__ import annotations

from qtpy import QtCore


class SessionSaveWorker(QtCore.QObject):
    """Persist one Session result away from the GUI thread.

    The worker owns no Session mutation. It only performs the potentially slow
    ``result.save`` call through a preconfigured ``SessionResultStore`` and
    returns the receipt to the GUI thread, where the Session catalog is updated.
    """

    succeeded = QtCore.Signal(object, str, str, object)
    failed = QtCore.Signal(object, str, str, str)

    def __init__(self, store, result, dataset, display_name: str = "", parent=None):
        super().__init__(parent)
        self._store = store
        self._result = result
        self._dataset = dataset
        self._dataset_id = str(dataset.id)
        self._display_name = str(display_name or "")

    @QtCore.Slot()
    def run(self) -> None:
        try:
            receipt = self._store.save(self._result, self._dataset, self._display_name)
        except Exception as exc:
            self.failed.emit(
                self._result,
                self._dataset_id,
                self._display_name,
                str(exc),
            )
            return
        self.succeeded.emit(
            self._result,
            self._dataset_id,
            self._display_name,
            receipt,
        )


__all__ = ["SessionSaveWorker"]
