"""Stable host-facing Qt integration API for :mod:`slmcore`.

Importing :mod:`slmcore` itself never imports Qt. Advanced implementation
widgets remain available from their explicit ``slmcore.qt.*`` modules rather
than being re-exported here.
"""

from .application.factory import SLMQtSessionFactory
from ..application.control_mode import SLMControlMode
from .application.interaction import (
    DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS,
    RuntimeViewInteractionSettings,
)
from .application.session import SLMQtSession
from .application.session_group import SLMQtSessionGroup
from .panel.panel import SLMPanel
from .panel.policy import (
    DEFAULT_SLM_PANEL_LAYOUT_POLICY,
    PreviewContainer,
    PreviewPlacement,
    SLMPanelLayoutPolicy,
)
from .preview.panel import SLMPreviewPanel
from .preview.view import SLMPreviewView
from .sections.display import SectionsDisplayMode
from .sections.policy import DEFAULT_RENDER_POLICY,RenderPolicy
from .widgets.control_mode import SLMControlModeSelector

__all__ = [
    "DEFAULT_RENDER_POLICY",
    "DEFAULT_RUNTIME_VIEW_INTERACTION_SETTINGS",
    "DEFAULT_SLM_PANEL_LAYOUT_POLICY",
    "PreviewContainer",
    "PreviewPlacement",
    "RenderPolicy",
    "RuntimeViewInteractionSettings",
    "SectionsDisplayMode",
    "SLMPanel",
    "SLMPanelLayoutPolicy",
    "SLMPreviewPanel",
    "SLMPreviewView",
    "SLMQtSession",
    "SLMQtSessionFactory",
    "SLMQtSessionGroup",
    "SLMControlMode",
    "SLMControlModeSelector",
]
