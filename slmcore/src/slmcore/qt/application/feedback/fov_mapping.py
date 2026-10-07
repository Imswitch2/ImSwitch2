"""Qt presentation for the saved/runtime FOV correction state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ....application.fov_mapping_runtime import FOVMappingRuntimeService
    from ...cgh.session_window import CGHSessionWindow


class FOVMappingRuntimeCoordinator:
    """Synchronize read-only FOV correction inspection in the feedback UI."""

    def __init__(self,runtime_service: "FOVMappingRuntimeService") -> None:
        self._runtime = runtime_service

    def synchronize(
        self,section_key: str,window: "CGHSessionWindow",
    ) -> None:
        window.set_fov_mapping_correction_state(
            self._runtime.fov_mapping_context(section_key),
            self._runtime.fov_mapping_preview(section_key),
        )
