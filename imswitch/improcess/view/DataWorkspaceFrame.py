from __future__ import annotations

from qtpy import QtWidgets


class DataWorkspaceFrame(QtWidgets.QFrame):
    """Hosts the four data-management views without duplicating their logic."""

    LOADED = 0
    SESSION = 1
    QUEUE = 2
    MULTIDATA = 3

    def __init__(
        self,
        loaded_frame,
        session_frame,
        queue_frame,
        multidata_frame,
        *args,
        show_session: bool = True,
        show_multidata: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)

        self.tabs.addTab(loaded_frame, "Loaded")
        self.tabs.addTab(session_frame, "Session")
        self.tabs.addTab(queue_frame, "Queue")
        self.tabs.addTab(multidata_frame, "Multi-data")
        self.tabs.setTabVisible(self.SESSION, bool(show_session))
        self.tabs.setTabVisible(self.MULTIDATA, bool(show_multidata))

        queue_frame.sigQueueCountChanged.connect(self.setQueueCount)
        if hasattr(multidata_frame, 'sigCountChanged'):
            multidata_frame.sigCountChanged.connect(self.setMultiDataCount)

    def selectLoaded(self) -> None:
        self.tabs.setCurrentIndex(self.LOADED)

    def selectSession(self) -> None:
        self.tabs.setCurrentIndex(self.SESSION)

    def selectQueue(self) -> None:
        self.tabs.setCurrentIndex(self.QUEUE)

    def selectMultiData(self) -> None:
        self.tabs.setCurrentIndex(self.MULTIDATA)

    def setQueueCount(self, count: int) -> None:
        count = max(0, int(count or 0))
        self.tabs.setTabText(self.QUEUE, f"Queue ({count})" if count else "Queue")

    def setMultiDataCount(self, count: int) -> None:
        count = max(0, int(count or 0))
        self.tabs.setTabText(
            self.MULTIDATA, f"Multi-data ({count})" if count else "Multi-data"
        )
