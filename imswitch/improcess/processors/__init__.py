"""
ImProcess processor plugins.

Processors operate on ProcessingResults and are stackable. They are registered
in the global registry alongside reconstructors.
"""

from .base import Processor
from .channel_merge import ChannelMergeProcessor
from .channel_split import ChannelSplitProcessor
from .colocalization import ColocalizationProcessor
from .combine import StackCombineProcessor
from .denoise import DenoiseProcessor
from .drift_correct import DriftCorrectProcessor
from .frc import FRCProcessor
from .make_composite import MakeCompositeProcessor
from .make_rgb import MakeRGBProcessor
from .multicolor_apply import MulticolorApplyProcessor
from .multicolor_registration import MulticolorRegistrationProcessor
from .projection import ProjectionProcessor
from .psf_resolution import PSFResolutionProcessor
from .segmentation import SegmentationProcessor
from .smlm_drift import SmlmDriftProcessor
from .smlm_filter import SmlmFilterProcessor
from .smlm_group import SmlmGroupProcessor
from .smlm_render import SmlmRenderProcessor
from .stack_split import StackSplitProcessor
from .stack_subset import StackSubsetProcessor


_AVAILABLE_PROCESSOR_CLASSES = {
    'channel-merge': ChannelMergeProcessor,
    'channel-split': ChannelSplitProcessor,
    'colocalization': ColocalizationProcessor,
    'drift-correct': DriftCorrectProcessor,
    'frc': FRCProcessor,
    'make-composite': MakeCompositeProcessor,
    'make-rgb': MakeRGBProcessor,
    'multicolor-apply': MulticolorApplyProcessor,
    'multicolor-registration': MulticolorRegistrationProcessor,
    'denoise': DenoiseProcessor,
    'projection': ProjectionProcessor,
    'psf-resolution': PSFResolutionProcessor,
    'segmentation': SegmentationProcessor,
    'smlm-drift': SmlmDriftProcessor,
    'smlm-filter': SmlmFilterProcessor,
    'smlm-group': SmlmGroupProcessor,
    'smlm-render': SmlmRenderProcessor,
    'stack-combine': StackCombineProcessor,
    'stack-split': StackSplitProcessor,
    'stack-subset': StackSubsetProcessor,
}

# User drop-in analysis plugins discovered from the plugins directory
# (imswitch/improcess/plugins). Populated at ImProcess startup by
# load_user_plugins(); empty until then, so importing this package never runs
# user code. Built-in ids always win a collision (enforced at load time), so a
# stray file cannot shadow a core processor.
_USER_PROCESSOR_CLASSES: dict[str, type] = {}


def _all_processor_classes() -> dict[str, type]:
    """Built-in processor classes plus discovered user drop-in plugins."""
    return {**_AVAILABLE_PROCESSOR_CLASSES, **_USER_PROCESSOR_CLASSES}


def load_user_plugins(directory: str | None = None) -> tuple[list[str], list]:
    """Discover user drop-in processor plugins and register them for use.

    Populates the module-level user-plugin table so discovered processors show
    up in every enumeration (runtime tool loader, config validation) and can be
    instantiated by id like a built-in. Built-in ids take precedence: a user
    plugin that reuses a built-in id is rejected with a logged error.

    Returns ``(loaded_ids, errors)``. Never raises — discovery is tolerant.
    """
    from imswitch.improcess.plugins.user_plugins import (
        PluginLoadError,
        discover_processor_plugins,
    )

    classes, errors = discover_processor_plugins(directory)
    errors = list(errors)
    _USER_PROCESSOR_CLASSES.clear()
    loaded: list[str] = []
    for processor_id, processor_cls in classes.items():
        if processor_id in _AVAILABLE_PROCESSOR_CLASSES:
            errors.append(
                PluginLoadError(
                    path=getattr(processor_cls, "__module__", processor_id),
                    message=(
                        f"Plugin processor id {processor_id!r} collides with a "
                        f"built-in; the built-in is kept."
                    ),
                )
            )
            continue
        _USER_PROCESSOR_CLASSES[processor_id] = processor_cls
        loaded.append(processor_id)
    return loaded, errors


def clear_user_plugins() -> None:
    """Forget all discovered user plugins (test/reset helper)."""
    _USER_PROCESSOR_CLASSES.clear()


def available_processor_ids() -> list[str]:
    """Return processor IDs (built-in + user plugins) accepted by config."""
    return sorted(_all_processor_classes())


def available_processor_choices() -> list[tuple[str, str]]:
    """Return processor ids and display names (built-in + user plugins)."""
    return sorted(
        (processor_id, plugin_cls.name)
        for processor_id, plugin_cls in _all_processor_classes().items()
    )


def available_processor_specs() -> list[tuple[str, str, str]]:
    """Return processor ids, display names and categories (built-in + user)."""
    return sorted(
        (
            processor_id,
            plugin_cls.name,
            getattr(plugin_cls, "category", "Other"),
        )
        for processor_id, plugin_cls in _all_processor_classes().items()
    )


def register_processor_by_id(registry, processor_id: str) -> Processor:
    """Instantiate and register one processor (built-in or user plugin) by id."""
    try:
        plugin_cls = _all_processor_classes()[processor_id]
    except KeyError as exc:
        raise KeyError(f"Unknown processor id: {processor_id!r}") from exc
    plugin = plugin_cls()
    registry.register_processor(plugin)
    return plugin


def register_default_processors(registry, filter_ids: list[str] | None = None) -> None:
    """
    Register built-in processors plus all discovered user drop-in plugins.

    Built-ins are gated by config (``filter_ids``); user plugins are gated by
    their presence on disk, so a dropped-in plugin is always active.

    Args:
        registry: Plugin registry to populate.
        filter_ids: Optional list of built-in processor ids to register. None
            registers all built-ins.
    """
    if filter_ids is None:
        to_register = list(_AVAILABLE_PROCESSOR_CLASSES.items())
    else:
        all_classes = _all_processor_classes()
        unknown = [pid for pid in filter_ids if pid not in all_classes]
        if unknown:
            raise KeyError(
                f"Unknown processor id(s): {unknown}. "
                f"Available processors: {available_processor_ids()}"
            )
        to_register = [(pid, all_classes[pid]) for pid in filter_ids]

    registered_ids = set()
    for pid, plugin_cls in to_register:
        registry.register_processor(plugin_cls())
        registered_ids.add(pid)

    # User drop-in plugins: active whenever present, regardless of the built-in
    # filter (unless already registered via an explicit filter id).
    for pid, plugin_cls in _USER_PROCESSOR_CLASSES.items():
        if pid in registered_ids:
            continue
        registry.register_processor(plugin_cls())


__all__ = [
    "Processor",
    "ChannelMergeProcessor",
    "ChannelSplitProcessor",
    "ColocalizationProcessor",
    "DriftCorrectProcessor",
    "FRCProcessor",
    "MakeCompositeProcessor",
    "MakeRGBProcessor",
    "MulticolorApplyProcessor",
    "MulticolorRegistrationProcessor",
    "DenoiseProcessor",
    "ProjectionProcessor",
    "PSFResolutionProcessor",
    "SegmentationProcessor",
    "SmlmDriftProcessor",
    "SmlmFilterProcessor",
    "SmlmGroupProcessor",
    "SmlmRenderProcessor",
    "StackCombineProcessor",
    "StackSplitProcessor",
    "StackSubsetProcessor",
    "available_processor_choices",
    "available_processor_ids",
    "available_processor_specs",
    "register_processor_by_id",
    "register_default_processors",
    "load_user_plugins",
    "clear_user_plugins",
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
