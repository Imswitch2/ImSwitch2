"""Workspace-wide position-reference catalog and inspector."""

from __future__ import annotations

from typing import Any,Mapping,Sequence

from qtpy import QtCore,QtWidgets


class PositionReferenceManagerView(QtWidgets.QWidget):
    """Inspect and manage the workspace-wide named position-reference store."""

    sigPersistRequested = QtCore.Signal(str,str)
    sigDeleteRequested = QtCore.Signal(str,str)

    def __init__(self,parent: QtWidgets.QWidget | None=None) -> None:
        super().__init__(parent)
        self._rows: dict[tuple[str,str],dict[str,Any]]={}
        self._mutations_enabled=True
        self._mutation_disabled_reason=""

        root=QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0,6,0,0)
        root.setSpacing(7)

        scope=QtWidgets.QLabel(
            "Workspace scope · references are shared across all loaded SLMs and grouped by observation plane."
        )
        scope.setWordWrap(True)
        scope.setStyleSheet("color: #777;")
        root.addWidget(scope)

        splitter=QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root.addWidget(splitter,1)

        left=QtWidgets.QWidget()
        left_layout=QtWidgets.QVBoxLayout(left)
        left_layout.setContentsMargins(0,0,6,0)
        self.table=QtWidgets.QTableWidget(0,7,left)
        self.table.setHorizontalHeaderLabels(
            ["Name","Observation plane","Lifetime","Source","Detector","Sites","Created"]
        )
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        left_layout.addWidget(self.table,1)

        actions=QtWidgets.QHBoxLayout()
        self.persist_button=QtWidgets.QPushButton("Keep permanently")
        self.delete_button=QtWidgets.QPushButton("Delete")
        actions.addWidget(self.persist_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        left_layout.addLayout(actions)
        splitter.addWidget(left)

        right=QtWidgets.QWidget()
        right_layout=QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(6,0,0,0)
        title=QtWidgets.QLabel("Reference details")
        font=title.font(); font.setPointSize(max(font.pointSize()+2,11)); font.setBold(True); title.setFont(font)
        right_layout.addWidget(title)
        self.details=QtWidgets.QLabel("Select a reference.")
        self.details.setAlignment(QtCore.Qt.AlignTop|QtCore.Qt.AlignLeft)
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        right_layout.addWidget(self.details,1)
        splitter.addWidget(right)
        splitter.setSizes([760,420])

        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.persist_button.clicked.connect(self._persist_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self._refresh_actions()

    def selected_key(self) -> tuple[str,str] | None:
        row=self.table.currentRow()
        if row < 0:
            return None
        item=self.table.item(row,0)
        if item is None:
            return None
        value=item.data(QtCore.Qt.UserRole)
        if not isinstance(value,tuple) or len(value)!=2:
            return None
        return str(value[0]),str(value[1])

    def set_records(self,rows: Sequence[Mapping[str,Any]]) -> None:
        selected=self.selected_key()
        self._rows={}
        blocker=QtCore.QSignalBlocker(self.table)
        self.table.setRowCount(len(rows))
        selected_row=-1
        for row_index,raw in enumerate(rows):
            row=dict(raw)
            plane=str(row.get("observation_plane") or "")
            name=str(row.get("name") or "")
            key=(plane,name)
            self._rows[key]=row
            values=(
                name,
                plane,
                str(row.get("lifetime") or ""),
                str(row.get("source_label") or "—"),
                str(row.get("detector") or "—"),
                str(row.get("sites") or "—"),
                str(row.get("created") or "—"),
            )
            for column,text in enumerate(values):
                item=QtWidgets.QTableWidgetItem(text)
                if column==0:
                    item.setData(QtCore.Qt.UserRole,key)
                if column==3:
                    item.setToolTip(str(row.get("source_detail") or ""))
                self.table.setItem(row_index,column,item)
            if key==selected:
                selected_row=row_index
        del blocker
        self.table.resizeColumnsToContents()
        if selected_row < 0 and self.table.rowCount():
            selected_row=0
        if selected_row >= 0:
            self.table.selectRow(selected_row)
        else:
            self.table.clearSelection()
        self._selection_changed()

    def set_mutations_enabled(self,enabled: bool,reason: str="") -> None:
        self._mutations_enabled=bool(enabled)
        self._mutation_disabled_reason=str(reason or "")
        self._refresh_actions()

    def _selection_changed(self) -> None:
        key=self.selected_key()
        row={} if key is None else self._rows.get(key,{})
        self.details.setText(
            str(row.get("details") or "Select a reference.")
        )
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        key=self.selected_key()
        row={} if key is None else self._rows.get(key,{})
        lifetime=str(row.get("lifetime") or "").lower()
        selected=key is not None
        persist_enabled=selected and lifetime=="session" and bool(row.get("persistence_available",False))
        delete_enabled=selected
        self.persist_button.setEnabled(self._mutations_enabled and persist_enabled)
        self.delete_button.setEnabled(self._mutations_enabled and delete_enabled)
        reason="" if self._mutations_enabled else self._mutation_disabled_reason
        self.persist_button.setToolTip(reason)
        self.delete_button.setToolTip(reason)

    def _persist_selected(self) -> None:
        key=self.selected_key()
        if key is not None:
            self.sigPersistRequested.emit(key[0],key[1])

    def _delete_selected(self) -> None:
        key=self.selected_key()
        if key is not None:
            self.sigDeleteRequested.emit(key[0],key[1])


__all__=["PositionReferenceManagerView"]
