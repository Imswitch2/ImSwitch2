"""What the workflow editor can offer: every installed plugin, described.

Built from the same registry a run uses (:func:`bootstrap_registry`), so a
step the editor offers is one the runner will find, and read through the
same declarations the runner validates against: ``default_params``,
``param_keys``, ``output_spec``, the arity, and the headless contract
(:func:`contract_problem`). The field specs come from ``param_spec()``
(:mod:`imswitch.improcess.model.param_spec`). Nothing here imports Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from imswitch.improcess.model.param_spec import ParamField, fields_by_key, spec_for

PROCESSOR = "processor"
RECONSTRUCTOR = "reconstructor"
BUILTIN = "builtin"
USER = "user"


@dataclass(frozen=True)
class PluginEntry:
    """One plugin as the editor sees it.

    ``fields`` is the parameter spec; ``extra_keys`` the keys permitted beyond
    it (no default, so no field); ``accepts_any_key`` says the plugin takes
    free-form parameters. ``gui_only`` is ``None`` when the plugin can run in
    a workflow, otherwise why not (it is still listed, greyed, so the reason
    is visible).
    """

    id: str
    kind: str  # PROCESSOR | RECONSTRUCTOR
    name: str
    category: str
    version: str
    description: str
    origin: str  # BUILTIN | USER
    fields: tuple[ParamField, ...]
    extra_keys: tuple[str, ...]
    accepts_any_key: bool
    min_inputs: int
    max_inputs: int | None
    accepted_kinds: tuple[str, ...]
    supports_consolidation: bool
    accepts_roi: bool
    roi_modes: tuple[str, ...]
    gui_only: str | None
    plugin: Any = field(repr=False, compare=False, hash=False, default=None)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})"

    @property
    def is_processor(self) -> bool:
        return self.kind == PROCESSOR

    @property
    def arity(self) -> str:
        """``1``, ``2..2``, ``2..∞``: how many inputs one run takes."""
        if self.max_inputs == self.min_inputs:
            return str(self.min_inputs)
        high = "∞" if self.max_inputs is None else str(self.max_inputs)
        return f"{self.min_inputs}..{high}"

    def defaults(self) -> dict:
        return {f.key: f.default for f in self.fields}

    def field(self, key: str) -> ParamField | None:
        return fields_by_key(self.fields).get(key)

    def param_keys(self) -> frozenset[str] | None:
        """Keys a step may set, or ``None`` when any key is accepted."""
        if self.accepts_any_key:
            return None
        return frozenset(f.key for f in self.fields) | frozenset(self.extra_keys)

    def output_spec(self, params: dict | None = None):
        """The processor's ports for ``params`` (defaults filled in); ``None``
        for a reconstructor, whose one port is always ``out``."""
        if self.plugin is None or self.kind != PROCESSOR:
            return None
        merged = {**self.defaults(), **dict(params or {})}
        try:
            return self.plugin.output_spec(merged)
        except Exception:
            return None


@dataclass(frozen=True)
class Catalog:
    """Every plugin the editor may put in a step, plus the save vocabulary."""

    registry: Any
    processors: tuple[PluginEntry, ...]
    reconstructors: tuple[PluginEntry, ...]
    save_formats: tuple[str, ...]
    path_placeholders: tuple[str, ...]

    def get(self, kind: str, plugin_id: str) -> PluginEntry | None:
        entries = self.processors if kind == PROCESSOR else self.reconstructors
        for entry in entries:
            if entry.id == plugin_id:
                return entry
        return None

    def processor(self, plugin_id: str) -> PluginEntry | None:
        return self.get(PROCESSOR, plugin_id)

    def reconstructor(self, plugin_id: str) -> PluginEntry | None:
        return self.get(RECONSTRUCTOR, plugin_id)

    def categories(self) -> tuple[str, ...]:
        """Processor categories, alphabetical, ``Other`` last."""
        names = sorted({entry.category for entry in self.processors})
        if "Other" in names:
            names.remove("Other")
            names.append("Other")
        return tuple(names)

    def processors_in(self, category: str) -> tuple[PluginEntry, ...]:
        return tuple(entry for entry in self.processors if entry.category == category)

    def consolidators(self) -> tuple[PluginEntry, ...]:
        return tuple(entry for entry in self.reconstructors if entry.supports_consolidation)

    def runnable(self, kind: str) -> tuple[PluginEntry, ...]:
        entries = self.processors if kind == PROCESSOR else self.reconstructors
        return tuple(entry for entry in entries if entry.gui_only is None)


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


def build_catalog(registry=None, *, user_plugins: bool = True) -> Catalog:
    """Describe every plugin in ``registry``; a fresh full registry when None.

    Built when the editor opens rather than at import, because drop-in
    plugins can change between sessions and the registry stamps versions.
    """
    from imswitch.improcess.model.save_protocol import FORMAT_ALIASES
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
