from __future__ import annotations

import os
from typing import Mapping,Sequence

from qtpy import QtCore,QtGui,QtWidgets


def exec_dialog(dialog) -> int:
    return dialog.exec_() if hasattr(dialog,"exec_") else dialog.exec()


def confirm_destructive_change(parent,title: str,message: str) -> bool:
    result = QtWidgets.QMessageBox.question(
        parent,title,str(message),
        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
        QtWidgets.QMessageBox.Cancel,
    )
    return result == QtWidgets.QMessageBox.Yes


def request_save_as(parent,existing_stems):
    while True:
        dialog = QtWidgets.QDialog(parent)
        dialog.setWindowTitle("Save as...")
        layout = QtWidgets.QVBoxLayout(dialog)
        form = QtWidgets.QFormLayout()
        name = QtWidgets.QLineEdit()
        info = QtWidgets.QLineEdit()
        form.addRow("Config name:",name)
        form.addRow("Config info:",info)
        layout.addLayout(form)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if exec_dialog(dialog) != QtWidgets.QDialog.Accepted:
            return None
        value = str(name.text() or "").strip()
        if not value:
            return None
        stem = os.path.splitext(value)[0]
        if stem in set(existing_stems):
            QtWidgets.QMessageBox.critical(
                parent,"Save Config Error",
                "A configuration named '%s' already exists." % value,
            )
            continue
        return value,str(info.text() or "")


def request_name(parent,title: str,label: str,default: str="") -> str | None:
    value,ok = QtWidgets.QInputDialog.getText(
        parent,title,label,text=str(default or ""),
    )
    text = str(value or "").strip()
    return text if ok and text else None


def confirm_delete(parent,name: str) -> bool:
    return confirm_destructive_change(
        parent,"Delete config",
        "Are you sure you want to delete the configuration '%s'?" % name,
    )


def request_update_info(
    parent,config_name: str,changes: str,current_info: str="",
) -> str | None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle("Update SLM config")
    dialog.resize(560,380)
    layout = QtWidgets.QVBoxLayout(dialog)
    layout.addWidget(QtWidgets.QLabel("This will overwrite '%s'." % config_name))
    title = QtWidgets.QLabel("Changed values")
    font = QtGui.QFont(title.font()); font.setBold(True); title.setFont(font)
    layout.addWidget(title)
    changes_edit = QtWidgets.QPlainTextEdit()
    changes_edit.setReadOnly(True)
    changes_edit.setPlainText(str(changes))
    layout.addWidget(changes_edit,1)
    form = QtWidgets.QFormLayout()
    info = QtWidgets.QLineEdit(str(current_info or ""))
    form.addRow("Config info:",info)
    layout.addLayout(form)
    buttons = QtWidgets.QDialogButtonBox(
        QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
    )
    buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("Update")
    buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if exec_dialog(dialog) != QtWidgets.QDialog.Accepted:
        return None
    return str(info.text() or "")


def show_config_inspection(parent,title: str,details: Sequence[Mapping],selected_path=None) -> None:
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(760,460)
    main = QtWidgets.QHBoxLayout(dialog)
    config_list = QtWidgets.QListWidget(); config_list.setMinimumWidth(220)
    for detail in details:
        path = detail.get("path")
        if not path:
            continue
        item = QtWidgets.QListWidgetItem(str(detail.get("name") or os.path.basename(str(path))))
        item.setData(QtCore.Qt.UserRole,dict(detail))
        item.setToolTip(str(detail.get("tooltip") or ""))
        config_list.addItem(item)
    main.addWidget(config_list,0)
    right = QtWidgets.QWidget(); layout = QtWidgets.QVBoxLayout(right)
    name = QtWidgets.QLabel(); f=QtGui.QFont(name.font()); f.setBold(True); name.setFont(f)
    created=QtWidgets.QLabel(); info=QtWidgets.QLabel(); info.setWordWrap(True)
    warnings=QtWidgets.QLabel(); warnings.setWordWrap(True); warnings.setStyleSheet("color: #a66;")
    layout.addWidget(name)
    form=QtWidgets.QFormLayout(); form.addRow("Created:",created); form.addRow("Info:",info); form.addRow("Warnings:",warnings); layout.addLayout(form)
    dump=QtWidgets.QPlainTextEdit(); dump.setReadOnly(True); layout.addWidget(dump,1)
    buttons=QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
    main.addWidget(right,1)
    def update(index):
        item=config_list.item(index)
        detail={} if item is None else (item.data(QtCore.Qt.UserRole) or {})
        path=str(detail.get("path") or "")
        name.setText(os.path.basename(path) if path else "No configs")
        created.setText(str(detail.get("created_at") or ""))
        info.setText(str(detail.get("info") or ""))
        warnings.setText(str(detail.get("warnings") or ""))
        dump.setPlainText(str(detail.get("summary") or ""))
    config_list.currentRowChanged.connect(update)
    row=0
    if selected_path:
        target=os.path.normcase(os.path.abspath(str(selected_path)))
        for i in range(config_list.count()):
            detail=config_list.item(i).data(QtCore.Qt.UserRole) or {}
            if detail.get("path") and os.path.normcase(os.path.abspath(str(detail["path"]))) == target:
                row=i; break
    if config_list.count(): config_list.setCurrentRow(row)
    else: update(-1)
    exec_dialog(dialog)
