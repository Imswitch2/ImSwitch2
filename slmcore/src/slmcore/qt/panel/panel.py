from __future__ import annotations

from dataclasses import replace
from typing import Any,Mapping

import numpy as np
from qtpy import QtCore,QtWidgets

from ...engine.section.snapshot import SLMSectionSnapshot
from ..configuration.controls import ConfigControls
from ..preview.panel import SLMPreviewPanel
from ..preview.view import SLMPreviewView
from ..sections.collection import SectionsCollectionView
from ..sections.display import SectionsDisplayMode
from ..sections.policy import DEFAULT_RENDER_POLICY,RenderPolicy
from ..sections.view_host import SectionsViewHost
from ..widgets.uitools import BetterPushButton,ElidedLabel
from .policy import (
    DEFAULT_SLM_PANEL_LAYOUT_POLICY,
    PreviewContainer,
    PreviewPlacement,
    SLMPanelLayoutPolicy,
)


class SLMPanel(QtWidgets.QWidget):
    """Reusable standard Qt composition for one SLM.

    The panel owns presentation widgets only. ``SLMQtSession`` owns runtime
    and workflow behavior and binds itself to this panel.
    """

    sigConnectionRequested = QtCore.Signal(bool)

    def __init__(
        self,
        *,
        section_snapshots: Mapping[str,SLMSectionSnapshot],
        initial_frame: np.ndarray,
        current_config_path: str | None=None,
        section_display_mode: SectionsDisplayMode=SectionsDisplayMode.TABS,
        render_policy: RenderPolicy=DEFAULT_RENDER_POLICY,
        layout_policy: SLMPanelLayoutPolicy=DEFAULT_SLM_PANEL_LAYOUT_POLICY,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.render_policy = render_policy
        if not isinstance(layout_policy,SLMPanelLayoutPolicy):
            raise TypeError("layout_policy must be an SLMPanelLayoutPolicy")
        self.layout_policy = layout_policy
        self.body_splitter: QtWidgets.QSplitter | None = None
        self.sections_widget: QtWidgets.QWidget | None = None

        section_render_policy = replace(
            render_policy,show_topology_settings=False,
        )
        self.section_collection = SectionsCollectionView(
            section_snapshots=section_snapshots,
            render_policy=section_render_policy,
            parent=self,
        )
        self.section_host = SectionsViewHost(
            self.section_collection,
            display_mode=section_display_mode,
            show_settings=False,
            parent=self,
        )
        self.config_controls = ConfigControls(self)
        if current_config_path:
            self.config_controls.set_current_config(
                {"path":str(current_config_path)}
            )

        self.preview_view: SLMPreviewView | None = None
        self.preview_panel: SLMPreviewPanel | None = None
        if layout_policy.preview_placement != PreviewPlacement.NONE:
            self.preview_view = SLMPreviewView(self)
            self.preview_view.bind_sections_host(
                self.section_host,
                highlight_active_section=layout_policy.highlight_active_section,
            )
            collapsible = (
                layout_policy.preview_container
                == PreviewContainer.COLLAPSIBLE
            )
            self.preview_panel = SLMPreviewPanel(
                view=self.preview_view,
                target_height=layout_policy.preview_initial_size,
                collapsible=collapsible,
                resizable=(
                    layout_policy.preview_resizable and collapsible
                ),
                min_content_height=layout_policy.preview_min_size,
                parent=self,
            )

        self._connection_button = BetterPushButton("Connect to SLM")
        self._connection_button.setCheckable(True)
        self._connection_button.setFixedHeight(20)
        self._connection_button.hide()
        self._connection_button.toggled.connect(
            lambda checked:self.sigConnectionRequested.emit(bool(checked))
        )

        self.status_label = ElidedLabel(parent=self)
        self._settings_button = self._create_settings_button()
        self._settings_in_preview_header = bool(
            self._settings_button is not None
            and self.preview_panel is not None
            and layout_policy.preview_container == PreviewContainer.COLLAPSIBLE
        )
        if self._settings_in_preview_header:
            self.preview_panel.add_header_widget(
                self._settings_button,trailing=True,
            )

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(4)

        # A bare child layout can itself receive excess vertical space from
        # QVBoxLayout, which makes its fixed-height controls appear vertically
        # centered when the editor body is hidden.  Wrap the header in a real
        # fixed-height widget so its geometry is always pinned to the top.
        self.header_widget = QtWidgets.QWidget(self)
        self.header_widget.setLayout(self._create_header())
        self.header_widget.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,QtWidgets.QSizePolicy.Fixed,
        )
        outer.addWidget(self.header_widget,0,QtCore.Qt.AlignTop)

        self.body_widget = self._create_body()
        outer.addWidget(self.body_widget,1)
        self._outer_layout = outer

        self.set_frame(initial_frame)

    def _create_header(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0,0,0,0)
        row.addWidget(self._connection_button)
        row.addWidget(self.status_label)
        row.addStretch(1)
        if self._settings_button is not None and not self._settings_in_preview_header:
            row.addWidget(self._settings_button)
        row.addWidget(self.config_controls)
        return row

    def _create_body(self) -> QtWidgets.QWidget:
        policy = self.layout_policy
        if (
            policy.preview_placement == PreviewPlacement.TOP
            and policy.preview_container == PreviewContainer.COLLAPSIBLE
        ):
            return self._create_collapsible_top_body()

        if policy.preview_placement == PreviewPlacement.NONE:
            return self._create_sections_scroll()

        return self._create_split_body()

    def _create_collapsible_top_body(self) -> QtWidgets.QScrollArea:
        if self.preview_panel is None:
            raise RuntimeError("TOP collapsible layout requires a preview panel")

        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        middle = QtWidgets.QWidget(scroll)
        middle_layout = QtWidgets.QVBoxLayout(middle)
        middle_layout.setContentsMargins(3,3,3,3)
        middle_layout.setSpacing(5)
        middle_layout.setAlignment(QtCore.Qt.AlignTop)
        middle_layout.addWidget(self.preview_panel)
        middle_layout.addWidget(self.section_host)
        self.sections_widget = self.section_host
        middle_layout.addStretch(1)
        scroll.setWidget(middle)
        return scroll

    def _create_sections_scroll(self) -> QtWidgets.QScrollArea:
        scroll = QtWidgets.QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        scroll.setWidget(self.section_host)
        self.sections_widget = scroll
        return scroll

    def _create_split_body(self) -> QtWidgets.QSplitter:
        policy = self.layout_policy
        if self.preview_panel is None:
            raise RuntimeError("Split preview layout requires a preview panel")

        orientation = (
            QtCore.Qt.Vertical
            if policy.preview_placement == PreviewPlacement.TOP
            else QtCore.Qt.Horizontal
        )
        splitter = QtWidgets.QSplitter(orientation,self)
        splitter.setObjectName("SLMPanelPreviewSplitter")
        splitter.setChildrenCollapsible(False)

        sections = self._create_sections_scroll()
        preview = self.preview_panel
        initial = policy.preview_initial_size
        remainder = max(2 * initial,600)

        if orientation == QtCore.Qt.Vertical:
            preview.setMinimumHeight(policy.preview_min_size)
        else:
            preview.setMinimumWidth(policy.preview_min_size)

        if policy.preview_placement == PreviewPlacement.RIGHT:
            splitter.addWidget(sections)
            splitter.addWidget(preview)
            splitter.setSizes([remainder,initial])
            splitter.setStretchFactor(0,1)
            splitter.setStretchFactor(1,0)
        else:
            splitter.addWidget(preview)
            splitter.addWidget(sections)
            splitter.setSizes([initial,remainder])
            splitter.setStretchFactor(0,0)
            splitter.setStretchFactor(1,1)

        if not policy.preview_resizable and splitter.count() > 1:
            splitter.handle(1).setEnabled(False)

        self.body_splitter = splitter
        return splitter

    def _create_settings_button(self):
        if not self.render_policy.show_topology_settings:
            return None
        button = QtWidgets.QToolButton()
        button.setText(chr(0x2699))
        button.setToolTip("SLM section settings")
        button.setAutoRaise(True)
        button.setFocusPolicy(QtCore.Qt.NoFocus)
        button.setFixedSize(22,22)
        button.clicked.connect(
            lambda _checked=False:self.section_host.sigSettingsRequested.emit()
        )
        button.setStyleSheet("""
            QToolButton { border: none; background: transparent; padding: 0px; margin: 0px; font-size: 16px; }
            QToolButton:hover { background: rgba(255,255,255,20); }
            QToolButton:pressed { background: rgba(255,255,255,35); }
        """)
        return button

    def set_frame(self,frame: np.ndarray) -> None:
        if self.preview_view is not None:
            self.preview_view.set_frame(frame)

    def clear_frame(self) -> None:
        if self.preview_view is not None:
            self.preview_view.clear_frame()

    def set_config_only_view(self,enabled: bool) -> None:
        """Hide editable sections while keeping config controls and preview."""
        config_only = bool(enabled)
        has_preview = self.preview_panel is not None

        if self.sections_widget is not None:
            self.sections_widget.setVisible(not config_only)
        self.body_widget.setVisible((not config_only) or has_preview)
        self._outer_layout.setStretchFactor(
            self.body_widget,1 if ((not config_only) or has_preview) else 0,
        )
        self._outer_layout.invalidate()

        self.config_controls.set_editing_actions_enabled(not config_only)
        if self._settings_button is not None:
            self._settings_button.setVisible(not config_only)
        if self.preview_view is not None:
            self.preview_view.set_section_highlight_enabled(
                (not config_only) and self.layout_policy.highlight_active_section
            )
        self.updateGeometry()

    def compact_height_hint(self) -> int:
        """Return the useful fast-mode height for header plus visible preview."""
        margins = self._outer_layout.contentsMargins()
        height = (
            self.header_widget.sizeHint().height()
            + margins.top() + margins.bottom()
        )
        if self.preview_panel is not None and not self.preview_panel.isHidden():
            preview_height = max(
                self.preview_panel.sizeHint().height(),
                self.preview_panel.height(),
            )
            height += self._outer_layout.spacing() + max(0,preview_height)
        return height

    def set_status(self,text: str,error: bool=False) -> None:
        value = str(text or "")
        if error and value:
            value = "Error: " + value
        self.status_label.set_full_text(value)
        self.status_label.setStyleSheet(
            "color: %s;" % ("#a33" if error else "#888")
        )

    @property
    def connection_control_visible(self) -> bool:
        return not self._connection_button.isHidden()

    @property
    def connection_state(self) -> bool:
        return bool(self._connection_button.isChecked())

    def set_connection_control_visible(self,visible: bool) -> None:
        self._connection_button.setVisible(bool(visible))

    def set_connection_busy(self,busy: bool) -> None:
        self._connection_button.setEnabled(not bool(busy))

    def set_connection_state(self,connected: bool) -> None:
        blocker = QtCore.QSignalBlocker(self._connection_button)
        try:
            self._connection_button.setChecked(bool(connected))
        finally:
            del blocker
        self._connection_button.setText(
            "Disconnect" if connected else "Connect to SLM"
        )

    def set_interaction_locked(self,locked: bool) -> None:
        self.setEnabled(not bool(locked))

    def replace_sections(
        self,section_snapshots: Mapping[str,SLMSectionSnapshot],
    ) -> SectionsCollectionView:
        old_collection = self.section_collection
        preferences = old_collection.auto_recompute_preferences()
        current_key = self.section_host.current_section_key()
        old_views = [
            old_collection.section_view(key)
            for key in old_collection.section_keys
        ]

        replacement = SectionsCollectionView(
            section_snapshots=section_snapshots,
            render_policy=old_collection.render_policy,
            group_factory=old_collection.group_factory,
            parent=self.section_host,
        )
        replacement.apply_auto_recompute_preferences(preferences)
        titles = {
            key:(
                getattr(snapshot.presentation,"title",None)
                or "Section %d" % (index + 1)
            )
            for index,(key,snapshot) in enumerate(section_snapshots.items())
        }
        self.section_host.set_collection(replacement,section_titles=titles)
        if current_key in replacement.section_keys:
            self.section_host.set_current_section_key(current_key)
        self.section_collection = replacement

        for view in old_views:
            view.deleteLater()
        old_collection.deleteLater()
        return replacement

    def show_error(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.critical(self,str(title),str(message))

    def show_warning(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.warning(self,str(title),str(message))

    def show_info(self,title: str,message: Any) -> None:
        QtWidgets.QMessageBox.information(self,str(title),str(message))
