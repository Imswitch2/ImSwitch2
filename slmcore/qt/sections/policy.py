"""Configurable rendering policy for the slmcore Qt projection."""

from __future__ import annotations

from dataclasses import dataclass


from ...engine.parameters.spec import ParamDisplayLevel,ParamSpec


@dataclass(frozen=True)
class RenderPolicy:
    """Policy controlling which backend parameters receive Qt fields."""

    display_levels: tuple[ParamDisplayLevel, ...] = (
        ParamDisplayLevel.PRIMARY,
        ParamDisplayLevel.ADVANCED,
    )
    show_unit_controls: bool = True
    show_topology_settings: bool = True
    show_calibration_controls: bool = True
    show_cgh_controls: bool = True

    def is_parameter_visible(self,spec: ParamSpec) -> bool:
        return (
            not spec.hidden
            and spec.display_level in self.display_levels
        )


DEFAULT_RENDER_POLICY = RenderPolicy()
