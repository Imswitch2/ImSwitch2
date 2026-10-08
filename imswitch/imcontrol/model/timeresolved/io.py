"""Reading and writing time-resolved products (format version 2).

One set of writers for the workflows and the Lifetime widget, so a script
and the panel produce identical files, and a reader so a later script (or
tutorial 11) can load what was saved.

HDF5 layout, version 2::

    <measurement>.h5
      attrs/ created_unix_s, workflow_name, backend, detector_name,
             format_version (2), metadata_json, tcspc_direction,
             frames_accumulated, overflows, pileup_max, background_rate_hz
      scan/           attrs/metadata_json
      time_resolved/  t_axis_ns, decay_counts, intensity, lifetime_ns?,
                      cube_counts? (integer counts, gzip; attrs/axes_json)
      gates/<key>     one image per gate; attrs/gate_name, start_ns, stop_ns,
                      reference, resolved_start_ns, resolved_stop_ns
      fit/            attrs/method, min_counts_per_pixel, laser_rep_rate_mhz,
                      peak_bin, peak_time_ns, global_tau_ns
      time_tagger/    attrs/metadata_json (model, serial, roles, conditioning,
                      direction) and the plain attrs model, serial, is_mock
      background/     attrs/rate_hz, per_bin
      irf/            t_axis_ns, counts; attrs/peak_ns, fwhm_ns (when known)

Version 1 files (no ``format_version``) load too: the new groups are
simply absent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import numpy as np

from .processing import resolve_gates
from .types import GateSpec, TimeResolvedScanProducts

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2


# --------------------------------------------------------------------------- #
# Gate presets                                                                 #
# --------------------------------------------------------------------------- #


def load_gate_preset(path) -> Dict[str, Any]:
    """A gate preset file: ``{"name", "reference", "gates": [...], "ratio"}``.

    Returns ``{"name": str, "gates": tuple[GateSpec, ...], "ratio":
    (numerator, denominator) | None}``. ``reference`` at the top applies to
    every gate that does not name its own; the Lifetime widget and tutorial
    12 share these files (``tutorial/timetagger/gate_presets``).
    """
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    default_reference = str(data.get("reference", "absolute"))
    gates = tuple(
        GateSpec(
            str(gate["name"]), float(gate["start_ns"]), float(gate["stop_ns"]),
            reference=str(gate.get("reference", default_reference)),
        )
        for gate in data.get("gates", [])
    )
    ratio = data.get("ratio")
    if ratio is not None:
        ratio = (str(ratio[0]), str(ratio[1]))
        names = {gate.name for gate in gates}
        if not set(ratio) <= names:
            raise ValueError(f"{path.name}: ratio names {ratio} are not gates of the preset")
    return {"name": str(data.get("name", path.stem)), "gates": gates, "ratio": ratio}


def save_gate_preset(path, gates: Iterable[GateSpec], *, name: Optional[str] = None,
                     ratio: Optional[tuple] = None) -> Path:
    path = Path(path)
    gates = tuple(gates)
    payload = {
        "name": name or path.stem,
        "gates": [
            {"name": g.name, "start_ns": g.start_ns, "stop_ns": g.stop_ns, "reference": g.reference}
            for g in gates
        ],
    }
    if ratio is not None:
        payload["ratio"] = [str(ratio[0]), str(ratio[1])]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Writers                                                                      #
# --------------------------------------------------------------------------- #


def _metadata_json(metadata: dict) -> str:
    return json.dumps(metadata or {}, default=_json_default, sort_keys=True)


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    try:
        return str(value)
    except Exception:
        return repr(value)


def safe_label(label: str) -> str:
    label = str(label).strip()
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    label = label.strip("._-")
    return label or "gate"


def _resolved_bounds(spec: GateSpec, peak_time_ns) -> tuple:
    if spec.reference == "absolute" or peak_time_ns is None:
        return float(spec.start_ns), float(spec.stop_ns)
    resolved = resolve_gates((spec,), float(peak_time_ns))[0]
    return float(resolved.start_ns), float(resolved.stop_ns)


def save_h5(
    products: TimeResolvedScanProducts,
    path,
    *,
    workflow_name: str,
    gates: tuple[GateSpec, ...] = (),
) -> Path:
    try:
        import h5py
    except ImportError as exc:
        raise RuntimeError(
            "h5py is required to save time-resolved products as HDF5. "
            "Set save_h5=False and save_npz=True to use NumPy output instead."
        ) from exc

    path = Path(path)
    metadata = products.metadata or {}
    gate_specs = {gate.name: gate for gate in gates}
    peak = metadata.get("peak_time_ns")

    with h5py.File(str(path), "w") as h5:
        h5.attrs["created_unix_s"] = time.time()
        h5.attrs["workflow_name"] = workflow_name
        h5.attrs["backend"] = str(metadata.get("backend", ""))
        h5.attrs["detector_name"] = str(metadata.get("detector_name", ""))
        h5.attrs["format_version"] = int(FORMAT_VERSION)
        h5.attrs["metadata_json"] = _metadata_json(metadata)
        h5.attrs["tcspc_direction"] = str(products.tcspc_direction)
        h5.attrs["frames_accumulated"] = int(products.frames_accumulated)
        h5.attrs["overflows"] = int(products.overflows)
        h5.attrs["pileup_max"] = float(products.pileup_max)
        h5.attrs["background_rate_hz"] = float(products.background_rate_hz)

        scan = h5.create_group("scan")
        scan.attrs["metadata_json"] = _metadata_json(metadata.get("scan_info", {}))

        tr = h5.create_group("time_resolved")
        tr.create_dataset("t_axis_ns", data=products.t_axis_ns)
        tr.create_dataset("intensity", data=products.intensity)
        tr.create_dataset("decay_counts", data=products.decay_counts)
        if products.cube_counts is not None:
            cube = np.asarray(products.cube_counts)
            if not np.issubdtype(cube.dtype, np.integer):
                cube = np.rint(np.clip(cube, 0, None)).astype(np.uint32)
            ds = tr.create_dataset("cube_counts", data=cube, compression="gzip")
            ds.attrs["axes_json"] = json.dumps(tuple(products.cube_axes))
        if products.lifetime_ns is not None:
            tr.create_dataset("lifetime_ns", data=products.lifetime_ns)
        tr.attrs["cube_axes_json"] = json.dumps(tuple(products.cube_axes))
        tr.attrs["global_tau_ns"] = float(products.global_tau_ns)
        tr.attrs["is_final"] = bool(products.is_final)

        gates_group = h5.create_group("gates")
        used: set = set()
        for idx, (name, image) in enumerate(products.gate_images.items()):
            key = safe_label(name)
            if key in used:
                key = f"{idx}_{key}"
            used.add(key)
            ds = gates_group.create_dataset(key, data=image)
            ds.attrs["gate_name"] = name
            spec = gate_specs.get(name)
            if spec is not None:
                ds.attrs["start_ns"] = float(spec.start_ns)
                ds.attrs["stop_ns"] = float(spec.stop_ns)
                ds.attrs["reference"] = str(spec.reference)
                start, stop = _resolved_bounds(spec, peak)
                ds.attrs["resolved_start_ns"] = start
                ds.attrs["resolved_stop_ns"] = stop

        fit = h5.create_group("fit")
        fit.attrs["method"] = str(metadata.get("fit_method", ""))
        fit.attrs["min_counts_per_pixel"] = int(metadata.get("min_counts_per_pixel", 0) or 0)
        fit.attrs["laser_rep_rate_mhz"] = float(metadata.get("laser_rep_rate_mhz", 0.0) or 0.0)
        fit.attrs["peak_bin"] = int(metadata.get("peak_bin", 0) or 0)
        fit.attrs["peak_time_ns"] = float(peak or 0.0)
        fit.attrs["global_tau_ns"] = float(products.global_tau_ns)

        card = h5.create_group("time_tagger")
        tt_meta = dict(metadata.get("time_tagger", {}) or {})
        card.attrs["metadata_json"] = _metadata_json(tt_meta)
        for key in ("model", "serial", "is_mock", "tcspc_direction", "conditioned"):
            if key in tt_meta and tt_meta[key] is not None:
                card.attrs[key] = tt_meta[key]

        background = h5.create_group("background")
        background.attrs["rate_hz"] = float(products.background_rate_hz)
        background.attrs["per_bin"] = float(metadata.get("background_per_bin", 0.0) or 0.0)

        irf = products.irf
        if irf and "t_axis_ns" in irf and "counts" in irf:
            group = h5.create_group("irf")
            group.create_dataset("t_axis_ns", data=np.asarray(irf["t_axis_ns"]))
            group.create_dataset("counts", data=np.asarray(irf["counts"]))
            for key in ("peak_ns", "fwhm_ns", "direction", "photon_delay_ps"):
                if key in irf and irf[key] is not None:
                    group.attrs[key] = irf[key]

    logger.info("Saved time-resolved HDF5 products to %s", path)
    return path


def save_npz(products: TimeResolvedScanProducts, path) -> Path:
    path = Path(path)
    arrays = {
        "format_version": np.asarray(FORMAT_VERSION),
        "t_axis_ns": products.t_axis_ns,
        "intensity": products.intensity,
        "decay_counts": products.decay_counts,
        "global_tau_ns": np.asarray(products.global_tau_ns, dtype=np.float64),
        "is_final": np.asarray(products.is_final, dtype=bool),
        "cube_axes_json": np.asarray(json.dumps(tuple(products.cube_axes))),
        "metadata_json": np.asarray(_metadata_json(products.metadata)),
        "tcspc_direction": np.asarray(products.tcspc_direction),
        "frames_accumulated": np.asarray(products.frames_accumulated),
        "overflows": np.asarray(products.overflows),
        "pileup_max": np.asarray(products.pileup_max),
        "background_rate_hz": np.asarray(products.background_rate_hz),
    }
    if products.cube_counts is not None:
        arrays["cube_counts"] = products.cube_counts
    if products.lifetime_ns is not None:
        arrays["lifetime_ns"] = products.lifetime_ns
    gate_key_map = {}
    for idx, (name, image) in enumerate(products.gate_images.items()):
        key = f"gate_{idx}_{safe_label(name)}"
        arrays[key] = image
        gate_key_map[name] = key
    arrays["gate_key_map_json"] = np.asarray(json.dumps(gate_key_map))
    np.savez_compressed(path, **arrays)
    logger.info("Saved time-resolved NumPy products to %s", path)
    return path


def save_tiffs(products: TimeResolvedScanProducts, folder, prefix: str) -> Dict[str, Path]:
    import tifffile as tf

    folder = Path(folder)
    paths: Dict[str, Path] = {}
    intensity_path = folder / f"{prefix}_intensity.tif"
    tf.imwrite(str(intensity_path), np.asarray(products.intensity).astype(np.float32, copy=False))
    paths["intensity_tiff"] = intensity_path
    if products.lifetime_ns is not None:
        lifetime_path = folder / f"{prefix}_lifetime_ns.tif"
        tf.imwrite(str(lifetime_path), np.asarray(products.lifetime_ns).astype(np.float32, copy=False))
        paths["lifetime_tiff"] = lifetime_path
    for name, image in products.gate_images.items():
        key = f"gate_{safe_label(name)}_tiff"
        gate_path = folder / f"{prefix}_gate_{safe_label(name)}.tif"
        tf.imwrite(str(gate_path), np.asarray(image).astype(np.float32, copy=False))
        paths[key] = gate_path
    return paths


# --------------------------------------------------------------------------- #
# Reader                                                                       #
# --------------------------------------------------------------------------- #


def load_products(path) -> TimeResolvedScanProducts:
    """Read an ``.h5`` or ``.npz`` written by :func:`save_h5` /
    :func:`save_npz` (version 1 or 2) back into a
    :class:`TimeResolvedScanProducts`. The file's ``format_version`` and,
    for HDF5, the gates' saved bounds go into ``metadata``."""
    path = Path(path)
    if path.suffix.lower() == ".npz":
        return _load_npz(path)
    return _load_h5(path)


def _load_h5(path: Path) -> TimeResolvedScanProducts:
    import h5py

    with h5py.File(str(path), "r") as h5:
        metadata = json.loads(str(h5.attrs.get("metadata_json", "{}")) or "{}")
        version = int(h5.attrs.get("format_version", 1))
        tr = h5["time_resolved"]
        cube = tr["cube_counts"][()] if "cube_counts" in tr else None
        axes_json = tr.attrs.get("cube_axes_json")
        cube_axes = tuple(json.loads(str(axes_json))) if axes_json else ("y", "x", "tcspc_bin")
        gate_images: Dict[str, np.ndarray] = {}
        gate_bounds: Dict[str, Dict[str, Any]] = {}
        if "gates" in h5:
            for key, ds in h5["gates"].items():
                name = str(ds.attrs.get("gate_name", key))
                gate_images[name] = ds[()]
                bounds = {k: ds.attrs[k] for k in ("start_ns", "stop_ns", "reference",
                                                    "resolved_start_ns", "resolved_stop_ns")
                          if k in ds.attrs}
                if bounds:
                    gate_bounds[name] = {k: (v.item() if hasattr(v, "item") else v)
                                         for k, v in bounds.items()}
        irf = None
        if "irf" in h5:
            group = h5["irf"]
            irf = {"t_axis_ns": group["t_axis_ns"][()], "counts": group["counts"][()]}
            irf.update({k: (v.item() if hasattr(v, "item") else v) for k, v in group.attrs.items()})
        metadata["format_version"] = version
        if gate_bounds:
            metadata["gates"] = gate_bounds
        if "time_tagger" in h5 and "time_tagger" not in metadata:
            metadata["time_tagger"] = json.loads(str(h5["time_tagger"].attrs.get("metadata_json", "{}")))
        return TimeResolvedScanProducts(
            cube_counts=cube,
            cube_axes=cube_axes,
            t_axis_ns=tr["t_axis_ns"][()],
            intensity=tr["intensity"][()],
            lifetime_ns=tr["lifetime_ns"][()] if "lifetime_ns" in tr else None,
            gate_images=gate_images,
            decay_counts=tr["decay_counts"][()],
            global_tau_ns=float(tr.attrs.get("global_tau_ns", 0.0)),
            metadata=metadata,
            is_final=bool(tr.attrs.get("is_final", True)),
            tcspc_direction=str(h5.attrs.get("tcspc_direction", metadata.get("tcspc_direction", "forward"))),
            background_rate_hz=float(h5.attrs.get("background_rate_hz", metadata.get("background_rate_hz", 0.0) or 0.0)),
            pileup_max=float(h5.attrs.get("pileup_max", metadata.get("pileup_max", 0.0) or 0.0)),
            overflows=int(h5.attrs.get("overflows", metadata.get("overflows", 0) or 0)),
            frames_accumulated=int(h5.attrs.get("frames_accumulated", 1)),
            irf=irf,
            format_version=version,
        )


def _load_npz(path: Path) -> TimeResolvedScanProducts:
    with np.load(str(path), allow_pickle=False) as data:
        metadata = json.loads(str(data["metadata_json"]))
        version = int(data["format_version"]) if "format_version" in data else 1
        gate_key_map = json.loads(str(data["gate_key_map_json"])) if "gate_key_map_json" in data else {}
        gate_images = {name: data[key] for name, key in gate_key_map.items()}
        metadata["format_version"] = version

        def scalar(name, default):
            return data[name].item() if name in data else default

        return TimeResolvedScanProducts(
            cube_counts=data["cube_counts"] if "cube_counts" in data else None,
            cube_axes=tuple(json.loads(str(data["cube_axes_json"]))),
            t_axis_ns=data["t_axis_ns"],
            intensity=data["intensity"],
            lifetime_ns=data["lifetime_ns"] if "lifetime_ns" in data else None,
            gate_images=gate_images,
            decay_counts=data["decay_counts"],
            global_tau_ns=float(data["global_tau_ns"]),
            metadata=metadata,
            is_final=bool(data["is_final"]),
            tcspc_direction=str(scalar("tcspc_direction", metadata.get("tcspc_direction", "forward"))),
            background_rate_hz=float(scalar("background_rate_hz", 0.0)),
            pileup_max=float(scalar("pileup_max", 0.0)),
            overflows=int(scalar("overflows", 0)),
            frames_accumulated=int(scalar("frames_accumulated", 1)),
            irf=None,
            format_version=version,
        )


__all__ = [
    "FORMAT_VERSION",
    "load_gate_preset",
    "load_products",
    "safe_label",
    "save_gate_preset",
    "save_h5",
    "save_npz",
    "save_tiffs",
]
