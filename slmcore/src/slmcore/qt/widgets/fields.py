"""Qt parameter editors driven directly by :mod:`slmcore.core.engine.parameters`."""

from __future__ import annotations

from typing import (
    Any,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)

from qtpy import QtCore,QtWidgets

from ...core.engine.parameters.spec import ParamDisplayLevel,ParamSpec
from ..application.interaction import ParameterCommitMode


class ParamField(QtWidgets.QWidget):
    """Editor for one keyed :class:`~slmcore.core.engine.parameters.ParamSpec`.

    The stored value is always canonical. Unit mode changes only display and
    editor interpretation.
    """

    sigValueChanged = QtCore.Signal(str,object)
    sigValidityChanged = QtCore.Signal(str,bool,str)

    _INVALID_STYLE = "QLineEdit { border: 1px solid #c44; }"

    def __init__(
        self,
        key: str,
        definition: ParamSpec,
        conversion_context: Callable[[], Any] | None=None,
        parent: QtWidgets.QWidget | None=None,
        editor_width: int=70,
        show_complementary: bool=False,
    ):
        super().__init__(parent)
        if not isinstance(key,str) or not key:
            raise ValueError("Parameter key must be a non-empty string")

        self._key = key
        self.definition = definition
        self.conversion_context = conversion_context or (lambda:None)

        converter = self.definition.converter
        self._canonical_unit = (
            converter.canonical_unit if converter is not None else None
        )
        self._unit_mode = self._canonical_unit
        self._canonical_value = definition.validate(definition.default)
        self._last_error = ""
        self._show_complementary = bool(show_complementary)
        self._updating_editor = False
        self._presentation_visible = True
        self._semantic_enabled = True
        self._interaction_enabled = True
        self._commit_mode = ParameterCommitMode.LIVE
        self._trailing_widget: QtWidgets.QWidget | None = None
        self._editor_cell: QtWidgets.QWidget | None = None

        self.label = QtWidgets.QLabel()
        self.editor = self._create_editor(editor_width)
        self.complementaryLabel = QtWidgets.QLabel()
        self.complementaryLabel.setStyleSheet("color: #888;")
        self.complementaryLabel.setVisible(self._show_complementary)

        # Keep fields hidden intially (not mounted in Qt parent yet)
        # They will be made visible when added to grid.
        self.label.hide()
        self.editor.hide()
        self.complementaryLabel.hide()

        self._connect_editor()
        self._render()


    @property
    def key(self) -> str:
        return self._key

    @property
    def unit_mode(self):
        return self._unit_mode
    
    @property
    def presentation_visible(self) -> bool:
        return self._presentation_visible

    @property
    def commit_mode(self) -> ParameterCommitMode:
        return self._commit_mode

    def set_commit_mode(self,mode: ParameterCommitMode) -> None:
        self._commit_mode = ParameterCommitMode(mode)

    def set_trailing_widget(self,widget: QtWidgets.QWidget | None) -> None:
        """Attach one Qt-only accessory immediately after the editor."""
        if self._editor_cell is not None:
            raise RuntimeError("Trailing widget must be set before field mounting")
        self._trailing_widget = widget
        if widget is not None:
            widget.hide()

    def set_semantic_enabled(self,enabled: bool) -> None:
        """Set backend/form-driven editor availability (for example links)."""
        self._semantic_enabled = bool(enabled)
        self._apply_editor_enabled()

    def set_interaction_enabled(self,enabled: bool) -> None:
        """Set temporary interaction availability without losing link state."""
        self._interaction_enabled = bool(enabled)
        self._apply_editor_enabled()

    def _apply_editor_enabled(self) -> None:
        self.editor.setEnabled(
            self._semantic_enabled and self._interaction_enabled
        )

    def set_presentation_visible(self,visible: bool) -> None:
        """Show/hide the field without changing its parameter definition."""
        self._presentation_visible = bool(visible)
        self._apply_presentation_visibility()

    def value(self) -> Any:
        return self._canonical_value

    def set_value(self,value: Any,emit: bool=False) -> None:
        canonical = self.definition.validate(value)
        changed = canonical != self._canonical_value
        self._canonical_value = canonical
        self._set_valid(True,"")
        self._render()
        if emit and changed:
            self.sigValueChanged.emit(self.key,canonical)

    def set_unit_mode(self,unit: str) -> None:
        converter = self.definition.converter
        if converter is None:
            return
        if unit not in converter.supported_units:
            raise ValueError(
                f"Unit '{unit}' is not supported for parameter '{self.key}'"
            )
        self._unit_mode = unit
        self._render()

    def refresh(self) -> None:
        self._render()

    def add_to_grid(
        self,layout: QtWidgets.QGridLayout,row: int,column: int,
    ) -> int:
        if self.definition.hidden:
            self.label.hide()
            self.editor.hide()
            self.complementaryLabel.hide()
            if self._trailing_widget is not None:
                self._trailing_widget.hide()
            return column

        alignment = QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
        if isinstance(self.editor,QtWidgets.QCheckBox):
            layout.addWidget(self.editor,row,column,1,1,alignment=alignment)
            return column + 1

        layout.addWidget(self.label,row,column,1,1,alignment=alignment)
        if self._trailing_widget is None:
            layout.addWidget(self.editor,row,column + 1,1,1,alignment=alignment)
        else:
            cell = QtWidgets.QWidget()
            cell_layout = QtWidgets.QHBoxLayout(cell)
            cell_layout.setContentsMargins(0,0,0,0)
            cell_layout.setSpacing(3)
            cell_layout.addWidget(self.editor)
            cell_layout.addWidget(self._trailing_widget)
            cell_layout.addStretch(1)
            self._editor_cell = cell
            layout.addWidget(cell,row,column + 1,1,1,alignment=alignment)
        next_column = column + 2

        if self._show_complementary:
            layout.addWidget(
                self.complementaryLabel,row,next_column,1,1,
                alignment=alignment,
            )
            next_column += 1

        # Make fields visible now that they have Qt parent.
        self._apply_presentation_visibility()
        return next_column

    def _editor_kind(self) -> str:
        editor = self.definition.editor
        if editor is None:
            if self.definition.ptype is bool:
                return "check_box"
            if self.definition.choices is not None:
                return "combo_box"
            return "line_edit"

        value = str(getattr(editor,"value",editor)).lower()
        aliases = {
            "lineedit":"line_edit",
            "spinbox":"spin_box",
            "int_spinbox":"spin_box",
            "double_spinbox":"double_spin_box",
            "doublespinbox":"double_spin_box",
            "checkbox":"check_box",
            "combo":"combo_box",
            "combobox":"combo_box",
        }
        return aliases.get(value,value)

    def _create_editor(self,editor_width: int) -> QtWidgets.QWidget:
        kind = self._editor_kind()
        label = self.definition.label_for(self.key)

        if kind == "check_box":
            editor = QtWidgets.QCheckBox(label)
        elif kind == "combo_box":
            editor = QtWidgets.QComboBox()
            if self.definition.allow_none:
                editor.addItem("None",None)
            for value,choice_label in zip(
                self.definition.choices or (),self.definition.display_choices,
            ):
                editor.addItem(choice_label,value)
            editor.setMinimumWidth(editor_width)
        elif kind == "spin_box":
            editor = (
                QtWidgets.QDoubleSpinBox()
                if self.definition.conversion_available
                else QtWidgets.QSpinBox()
            )
            editor.setKeyboardTracking(False)
            editor.setFixedWidth(editor_width)
        elif kind == "double_spin_box":
            editor = QtWidgets.QDoubleSpinBox()
            editor.setKeyboardTracking(False)
            editor.setFixedWidth(editor_width)
        elif kind == "line_edit":
            editor = QtWidgets.QLineEdit()
            editor.setFixedWidth(editor_width)
            editor.setAlignment(QtCore.Qt.AlignRight)
        else:
            raise ValueError(
                f"Unknown editor kind '{kind}' for parameter '{self.key}'"
            )

        if self.definition.tooltip:
            self.setToolTip(self.definition.tooltip)
            editor.setToolTip(self.definition.tooltip)
        return editor

    def _connect_editor(self) -> None:
        if isinstance(self.editor,QtWidgets.QLineEdit):
            self.editor.textEdited.connect(self._try_commit_editor)
            self.editor.editingFinished.connect(self._finish_line_edit)
        elif isinstance(self.editor,QtWidgets.QCheckBox):
            self.editor.toggled.connect(self._commit_editor)
        elif isinstance(self.editor,QtWidgets.QComboBox):
            self.editor.currentIndexChanged.connect(self._commit_editor)
        elif isinstance(
            self.editor,(QtWidgets.QSpinBox,QtWidgets.QDoubleSpinBox),
        ):
            self.editor.valueChanged.connect(self._commit_editor)
            self.editor.editingFinished.connect(self._finish_line_edit)
            line_edit = self.editor.lineEdit()
            if line_edit is not None:
                line_edit.textEdited.connect(self._try_commit_editor)

    def _require_conversion_context(self) -> Any:
        context = self.conversion_context()
        if context is None:
            raise RuntimeError(
                f"{self.key} requires a valid section conversion context"
            )
        return context

    def _converter(self):
        converter = self.definition.converter
        if converter is None:
            raise RuntimeError(f"{self.key} has no converter")
        if isinstance(converter,type):
            raise TypeError(
                f"{self.key}: converter must be an initialized converter instance"
            )
        return converter

    def _to_unit(self,value: Any,unit: str) -> Any:
        if not self.definition.conversion_available:
            return value
        return self._converter().to_unit(
            value,unit,self._require_conversion_context(),
        )

    def _to_canonical(self,value: Any) -> Any:
        if (
            self.definition.conversion_available
            and self._unit_mode != self._canonical_unit
        ):
            value = self._to_unit(value,self._canonical_unit)
        return self.definition.validate(value)

    def _read_editor(self) -> Any:
        if isinstance(self.editor,QtWidgets.QCheckBox):
            return self.editor.isChecked()
        if isinstance(self.editor,QtWidgets.QComboBox):
            return self.editor.currentData()
        if isinstance(self.editor,(QtWidgets.QSpinBox,QtWidgets.QDoubleSpinBox)):
            text = self.editor.cleanText().strip().replace(",",".")
            if text == "":
                raise ValueError(f"{self.key} cannot be empty")
            return self._parse_editor_text(text)

        text = self.editor.text().strip().replace(",",".")
        if text == "":
            if self.definition.allow_none:
                return None
            raise ValueError(f"{self.key} cannot be empty")
        return self._parse_editor_text(text)

    def _write_editor(self,value: Any) -> None:
        self._updating_editor = True
        try:
            if isinstance(self.editor,QtWidgets.QCheckBox):
                self.editor.setChecked(bool(value))
            elif isinstance(self.editor,QtWidgets.QComboBox):
                index = self.editor.findData(value)
                if index < 0:
                    index = self.editor.findText(str(value))
                if index >= 0:
                    self.editor.setCurrentIndex(index)
            elif isinstance(self.editor,QtWidgets.QSpinBox):
                self.editor.setValue(int(value))
            elif isinstance(self.editor,QtWidgets.QDoubleSpinBox):
                self.editor.setValue(float(value))
            else:
                self.editor.setText(self._format_value(value))
        finally:
            self._updating_editor = False

    @staticmethod
    def _format_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value,float):
            return "{:.8g}".format(value)
        return str(value)

    def _try_commit_editor(self,*_args: Any) -> None:
        if (
            self._commit_mode is ParameterCommitMode.EDIT_FINISHED
            and isinstance(
                self.editor,(QtWidgets.QSpinBox,QtWidgets.QDoubleSpinBox),
            )
        ):
            # QAbstractSpinBox with keyboardTracking(False) already commits
            # discrete stepping through valueChanged while typed text waits for
            # editingFinished.  Suppress the line-edit textEdited shortcut only.
            return
        try:
            self._commit_editor()
        except Exception:
            pass

    def _commit_editor(self,*_args: Any) -> None:
        if self._updating_editor:
            return
        try:
            canonical = self._to_canonical(self._read_editor())
        except Exception as error:
            self._set_valid(False,str(error))
            raise

        changed = canonical != self._canonical_value
        self._canonical_value = canonical
        self._set_valid(True,"")
        self._update_complementary_label()
        if changed:
            self.sigValueChanged.emit(self.key,canonical)

    def _finish_line_edit(self) -> None:
        try:
            self._commit_editor()
        except Exception:
            self._render()

    def _set_valid(self,valid: bool,message: str) -> None:
        self._last_error = message
        if isinstance(self.editor,QtWidgets.QLineEdit):
            self.editor.setStyleSheet("" if valid else self._INVALID_STYLE)
        self.editor.setToolTip(message or self.definition.tooltip or "")
        self.sigValidityChanged.emit(self.key,valid,message)

    def _render(self) -> None:
        if self.definition.hidden:
            self.label.hide()
            self.editor.hide()
            self.complementaryLabel.hide()
            return

        if (
            self.definition.conversion_available
            and self._unit_mode != self._canonical_unit
        ):
            displayed = self._to_unit(
                self._canonical_value,
                self._unit_mode,
            )
            label = (
                self.definition.converted_label
                or self.definition.label_for(self.key)
            )
        else:
            displayed = self._canonical_value
            label = self.definition.label_for(self.key)

        if not isinstance(self.editor,QtWidgets.QCheckBox):
            self.label.setText(f"{label}:")

        blocker = QtCore.QSignalBlocker(self.editor)
        try:
            self._apply_spinbox_constraints()
            self._write_editor(displayed)
        finally:
            del blocker
        self._apply_presentation_visibility()


    def _apply_presentation_visibility(self) -> None:
        visible = (
            not self.definition.hidden
            and self._presentation_visible
        )

        # ParamField components are created parentless and may be mounted
        # independently by custom views.  Never show a component merely
        # because another component of the same field has been mounted: an
        # unparented QWidget would become a top-level window.
        if self.editor.parentWidget() is not None:
            self.editor.setVisible(visible)

        if isinstance(self.editor,QtWidgets.QCheckBox):
            self.label.hide()
        elif self.label.parentWidget() is not None:
            self.label.setVisible(visible)
        else:
            self.label.hide()

        if self._editor_cell is not None:
            self._editor_cell.setVisible(visible)
        if self._trailing_widget is not None:
            self._trailing_widget.setVisible(
                visible and self._trailing_widget.parentWidget() is not None
            )

        if (
            visible
            and self.complementaryLabel.parentWidget() is not None
        ):
            self._update_complementary_label()
        else:
            self.complementaryLabel.hide()
            
    def _apply_spinbox_constraints(self) -> None:
        if not isinstance(
            self.editor,(QtWidgets.QSpinBox,QtWidgets.QDoubleSpinBox),
        ):
            return

        definition = self.definition
        if (
            self._unit_mode == self._canonical_unit
            or not definition.conversion_available
        ):
            minimum = definition.min_value
            maximum = definition.max_value
        else:
            # Canonical bounds cannot be converted generically. Converters may
            # be nonlinear, reverse interval orientation, etc.For now, we keep
            # converted editors broadly unbounded and enforce ParamSpec validation
            # after converting edits back to canonical.
            minimum = None
            maximum = None

        step = definition.step_for_unit(self._unit_mode)

        if isinstance(self.editor,QtWidgets.QSpinBox):
            self.editor.setMinimum(
                int(minimum) if minimum is not None else -2147483647,
            )
            self.editor.setMaximum(
                int(maximum) if maximum is not None else 2147483647,
            )
            self.editor.setSingleStep(max(1,int(step)))
            return

        self.editor.setMinimum(
            float(minimum) if minimum is not None else -1e100,
        )
        self.editor.setMaximum(
            float(maximum) if maximum is not None else 1e100,
        )
        self.editor.setSingleStep(float(step))
        self.editor.setDecimals(definition.decimals_for_unit(self._unit_mode))

    def _update_complementary_label(self) -> None:
        if (
            not self._presentation_visible
            or not self._show_complementary
            or not self.definition.conversion_available
        ):
            self.complementaryLabel.hide()
            return

        try:
            if self._unit_mode != self._canonical_unit:
                text = "({}: {})".format(
                    self.definition.label_for(self.key),
                    self._format_value(self._canonical_value),
                )
            else:
                unit = next(
                    candidate for candidate in self._converter().supported_units
                    if candidate != self._canonical_unit
                )
                value = self._to_unit(self._canonical_value,unit)
                text = "({}: {})".format(
                    self.definition.converted_label or unit,
                    self._format_value(value),
                )
            self.complementaryLabel.setText(text)
            self.complementaryLabel.show()
        except Exception:
            self.complementaryLabel.setText("(unavailable)")
            self.complementaryLabel.show()

    def _parse_editor_text(self,text: str) -> Any:
        value_type = (
            self._converter().type_for_unit(self._unit_mode)
            if self.definition.conversion_available
            else self.definition.ptype
        )
        return value_type(text)


class ParamForm(QtCore.QObject):
    """Collection and layout of keyed :class:`ParamField` objects.

    Primary and advanced fields share the same grid and ``per_row`` rule.
    Advanced fields are instantiated and bound normally, but start hidden
    behind an ``Advanced`` toggle.
    """

    sigValueChanged = QtCore.Signal(str,object)

    def __init__(
        self,
        name: str,
        definitions: Mapping[str,ParamSpec],
        conversion_context: Callable[[], Any] | None=None,
        parent: QtWidgets.QWidget | None=None,
        per_row: int=1,
        use_subsection: bool=True,
        editor_width: int=70,
        show_complementary: bool=False,
    ):
        super().__init__(parent)

        if not isinstance(definitions,Mapping):
            raise TypeError(
                "ParamForm definitions must be a mapping, got "
                f"{type(definitions).__name__}"
            )

        self.name = name
        self.use_subsection = bool(use_subsection)

        self._definitions = dict(definitions)
        self._fields: dict[str, ParamField] = {}

        self._per_row = max(1,int(per_row))
        self._unit_mode = None

        self._advanced_expanded = False
        self._advanced_toggle: QtWidgets.QToolButton | None = None

        for key,definition in self._definitions.items():
            if not isinstance(key,str) or not key:
                raise ValueError(
                    "Parameter keys must be non-empty strings"
                )

            field = ParamField(
                key=key,
                definition=definition,
                conversion_context=conversion_context,
                parent=None,
                editor_width=editor_width,
                show_complementary=show_complementary,
            )

            self._fields[key] = field
            field.sigValueChanged.connect(self.sigValueChanged)

            if (
                self._unit_mode is None
                and definition.converter is not None
            ):
                self._unit_mode = definition.converter.canonical_unit

        # Advanced fields exist normally, but start presentation-hidden.
        for field in self._advanced_fields():
            field.set_presentation_visible(False)


        for field in self._fields.values():
            field.sigValueChanged.connect(self._on_link_control_changed)
        self.refresh_links()

    @property
    def fields(self) -> dict[str, ParamField]:
        return self._fields

    @property
    def definitions(self) -> Mapping[str,ParamSpec]:
        return self._definitions

    @property
    def advanced_expanded(self) -> bool:
        return self._advanced_expanded

    def field(self,key: str) -> ParamField:
        return self._fields[key]

    def values(self) -> dict[str, Any]:
        return {
            key:field.value()
            for key,field in self._fields.items()
        }

    def set_values(
        self,
        values: Mapping[str,Any],
        emit: bool=False,
    ) -> None:
        for key,value in values.items():
            field = self._fields.get(key)

            if field is not None:
                field.set_value(value,emit=emit)

    def set_unit_mode(self,mode: str) -> None:
        for field in self._fields.values():
            field.set_unit_mode(mode)

        self._unit_mode = mode

    def refresh(self) -> None:
        for field in self._fields.values():
            if field.definition.conversion_available:
                field.refresh()

    def canonical_definitions(self) -> Iterable[ParamSpec]:
        return iter(self._definitions.values())

    def set_advanced_expanded(self,expanded: bool) -> None:
        expanded = bool(expanded)
        self._advanced_expanded = expanded

        button = self._advanced_toggle

        if button is not None:
            blocker = QtCore.QSignalBlocker(button)

            try:
                button.setChecked(expanded)
            finally:
                del blocker

            button.setText(
                "Hide advanced"
                if expanded
                else "Advanced…"
            )

            button.setToolTip(
                "Hide advanced parameters"
                if expanded
                else "Show advanced parameters"
            )

        for field in self._advanced_fields():
            field.set_presentation_visible(expanded)

        if (
            button is not None
            and button.parentWidget() is not None
        ):
            button.parentWidget().updateGeometry()
    # def set_advanced_expanded(self,expanded: bool) -> None:
    #     expanded = bool(expanded)
    #     self._advanced_expanded = expanded

    #     button = self._advanced_toggle

    #     if button is not None:
    #         blocker = QtCore.QSignalBlocker(button)

    #         try:
    #             button.setChecked(expanded)
    #         finally:
    #             del blocker

    #         button.setArrowType(
    #             QtCore.Qt.DownArrow
    #             if expanded
    #             else QtCore.Qt.RightArrow
    #         )

    #         button.setToolTip(
    #             "Hide advanced parameters"
    #             if expanded
    #             else "Show advanced parameters"
    #         )

    #     for field in self._advanced_fields():
    #         field.set_presentation_visible(expanded)

    #     if (
    #         button is not None
    #         and button.parentWidget() is not None
    #     ):
    #         button.parentWidget().updateGeometry()

    @staticmethod
    def _layout_blocks(
        fields: Sequence[ParamField],
    ):
        """Return fields grouped into atomic layout blocks."""

        grouped = {}
        for field in fields:
            group = field.definition.layout_group
            if group is not None:
                grouped.setdefault(group,[]).append(field)

        blocks = []
        emitted_groups = set()

        for field in fields:
            group = field.definition.layout_group

            if group is None:
                blocks.append((field,))
                continue

            if group in emitted_groups:
                continue

            blocks.append(tuple(grouped[group]))
            emitted_groups.add(group)

        return blocks


    def _add_fields(
        self,
        layout: QtWidgets.QGridLayout,
        fields: Sequence[ParamField],
        row: int,
        column: int,
        fields_in_row: int,
    ) -> tuple[int, int, int]:
        """Add fields while keeping layout groups together when possible."""

        for block in self._layout_blocks(fields):
            block_size = len(block)

            # If the complete group fits on one row, do not split it
            # merely because the preceding row has insufficient space.
            if (
                block_size <= self._per_row
                and fields_in_row > 0
                and fields_in_row + block_size > self._per_row
            ):
                row += 1
                column = 0
                fields_in_row = 0

            # A group larger than per_row simply falls back to normal wrapping.
            for field in block:
                if fields_in_row >= self._per_row:
                    row += 1
                    column = 0
                    fields_in_row = 0

                column = field.add_to_grid(layout,row,column)
                fields_in_row += 1

        return row,column,fields_in_row

    def add_to_grid(
        self,
        layout: QtWidgets.QGridLayout,
        start_row: int,
        layout_spec: Sequence[Sequence[str]] | None=None,
    ) -> int:

        visible_fields = [
            field
            for field in self._fields.values()
            if not field.definition.hidden
        ]

        if not visible_fields:
            return start_row

        advanced_fields = [
            field
            for field in visible_fields
            if (
                field.definition.display_level
                == ParamDisplayLevel.ADVANCED
            )
        ]

        # Advanced fields are hidden/shown in their predefined cells.
        if layout_spec is not None:
            next_row = self._add_layout_spec_to_grid(
                layout=layout,
                start_row=start_row,
                visible_fields=visible_fields,
                layout_spec=layout_spec,
            )

            if advanced_fields:
                self._add_advanced_toggle_to_explicit_layout(
                    layout=layout,
                    start_row=start_row,
                    visible_fields=visible_fields,
                )

            return next_row

        primary_fields = [
            field for field in visible_fields
            if field.definition.display_level != ParamDisplayLevel.ADVANCED
        ]

        row = start_row
        column = 0
        fields_in_row = 0

        # First add all primary fields using the existing per_row rule.
        row, column,fields_in_row=self._add_fields(
            layout,primary_fields,row,column,fields_in_row
        )

        if advanced_fields:
            
            # Add advanced toggle button
            single_row_form = self._per_row >= len(visible_fields)
            if not single_row_form:
                row += 1
                column = 0
                fields_in_row = 0

            button = self._ensure_advanced_toggle(layout.parentWidget())
            layout.addWidget(
                button,row,column,1,1,
                alignment=QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
            )

            # Add advanced fields
            if not single_row_form:
                row += 1
                column = 0
                fields_in_row = 0
            else:
                column += 1

            row,column,fields_in_row = self._add_fields(
                layout,advanced_fields,row,column,fields_in_row
            )

        return row + 1

    def _add_layout_spec_to_grid(
        self,
        layout: QtWidgets.QGridLayout,
        start_row: int,
        visible_fields: Sequence[ParamField],
        layout_spec: Sequence[Sequence[str]],
    ) -> int:
        """Preserve explicit layout-spec behavior."""

        visible_by_key = {
            field.key:field
            for field in visible_fields
        }

        specified = set()
        row = start_row

        for spec_row in layout_spec:
            column = 0
            row_has_fields = False

            for key in spec_row:

                if key not in self._fields:
                    raise KeyError(
                        f"Unknown parameter '{key}' in layout "
                        f"specification for form '{self.name}'"
                    )

                if key in specified:
                    raise ValueError(
                        f"Parameter '{key}' appears more than "
                        f"once in the layout specification for "
                        f"form '{self.name}'"
                    )

                specified.add(key)

                field = visible_by_key.get(key)

                if field is None:
                    continue

                column = field.add_to_grid(
                    layout,
                    row,
                    column,
                )

                row_has_fields = True

            if row_has_fields:
                row += 1

        remaining = [
            field
            for field in visible_fields
            if field.key not in specified
        ]

        if not remaining:
            return row

        column = 0
        fields_in_row = 0

        for field in remaining:

            if fields_in_row >= self._per_row:
                row += 1
                column = 0
                fields_in_row = 0

            column = field.add_to_grid(
                layout,
                row,
                column,
            )

            fields_in_row += 1

        return row + 1

    def _add_advanced_toggle_to_explicit_layout(
        self,
        layout: QtWidgets.QGridLayout,
        start_row: int,
        visible_fields: Sequence[ParamField],
    ) -> None:
        """Add toggle without altering explicit field positions."""

        primary_keys = {
            field.key
            for field in visible_fields
            if (
                field.definition.display_level
                != ParamDisplayLevel.ADVANCED
            )
        }

        target_row = start_row

        # Find the last row containing a primary parameter.
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()

            if widget is None:
                continue

            key = self._field_key_for_widget(widget)

            if key not in primary_keys:
                continue

            (
                row,
                _column,
                _row_span,
                _column_span,
            ) = layout.getItemPosition(index)

            target_row = max(
                target_row,
                row,
            )

        button = self._ensure_advanced_toggle(
            layout.parentWidget()
        )

        alignment = (
            QtCore.Qt.AlignLeft
            | QtCore.Qt.AlignVCenter
        )

        layout.addWidget(
            button,
            target_row,
            layout.columnCount(),
            1,
            1,
            alignment=alignment,
        )

    def _field_key_for_widget(
        self,
        widget: QtWidgets.QWidget,
    ) -> str | None:

        for key,field in self._fields.items():

            if widget in (
                field.label,
                field.editor,
                field.complementaryLabel,
            ):
                return key

        return None

    def _advanced_fields(self):
        return [
            field
            for field in self._fields.values()
            if (
                not field.definition.hidden
                and field.definition.display_level
                == ParamDisplayLevel.ADVANCED
            )
        ]
    def _ensure_advanced_toggle(
        self,
        parent: QtWidgets.QWidget | None,
    ) -> QtWidgets.QToolButton:

        if self._advanced_toggle is None:

            button = QtWidgets.QToolButton(parent)

            button.setText(
                "Hide advanced"
                if self._advanced_expanded
                else "Advanced…"
            )

            button.setCheckable(True)
            button.setChecked(self._advanced_expanded)

            button.setAutoRaise(True)
            button.setFocusPolicy(QtCore.Qt.NoFocus)
            button.setCursor(QtCore.Qt.PointingHandCursor)

            button.setFixedHeight(20)

            button.setToolButtonStyle(
                QtCore.Qt.ToolButtonTextOnly
            )

            button.setStyleSheet("""
                QToolButton {
                    border: 1px solid rgba(128, 128, 128, 80);
                    border-radius: 4px;
                    background: rgba(128, 128, 128, 12);
                    padding: 2px 7px;
                }

                QToolButton:hover {
                    border: 1px solid rgba(128, 128, 128, 140);
                    background: rgba(128, 128, 128, 28);
                }

                QToolButton:pressed {
                    background: rgba(128, 128, 128, 40);
                }
            """)

            button.setToolTip(
                "Hide advanced parameters"
                if self._advanced_expanded
                else "Show advanced parameters"
            )

            button.toggled.connect(
                self.set_advanced_expanded
            )

            self._advanced_toggle = button

        return self._advanced_toggle
    # def _ensure_advanced_toggle(
    #     self,
    #     parent: QtWidgets.QWidget | None,
    # ) -> QtWidgets.QToolButton:

    #     if self._advanced_toggle is None:

    #         button = QtWidgets.QToolButton(parent)

    #         button.setText("Advanced")
    #         button.setCheckable(True)
    #         button.setChecked(
    #             self._advanced_expanded
    #         )

    #         button.setAutoRaise(True)
    #         button.setFocusPolicy(
    #             QtCore.Qt.NoFocus
    #         )

    #         button.setFixedHeight(20)

    #         button.setToolButtonStyle(
    #             QtCore.Qt.ToolButtonTextBesideIcon
    #         )

    #         button.setArrowType(
    #             QtCore.Qt.DownArrow
    #             if self._advanced_expanded
    #             else QtCore.Qt.RightArrow
    #         )

    #         button.setToolTip(
    #             "Show advanced parameters"
    #         )

    #         button.toggled.connect(
    #             self.set_advanced_expanded
    #         )

    #         self._advanced_toggle = button

    #     return self._advanced_toggle

    def _on_link_control_changed(self,_key: str, _value: Any) -> None:
        self.refresh_links()


    def refresh_links(self) -> None:
        """Disable target editors controlled by currently active links."""

        controlled_targets = set()
        active_targets = set()

        for _source,spec in self._definitions.items():
            for link in spec.links:
                target = self._fields.get(link.target)
                control = self._fields.get(link.enabled_by)

                # A render policy may intentionally omit one of these fields.
                if target is None or control is None:
                    continue

                controlled_targets.add(link.target)

                if bool(control.value()):
                    active_targets.add(link.target)

        for target_key in controlled_targets:
            self._fields[target_key].set_semantic_enabled(
                target_key not in active_targets
            )
    
