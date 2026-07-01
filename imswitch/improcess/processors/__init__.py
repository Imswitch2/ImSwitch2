"""
ImProcess processor plugins.

Processors operate on ProcessingResults and are stackable. They are registered
in the global registry alongside reconstructors.
"""

from .base import Processor
from .channel_split import ChannelSplitProcessor
from .colocalization import ColocalizationProcessor
from .denoise import DenoiseProcessor
from .drift_correct import DriftCorrectProcessor
from .frc import FRCProcessor
from .make_composite import MakeCompositeProcessor
from .multicolor_apply import MulticolorApplyProcessor
from .multicolor_registration import MulticolorRegistrationProcessor
from .projection import ProjectionProcessor
from .psf_resolution import PSFResolutionProcessor
from .segmentation import SegmentationProcessor
from .stack_split import StackSplitProcessor
from .stack_subset import StackSubsetProcessor


_AVAILABLE_PROCESSOR_CLASSES = {
    'channel-split': ChannelSplitProcessor,
    'colocalization': ColocalizationProcessor,
    'drift-correct': DriftCorrectProcessor,
    'frc': FRCProcessor,
    'make-composite': MakeCompositeProcessor,
    'multicolor-apply': MulticolorApplyProcessor,
    'multicolor-registration': MulticolorRegistrationProcessor,
    'denoise': DenoiseProcessor,
    'projection': ProjectionProcessor,
    'psf-resolution': PSFResolutionProcessor,
    'segmentation': SegmentationProcessor,
    'stack-split': StackSplitProcessor,
    'stack-subset': StackSubsetProcessor,
}


def available_processor_ids() -> list[str]:
    """Return built-in processor IDs accepted by setup processing config."""
    return sorted(_AVAILABLE_PROCESSOR_CLASSES)


def available_processor_choices() -> list[tuple[str, str]]:
    """Return built-in processor ids and display names."""
    return sorted(
        (processor_id, plugin_cls.name)
        for processor_id, plugin_cls in _AVAILABLE_PROCESSOR_CLASSES.items()
    )


def register_processor_by_id(registry, processor_id: str) -> Processor:
    """Instantiate and register one built-in processor by id."""
    try:
        plugin_cls = _AVAILABLE_PROCESSOR_CLASSES[processor_id]
    except KeyError as exc:
        raise KeyError(f"Unknown built-in processor id: {processor_id!r}") from exc
    plugin = plugin_cls()
    registry.register_processor(plugin)
    return plugin


def register_default_processors(registry, filter_ids: list[str] | None = None) -> None:
    """
    Register built-in processors.

    Args:
        registry: Plugin registry to populate.
        filter_ids: Optional list of processor ids to register. None registers all.
    """
    if filter_ids is None:
        to_register = _AVAILABLE_PROCESSOR_CLASSES.items()
    else:
        unknown = [pid for pid in filter_ids if pid not in _AVAILABLE_PROCESSOR_CLASSES]
        if unknown:
            raise KeyError(
                f"Unknown built-in processor id(s): {unknown}. "
                f"Available processors: {available_processor_ids()}"
            )
        to_register = [
            (pid, _AVAILABLE_PROCESSOR_CLASSES[pid])
            for pid in filter_ids
        ]
    for _pid, plugin_cls in to_register:
        registry.register_processor(plugin_cls())


__all__ = [
    "Processor",
    "ChannelSplitProcessor",
    "ColocalizationProcessor",
    "DriftCorrectProcessor",
    "FRCProcessor",
    "MakeCompositeProcessor",
    "MulticolorApplyProcessor",
    "MulticolorRegistrationProcessor",
    "DenoiseProcessor",
    "ProjectionProcessor",
    "PSFResolutionProcessor",
    "SegmentationProcessor",
    "StackSplitProcessor",
    "StackSubsetProcessor",
    "available_processor_choices",
    "available_processor_ids",
    "register_processor_by_id",
    "register_default_processors",
]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version).
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
