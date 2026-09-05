"""napari plugins as one-way endpoints for ImProcess results.

An *endpoint* is a place a result can be sent: a plugin's dock widget that
will see the result's layers in our own viewer, or a plugin's reader that
will open a file we export. It is a one-way street by design -- ImProcess
provides layers or a file, the plugin does what it does, and nothing comes
back unless the user explicitly imports a layer (:mod:`.napari_import`).

Two kinds of endpoint, and an honest distinction between them:

* **Verified** endpoints are adapters somebody wrote: they say which result
  kinds the plugin makes sense for, and for a reader, which of *our* export
  schemas the reader actually understands. A filename pattern match is not
  that; ``*.hdf5`` says nothing about what is inside.
* **Unverified** endpoints are dock widgets found on the installed npe2
  plugins. They are offered for every layerable result, labelled as such,
  because a dock widget that receives layers it does not want simply
  ignores them. Readers are never discovered this way.

Nothing here imports napari or Qt; the npe2 plugin manager is passed in so
tests can hand over a fake one.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from imswitch.improcess.model.napari_layers import LAYERABLE_KINDS
from imswitch.improcess.model.result import RESULT_KINDS, result_kind

LANES = ("dock", "reader", "detached")

#: npe2 manifests that are napari itself, never a plugin to send things to.
_NOT_PLUGINS = frozenset({"napari", "napari-builtins", "builtins"})


class EndpointError(ValueError):
    """An endpoint description is inconsistent, or cannot be used."""


# --------------------------------------------------------------------------
# exporters: the schemas a reader endpoint may name
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Exporter:
    """One way of writing a result to a file a reader plugin can open."""

    id: str
    kinds: tuple[str, ...]
    suffix: str
    write: Callable[[Any, Path], Path]
    description: str = ""


def _via_result_save(fmt: str) -> Callable[[Any, Path], Path]:
    def write(result, path: Path) -> Path:
        result.save(path, fmt)
        return Path(path)

    return write


def _write_picasso(result, path: Path) -> Path:
    from imswitch.improcess.analysis.smlm_export import export_picasso_hdf5

    return Path(export_picasso_hdf5(result, Path(path)))


def _write_localizations_csv(result, path: Path) -> Path:
    result.save(path, "csv")
    return Path(path)


EXPORTERS: dict[str, Exporter] = {
    exporter.id: exporter
    for exporter in (
        Exporter("ome-tiff", ("image", "composite", "rgb"), ".ome.tif", _via_result_save("tiff"),
                 "OME-TIFF with calibration and provenance"),
        Exporter("hdf5", ("image", "composite", "rgb", "labels"), ".h5", _via_result_save("hdf5"),
                 "HDF5 with OME-XML and provenance"),
        Exporter("ome-zarr", ("image", "composite", "rgb"), ".ome.zarr", _via_result_save("zarr"),
                 "OME-NGFF group"),
        Exporter("labels-tiff", ("labels",), ".tif", _via_result_save("tiff"),
                 "Integer label image as TIFF"),
        Exporter("picasso-hdf5", ("localization",), ".hdf5", _write_picasso,
                 "Picasso-style localization HDF5 with YAML info sidecar"),
        Exporter("localizations-csv", ("localization",), ".csv", _write_localizations_csv,
                 "Localization table as CSV (nm columns)"),
    )
}


def exporter_for(exporter_id: str | None) -> Exporter | None:
    return EXPORTERS.get(str(exporter_id or ""))


# --------------------------------------------------------------------------
# descriptors
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OutputMapping:
    """How a layer a plugin *creates* maps back to an ImProcess result.

    Declared by a verified dock adapter, so an import can be offered without
    guessing from a layer-added event. ``layer_name_pattern`` is an fnmatch
    glob against the layer name; ``preserves_grid`` says the plugin's output
    is pixel-aligned with the input it was given (a threshold, a filter), and
    even then the transform equality check in :mod:`.napari_import` applies.
    """

    layer_name_pattern: str
    layer_type: str                     # image | labels | points | shapes
    result_kind: str                    # image | labels | table | rois
    preserves_grid: bool = False
    label: str = ""

    def matches(self, layer_name: str, layer_type: str) -> bool:
        return (
            str(layer_type).lower() == self.layer_type
            and fnmatch.fnmatchcase(str(layer_name), self.layer_name_pattern)
        )


@dataclass(frozen=True)
class NapariEndpoint:
    """Where a result can be sent, and how."""

    id: str
    label: str
    lane: str                            # "dock" | "reader" | "detached"
    plugin_name: str
    widget_name: str | None = None       # dock: display name, or an fnmatch pattern
    reader_plugin: str | None = None     # reader / detached
    export_format: str | None = None     # reader / detached: an Exporter id
    kinds: tuple[str, ...] = ()          # () only for unverified docks
    verified: bool = False
    include_preview: bool = False        # localization: also send the histogram preview
    output_mappings: tuple[OutputMapping, ...] = ()
    description: str = ""

    def __post_init__(self):
        if self.lane not in LANES:
            raise EndpointError(f"{self.id}: unknown lane {self.lane!r}")
        unknown = [kind for kind in self.kinds if kind not in RESULT_KINDS]
        if unknown:
            raise EndpointError(f"{self.id}: unknown result kinds {unknown}")
        if self.lane == "dock":
            if not self.widget_name:
                raise EndpointError(f"{self.id}: a dock endpoint needs a widget name")
            if self.verified and not self.kinds:
                raise EndpointError(f"{self.id}: a verified dock endpoint must declare kinds")
        else:
            if not self.verified:
                raise EndpointError(
                    f"{self.id}: reader endpoints must be verified adapters; a filename "
                    "match does not say the reader understands the exported schema"
                )
            if not self.reader_plugin or not self.export_format:
                raise EndpointError(f"{self.id}: a reader endpoint needs reader_plugin and export_format")
            exporter = exporter_for(self.export_format)
            if exporter is None:
                raise EndpointError(f"{self.id}: unknown export format {self.export_format!r}")
            if not self.kinds:
                raise EndpointError(f"{self.id}: a reader endpoint must declare kinds")
            outside = [kind for kind in self.kinds if kind not in exporter.kinds]
            if outside:
                raise EndpointError(
                    f"{self.id}: exporter {self.export_format!r} cannot write {outside}"
                )

    @property
    def exporter(self) -> Exporter | None:
        return exporter_for(self.export_format)

    def widget_matches(self, display_name: str) -> bool:
        pattern = str(self.widget_name or "")
        return display_name == pattern or fnmatch.fnmatchcase(display_name, pattern)


@dataclass(frozen=True)
class NapariWriterFormat:
    """A plugin writer contribution, usable as an export format for layers."""

    plugin_name: str
    writer_id: str
    display_name: str
    layer_types: tuple[str, ...]
    extensions: tuple[str, ...]


# --------------------------------------------------------------------------
# built-in adapters
# --------------------------------------------------------------------------

BUILTIN_ENDPOINTS: tuple[NapariEndpoint, ...] = (
    NapariEndpoint(
        id="napari-storm:dock",
        label="napari-storm",
        lane="dock",
        plugin_name="napari-storm",
        widget_name="Napari STORM",
        kinds=("localization",),
        verified=True,
        description="SMLM rendering; receives the localization table as a Points layer",
    ),
    NapariEndpoint(
        id="napari-storm:reader",
        label="napari-storm (open exported Picasso file)",
        lane="reader",
        plugin_name="napari-storm",
        reader_plugin="napari-storm",
        export_format="picasso-hdf5",
        kinds=("localization",),
        verified=True,
        description="Exports a Picasso HDF5 and opens it with napari-storm's own reader",
    ),
    NapariEndpoint(
        id="napari-skimage:gaussian",
        label="napari-skimage: Gaussian filter",
        lane="dock",
        plugin_name="napari-skimage",
        widget_name="Gaussian filter",
        kinds=("image", "composite"),
        verified=True,
        output_mappings=(
            OutputMapping("*", "image", "image", preserves_grid=True, label="filtered image"),
        ),
    ),
    NapariEndpoint(
        id="napari-skimage:threshold",
        label="napari-skimage: Automated threshold",
        lane="dock",
        plugin_name="napari-skimage",
        widget_name="Automated Threshold",
        kinds=("image",),
        verified=True,
        output_mappings=(
            OutputMapping("*", "labels", "labels", preserves_grid=True, label="threshold mask"),
            OutputMapping("*", "image", "image", preserves_grid=True, label="threshold image"),
        ),
    ),
    NapariEndpoint(
        id="napari-skimage:label",
        label="napari-skimage: Label connected components",
        lane="dock",
        plugin_name="napari-skimage",
        widget_name="Label connected components",
        kinds=("labels", "image"),
        verified=True,
        output_mappings=(
            OutputMapping("*", "labels", "labels", preserves_grid=True, label="connected components"),
        ),
    ),
)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

_KEYS = {
    "plugin": "plugin_name", "plugin_name": "plugin_name",
    "widget": "widget_name", "widget_name": "widget_name",
    "reader": "reader_plugin", "reader_plugin": "reader_plugin",
    "export": "export_format", "export_format": "export_format",
    "includePreview": "include_preview", "include_preview": "include_preview",
    "outputMappings": "output_mappings", "output_mappings": "output_mappings",
}


def _mapping_from_dict(item: dict) -> OutputMapping:
    return OutputMapping(
        layer_name_pattern=str(item.get("pattern") or item.get("layer_name_pattern") or "*"),
        layer_type=str(item.get("layerType") or item.get("layer_type") or "image").lower(),
        result_kind=str(item.get("kind") or item.get("result_kind") or "image").lower(),
        preserves_grid=bool(item.get("preservesGrid", item.get("preserves_grid", False))),
        label=str(item.get("label") or ""),
    )


def endpoint_from_dict(item: dict) -> NapariEndpoint:
    """One ``processing.napariEndpoints`` entry. Config entries are adapters,
    so they are verified unless they say otherwise."""
    if not isinstance(item, dict):
        raise EndpointError("endpoint entry is not an object")
    fields: dict[str, Any] = {}
    for key, value in item.items():
        fields[_KEYS.get(str(key), str(key))] = value
    kinds = fields.get("kinds", ())
    if isinstance(kinds, str):
        kinds = (kinds,)
    mappings = tuple(_mapping_from_dict(m) for m in (fields.get("output_mappings") or []))
    endpoint_id = str(fields.get("id") or "").strip()
    if not endpoint_id:
        raise EndpointError("endpoint entry has no id")
    return NapariEndpoint(
        id=endpoint_id,
        label=str(fields.get("label") or endpoint_id),
        lane=str(fields.get("lane") or "dock"),
        plugin_name=str(fields.get("plugin_name") or ""),
        widget_name=fields.get("widget_name"),
        reader_plugin=fields.get("reader_plugin"),
        export_format=fields.get("export_format"),
        kinds=tuple(str(kind) for kind in kinds),
        verified=bool(fields.get("verified", True)),
        include_preview=bool(fields.get("include_preview", False)),
        output_mappings=mappings,
        description=str(fields.get("description") or ""),
    )


def endpoints_from_config(config: dict | None) -> tuple[list[NapariEndpoint], list[str]]:
    """Adapters from the setup's ``processing`` block; tolerant of bad entries."""
    endpoints: list[NapariEndpoint] = []
    errors: list[str] = []
    entries = (config or {}).get("napariEndpoints") or (config or {}).get("napari_endpoints") or []
    if not isinstance(entries, list):
        return [], ["processing.napariEndpoints must be a list"]
    for index, item in enumerate(entries):
        try:
            endpoints.append(endpoint_from_dict(item))
        except EndpointError as exc:
            errors.append(f"napariEndpoints[{index}]: {exc}")
    return endpoints, errors


# --------------------------------------------------------------------------
# adapters contributed by drop-in plugin files
# --------------------------------------------------------------------------

#: ``id -> (endpoint, source path)`` for adapters found in drop-in plugin
#: files (a module-level ``NAPARI_ENDPOINTS`` list). Filled by the plugin
#: loader at startup and on reload; empty until then.
_USER_ENDPOINTS: dict[str, tuple[NapariEndpoint, str]] = {}


def register_user_endpoints(items, *, source: str = "") -> tuple[int, list[str]]:
    """Accept ``NapariEndpoint`` objects or setup-file dicts; returns (count, problems)."""
    count = 0
    problems: list[str] = []
    for index, item in enumerate(items or []):
        try:
            endpoint = item if isinstance(item, NapariEndpoint) else endpoint_from_dict(item)
        except EndpointError as exc:
            problems.append(f"NAPARI_ENDPOINTS[{index}]: {exc}")
            continue
        if endpoint.id in _USER_ENDPOINTS and _USER_ENDPOINTS[endpoint.id][1] != source:
            problems.append(
                f"NAPARI_ENDPOINTS[{index}]: id {endpoint.id!r} already defined by "
                f"{_USER_ENDPOINTS[endpoint.id][1]}; keeping the first"
            )
            continue
        _USER_ENDPOINTS[endpoint.id] = (endpoint, source)
        count += 1
    return count, problems


def user_endpoints() -> list[NapariEndpoint]:
    return [endpoint for endpoint, _source in _USER_ENDPOINTS.values()]


def clear_user_endpoints() -> None:
    _USER_ENDPOINTS.clear()


# --------------------------------------------------------------------------
# discovery against the installed plugins
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class InstalledWidget:
    plugin_name: str
    display_name: str
    command: str = ""


@dataclass(frozen=True)
class InstalledPlugins:
    """What discovery learned about the environment, in a form tests can fake."""

    plugin_names: frozenset[str] = frozenset()
    widgets: tuple[InstalledWidget, ...] = ()
    reader_plugins: frozenset[str] = frozenset()
    writers: tuple[NapariWriterFormat, ...] = ()

    def has_plugin(self, name: str) -> bool:
        return name in self.plugin_names

    def widgets_of(self, plugin_name: str) -> list[InstalledWidget]:
        return [w for w in self.widgets if w.plugin_name == plugin_name]


def inspect_installed_plugins(manager=None) -> InstalledPlugins:
    """Read the npe2 plugin manager once; never raises.

    npe1 plugins are indexed too (napari still loads them through npe2's
    adapter), so a plugin that only has an old-style hook is not invisible.
    """
    if manager is None:
        try:
            from npe2 import PluginManager

            manager = PluginManager.instance()
            try:
                manager.discover(include_npe1=True)
                manager.index_npe1_adapters()
            except Exception:
                pass
        except Exception:
            return InstalledPlugins()
    names: set[str] = set()
    widgets: list[InstalledWidget] = []
    readers: set[str] = set()
    writers: list[NapariWriterFormat] = []
    try:
        manifests = list(manager.iter_manifests())
    except Exception:
        return InstalledPlugins()
    for manifest in manifests:
        name = str(getattr(manifest, "name", "") or "")
        if not name or name in _NOT_PLUGINS:
            continue
        names.add(name)
        contributions = getattr(manifest, "contributions", None)
        for widget in (getattr(contributions, "widgets", None) or []):
            widgets.append(InstalledWidget(
                name, str(getattr(widget, "display_name", "") or ""), str(getattr(widget, "command", "") or "")
            ))
        if getattr(contributions, "readers", None):
            readers.add(name)
        for writer in (getattr(contributions, "writers", None) or []):
            writers.append(NapariWriterFormat(
                plugin_name=name,
                writer_id=str(getattr(writer, "command", "") or ""),
                display_name=str(getattr(writer, "display_name", "") or name),
                layer_types=tuple(str(t) for t in (getattr(writer, "layer_types", None) or [])),
                extensions=tuple(str(e) for e in (getattr(writer, "filename_extensions", None) or [])),
            ))
    return InstalledPlugins(frozenset(names), tuple(widgets), frozenset(readers), tuple(writers))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "widget"


def discover_endpoints(
    *,
    installed: InstalledPlugins | None = None,
    config: dict | None = None,
    builtin: Iterable[NapariEndpoint] = BUILTIN_ENDPOINTS,
    include_unverified: bool = True,
) -> tuple[list[NapariEndpoint], list[str]]:
    """Every endpoint that could be offered, verified adapters first.

    Precedence by id: config over drop-in plugin files over built-in. A
    discovered widget that a verified adapter already covers (same plugin,
    matching widget name) is not listed a second time as unverified.
    """
    installed = installed if installed is not None else inspect_installed_plugins()
    by_id: dict[str, NapariEndpoint] = {endpoint.id: endpoint for endpoint in builtin}
    for endpoint in user_endpoints():
        by_id[endpoint.id] = endpoint
    configured, errors = endpoints_from_config(config)
    for endpoint in configured:
        by_id[endpoint.id] = endpoint

    if include_unverified:
        verified_docks = [e for e in by_id.values() if e.lane == "dock" and e.verified]
        for widget in installed.widgets:
            covered = any(
                e.plugin_name == widget.plugin_name and e.widget_matches(widget.display_name)
                for e in verified_docks
            )
            if covered:
                continue
            endpoint_id = f"{widget.plugin_name}:{_slug(widget.display_name)}"
            if endpoint_id in by_id:
                continue
            by_id[endpoint_id] = NapariEndpoint(
                id=endpoint_id,
                label=f"{widget.plugin_name}: {widget.display_name} (unverified)",
                lane="dock",
                plugin_name=widget.plugin_name,
                widget_name=widget.display_name,
                kinds=(),
                verified=False,
            )

    ordered = sorted(by_id.values(), key=lambda e: (not e.verified, e.label.lower()))
    return ordered, errors


# --------------------------------------------------------------------------
# offering and availability
# --------------------------------------------------------------------------

def endpoints_for(result, endpoints: Iterable[NapariEndpoint]) -> list[NapariEndpoint]:
    """The endpoints a result may be sent to.

    Dock endpoints are gated by layerability first, then by declared kinds;
    reader endpoints by their verified exporter/reader pair. A table or a
    curve reaches no endpoint here -- it has no layer form, and no reader
    pair claims to understand a metrics table.
    """
    kind = result_kind(result)
    offered = []
    for endpoint in endpoints:
        if endpoint.lane == "dock":
            if kind not in LAYERABLE_KINDS:
                continue
            if endpoint.kinds and kind not in endpoint.kinds:
                continue
            offered.append(endpoint)
        else:
            exporter = endpoint.exporter
            if not endpoint.verified or exporter is None:
                continue
            if kind in endpoint.kinds and kind in exporter.kinds:
                offered.append(endpoint)
    return offered


def availability(endpoint: NapariEndpoint, installed: InstalledPlugins) -> tuple[bool, str]:
    """Whether the plugin behind ``endpoint`` is installed, and if not, why."""
    if not installed.has_plugin(endpoint.plugin_name):
        return False, f"{endpoint.plugin_name} is not installed (pip install {endpoint.plugin_name})"
    if endpoint.lane == "dock":
        if not resolve_widget(endpoint, installed):
            return False, (
                f"{endpoint.plugin_name} is installed but has no widget named "
                f"{endpoint.widget_name!r}"
            )
        return True, ""
    reader = str(endpoint.reader_plugin)
    if reader not in installed.plugin_names:
        return False, f"{reader} is not installed (pip install {reader})"
    if installed.reader_plugins and reader not in installed.reader_plugins:
        return False, f"{reader} is installed but contributes no reader"
    return True, ""


def resolve_widget(endpoint: NapariEndpoint, installed: InstalledPlugins) -> str | None:
    """The concrete widget display name for a dock endpoint, or None."""
    for widget in installed.widgets_of(endpoint.plugin_name):
        if endpoint.widget_matches(widget.display_name):
            return widget.display_name
    return None


def export_filename(endpoint: NapariEndpoint, result) -> str:
    exporter = endpoint.exporter
    if exporter is None:
        raise EndpointError(f"{endpoint.id} has no exporter")
    stem = _slug(getattr(result, "name", "") or "result")
    return f"{stem}{exporter.suffix}"


def export_result(endpoint: NapariEndpoint, result, directory: Path) -> Path:
    """Write ``result`` the way the endpoint's reader expects; returns the path."""
    exporter = endpoint.exporter
    if exporter is None:
        raise EndpointError(f"{endpoint.id} has no exporter")
    kind = result_kind(result)
    if kind not in exporter.kinds:
        raise EndpointError(f"{exporter.id} cannot write a {kind!r} result")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return exporter.write(result, directory / export_filename(endpoint, result))


def writer_formats_for(layer_types: Iterable[str], installed: InstalledPlugins) -> list[NapariWriterFormat]:
    """Writer contributions able to save *all* of ``layer_types``."""
    wanted = {str(t).lower() for t in layer_types}
    formats = []
    for writer in installed.writers:
        accepted = {t.split("{")[0].lower() for t in writer.layer_types}
        if wanted and (wanted <= accepted or "*" in accepted):
            formats.append(writer)
    return formats


__all__ = [
    "BUILTIN_ENDPOINTS",
    "EXPORTERS",
    "EndpointError",
    "Exporter",
    "InstalledPlugins",
    "InstalledWidget",
    "LANES",
    "NapariEndpoint",
    "NapariWriterFormat",
    "OutputMapping",
    "availability",
    "clear_user_endpoints",
    "discover_endpoints",
    "endpoint_from_dict",
    "endpoints_for",
    "endpoints_from_config",
    "export_filename",
    "export_result",
    "exporter_for",
    "inspect_installed_plugins",
    "register_user_endpoints",
    "resolve_widget",
    "user_endpoints",
    "writer_formats_for",
]


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
