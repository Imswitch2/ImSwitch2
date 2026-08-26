from __future__ import annotations



from qtpy import QtWidgets

from ..widgets.uitools import BetterPushButton,CollapsibleSection
from .view import SLMPreviewView


_PREVIEW_STYLE = {"button_height":20,"fontsize":9}


class SLMPreviewPanel(QtWidgets.QWidget):
    """Standard decorated presentation around :class:`SLMPreviewView`.

    ``collapsible=True`` provides the standard compact SLM-panel presentation.
    ``collapsible=False`` keeps the viewer independent of collapse semantics so
    it can participate naturally in a ``QSplitter``.
    """

    def __init__(
        self,
        *,
        view: SLMPreviewView | None=None,
        title: str="SLM Preview",
        target_height: int=220,
        collapsible: bool=True,
        resizable: bool=False,
        min_content_height: int=0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.view = view or SLMPreviewView(self)
        self.section: CollapsibleSection | None = None
        self._header_layout: QtWidgets.QHBoxLayout | None = None

        self.reset_button = BetterPushButton("Reset View")
        self.reset_button.setFixedHeight(20)
        self.reset_button.clicked.connect(
            lambda _checked=False:self.view.reset_view()
        )

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        if collapsible:
            section = CollapsibleSection(
                title,
                target_height=target_height,
                resizable=resizable,
                min_content_height=min_content_height,
                **_PREVIEW_STYLE,
            )
            section.addHeaderWidget(self.reset_button)

            content = QtWidgets.QVBoxLayout()
            content.setContentsMargins(15,0,15,0)
            content.addWidget(self.view)
            section.setContentLayout(content)
            self.section = section
            layout.addWidget(section)
        else:
            header = QtWidgets.QHBoxLayout()
            header.setContentsMargins(0,0,0,0)
            header.setSpacing(4)
            title_label = QtWidgets.QLabel(str(title),self)
            header.addWidget(title_label)
            header.addStretch(1)
            header.addWidget(self.reset_button)
            self._header_layout = header
            layout.addLayout(header)

            content_widget = QtWidgets.QWidget(self)
            content = QtWidgets.QVBoxLayout(content_widget)
            content.setContentsMargins(15,0,15,0)
            content.addWidget(self.view)
            layout.addWidget(content_widget,1)

    @property
    def collapsible(self) -> bool:
        return self.section is not None

    def add_header_widget(
        self,widget,*,position=None,trailing: bool=False,
    ) -> None:
        if self.section is not None:
            if trailing:
                position = self.section.headerLayout.count()
            self.section.addHeaderWidget(widget,position=position)
            return

        if self._header_layout is None:
            return
        if trailing:
            position = self._header_layout.count()
        if position is None:
            position = self._header_layout.count() - 1
        self._header_layout.insertWidget(position,widget)

    def bind_sections_host(
        self,section_host,*,highlight_active_section: bool=True,
    ) -> None:
        self.view.bind_sections_host(
            section_host,
            highlight_active_section=highlight_active_section,
        )

    def set_frame(self,frame) -> None:
        self.view.set_frame(frame)

    def reset_view(self) -> None:
        self.view.reset_view()
