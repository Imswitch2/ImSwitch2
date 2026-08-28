"""Qt projections of slmcore section-group states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any,Callable,Mapping

from qtpy import QtCore,QtWidgets

from ...core.engine.section.components import CGHState
from ...core.engine.section.snapshot import SectionGroupSnapshot
from ...core.cgh.targets.lattice import LatticeLockState
from ...core.cgh.feedback.model import (
    FeedbackCapability,FeedbackStatus,
    base_cgh_recompute_would_discard_feedback,
)
from ...core.engine.parameters.converters import SLM_UNIT
from ...core.engine.parameters.spec import ParamRole,ParamSpec,make_display_name
from ...core.engine.state.base import ParamPath,StateModel
from ...core.engine.state.groups import DynamicGroupState
from ...core.engine.state.items import CGHTargetState,ItemState
from ..widgets.binding import ParameterBinding
from ..cgh.session_views import format_target_summary
from ..widgets.fields import ParamForm
from ..application.interaction import (
    ParameterCommitMode,is_group_target_parameter_path,
)
from ..widgets.param_section import PARAM_ACTIVE_KEY,ParamSection
from .policy import RenderPolicy

_GROUP_STYLE = {"button_height":20,"fontsize":9}
_LOCAL_FORM_NAME = "values"
_CGH_TARGET_FORM_PREFIX = "target__"
_CGH_COMPUTATION_FORM_PREFIX = "computation__"


class CghAction(str,Enum):
    """Reusable actions exposed by :class:`CghGroupView`."""

    COMPUTE = "compute"
    RESTORE_CURRENT_TARGET = "restore_current_target"
    TARGET_PREVIEW = "target_preview"
    METRICS = "metrics"
    PROPAGATION = "propagation"
    CLEAR_CGH_SESSION = "clear_cgh_session"
    OPEN_MEASUREMENTS_CORRECTIONS = "open_measurements_corrections"


def visible_specs(
    state: StateModel,policy: RenderPolicy,
) -> dict[str, ParamSpec]:
    return {
        key:spec for key,spec in state.param_specs().items()
        if policy.is_parameter_visible(spec)
    }


def pop_active_spec(
    specs: Mapping[str,ParamSpec],
) -> tuple[ParamSpec | None, dict[str, ParamSpec]]:
    remaining = dict(specs)
    active = remaining.get("active")
    if active is None or active.role is not ParamRole.ACTIVE:
        return None,remaining
    remaining.pop("active")
    return active,remaining


def add_form_to_grid_layout(
    form: ParamForm,
    layout: QtWidgets.QGridLayout,
    *,
    start_row: int=0,
    contents_margins: tuple[int, int, int, int]=(6,6,6,6),
) -> int:
    """Mount a form using the standard compact slmcore grid policy."""
    layout.setContentsMargins(*contents_margins)
    layout.setHorizontalSpacing(10)
    layout.setVerticalSpacing(4)
    row = form.add_to_grid(layout=layout,start_row=start_row)
    layout.setColumnStretch(layout.columnCount(),1)
    return row


def add_form_to_group_box(form: ParamForm,title: str) -> QtWidgets.QGroupBox:
    box = QtWidgets.QGroupBox(title)
    layout = QtWidgets.QGridLayout(box)
    add_form_to_grid_layout(form,layout)
    return box


@dataclass(frozen=True)
class GroupPresentationState:
    expanded: bool = True
    auto_recompute_enabled: bool | None = None


@dataclass
class FeedbackControls:
    """Compact CGH-page entry point for the external feedback workspace."""

    container: QtWidgets.QWidget
    status_value_label: QtWidgets.QLabel
    feedback_summary_label: QtWidgets.QLabel
    open_button: QtWidgets.QPushButton


class BaseGroupView:
    """Retained Qt projection of one ``SectionGroupSnapshot``."""

    SUMMARY_MODE = "none"
    SUMMARY_MAX_WIDTH = 240

    def __init__(
        self,
        *,
        entry: SectionGroupSnapshot,
        conversion_context: Callable[[],Any],
        on_edit: Callable[[str,Mapping[ParamPath,Any]],None],
        render_policy: RenderPolicy,
    ) -> None:
        self.state_key = entry.state_key
        self.group_key = entry.group_key
        self.binding = ParameterBinding()
        self.render_policy = render_policy
        self._conversion_context = conversion_context
        self._on_edit = on_edit
        self._on_action: Callable[[str, Mapping[str, Any]], None] | None = None

        local_specs = visible_specs(entry.state,render_policy)
        active_spec,remaining = pop_active_spec(local_specs)
        metadata_specs,remaining = self._partition_local_specs(
            entry.state,remaining,
        )

        self.param_section = ParamSection(
            name=entry.group_key,title=entry.state.title(),
            active_def=active_spec,
            metadata_definitions=metadata_specs,
            summary_mode=self.SUMMARY_MODE,
            summary_max_width=self.SUMMARY_MAX_WIDTH,
            conversion_context=conversion_context,
            collapsible_kwargs=_GROUP_STYLE,
        )
        self.widget = self.param_section.widget

        if self.param_section.metadata_form is not None:
            self.binding.bind_form(
                self.param_section.metadata_form,(),self._emit_edit,
            )

        self._build(entry.state,remaining)
        self.binding.validate_coverage(entry.state,render_policy)
        self.apply_state(entry.state)

    def _partition_local_specs(
        self,
        state: StateModel,
        remaining_local_specs: Mapping[str,ParamSpec],
    ) -> tuple[dict[str, ParamSpec], dict[str, ParamSpec]]:
        return {},dict(remaining_local_specs)

    def _build(
        self,
        state: StateModel,
        remaining_local_specs: Mapping[str,ParamSpec],
    ) -> None:
        self._add_local_form(remaining_local_specs)

    def _add_local_form(
        self,specs: Mapping[str,ParamSpec],*,per_row: int=1,
    ) -> ParamForm | None:
        if not specs:
            return None
        form = self.param_section.add_form(
            name=_LOCAL_FORM_NAME,definitions=specs,
            use_subsection=False,per_row=per_row,
            editor_width=70,show_complementary=False,
        )
        self.binding.bind_form(form,(),self._emit_edit)
        return form

    def _create_item_form(
        self,
        *,
        form_name: str,
        state: StateModel,
        prefix: ParamPath,
        per_row: int,
    ) -> ParamForm | None:
        visible = visible_specs(state,self.render_policy)
        active_spec,definitions = pop_active_spec(visible)
        if active_spec is None and not definitions:
            return None

        form = self.param_section.add_form(
            name=form_name,definitions=definitions,
            active_def=active_spec,use_subsection=True,
            per_row=per_row,editor_width=70,
            show_complementary=False,mount=False,
        )
        self.binding.bind_form(form,prefix,self._emit_edit)
        return form

    def _add_item_form(
        self,
        *,
        form_name: str,
        state: StateModel,
        prefix: ParamPath,
        title: str,
        per_row: int,
    ) -> QtWidgets.QGroupBox | None:
        form = self._create_item_form(
            form_name=form_name,state=state,prefix=prefix,
            per_row=per_row,
        )
        if form is None:
            return None
        return add_form_to_group_box(form,title)

    def _emit_edit(self,path: ParamPath,value: Any) -> None:
        self._emit_edits({tuple(path):value})

    def _emit_edits(
        self,changes: Mapping[ParamPath,Any],
    ) -> None:
        self._on_edit(
            self.state_key,
            {tuple(path):value for path,value in changes.items()},
        )

    def set_action_handler(
        self,handler: Callable[[str, Mapping[str, Any]], None] | None,
    ) -> None:
        """Set an optional host callback for non-parameter group actions."""
        self._on_action = handler

    def _emit_action(
        self,action: CghAction,options: Mapping[str, Any] | None=None,
    ) -> None:
        if self._on_action is not None:
            self._on_action(action.value,dict(options or {}))

    def apply_state(self,state: StateModel) -> None:
        self.binding.apply_state(state)
        self.param_section.refresh_summary()

    def set_parameter(self,path: ParamPath,value: Any) -> bool:
        changed = self.binding.set_parameter(path,value)
        if changed:
            self.param_section.refresh_summary()
        return changed

    def set_unit_mode(self,mode: str) -> None:
        self.param_section.set_unit_mode(mode)

    def refresh_conversions(self) -> None:
        self.param_section.refresh()

    def capture_presentation_state(self) -> GroupPresentationState:
        return GroupPresentationState(expanded=self.widget.expanded)

    def restore_presentation_state(
        self,state: GroupPresentationState,
    ) -> None:
        self.widget.set_expanded(state.expanded,animate=False)

    def dispose(self) -> None:
        self.widget.deleteLater()


class PatternsGroupView(BaseGroupView):
    SUMMARY_MODE = "active_forms"

    def _build(
        self,state: StateModel,
        remaining_local_specs: Mapping[str,ParamSpec],
    ) -> None:
        self._add_local_form(remaining_local_specs)
        if not isinstance(state,DynamicGroupState):
            raise TypeError("Patterns group must be a DynamicGroupState")

        row = self.param_section.next_row
        for item_key,item in state.items.items():
            title = make_display_name(item_key)
            form = self._create_item_form(
                form_name=item_key,
                state=item.params,
                prefix=ItemState.params_path(item_key),
                per_row=len(visible_specs(item.params,self.render_policy)),
            )
            if form is None:
                continue

            try:
                active_editor = form.field(PARAM_ACTIVE_KEY).editor
            except KeyError:
                active_editor = None

            if isinstance(active_editor,QtWidgets.QCheckBox):
                active_editor.setText(f"{title}:")

            row = form.add_to_grid(layout=self.param_section.layout,
                                   start_row=row)

        self.param_section.layout.setColumnStretch(
            self.param_section.layout.columnCount(),1,
        )


class AberrationsGroupView(BaseGroupView):
    SUMMARY_MODE = "nonzero_fields"

    def _build(
        self,state: StateModel,
        remaining_local_specs: Mapping[str,ParamSpec],
    ) -> None:
        self._add_local_form(remaining_local_specs)
        if not isinstance(state,DynamicGroupState):
            raise TypeError("Aberrations group must be a DynamicGroupState")

        row = self.param_section.next_row
        for item_key,item in state.items.items():
            box = self._add_item_form(
                form_name=item_key,state=item.params,
                prefix=ItemState.params_path(item_key),
                title=make_display_name(item_key),per_row=2,
            )
            if box is not None:
                self.param_section.layout.addWidget(box,row,0,1,1)
                row += 1
        self.param_section.layout.setColumnStretch(0,1)


class CghGroupView(BaseGroupView):
    """Projection of ``CGHState`` including target/computation pages."""

    SUMMARY_MODE = "runtime_value"

    def __init__(self,*args: Any,**kwargs: Any) -> None:
        self.stack: QtWidgets.QStackedWidget | None = None
        self._computation_stack: QtWidgets.QStackedWidget | None = None
        self.status_label: QtWidgets.QLabel | None = None
        self.compute_button: QtWidgets.QPushButton | None = None
        self.restore_target_button: QtWidgets.QPushButton | None = None
        self.auto_recompute_checkbox: QtWidgets.QCheckBox | None = None
        self.preview_button: QtWidgets.QPushButton | None = None
        self.metrics_button: QtWidgets.QPushButton | None = None
        self.propagation_button: QtWidgets.QPushButton | None = None
        self.clear_button: QtWidgets.QPushButton | None = None
        self.propagation_pad_size: QtWidgets.QSpinBox | None = None
        self._action_widget: QtWidgets.QWidget | None = None
        self._action_host_layout: QtWidgets.QGridLayout | None = None
        self._computation_boxes: dict[str, QtWidgets.QGroupBox] = {}
        self._target_boxes: dict[str, QtWidgets.QWidget] = {}
        self._target_forms: dict[str, ParamForm] = {}
        self._lock_buttons: dict[str, dict[str, QtWidgets.QToolButton]] = {}
        self._lock_states: dict[str, LatticeLockState] = {}
        self._target_lock_handler: Callable[[str, str | None], None] | None = None
        self._page_by_target: dict[str | None, int] = {}
        self._computation_page_by_target: dict[str | None, int] = {}
        self._feedback_capabilities: dict[str, tuple[FeedbackCapability, ...]] = {}
        self._feedback_controls: FeedbackControls | None = None
        self._status: Any = None
        self._feedback_status: FeedbackStatus | None = None
        self._target_presentation: dict[str, Any] = {}
        self._target_param_specs: dict[str, Mapping[str, ParamSpec]] = {}
        self._unit_mode = SLM_UNIT
        self._computing = False
        super().__init__(*args,**kwargs)

    def capture_presentation_state(self) -> GroupPresentationState:
        return GroupPresentationState(
            expanded=self.widget.expanded,
            auto_recompute_enabled=self.auto_recompute_enabled(),
        )

    def restore_presentation_state(
        self,state: GroupPresentationState,
    ) -> None:
        super().restore_presentation_state(state)
        if state.auto_recompute_enabled is not None:
            self.set_auto_recompute_enabled(state.auto_recompute_enabled)

    def _partition_local_specs(
        self,
        state: StateModel,
        remaining_local_specs: Mapping[str,ParamSpec],
    ) -> tuple[dict[str, ParamSpec], dict[str, ParamSpec]]:
        metadata = {}
        body = dict(remaining_local_specs)
        selector_key = CGHState.SELECTED_TARGET_KEY
        selector = body.pop(selector_key,None)
        if selector is not None:
            metadata[selector_key] = selector
        return metadata,body

    def _build(
        self,state: StateModel,
        remaining_local_specs: Mapping[str,ParamSpec],
    ) -> None:
        if not isinstance(state,CGHState):
            raise TypeError("CghGroupView requires CGHState")

        self._add_local_form(remaining_local_specs)
        if self.param_section.summary_prefix_label is not None:
            self.param_section.summary_prefix_label.setText("")

        body = QtWidgets.QWidget()
        body_layout = QtWidgets.QVBoxLayout(body)
        body_layout.setContentsMargins(0,0,0,0)
        body_layout.setSpacing(0)

        if self.render_policy.show_cgh_controls:
            body_layout.addWidget(self._build_feedback_box())

        selector = None
        if self.param_section.metadata_form is not None:
            try:
                selector = self.param_section.metadata_field(
                    CGHState.SELECTED_TARGET_KEY,
                )
            except KeyError:
                pass

        target_frame = QtWidgets.QFrame()
        target_frame.setFrameShape(QtWidgets.QFrame.StyledPanel)
        target_frame.setFrameShadow(QtWidgets.QFrame.Sunken)
        target_frame_layout = QtWidgets.QVBoxLayout(target_frame)
        target_frame_layout.setContentsMargins(8,6,8,8)
        target_frame_layout.setSpacing(8)

        if selector is not None:
            selector.label.setText("Selected Target:")

            if self.render_policy.show_cgh_controls:
                body_layout.addSpacing(8)

            selector_row = QtWidgets.QHBoxLayout()
            selector_row.setContentsMargins(0,0,0,0)
            selector_row.setSpacing(6)
            selector_row.addWidget(selector.label)
            selector_row.addWidget(selector.editor)

            if self.render_policy.show_cgh_controls:
                preview = QtWidgets.QPushButton("Visualize Target")
                preview.setFixedHeight(18)
                preview.clicked.connect(
                    lambda _checked=False:self._emit_action(
                        CghAction.TARGET_PREVIEW,
                    )
                )
                self.preview_button = preview
                selector_row.addWidget(preview)

            selector_row.addStretch(1)
            target_frame_layout.addLayout(selector_row)

        self.stack = QtWidgets.QStackedWidget()
        placeholder = QtWidgets.QLabel("No CGH target selected")
        placeholder.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        placeholder.setStyleSheet("color: #a66a00;")
        self._page_by_target[None] = self.stack.addWidget(placeholder)
        target_frame_layout.addWidget(self.stack)
        body_layout.addWidget(target_frame)

        self._computation_stack = QtWidgets.QStackedWidget()
        self._computation_page_by_target[None] = (
            self._computation_stack.addWidget(QtWidgets.QWidget())
        )

        for target_key,target in state.items.items():
            self._target_param_specs[target_key] = target.params.param_specs()

            target_form = self._create_item_form(
                form_name=_CGH_TARGET_FORM_PREFIX + target_key,
                state=target.params,
                prefix=state.target_params_path(target_key),
                per_row=2,
            )
            target_box = QtWidgets.QWidget()
            target_layout = QtWidgets.QGridLayout(target_box)
            if target_form is not None:
                self._target_forms[target_key] = target_form
                if isinstance(target.lock_state,LatticeLockState):
                    self._decorate_lattice_lock_form(
                        target_key,target_form,target.lock_state,
                    )
                add_form_to_grid_layout(
                    target_form,
                    target_layout,
                    contents_margins=(0,0,0,0),
                )
            self._target_boxes[target_key] = target_box
            self._page_by_target[target_key] = self.stack.addWidget(target_box)

            computation_page = QtWidgets.QWidget()
            computation_page_layout = QtWidgets.QVBoxLayout(computation_page)
            computation_page_layout.setContentsMargins(0,8,0,0)
            computation_page_layout.setSpacing(0)

            computation_title = (
                f"Computation ({make_display_name(target.algorithm)})"
            )
            computation_box = self._add_item_form(
                form_name=_CGH_COMPUTATION_FORM_PREFIX + target_key,
                state=target.computation.params,
                prefix=state.computation_params_path(target_key),
                title=computation_title,
                per_row=2,
            )
            if self.render_policy.show_cgh_controls and computation_box is None:
                computation_box = QtWidgets.QGroupBox(computation_title)
                computation_box.setLayout(QtWidgets.QGridLayout())
            if computation_box is not None:
                self._computation_boxes[target_key] = computation_box
                computation_page_layout.addWidget(computation_box)

            capabilities = tuple(
                FeedbackCapability(item) for item in target.feedback_capabilities
            )
            self._feedback_capabilities[target_key] = capabilities

            computation_page_layout.addStretch(1)
            self._computation_page_by_target[target_key] = (
                self._computation_stack.addWidget(computation_page)
            )

        body_layout.addWidget(self._computation_stack)
        self.param_section.layout.addWidget(
            body,self.param_section.next_row,0,1,1,
        )
        self.param_section.layout.setColumnStretch(0,1)

        # Target spinboxes defer free-form typed text until editingFinished,
        # while arrow/key/wheel stepping remains live through valueChanged.
        for path,field in self.binding.fields.items():
            if is_group_target_parameter_path(path):
                field.set_commit_mode(ParameterCommitMode.EDIT_FINISHED)

        if selector is not None and isinstance(
            selector.editor,QtWidgets.QComboBox,
        ):
            selector.editor.currentIndexChanged.connect(
                self._sync_stack_from_field,
            )
        self._sync_stack_from_field()
        self._refresh_action_controls()

    def _emit_edit(self,path: ParamPath,value: Any) -> None:
        """Emit one edit, capturing the unit basis of activated links.

        Unit switching itself remains presentation-only. The current unit is
        persisted into a hidden semantic parameter only when a unit-aware
        link control (currently ``square``) is activated.
        """
        path = tuple(path)
        changes: dict[ParamPath, Any] = {path:value}

        if (
            len(path) == 3
            and path[1] == CGHTargetState.PARAMS_STATE_KEY
            and bool(value)
        ):
            target_key = path[0]
            control_key = path[2]
            specs = self._target_param_specs.get(target_key,{})
            unit_controls = {
                link.unit_by
                for spec in specs.values()
                for link in spec.links
                if (
                    link.enabled_by == control_key
                    and link.unit_by is not None
                )
            }
            for unit_key in unit_controls:
                changes[(
                    target_key,
                    CGHTargetState.PARAMS_STATE_KEY,
                    unit_key,
                )] = self._unit_mode

        self._emit_edits(changes)

    def set_target_lock_handler(
        self,handler: Callable[[str, str | None], None] | None,
    ) -> None:
        self._target_lock_handler = handler

    def _decorate_lattice_lock_form(
        self,target_key: str,form: ParamForm,lock: LatticeLockState,
    ) -> None:
        buttons = {}
        for kind,field_key in (("fov","fov_y_px"),("n_foci","n_foci_y")):
            if field_key not in form.fields:
                continue
            button = QtWidgets.QToolButton()
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setFixedSize(22,22)
            button.clicked.connect(
                lambda checked,k=kind,t=target_key:
                    self._on_lattice_lock_clicked(t,k,bool(checked))
            )
            form.field(field_key).set_trailing_widget(button)
            buttons[kind] = button
        if buttons:
            self._lock_buttons[target_key] = buttons
            self._set_lattice_lock_presentation(target_key,lock)

    def _on_lattice_lock_clicked(
        self,target_key: str,kind: str,checked: bool,
    ) -> None:
        requested = kind if checked else None
        self._set_lattice_lock_button_checks(target_key,requested)
        handler = self._target_lock_handler
        if handler is not None:
            handler(target_key,requested)

    def _set_lattice_lock_button_checks(
        self,target_key: str,kind: str | None,
    ) -> None:
        for name,button in self._lock_buttons.get(target_key,{}).items():
            blocker = QtCore.QSignalBlocker(button)
            try:
                button.setChecked(name == kind)
            finally:
                del blocker
            button.setText("🔒" if name == kind else "🔓")

    def _set_lattice_lock_presentation(
        self,target_key: str,lock: LatticeLockState,
    ) -> None:
        self._lock_states[target_key] = LatticeLockState(
            kind=lock.kind,
            reference=(None if lock.reference is None else tuple(lock.reference)),
        )
        self._lock_states[target_key].validate()
        self._set_lattice_lock_button_checks(target_key,lock.kind)
        for name,button in self._lock_buttons.get(target_key,{}).items():
            if lock.kind == name and lock.reference is not None:
                x,y = lock.reference
                label = "FOV" if name == "fov" else "Foci count"
                button.setToolTip(
                    f"{label} reference locked: X={x:g}, Y={y:g}. "
                    "Click to unlock. Realized values may differ after rasterization."
                )
            else:
                label = "FOV" if name == "fov" else "foci count"
                button.setToolTip(
                    f"Keep the current X/Y {label} as the reference across "
                    "subsequent raster target changes."
                )

    def sync_target_lock_states(self,state: CGHState) -> None:
        for target_key,target in state.items.items():
            lock = target.lock_state
            if isinstance(lock,LatticeLockState) and target_key in self._lock_buttons:
                self._set_lattice_lock_presentation(target_key,lock)

    def _build_feedback_box(self) -> QtWidgets.QWidget:
        """Build the compact aligned CGH status and feedback dashboard."""
        widget = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(widget)
        grid.setContentsMargins(0,0,0,0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(3)
        # Keep the control cluster compact. Column 4 absorbs extra width
        # instead of opening a gap before the action buttons.
        grid.setColumnStretch(4,1)

        status_title = QtWidgets.QLabel("CGH status:")
        grid.addWidget(status_title,0,0,alignment=QtCore.Qt.AlignVCenter)

        status_cell = QtWidgets.QWidget()
        status_layout = QtWidgets.QHBoxLayout(status_cell)
        status_layout.setContentsMargins(0,0,0,0)
        status_layout.setSpacing(6)

        status_value = QtWidgets.QLabel("Missing")
        status_value.setSizePolicy(
            QtWidgets.QSizePolicy.Maximum,QtWidgets.QSizePolicy.Preferred,
        )
        status_layout.addWidget(status_value)

        restore_target = QtWidgets.QPushButton("Restore")
        restore_target.setFixedHeight(18)
        restore_target.setToolTip(
            "Restore target parameters to those of the current computed CGH."
        )
        restore_target.clicked.connect(
            lambda _checked=False:self._emit_action(
                CghAction.RESTORE_CURRENT_TARGET,
            )
        )
        self.restore_target_button = restore_target
        status_layout.addWidget(restore_target)
        status_layout.addStretch(1)
        grid.addWidget(status_cell,0,1)

        compute = QtWidgets.QPushButton("Compute")
        compute.setFixedHeight(18)
        compute.clicked.connect(self._request_compute)
        self.compute_button = compute
        grid.addWidget(compute,0,2)

        auto_recompute = QtWidgets.QCheckBox("Auto recompute")
        auto_recompute.setChecked(False)
        self.auto_recompute_checkbox = auto_recompute
        grid.addWidget(
            auto_recompute,0,3,alignment=(
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
            ),
        )

        feedback_title = QtWidgets.QLabel("Feedback:")
        grid.addWidget(feedback_title,1,0,alignment=QtCore.Qt.AlignVCenter)

        feedback_summary = QtWidgets.QLabel("None")
        feedback_summary.setStyleSheet("color: #888;")
        grid.addWidget(
            feedback_summary,1,1,alignment=(
                QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter
            ),
        )

        open_button = QtWidgets.QPushButton("Open session")
        open_button.setFixedHeight(18)
        open_button.clicked.connect(
            lambda _checked=False:self._emit_action(
                CghAction.OPEN_MEASUREMENTS_CORRECTIONS,
            )
        )
        grid.addWidget(open_button,1,2)

        clear = QtWidgets.QPushButton("Clear session")
        clear.setFixedHeight(18)
        clear.clicked.connect(
            lambda _checked=False:self._emit_action(CghAction.CLEAR_CGH_SESSION)
        )
        self.clear_button = clear
        grid.addWidget(clear,1,3)

        self._feedback_controls = FeedbackControls(
            container=widget,
            status_value_label=status_value,
            feedback_summary_label=feedback_summary,
            open_button=open_button,
        )
        return widget

    def auto_recompute_enabled(self) -> bool:
        checkbox = self.auto_recompute_checkbox
        return bool(checkbox is not None and checkbox.isChecked())

    def set_auto_recompute_enabled(self,enabled: bool) -> None:
        checkbox = self.auto_recompute_checkbox
        if checkbox is None:
            return
        blocker = QtCore.QSignalBlocker(checkbox)
        try:
            checkbox.setChecked(bool(enabled))
        finally:
            del blocker
        self._refresh_auto_recompute_availability()

    def apply_feedback_status(self,status: FeedbackStatus) -> None:
        self._feedback_status = status
        self._refresh_feedback_controls()
        self._refresh_auto_recompute_availability()

    def _refresh_auto_recompute_availability(self) -> None:
        checkbox = self.auto_recompute_checkbox
        if checkbox is None:
            return
        blocked = base_cgh_recompute_would_discard_feedback(
            self._feedback_status
        )
        checkbox.setEnabled(not blocked and not self._computing)
        if self._computing:
            tooltip = "CGH computation is in progress."
        elif blocked:
            tooltip = (
                "Auto recompute is unavailable while CGH feedback is active, "
                "because computing a new base CGH would discard the current "
                "feedback state."
            )
        else:
            tooltip = "Automatically recompute after a committed CGH target edit."
        checkbox.setToolTip(tooltip)

    def _refresh_feedback_controls(self) -> None:
        controls = self._feedback_controls
        if controls is None:
            return

        selected = self._selected_target_key()
        status = self._feedback_status
        result_state = getattr(
            getattr(self._status,"result_state",None),"value",None,
        ) or "missing"
        state_text = {
            "current":"Current",
            "stale":"Stale",
            "missing":"Missing",
        }.get(result_state,make_display_name(result_state))
        controls.status_value_label.setText(state_text)
        controls.status_value_label.setStyleSheet({
            "current":"color: #286b2d;",
            "stale":"color: #a66a00;",
            "missing":"color: #888;",
        }.get(result_state,"color: #888;"))

        capabilities = set(self._feedback_capabilities.get(selected,()))
        parts = []
        if selected is not None and capabilities and status is not None:
            if FeedbackCapability.INTENSITY in capabilities:
                parts.append("Intensity ×%d" % status.intensity_count)
            if FeedbackCapability.POSITION_CORRECTION in capabilities:
                parts.append(
                    "Position %s" % ("ON" if status.position_active else "OFF")
                )
        controls.feedback_summary_label.setText(
            " · ".join(parts) if parts else "None"
        )
        controls.feedback_summary_label.setToolTip(
            self._feedback_status_tooltip(status)
            if selected is not None and capabilities else ""
        )


        # Opening the workspace is also how a future saved session is loaded,
        # so it must not depend on a currently selected target.
        controls.open_button.setEnabled(not self._computing)

    def _selected_target_summary_text(self) -> str:
        data = self._target_presentation
        presentation = data.get("target_presentation")
        if presentation is None:
            return ""
        return format_target_summary(
            presentation,
            data.get("target_params") or {},
            data.get("target_param_specs") or {},
            unit_mode=self._unit_mode,
            conversion_context=self._conversion_context(),
        )

    @staticmethod
    def _feedback_status_tooltip(status: FeedbackStatus | None) -> str:
        """Return compact details while leaving full provenance to Inspect."""
        if status is None:
            return "No measurement session information is available."

        lines = [
            "Measurement: acquired"
            if status.acquisition_available else "Measurement: none",
        ]
        if status.localization_available:
            lines.append(
                "Localization: %d/%d matched" % (
                    status.localization_matched_count,
                    status.localization_total_count,
                )
            )
            if status.localization_missing_count:
                lines.append(
                    "Missing lattice spots: %d" % status.localization_missing_count
                )
            if status.localization_unmatched_detection_count:
                lines.append(
                    "Unmatched detections: %d"
                    % status.localization_unmatched_detection_count
                )
            if status.localization_rms_residual_px is not None:
                lines.append(
                    "Localization RMS: %.3g px"
                    % status.localization_rms_residual_px
                )
        else:
            lines.append("Localization: none")
        lines.append("Intensity rounds: %d" % status.intensity_count)
        lines.append(
            "Position correction: %s"
            % ("applied" if status.position_active
               else "available" if status.position_available else "none")
        )
        lines.append("Open the workspace and use Inspect for full provenance.")
        return "\n".join(lines)

    def _selected_target_key(self) -> str | None:
        selector = self.binding.fields.get(CGHState.selected_target_path())
        return None if selector is None else selector.value()

    def _request_compute(self,_checked: bool=False) -> None:
        self._emit_action(CghAction.COMPUTE)

    def _request_propagation(self,_checked: bool=False) -> None:
        pad_size = (
            self.propagation_pad_size.value()
            if self.propagation_pad_size is not None else 1024
        )
        self._emit_action(
            CghAction.PROPAGATION,{"pad_size":int(pad_size)},
        )

    def set_parameter(self,path: ParamPath,value: Any) -> bool:
        changed = super().set_parameter(path,value)
        if changed and tuple(path) == CGHState.selected_target_path():
            # Programmatic reconciliation (for example config loading) updates
            # fields without emitting Qt edit signals. Keep the retained target
            # and computation pages synchronized with the selector explicitly.
            self._sync_stack_from_field()
            self._refresh_action_controls()
        return changed

    def apply_state(self,state: StateModel) -> None:
        super().apply_state(state)
        if isinstance(state,CGHState):
            self.sync_target_lock_states(state)
        self._sync_stack_from_field()
        self._refresh_action_controls()

    def apply_status(self,status: Any) -> None:
        self._status = status
        self._refresh_runtime_summary()
        self._refresh_action_controls()

    def set_target_presentation(self,summary: Mapping[str,Any]) -> None:
        """Set registry-owned semantic presentation data for the active target."""
        self._target_presentation = dict(summary or {})
        self._refresh_runtime_summary()

    def set_unit_mode(self,mode: str) -> None:
        super().set_unit_mode(mode)
        self._unit_mode = str(mode)
        self._refresh_runtime_summary()

    def refresh_conversions(self) -> None:
        super().refresh_conversions()
        self._refresh_runtime_summary()

    def _refresh_runtime_summary(self) -> None:
        status = self._status
        state = getattr(getattr(status,"result_state",None),"value",None)
        data = self._target_presentation

        # The collapsed header describes what is physically applied, not the
        # editable/draft target currently selected in the form.
        if state == "missing":
            text = "Not computed yet"
        else:
            presentation = data.get("applied_target_presentation")
            if presentation is not None:
                text = format_target_summary(
                    presentation,
                    data.get("applied_target_params") or {},
                    data.get("applied_target_param_specs") or {},
                    unit_mode=data.get("applied_unit_mode",self._unit_mode),
                    conversion_context=data.get("applied_conversion_context"),
                )
            else:
                text = str(getattr(status,"target_name","") or "").strip()
            if not text:
                text = "Not computed yet"
        self.param_section.set_summary_value(text)
        self._refresh_feedback_controls()

    def set_computing(self,computing: bool) -> None:
        self._computing = bool(computing)
        self._refresh_action_controls()

    def _refresh_compute_button_presentation(self) -> None:
        button = self.compute_button
        if button is None:
            return

        if self._computing:
            button.setText("Computing...")
            button.setToolTip("")
            return

        result_state = getattr(
            getattr(self._status,"result_state",None),"value",None,
        )
        if result_state == "current":
            button.setText("Recompute")
            button.setToolTip(
                "CGH is already current. Recompute to regenerate it "
                "with the current settings."
            )
        else:
            button.setText("Compute")
            button.setToolTip("")

    def _refresh_action_controls(self) -> None:
        self._refresh_compute_button_presentation()
        status = self._status
        enabled = bool(getattr(status,"enabled",True))
        selected = getattr(status,"target_type",None)
        result_state = getattr(getattr(status,"result_state",None),"value",None)
        has_result = result_state not in (None,"missing")

        for field in self.binding.fields.values():
            field.set_interaction_enabled(not self._computing)

        selector_field = self.binding.fields.get(CGHState.selected_target_path())
        if selector_field is not None:
            selector_field.set_interaction_enabled(not self._computing)
        for box in self._target_boxes.values():
            box.setEnabled(not self._computing)
        for box in self._computation_boxes.values():
            box.setEnabled(not self._computing)

        if self.restore_target_button is not None:
            self.restore_target_button.setEnabled(
                enabled
                and has_result
                and bool(getattr(status,"target_restore_available",False))
                and not self._computing
            )
        if self.compute_button is not None:
            self.compute_button.setEnabled(
                enabled and selected is not None and not self._computing
            )
        if self.preview_button is not None:
            self.preview_button.setEnabled(
                enabled
                and selected is not None
                and not self._computing
            )
        for button in (
            self.metrics_button,self.propagation_button,self.clear_button,
        ):
            if button is not None:
                button.setEnabled(has_result and not self._computing)
        if self.propagation_pad_size is not None:
            self.propagation_pad_size.setEnabled(
                has_result and not self._computing
            )
        self._refresh_feedback_controls()
        self._refresh_auto_recompute_availability()

    def _sync_stack_from_field(self,*_args: Any) -> None:
        if self.stack is None:
            return
        selector = self.binding.fields.get(CGHState.selected_target_path())
        target = None if selector is None else selector.value()
        self.stack.setCurrentIndex(self._page_by_target.get(target,0))
        if self._computation_stack is not None:
            self._computation_stack.setCurrentIndex(
                self._computation_page_by_target.get(target,0)
            )
            self._computation_stack.setVisible(target is not None)
        self._move_action_row_to_computation(target)
        self._refresh_feedback_controls()

    def _move_action_row_to_computation(
        self,target_key: str | None,
    ) -> None:
        """Keep one computation action row inside the selected target box."""
        widget = self._action_widget
        if widget is None:
            return

        if self._action_host_layout is not None:
            self._action_host_layout.removeWidget(widget)
            self._action_host_layout = None

        box = (
            None if target_key is None
            else self._computation_boxes.get(str(target_key))
        )
        if box is None:
            widget.hide()
            return

        layout = box.layout()
        if not isinstance(layout,QtWidgets.QGridLayout):
            raise TypeError("CGH computation boxes require a QGridLayout")
        widget.setParent(box)
        layout.addWidget(
            widget,layout.rowCount(),0,1,max(1,layout.columnCount()),
        )
        self._action_host_layout = layout
        widget.show()
