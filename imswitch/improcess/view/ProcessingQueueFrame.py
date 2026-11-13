from __future__ import annotations

from qtpy import QtCore, QtWidgets

from imswitch.improcess.model.timepoint_scope import TimepointScope


class ProcessingQueueFrame(QtWidgets.QFrame):
    """View surface for the shared ImProcess processing queue."""

    sigPauseRequested = QtCore.Signal()
    sigResumeRequested = QtCore.Signal()
    sigSkipCurrentRequested = QtCore.Signal()
    sigRemoveWaitingRequested = QtCore.Signal(object)
    sigMoveWaitingRequested = QtCore.Signal(object, int)
    sigQueueCountChanged = QtCore.Signal(int)
    sigWaitingSelectionStateChanged = QtCore.Signal(bool, bool, bool)

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
        self.summaryLabel.hide()

        self.queueList = QtWidgets.QTreeWidget()
        self.queueList.setColumnCount(3)
        self.queueList.setHeaderLabels(["Dataset", "State", "Origin"])
        # Explicit section styling wins over theme-level QHeaderView fonts.
        self.queueList.header().setStyleSheet(
            "QHeaderView::section { font-size: 7pt; font-weight: normal; "
            "padding: 0px 3px; }"
        )
        self.queueList.setRootIsDecorated(False)
        self.queueList.setUniformRowHeights(True)
        self.queueList.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.queueList.header().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
        self.queueList.header().setSectionResizeMode(1, QtWidgets.QHeaderView.ResizeToContents)
        self.queueList.header().setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        root.addWidget(self.queueList, 1)

        actions = QtWidgets.QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)
        self.pauseResumeBtn = QtWidgets.QPushButton("Pause")
        self.skipCurrentBtn = QtWidgets.QPushButton("Skip current")
        self.removeWaitingBtn = QtWidgets.QPushButton("Remove")
        self.removeWaitingBtn.setToolTip("Remove selected waiting job(s) from the queue")
        self.moveUpBtn = QtWidgets.QToolButton()
        self.moveUpBtn.setText("↑")
        self.moveUpBtn.setToolTip("Move selected waiting job(s) earlier")
        self.moveDownBtn = QtWidgets.QToolButton()
        self.moveDownBtn.setText("↓")
        self.moveDownBtn.setToolTip("Move selected waiting job(s) later")
        actions.addWidget(self.pauseResumeBtn)
        actions.addWidget(self.skipCurrentBtn)
        actions.addWidget(self.removeWaitingBtn)
        actions.addWidget(self.moveUpBtn)
        actions.addWidget(self.moveDownBtn)
        actions.addStretch(1)
        self.pauseResumeBtn.hide()
        self.skipCurrentBtn.hide()
        self.removeWaitingBtn.hide()
        self.moveUpBtn.hide()
        self.moveDownBtn.hide()

        self.pauseResumeBtn.clicked.connect(self._pause_resume_clicked)
        self.skipCurrentBtn.clicked.connect(self.sigSkipCurrentRequested)
        self.removeWaitingBtn.clicked.connect(self._remove_selected_waiting)
        self.moveUpBtn.clicked.connect(
            lambda: self.sigMoveWaitingRequested.emit(self._selected_waiting_ids(), -1)
        )
        self.moveDownBtn.clicked.connect(
            lambda: self.sigMoveWaitingRequested.emit(self._selected_waiting_ids(), 1)
        )
        self.queueList.itemSelectionChanged.connect(self._update_waiting_actions)
        self.setProcessingQueue({})

    def _pause_resume_clicked(self) -> None:
        if self._paused:
            self.sigResumeRequested.emit()
        else:
            self.sigPauseRequested.emit()

    @staticmethod
    def _value(value) -> str:
        return str(getattr(value, "value", value) or "")

    @staticmethod
    def _job_label(job) -> str:
        label = str(getattr(job, "label", "") or "Data")
        total = getattr(job, "timepoint_total", None)
        if total:
            try:
                scope = getattr(job, "timepoint_scope", TimepointScope.all())
                label = f"{label} · {scope.label(total)}"
            except Exception:
                pass
        return label

    def _selected_waiting_ids(self) -> tuple[str, ...]:
        out = []
        for item in self.queueList.selectedItems():
            if item.data(0, QtCore.Qt.UserRole + 1) != "waiting":
                continue
            job_id = item.data(0, QtCore.Qt.UserRole)
            if job_id:
                out.append(str(job_id))
        return tuple(out)

    def _remove_selected_waiting(self) -> None:
        ids = self._selected_waiting_ids()
        if ids:
            self.sigRemoveWaitingRequested.emit(ids)

    def _update_waiting_actions(self) -> None:
        selected = set(self._selected_waiting_ids())
        waiting = list((self._lastSnapshot or {}).get("waiting", ()) or ())
        waiting_ids = [str(getattr(job, "id", "")) for job in waiting]
        indices = [index for index, job_id in enumerate(waiting_ids) if job_id in selected]
        has_selection = bool(indices)
        can_up = bool(indices) and min(indices) > 0
        can_down = bool(indices) and max(indices) < len(waiting_ids) - 1
        self.removeWaitingBtn.setEnabled(has_selection)
        self.moveUpBtn.setEnabled(can_up)
        self.moveDownBtn.setEnabled(can_down)
        self.sigWaitingSelectionStateChanged.emit(has_selection, can_up, can_down)


    def requestPauseResume(self) -> None:
        self._pause_resume_clicked()

    def requestSkipCurrent(self) -> None:
        if (self._lastSnapshot or {}).get("current") is not None:
            self.sigSkipCurrentRequested.emit()

    def requestRemoveSelected(self) -> None:
        self._remove_selected_waiting()

    def requestMoveSelected(self, direction: int) -> None:
        ids = self._selected_waiting_ids()
        if ids and int(direction) in {-1, 1}:
            self.sigMoveWaitingRequested.emit(ids, int(direction))

    def setProcessingQueue(self, snapshot: dict | None) -> None:
        selected_ids = set(self._selected_waiting_ids())
        snapshot = dict(snapshot or {})
        self._lastSnapshot = snapshot
        self._paused = bool(snapshot.get("paused", False))
        current = snapshot.get("current")
        waiting = list(snapshot.get("waiting", ()) or ())
        waiting_count = int(snapshot.get("waiting_count", len(waiting)) or 0)

        self.queueList.clear()
        jobs = ([current] if current is not None else []) + waiting
        for job in jobs:
            state = self._value(getattr(job, "state", ""))
            origin = self._value(getattr(job, "origin", ""))
            source = str(getattr(job, "source", "session") or "session")
            origin_text = "Auto" if origin == "auto" else source.replace("_", " ").title()
            pending = str(getattr(job, "readiness_status", "") or "")
            state_text = ("RECONSTRUCTING" if state in {"processing", "running"}
                          else "WAITING FOR ACQUISITION" if state == "waiting" and pending
                          else state.upper())
            item = QtWidgets.QTreeWidgetItem([
                self._job_label(job), state_text, origin_text,
            ])
            if pending:
                item.setToolTip(1, pending)
            job_id = str(getattr(job, "id", ""))
            item.setData(0, QtCore.Qt.UserRole, job_id)
            item.setData(0, QtCore.Qt.UserRole + 1, state)
            if state != "waiting":
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.queueList.addTopLevelItem(item)
            if state == "waiting" and job_id in selected_ids:
                item.setSelected(True)
        hidden = max(0, waiting_count - len(waiting))
        if hidden:
            item = QtWidgets.QTreeWidgetItem([f"… {hidden} more", "", ""])
            item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
            self.queueList.addTopLevelItem(item)

        if current is not None:
            label = self._job_label(current)
            summary = f"▶ {label} · reconstructing"
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
        self._update_waiting_actions()
        self.sigQueueCountChanged.emit(waiting_count + (1 if current is not None else 0))
