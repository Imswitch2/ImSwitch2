"""The patcher canvas: a step per box, a cable per reference.

:class:`WorkflowScene` draws a :class:`~imswitch.improcess.model.workfloweditor.WorkflowDocument`
and turns gestures into requests; :class:`CanvasView` is the widget around
it (zoom, pan, overlays, palette drops); :class:`QuickAddPopup` the search
that opens when a cable is dropped in empty space.
"""

from .items import EdgeItem, NodeItem, PortItem, RunView
from .quickadd import QuickAddPopup
from .scene import WorkflowScene
from .view import STEP_MIME, CanvasView

__all__ = ["STEP_MIME", "CanvasView", "EdgeItem", "NodeItem", "PortItem", "QuickAddPopup", "RunView", "WorkflowScene"]
