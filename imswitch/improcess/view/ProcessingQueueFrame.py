from __future__ import annotations

from qtpy import QtCore, QtWidgets


class ProcessingQueueFrame(QtWidgets.QFrame):
    """View/control surface for the existing shared Session processing queue."""

    sigPauseRequested = QtCore.Signal()
    sigResumeRequested = QtCore.Signal()
    sigSkipCurrentRequested = QtCore.Signal()
    sigQueueCountChanged = QtCore.Signal(int)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._paused = False
        self._lastSnapshot = {}
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        self.summaryLabel = QtWidgets.QLabel("Queue idle")
        self.summaryLabel.setStyleSheet("color: palette(mid);")
        root.addWidget(self.summaryLabel)

        self.queueList = QtWidgets.QTreeWidget()
        self.queueList.setColumnCount(3)
        self.queueList.setHeaderLabels(["Dataset", "State", "Origin"])
        self.queueList.setRootIsDecorated(False)
        self.queueList.setUniformRowHeights(True)
        self.queueList.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.queueList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.queueList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.queueList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        root.addWidget(self.queueList, 1)

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        self.pauseResumeBtn = QtWidgets.QPushButton("Pause")
        self.skipCurrentBtn = QtWidgets.QPushButton("Skip current")
        actions.addWidget(self.pauseResumeBtn)
        actions.addWidget(self.skipCurrentBtn)
        actions.addStretch(1)
        root.addLayout(actions)

        self.pauseResumeBtn.clicked.connect(self._pause_resume_clicked)
        self.skipCurrentBtn.clicked.connect(self.sigSkipCurrentRequested)
        self.setProcessingQueue({})

    def _pause_resume_clicked(self) -> None:
        if self._paused:
            self.sigResumeRequested.emit()
        else:
            self.sigPauseRequested.emit()

    @staticmethod
    def _value(value) -> str:
        return str(getattr(value, "value", value) or "")

    def setProcessingQueue(self, snapshot: dict | None) -> None:
        snapshot = dict(snapshot or {})
        self._lastSnapshot = snapshot
        self._paused = bool(snapshot.get("paused", False))
        current = snapshot.get("current")
        waiting = list(snapshot.get("waiting", ()) or ())
        waiting_count = int(snapshot.get("waiting_count", len(waiting)) or 0)

        self.queueList.clear()
        jobs = ([current] if current is not None else []) + waiting
        for job in jobs:
            item = QtWidgets.QTreeWidgetItem([
                str(getattr(job, "label", "")),
                self._value(getattr(job, "state", "")).upper(),
                self._value(getattr(job, "origin", "")).replace("_", " ").title(),
            ])
            self.queueList.addTopLevelItem(item)
        hidden = max(0, waiting_count - len(waiting))
        if hidden:
            self.queueList.addTopLevelItem(QtWidgets.QTreeWidgetItem([f"… {hidden} more", "", ""]))

        if current is not None:
            label = str(getattr(current, "label", "processing"))
            summary = f"▶ {label} · processing"
            if waiting_count:
                summary += f" · {waiting_count} waiting"
        elif waiting_count:
            summary = f"{waiting_count} waiting"
            if self._paused:
                summary += " · paused"
        else:
            summary = "Queue paused" if self._paused else "Queue idle"
        self.summaryLabel.setText(summary)

        self.pauseResumeBtn.setText("Resume" if self._paused else "Pause")
        self.pauseResumeBtn.setEnabled(bool(current or waiting_count or self._paused))
        self.skipCurrentBtn.setEnabled(bool(current))
        self.sigQueueCountChanged.emit(waiting_count + (1 if current is not None else 0))
