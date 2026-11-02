"""Session-facing labels/tooltips for saved processing results.

This is deliberately presentation code, not part of ``ProcessingSession``'s
catalog semantics.  Modality-specific provenance details can evolve here
without teaching the generic Session model about reconstruction parameters.
"""

from __future__ import annotations

from pathlib import Path


def filename_display_name(path: Path) -> str:
    name = path.name
    for suffix in (
        ".ome.zarr",
        ".ome.tiff",
        ".ome.tif",
        ".hdf5",
        ".tiff",
        ".zarr",
        ".h5",
        ".tif",
    ):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def reconstruction_presentation(
    graph: dict | None, *, artifact: dict | None = None, fallback_name: str = ""
) -> tuple[str, str, str]:
    """Return ``(session_label, object_name, tooltip)`` from provenance."""
    artifact = dict(artifact or {})
    graph = graph if isinstance(graph, dict) else None
    node = {}
    if graph is not None:
        node_id = artifact.get("node") or (graph.get("output") or {}).get("node")
        if node_id:
            node = (graph.get("nodes") or {}).get(node_id) or {}

    plugin_label = str(node.get("label") or "").strip()
    params = node.get("params") if isinstance(node.get("params"), dict) else {}
    preset = str(params.get("preset") or "").strip()
    # Only promote the plugin label into the compact Session name when it adds
    # useful differentiation (currently a preset/mode). Otherwise keep the
    # result/file name so generic reconstructors do not all collapse to one label.
    session_label = str(fallback_name or plugin_label or "reconstruction")
    if plugin_label and preset:
        session_label = f"{plugin_label} - {preset}"

    object_name = str(artifact.get("result_name") or fallback_name or session_label)

    lines = [session_label]
    if plugin_label:
        lines.append(f"Reconstructor: {plugin_label}")
    if preset:
        lines.append(f"Mode: {preset}")

    # Modality-specific provenance is intentionally presentation-only.  Keeping
    # it here prevents the Session catalog from depending on MoNaLISA's parameter
    # vocabulary while retaining the useful tooltip built during the prototype.
    fwhm = params.get("spot_fwhm")
    fwhm_unit = str(params.get("spot_fwhm_unit") or "").strip()
    if isinstance(fwhm, (int, float)):
        lines.append(f"FWHM: {fwhm:g}{(' ' + fwhm_unit) if fwhm_unit else ''}")
    reassignment = params.get("reassignment")
    if reassignment is not None:
        text = str(reassignment)
        if text.lower() == "fixed" and isinstance(params.get("shift_factor"), (int, float)):
            text += f" ({params['shift_factor']:g})"
        lines.append(f"Reassignment: {text}")
    for key, label in (
        ("intensity_correction", "Intensity correction"),
        ("cell_offsets", "Cell offsets"),
        ("sharpen", "Sharpening"),
    ):
        if key in params:
            lines.append(f"{label}: {'On' if bool(params.get(key)) else 'Off'}")
    for key, label in (
        ("localization_mode", "Localization"),
        ("orientation_mode", "Orientation"),
        ("placement_mode", "Placement"),
    ):
        value = params.get(key)
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    return session_label, object_name, "\n".join(lines)


def result_presentation(path: Path, document) -> tuple[str, str, str]:
    artifact = getattr(document, "artifact", None) or {}
    return reconstruction_presentation(
        getattr(document, "graph", None),
        artifact=artifact,
        fallback_name=filename_display_name(path),
    )
