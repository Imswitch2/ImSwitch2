from __future__ import annotations

from qtpy import QtWidgets


class DataWorkspaceFrame(QtWidgets.QFrame):
    """Unified data-management area plus one shared processing footer."""

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
        actions_frame,
        *args,
        show_session: bool = True,
        show_multidata: bool = True,
        show_actions: bool = True,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
        self.loadedFrame = loaded_frame
        self.sessionFrame = session_frame
        self.queueFrame = queue_frame
        self.multiDataFrame = multidata_frame
        self.actionsFrame = actions_frame

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs, 1)

        self.tabs.addTab(loaded_frame, "Loaded")
        self.tabs.addTab(session_frame, "Session")
        self.tabs.addTab(queue_frame, "Queue")
        self.tabs.addTab(multidata_frame, "Multi-data")
        self.tabs.setTabVisible(self.SESSION, bool(show_session))
        self.tabs.setTabVisible(self.MULTIDATA, bool(show_multidata))

        actions_frame.setVisible(bool(show_actions))
        layout.addWidget(actions_frame, 0)

        queue_frame.sigQueueCountChanged.connect(self.setQueueCount)
        queue_frame.sigWaitingSelectionStateChanged.connect(
            actions_frame.setQueueSelectionState
        )
        if hasattr(multidata_frame, 'sigCountChanged'):
            multidata_frame.sigCountChanged.connect(self.setMultiDataCount)
        if hasattr(loaded_frame, 'sigSelectionCountChanged'):
            loaded_frame.sigSelectionCountChanged.connect(
                lambda count: actions_frame.setSelectionCount('loaded', count)
            )
        if hasattr(session_frame, 'sigSelectionCountChanged'):
            session_frame.sigSelectionCountChanged.connect(
                lambda count: actions_frame.setSelectionCount('session', count)
            )
        if hasattr(session_frame, 'sigSessionActiveChanged'):
            session_frame.sigSessionActiveChanged.connect(actions_frame.setSessionActive)
        self.tabs.currentChanged.connect(self._tabChanged)
        self._tabChanged(self.tabs.currentIndex())

    def _tabChanged(self, index: int) -> None:
        context = {
            self.LOADED: 'loaded',
            self.SESSION: 'session',
            self.QUEUE: 'queue',
            self.MULTIDATA: 'multidata',
        }.get(int(index), 'loaded')
        self.actionsFrame.setContext(context)

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
        self.actionsFrame.setMultiDataCount(count)
