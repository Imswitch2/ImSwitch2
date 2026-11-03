"""The designer's catalogue: the workflow editor opened on a smart pipeline.

The editor takes a :class:`~imswitch.imcommon.model.workfloweditor.catalog.Catalog`;
for a pipeline that is ImProcess's catalogue of processors (built by
ImProcess's own builder when that module is loaded; empty otherwise) plus
the five pipeline step kinds as descriptors, with the setup's detectors, the
calibration ids and the action catalogue filled into their forms, and the
pipeline recipes on the start page. Qt-free.
"""

from __future__ import annotations

from dataclasses import replace

from imswitch.imcommon.model.smartpipeline.actions import ActionCatalog
from imswitch.imcommon.model.smartpipeline.descriptors import pipeline_descriptors
from imswitch.imcommon.model.smartpipeline.recipes import pipeline_recipes
from imswitch.imcommon.model.workfloweditor.catalog import Catalog


def improcess_catalog(registry=None):
    """ImProcess's editor catalogue over ``registry``, or ``None`` when the
    ImProcess module cannot be imported (a lazy import: ImControl never
    imports ImProcess at module level)."""
    try:
        from imswitch.improcess.model.workfloweditor.catalog import build_catalog
    except Exception:  # noqa: BLE001 - ImProcess is optional
        return None
    try:
        return build_catalog(registry) if registry is not None else build_catalog()
    except Exception:  # noqa: BLE001
        return None


def pipeline_catalog(*, registry=None, detectors=(), transforms=(), actions: ActionCatalog | None = None,
                     recipe_defaults: dict | None = None, base: Catalog | None = None) -> Catalog:
    """The catalogue the designer opens with.

    ``base`` is the processor catalogue to extend (ImProcess's for
    ``registry`` when not given); ``detectors`` and ``transforms`` are the
    names the live and transform steps may pick from; ``actions`` the
    action catalogue; ``recipe_defaults`` the keyword arguments of
    :func:`~imswitch.imcommon.model.smartpipeline.recipes.pipeline_recipes`.
    """
    if base is None:
        base = improcess_catalog(registry) or Catalog.empty()
    steps = pipeline_descriptors(detectors=detectors, transforms=transforms, actions=actions)
    defaults = dict(recipe_defaults or {})
    if "detector" not in defaults and detectors:
        defaults["detector"] = str(tuple(detectors)[0])
    if "transform" not in defaults and transforms:
        defaults["transform"] = str(tuple(transforms)[0])

    def recipes():
        return pipeline_recipes(**defaults)

    return replace(base, steps=steps, recipes=recipes, source_file_filter=None, snippets=base.snippets)


__all__ = ["improcess_catalog", "pipeline_catalog"]


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
