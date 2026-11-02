"""What the workflow editor can offer: every installed plugin, described.

Built by the host from the same registry a run uses (ImProcess:
``imswitch.improcess.model.workfloweditor.catalog.build_catalog``), so a
step the editor offers is one the runner will find, and read through the
same declarations the runner validates against: ``default_params``,
``param_keys``, ``output_spec``, the arity, and the headless contract.
The field specs come from ``param_spec()``
(:mod:`imswitch.imcommon.model.param_spec`). Nothing here imports Qt, and
nothing here imports the host: what the editor needs of it beyond the
plugins (a file filter for recordings, recipes, a snippet store, how a save
format is spelled) is carried by the :class:`Catalog`, each part optional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from imswitch.imcommon.model.param_spec import ParamField, fields_by_key, spec_for

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
            from imswitch.imcommon.model.workflow.output_spec import call_output_spec

            return call_output_spec(self.plugin, merged)
        except Exception:
            return None


@dataclass(frozen=True)
class Catalog:
    """Every plugin the editor may put in a step, plus the save vocabulary.

    ``registry`` is what a run uses and what ``validate()`` is handed; the
    editor also asks it, duck-typed and only when present, for
    ``normalize_format(fmt)``, ``contract_problem(plugin)``,
    ``close_source(handle)`` and ``source_stem_of(path)`` (see
    :mod:`imswitch.imcommon.model.workflow.steps` and :class:`.session.RunSession`).
    """

    registry: Any
    processors: tuple[PluginEntry, ...]
    reconstructors: tuple[PluginEntry, ...]
    save_formats: tuple[str, ...]
    path_placeholders: tuple[str, ...]
    #: What the host offers beyond its plugins. Each is optional; the editor
    #: does without when it is None.
    #: A file-dialog name filter for the recordings a Source can be bound to.
    source_file_filter: str | None = None
    #: ``recipes() -> list`` of ready-made workflows for the start page: objects
    #: with ``name``, ``title``, ``summary`` and ``workflow() -> Workflow`` (which
    #: may raise ``ValueError``). Called whenever the start page is shown.
    recipes: Callable[[], list] | None = field(default=None, compare=False)
    #: A snippet store for Python steps: ``list_snippets() -> [name]`` and
    #: ``load_snippet(name) -> (code, ports)``. ImProcess passes its module.
    snippets: Any = field(default=None, compare=False)

    @classmethod
    def empty(cls) -> "Catalog":
        """A catalog with nothing in it (an editor with no host)."""
        return cls(registry=None, processors=(), reconstructors=(), save_formats=(), path_placeholders=())

    def normalize_format(self, fmt: str) -> str:
        """``fmt`` as the registry spells it (``ValueError`` when it cannot
        write it); ``fmt`` itself when the registry does not say."""
        normalize = getattr(self.registry, "normalize_format", None)
        return normalize(fmt) if callable(normalize) else str(fmt)

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


__all__ = ["BUILTIN", "PROCESSOR", "RECONSTRUCTOR", "USER", "Catalog", "PluginEntry"]


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
