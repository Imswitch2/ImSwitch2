"""ImProcess's catalog for the workflow editor: every installed plugin, described.

The catalog types live in :mod:`imswitch.imcommon.model.workfloweditor.catalog`
(re-exported here); :func:`build_catalog` fills one from the same registry a
run uses (:func:`bootstrap_registry`), so a step the editor offers is one the
runner will find, and read through the same declarations the runner validates
against: ``default_params``, ``param_keys``, ``output_spec``, the arity, and
the headless contract (:func:`contract_problem`). It also hands the editor
what ImProcess offers beyond its plugins: the file filter for recordings, the
Python-step recipes and the snippet store. Nothing here imports Qt.
"""

from __future__ import annotations

from imswitch.imcommon.model.workfloweditor.catalog import *  # noqa: F401,F403
from imswitch.imcommon.model.workfloweditor.catalog import (
    BUILTIN,
    PROCESSOR,
    RECONSTRUCTOR,
    USER,
    Catalog,
    PluginEntry,
)
from imswitch.imcommon.model.param_spec import spec_for


def _description(plugin) -> str:
    declared = getattr(plugin, "description", "")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    doc = (getattr(type(plugin), "__doc__", "") or "").strip()
    return doc.splitlines()[0].strip() if doc else ""


def _entry(plugin, kind: str, origin: str) -> PluginEntry:
    from imswitch.improcess.model.plugin_contract import contract_problem

    cls = type(plugin)
    extra = getattr(cls, "extra_param_keys", ())
    max_inputs = getattr(plugin, "max_inputs", 1) if kind == PROCESSOR else 1
    return PluginEntry(
        id=str(plugin.id),
        kind=kind,
        name=str(getattr(plugin, "name", plugin.id)),
        category=str(getattr(plugin, "category", "Other") or "Other") if kind == PROCESSOR else "",
        version=str(getattr(plugin, "version", "") or ""),
        description=_description(plugin),
        origin=origin,
        fields=spec_for(cls),
        extra_keys=tuple(str(key) for key in (extra or ())),
        accepts_any_key=extra is None,
        min_inputs=int(getattr(plugin, "min_inputs", 1) or 1) if kind == PROCESSOR else 1,
        max_inputs=None if max_inputs is None else int(max_inputs),
        accepted_kinds=tuple(getattr(plugin, "kinds", ()) or ()) if kind == PROCESSOR else (),
        supports_consolidation=bool(getattr(plugin, "supports_consolidation", False)),
        accepts_roi=bool(getattr(plugin, "accepts_roi", False)),
        roi_modes=tuple(getattr(plugin, "roi_modes", ()) or ()),
        gui_only=contract_problem(plugin),
        plugin=plugin,
    )


def _source_file_filter() -> str | None:
    """The recordings dialog's name filter, or None when it cannot be made."""
    try:
        from imswitch.improcess.model.dataset_sources import SOURCE_SPECS, file_dialog_filter

        return file_dialog_filter(SOURCE_SPECS)
    except Exception:  # noqa: BLE001 - the dialog then offers every file
        return None


def build_catalog(registry=None, *, user_plugins: bool = True) -> Catalog:
    """Describe every plugin in ``registry``; a fresh full registry when None.

    Built when the editor opens rather than at import, because drop-in
    plugins can change between sessions and the registry stamps versions.
    """
    from imswitch.improcess.model import snippets
    from imswitch.improcess.model.save_protocol import FORMAT_ALIASES
    from imswitch.improcess.model.workfloweditor.recipes import installed_recipes
    from imswitch.improcess.processors import available_processor_specs
    from imswitch.improcess.reconstructors import builtin_reconstructor_ids
    from imswitch.improcess.workflows.runner import PATH_PLACEHOLDERS

    if registry is None:
        from imswitch.improcess.workflows.runtime import bootstrap_registry

        registry = bootstrap_registry(user_plugins=user_plugins)

    builtin_processors = {pid for pid, _name, _cat in available_processor_specs(origin=BUILTIN)}
    builtin_reconstructors = set(builtin_reconstructor_ids())
    processors = tuple(sorted(
        (_entry(plugin, PROCESSOR, BUILTIN if plugin.id in builtin_processors else USER)
         for plugin in registry.processors()),
        key=lambda entry: (entry.category, entry.name.lower(), entry.id),
    ))
    reconstructors = tuple(sorted(
        (_entry(plugin, RECONSTRUCTOR, BUILTIN if plugin.id in builtin_reconstructors else USER)
         for plugin in registry.reconstructors()),
        key=lambda entry: (entry.name.lower(), entry.id),
    ))
    formats = tuple(sorted(set(FORMAT_ALIASES.values())))
    return Catalog(
        registry=registry,
        processors=processors,
        reconstructors=reconstructors,
        save_formats=formats,
        path_placeholders=tuple(PATH_PLACEHOLDERS),
        source_file_filter=_source_file_filter(),
        recipes=installed_recipes,
        snippets=snippets,
    )


__all__ = ["BUILTIN", "PROCESSOR", "RECONSTRUCTOR", "USER", "Catalog", "PluginEntry", "build_catalog"]


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
