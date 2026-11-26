"""Camera -> scanner calibration from two bead recordings, as ImSwitch writes them.

The ImControl side of the bead registration
(:mod:`imswitch.imcommon.algorithms.transforms.bead_registration`): it reads a
camera recording and a point-scan (APD) recording of the same bead field --
the HDF5 or Zarr files the Recording panel writes, unconverted -- takes the
camera grid and the scan geometry from their metadata, runs the
registration, and saves the result where both consumers find it:

* ``<ImSwitchConfig>/calibrations/transforms/<id>.json`` -- the shared
  transform module's record (``imswitch.transform/1``), which the Transform
  Calibration panel loads;
* ``<ImSwitchConfig>/calibrations/<id>.json`` -- a smart-pipeline transform
  of type ``spatial-transform`` that points at that record, so a pipeline's
  ``transform`` step can name ``<id>``;
* ``<id>_qc.png`` and ``<id>_report.txt`` next to the record.

What is read from the files (and what is done when it is missing):

* The image: the dataset of the detector asked for (``<detector>/data``),
  all frames averaged into one 2-D image (a snap is one frame; a stack or a
  scan-lapse is averaged). With several detectors in one file the detector
  must be named.
* Camera grid (``Detector:<camera>:*``): ``ROI`` ``[x0, y0, width, height]``,
  ``Binning``, ``Pixel size`` ``[z, y, x]`` µm. Missing ROI/binning: the
  identity is the image shape alone (offset 0, binning 1) and the report says
  so; missing pixel size: no scale check.
* Scan geometry (``ScanStage:*``): ``target_device``, ``axis_length``,
  ``axis_step_size``, ``axis_centerpos`` per device, the scanned devices in
  scan order (``scan_axis_devices``, else the non-``None`` entries of
  ``scan_dim_target_device``), and the image's (rows, columns) as the pixel
  counts of the slow and fast axis. The pixel positions follow the scan
  designer's rule (:func:`imswitch.imcontrol.model.scan_frame.frame_geometry_from_scan`,
  the same rule that labels live point-scan frames for the ``scan-geometry``
  transform). Any of these missing raises :class:`BeadCalibrationError`
  naming it; pass ``scan_geometry=`` (``{"axes": [{"device", "step_um",
  "first_um"}, ...]}``, fast axis first) instead of guessing. When a length
  is not a whole number of steps the fast axis's first pixel depends on
  whether it is swept or stepped, which the file does not record: pass
  ``fast_axis_swept``. ``AcquisitionLayout:json``, when present, must say
  the rows are the slow axis and both axes are scanned forward (no
  bidirectional/snake scans).
* Recorded with the calibration (provenance and target context):
  ``ScanStage:phase_delay``, ``sequence_time``, centre/length/step per axis,
  ``setup:name``, ``setup:hash``, ``metadata:schema_version``,
  ``acquisition:software_version`` and start time, the file names.

Run it: ``python -m imswitch.imcontrol.model.bead_calibration CAMERA_FILE
APD_FILE --id camera_to_galvo`` (``--help`` for the options), or from a
script with :func:`calibrate_from_files`.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from imswitch.imcommon.algorithms.transforms import AcquisitionContext, SpatialTransform, io as transform_io
from imswitch.imcommon.algorithms.transforms.bead_registration import (
    BeadRegistration,
    BeadRegistrationError,
    BeadRegistrationOptions,
    register_bead_images,
    scan_geometry_affine,
)
from imswitch.imcommon.algorithms.transforms.correspondence import detect_beads

#: The transform-record subfolder of the calibrations folder (the Transform
#: Calibration panel's folder).
TRANSFORMS_DIRNAME = "transforms"


class BeadCalibrationError(ValueError):
    """A recording cannot be used for the bead calibration as it is."""


# --------------------------------------------------------------------------
# reading recordings
# --------------------------------------------------------------------------

@dataclass
class Recording:
    """One detector's image from a recording: ``image`` (2-D float, frames
    averaged), ``attrs`` (``"Category:key:..."`` -> value), the detector,
    the file and how many frames were averaged."""

    image: np.ndarray
    attrs: dict
    detector: str
    path: Path
    frames: int
    dtype: str
    notes: list = field(default_factory=list)

    def attr(self, *keys, default=None):
        return self.attrs.get(":".join(keys), default)


def _flat(attrs) -> dict:
    out = {}
    for key, value in dict(attrs).items():
        name = ":".join(str(k) for k in key) if isinstance(key, tuple) else str(key)
        out[name] = value
    return out


def _pick(datasets: Sequence[str], detector: str | None, path: Path) -> str:
    datasets = [str(d) for d in datasets]
    if detector is not None:
        if detector not in datasets:
            raise BeadCalibrationError(f"{path.name} has no dataset for detector {detector!r} (it has {datasets})")
        return detector
    if len(datasets) != 1:
        raise BeadCalibrationError(
            f"{path.name} holds {len(datasets)} detectors ({', '.join(datasets)}); name the one to use")
    return datasets[0]


def _average(data, path: Path) -> tuple[np.ndarray, int]:
    array = np.asarray(data)
    if array.ndim < 2:
        raise BeadCalibrationError(f"{path.name}: the data is not an image (shape {array.shape})")
    frames = int(np.prod(array.shape[:-2])) if array.ndim > 2 else 1
    image = array.reshape((-1,) + array.shape[-2:]).astype(np.float64).mean(axis=0) if frames > 1 \
        else array.reshape(array.shape[-2:]).astype(np.float64)
    return image, frames


def read_recording(path, detector: str | None = None) -> Recording:
    """The image and metadata of ``detector`` in an ImSwitch recording:
    an HDF5 file (``.h5``/``.hdf5``) or a Zarr store (``.zarr`` folder)."""
    from imswitch.imcommon.model import SharedAttributes

    path = Path(path)
    if not path.exists():
        raise BeadCalibrationError(f"{path} does not exist")
    notes = []
    suffix = path.suffix.lower()
    if suffix in (".h5", ".hdf5"):
        import h5py

        with h5py.File(str(path), "r") as handle:
            name = _pick(list(handle.keys()), detector, path)
            node = handle[name]
            data = node["data"] if isinstance(node, h5py.Group) and "data" in node else node
            dtype = str(data.dtype)
            image, frames = _average(data[()], path)
            dataset_attrs = dict(data.attrs) if hasattr(data, "attrs") else {}
            attrs = _flat(SharedAttributes.fromHDF5File(handle, name))
    elif suffix == ".zarr" or path.is_dir():
        import zarr

        root = zarr.open(str(path), mode="r")
        name = _pick(list(root.keys()), detector, path)
        node = root[name]
        data = node["data"] if "data" in list(node.keys()) else node
        dtype = str(data.dtype)
        image, frames = _average(data[...], path)
        dataset_attrs = dict(data.attrs)
        attrs = _flat(SharedAttributes.fromZarrStore(root, name))
    else:
        raise BeadCalibrationError(
            f"{path.name}: record the bead images as HDF5 or Zarr (Recording panel, File format); "
            "a TIFF does not carry the scan geometry and the camera grid")
    for key, value in dataset_attrs.items():
        attrs.setdefault(str(key), value)
    outcome = attrs.get("recording:completion_outcome")
    if isinstance(outcome, bytes):
        outcome = outcome.decode()
    if outcome not in (None, "complete"):
        notes.append(f"{path.name}: the recording says it is {outcome!r}, not complete")
    return Recording(image=image, attrs=attrs, detector=name, path=path, frames=frames, dtype=dtype, notes=notes)


def _seq(value) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        text = value.decode() if isinstance(value, bytes) else value
        try:
            return list(json.loads(text))
        except (ValueError, TypeError):
            return [text]
    return [v.decode() if isinstance(v, bytes) else v for v in np.asarray(value, dtype=object).ravel().tolist()]


def _required(recording: Recording, key: str) -> list:
    value = recording.attrs.get(key)
    if value is None:
        raise BeadCalibrationError(
            f"{recording.path.name} has no {key!r}; pass the scan geometry explicitly "
            "(scan_geometry={'axes': [{'device', 'step_um', 'first_um'}, ...]}, fast axis first)")
    return _seq(value)


def scan_geometry_from_recording(recording: Recording, *, fast_axis_swept: bool | None = None) -> dict:
    """The scan geometry of a point-scan recording (``{"axes": [...]}``,
    fast axis first), from its ``ScanStage:*`` metadata and image shape."""
    from imswitch.imcontrol.model.scan_frame import frame_geometry_from_scan

    devices = [str(d) for d in _required(recording, "ScanStage:target_device")]
    lengths = [float(v) for v in _required(recording, "ScanStage:axis_length")]
    steps = [float(v) for v in _required(recording, "ScanStage:axis_step_size")]
    centres = [float(v) for v in _required(recording, "ScanStage:axis_centerpos")]
    names = [str(d) for d in _seq(recording.attrs.get("ScanStage:scan_axis_devices")) if str(d) != "None"]
    if not names:
        names = [str(d) for d in _required(recording, "ScanStage:scan_dim_target_device") if str(d) != "None"]
    if len(names) != 2:
        raise BeadCalibrationError(
            f"{recording.path.name}: the scan moved {len(names)} axes ({names}); a 2-D scan of two axes is needed")
    rows, cols = recording.image.shape
    _check_layout(recording, names)
    analog = {"target_device": devices, "axis_length": lengths, "axis_step_size": steps, "axis_centerpos": centres}
    candidates = {}
    for swept in (True, False):
        info = {"axis_names": names, "img_dims": [cols, rows], "smooth_axes": [swept, False]}
        try:
            candidates[swept] = frame_geometry_from_scan(analog, info).to_dict()
        except (ValueError, KeyError, IndexError) as error:
            raise BeadCalibrationError(f"{recording.path.name}: the scan metadata does not fit the image "
                                       f"({rows} x {cols}): {error}") from None
    firsts = {s: [a["first_um"] for a in g["axes"]] for s, g in candidates.items()}
    if fast_axis_swept is None:
        if not np.allclose(firsts[True], firsts[False], atol=1e-9):
            raise BeadCalibrationError(
                f"{recording.path.name}: the fast axis length is not a whole number of steps, so its first pixel "
                "depends on whether it was swept (a galvo, smooth) or stepped; pass fast_axis_swept=True/False")
        fast_axis_swept = True
    geometry = candidates[bool(fast_axis_swept)]
    geometry.pop("run", None)
    geometry.pop("iteration", None)
    return geometry


def _check_layout(recording: Recording, names: Sequence[str]) -> None:
    text = recording.attrs.get("AcquisitionLayout:json")
    if not text:
        return
    try:
        layout = json.loads(text if isinstance(text, str) else bytes(text).decode())
    except (ValueError, TypeError):
        return
    loops = {loop.get("device"): loop for loop in layout.get("event_loops") or []}
    storage = list(layout.get("storage_axes") or [])
    fast, slow = names[0], names[1]
    if fast in loops and slow in loops and storage:
        fast_axis, slow_axis = loops[fast].get("storage_axis"), loops[slow].get("storage_axis")
        if fast_axis in storage and slow_axis in storage and storage.index(slow_axis) > storage.index(fast_axis):
            raise BeadCalibrationError(f"{recording.path.name}: the recording stores {fast} along rows; "
                                       "the calibration expects rows = slow axis")
    for step in layout.get("traversal") or []:
        if str(step.get("order", "forward")) != "forward" or step.get("parity_loops"):
            raise BeadCalibrationError(f"{recording.path.name}: bidirectional/snake scans are not supported")


def camera_context_from_recording(recording: Recording) -> tuple[AcquisitionContext, list]:
    """The camera grid (shape, ROI ``[x0, y0, w, h]``, binning, pixel size)
    as an :class:`AcquisitionContext`, and notes on what was missing."""
    notes = []
    name = recording.detector
    roi = recording.attrs.get(f"Detector:{name}:ROI")
    binning = recording.attrs.get(f"Detector:{name}:Binning")
    pixel = recording.attrs.get(f"Detector:{name}:Pixel size")
    rows, cols = recording.image.shape
    if roi is not None:
        roi = tuple(int(v) for v in _seq(roi))
        if len(roi) != 4:
            roi = None
    if roi is None:
        notes.append(f"{recording.path.name}: no Detector:{name}:ROI; assuming an uncropped frame at offset 0")
    elif (roi[3], roi[2]) != (rows, cols):
        notes.append(f"{recording.path.name}: ROI {list(roi)} does not match the image {rows} x {cols} "
                     "(binned ROI?); the identity uses the image shape and the ROI offset")
    try:
        binning = int(_seq(binning)[0]) if binning is not None else None
    except (TypeError, ValueError, IndexError):
        binning = None
    if binning is None:
        notes.append(f"{recording.path.name}: no Detector:{name}:Binning; assuming 1")
    pixel_size = None
    if pixel is not None:
        values = [float(v) for v in _seq(pixel)]
        if len(values) >= 2:
            pixel_size = (values[-2], values[-1])
    if pixel_size is None:
        notes.append(f"{recording.path.name}: no Detector:{name}:Pixel size; the scale is not checked")
    extra = {"roi_convention": "[x0, y0, width, height] on the chip, as Detector:<name>:ROI",
             "file": recording.path.name, "frames_averaged": recording.frames, "dtype": recording.dtype}
    for key in ("setup:name", "setup:hash", "metadata:schema_version"):
        if recording.attrs.get(key) is not None:
            extra[key] = _plain(recording.attrs[key])
    context = AcquisitionContext(detector=name, image_shape=(rows, cols), roi=roi, binning=binning,
                                 pixel_size_um=pixel_size, extra=extra)
    return context, notes


def frame_identity_of_context(context: AcquisitionContext):
    """The smart pipeline's ``FrameIdentity`` for a camera context: shape
    (rows, cols), binning per axis, ROI offset as (y, x)."""
    from imswitch.imcommon.model.smartpipeline import FrameIdentity

    shape = tuple(int(v) for v in (context.image_shape or ()))
    binning = int(context.binning or 1)
    offset = (int(context.roi[1]), int(context.roi[0])) if context.roi else (0, 0)
    return FrameIdentity(shape=shape, binning=(binning, binning), offset=offset)


def _plain(value):
    if isinstance(value, bytes):
        return value.decode()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    return value


def scan_context_from_recording(recording: Recording, geometry: Mapping) -> AcquisitionContext:
    keys = ("ScanStage:target_device", "ScanStage:axis_length", "ScanStage:axis_step_size",
            "ScanStage:axis_centerpos", "ScanStage:phase_delay", "ScanStage:sequence_time",
            "ScanStage:scan_axis_devices", "ScanStage:positive_direction", "setup:name", "setup:hash",
            "metadata:schema_version", "acquisition:software_version", "acquisition:start_time")
    extra = {key: _plain(recording.attrs[key]) for key in keys if recording.attrs.get(key) is not None}
    extra.update({"file": recording.path.name, "frames_averaged": recording.frames})
    rows, cols = recording.image.shape
    steps = [float(a["step_um"]) for a in geometry["axes"]]
    return AcquisitionContext(detector=recording.detector, image_shape=(rows, cols),
                              pixel_size_um=(steps[1], steps[0]), extra=extra)


# --------------------------------------------------------------------------
# calibrate, save, check
# --------------------------------------------------------------------------

def calibrations_root(folder=None) -> Path:
    """``folder``, or ``<ImSwitchConfig>/calibrations`` when ``None``."""
    from imswitch.imcontrol.model.smartpipeline.calibrations import calibrations_folder

    return calibrations_folder() if folder is None else Path(folder)


@dataclass
class BeadCalibration:
    """A finished calibration: the registration, the two recordings and the
    files written (``record``, ``pipeline``, ``figure``, ``report``)."""

    registration: BeadRegistration
    camera: Recording
    scan: Recording
    scan_geometry: dict
    calibration_id: str
    notes: list
    paths: dict = field(default_factory=dict)

    @property
    def transform(self) -> SpatialTransform:
        return self.registration.transform

    def camera_context_pixel(self) -> float | None:
        """The camera's configured pixel size (µm), when recorded."""
        pixel = self.transform.source_context.pixel_size_um
        return float(np.mean(pixel)) if pixel else None

    def report(self) -> str:
        lines = [f"bead calibration {self.calibration_id!r}: {self.transform.source_frame} -> "
                 f"{self.transform.target_frame}",
                 f"camera: {self.camera.path.name} ({self.camera.detector}, {self.camera.frames} frame(s) averaged, "
                 f"{self.camera.image.shape[0]} x {self.camera.image.shape[1]} px)",
                 f"scan:   {self.scan.path.name} ({self.scan.detector}, {self.scan.frames} frame(s) averaged), "
                 f"geometry {json.dumps(self.scan_geometry['axes'])}",
                 self.registration.report()]
        lines += [f"NOTE: {n}" for n in self.notes]
        a0, a1 = self.registration.target_axes
        lines.append(f"use in a pipeline: transform '{self.calibration_id}', then run-scan with axes "
                     f"[\"{a0}\", \"{a1}\"] (the output columns are {a0}, {a1} in um: Advanced scan centres)")
        for kind, path in self.paths.items():
            lines.append(f"{kind}: {path}")
        return "\n".join(lines)


def calibrate_from_files(camera_file, scan_file, calibration_id: str = "cam_to_scan", *,
                         camera_detector: str | None = None, scan_detector: str | None = None,
                         options: BeadRegistrationOptions | None = None,
                         scan_geometry: Mapping | None = None, fast_axis_swept: bool | None = None,
                         target_frame: str = "scanner:galvo", folder=None, save: bool = True,
                         figure: bool = True, overwrite: bool = False) -> BeadCalibration:
    """Calibrate camera pixels -> scan µm from two recordings (see the
    module docstring); with ``save`` write the record, the pipeline entry,
    the QC figure and the report into ``folder`` (default: the
    calibrations folder). Raises :class:`BeadRegistrationError` /
    :class:`BeadCalibrationError` with the reason when it cannot be trusted."""
    camera = read_recording(camera_file, camera_detector)
    scan = read_recording(scan_file, scan_detector)
    geometry = dict(scan_geometry) if scan_geometry else scan_geometry_from_recording(
        scan, fast_axis_swept=fast_axis_swept)
    camera_context, notes = camera_context_from_recording(camera)
    notes = list(camera.notes) + list(scan.notes) + notes
    options = options or BeadRegistrationOptions()
    if options.camera_pixel_um is None and camera_context.pixel_size_um:
        options = replace(options, camera_pixel_um=float(np.mean(camera_context.pixel_size_um)))
    if options.target_axes is None:
        options = replace(options, target_axes=_physical_order(scan, geometry))
    import imswitch

    registration = register_bead_images(
        camera.image, scan.image, geometry, options, source_frame=f"detector:{camera.detector}",
        target_frame=target_frame, camera_context=camera_context,
        scan_context=scan_context_from_recording(scan, geometry),
        provenance={"calibration_id": calibration_id, "files": [str(camera.path), str(scan.path)],
                    "software_version": getattr(imswitch, "__version__", None),
                    "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "camera_detector": camera.detector, "scan_detector": scan.detector})
    result = BeadCalibration(registration, camera, scan, geometry, calibration_id, notes)
    if save:
        result.paths = save_bead_calibration(result, folder=folder, figure=figure, overwrite=overwrite)
    return result


def _physical_order(scan: Recording, geometry: Mapping) -> tuple[str, str]:
    """``(Y device, X device)`` when the recording says which scanned
    device is physical Y and X (``ScanStage:scan_axis_physical``), so the
    output columns match ``run-scan``'s default axes; else (slow, fast)."""
    axes = geometry["axes"]
    names = [str(axes[1]["device"]), str(axes[0]["device"])]
    devices = [str(d) for d in _seq(scan.attrs.get("ScanStage:scan_axis_devices"))]
    physical = [str(p).upper() for p in _seq(scan.attrs.get("ScanStage:scan_axis_physical"))]
    by = dict(zip(physical, devices))
    if "Y" in by and "X" in by and sorted([by["Y"], by["X"]]) == sorted(names):
        return by["Y"], by["X"]
    return names[0], names[1]


def save_bead_calibration(result: BeadCalibration, folder=None, *, figure: bool = True,
                          overwrite: bool = False) -> dict:
    """Write the module record, the pipeline entry, the report and (with
    ``figure``) the QC figure; returns their paths."""
    from imswitch.imcontrol.model.smartpipeline.calibrations import (
        CalibrationError, _file_name, save_calibration,
    )
    from imswitch.imcommon.model.smartpipeline import SpatialTransformRef

    root = calibrations_root(folder)
    name = _file_name(result.calibration_id)
    record_path = root / TRANSFORMS_DIRNAME / f"{name}.json"
    if record_path.exists() and not overwrite:
        raise CalibrationError(f"{record_path} exists; choose another id or pass overwrite")
    transform_io.save_json(record_path, result.transform)
    reference = SpatialTransformRef(
        id=result.calibration_id, source_frame=result.transform.source_frame,
        target_frame=result.transform.target_frame, path=f"{TRANSFORMS_DIRNAME}/{record_path.name}",
        base=root)
    pipeline_path = save_calibration(reference, root, overwrite=True)
    paths = {"record": record_path, "pipeline": pipeline_path}
    if figure:
        paths["figure"] = qc_figure(result.registration, result.camera.image, result.scan.image,
                                    record_path.with_name(f"{name}_qc.png"))
    report_path = record_path.with_name(f"{name}_report.txt")
    result.paths = dict(paths)
    report_path.write_text(result.report() + f"\nreport: {report_path}\n", encoding="utf-8")
    paths["report"] = report_path
    return paths


def qc_figure(registration: BeadRegistration, camera_image, scan_image, path=None, *, magnify: float | None = None):
    """Both images with the paired beads numbered, the residual vectors
    magnified on the scan image, and a residual histogram. Saved as PNG when
    ``path`` is given (returns the path), else returns the figure."""
    import matplotlib

    if path is not None:
        matplotlib.use("Agg", force=False)
    from matplotlib.figure import Figure

    fig = Figure(figsize=(15, 5.2), dpi=110)
    ax_cam, ax_scan, ax_hist = fig.subplots(1, 3)
    vmin, vmax = np.percentile(camera_image, (1, 99.7))
    ax_cam.imshow(camera_image, cmap="gray", vmin=vmin, vmax=vmax)
    cam_all = registration.camera_detections.positions
    ax_cam.scatter(cam_all[:, 1], cam_all[:, 0], s=14, facecolors="none", edgecolors="tab:blue", linewidths=0.6)
    inl = registration.inliers
    pts = registration.camera_points
    ax_cam.scatter(pts[inl, 1], pts[inl, 0], s=40, facecolors="none", edgecolors="lime", linewidths=1.0)
    ax_cam.scatter(pts[~inl, 1], pts[~inl, 0], s=40, marker="x", color="red")
    # The scan field's outline mapped into the camera image.
    corners = np.array([[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]], float) * (np.array(scan_image.shape) - 1)
    affine, _ = scan_geometry_affine({"axes": registration.transform.target_context.extra["scan_geometry"]["axes"]},
                                     registration.target_axes)
    outline = registration.transform.inverse().map_points(affine.map_points(corners))
    ax_cam.plot(outline[:, 1], outline[:, 0], color="orange", lw=1.0)
    for k, (r, c) in enumerate(pts):
        ax_cam.annotate(str(k), (c, r), color="yellow", fontsize=6, xytext=(3, 3), textcoords="offset points")
    ax_cam.set_title(f"camera: {len(registration.camera_detections)} beads, {len(pts)} paired (green)")
    vmin, vmax = np.percentile(scan_image, (1, 99.7))
    ax_scan.imshow(scan_image, cmap="gray", vmin=vmin, vmax=vmax)
    sp = registration.scan_points_px
    ax_scan.scatter(sp[:, 1], sp[:, 0], s=40, facecolors="none", edgecolors="lime", linewidths=1.0)
    residual_um = registration.residual_vectors
    # residuals in scan pixels (rows = slow, cols = fast), via the geometry
    residual_px = affine.inverse().map_points(registration.fit.mapped_points) - sp
    norm = np.linalg.norm(residual_px, axis=1)
    if magnify is None:
        typical = float(np.median(norm)) if len(norm) and np.median(norm) > 0 else 1.0
        magnify = max(1.0, round(0.05 * max(scan_image.shape) / typical))
    ax_scan.quiver(sp[:, 1], sp[:, 0], residual_px[:, 1] * magnify, residual_px[:, 0] * magnify, angles="xy",
                   scale_units="xy", scale=1, color="red", width=0.004)
    for k, (r, c) in enumerate(sp):
        ax_scan.annotate(str(k), (c, r), color="yellow", fontsize=6, xytext=(3, 3), textcoords="offset points")
    ax_scan.set_title(f"scan: residuals x{magnify:g} (RMS {registration.rms * 1000:.0f} nm)")
    ax_hist.hist(np.linalg.norm(residual_um, axis=1) * 1000, bins=max(5, len(residual_um) // 2), color="tab:gray")
    ax_hist.set_xlabel("residual (nm)")
    ax_hist.set_ylabel("beads")
    g = registration.geometry()
    ax_hist.set_title(f"{registration.transform.provenance.get('estimator')}: rot {g['rotation_deg']:.2f} deg, "
                      f"{g['scale'] * 1000:.2f} nm/px, mirror {'yes' if g['mirror'] else 'no'}", fontsize=9)
    fig.tight_layout()
    if path is None:
        return fig
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(path))
    return path


def load_bead_calibration(calibration, folder=None) -> SpatialTransform:
    """A saved calibration by id (``<calibrations>/transforms/<id>.json``)
    or by path."""
    path = Path(calibration)
    if not path.suffix:
        path = calibrations_root(folder) / TRANSFORMS_DIRNAME / f"{calibration}.json"
    return transform_io.load_json(path)


def predict_scan_centre(transform: SpatialTransform, row: float, col: float) -> dict:
    """The scan centre (axis -> µm) for camera pixel ``(row, col)``: what to
    enter as the Advanced scan's centre to scan around that pixel."""
    axes = list(transform.target_context.extra.get("axes") or ("0", "1"))
    point = transform.map_points(np.array([[float(row), float(col)]]))[0]
    return {str(axes[0]): float(point[0]), str(axes[1]): float(point[1])}


def verify_bead_calibration(transform: SpatialTransform, camera_image, scan_image, scan_geometry: Mapping, *,
                            camera_options: Mapping | None = None, scan_options: Mapping | None = None,
                            tolerance: float = 0.5) -> dict:
    """Independent check of a saved calibration on a (preferably new) pair
    of bead images: camera beads are mapped into scan µm with ``transform``
    and each scan bead is compared with the nearest mapped camera bead. No
    fitting; reports how far they land (``distances_um``, ``rms_um``,
    ``median_um``, ``max_um`` over pairs within ``tolerance``), the mean
    offset (a shift since the calibration) and how many scan beads have no
    camera bead within ``tolerance``."""
    from scipy import spatial

    axes = tuple(transform.target_context.extra.get("axes") or ())
    affine, axes = scan_geometry_affine(scan_geometry, axes or None)
    camera = detect_beads(camera_image, **dict(camera_options or {}))
    scan = detect_beads(scan_image, **dict(scan_options or {}))
    if len(camera) == 0 or len(scan) == 0:
        raise BeadRegistrationError(f"no beads to compare: camera {len(camera)}, scan {len(scan)}")
    scan_um = affine.map_points(scan.positions)
    mapped = transform.map_points(camera.positions)
    distance, index = spatial.cKDTree(mapped).query(scan_um, k=1)
    close = distance < tolerance
    offsets = scan_um[close] - mapped[index[close]]
    report = {"axes": list(axes), "scan_beads": len(scan), "camera_beads": len(camera),
              "matched": int(close.sum()), "unmatched_scan_beads": int((~close).sum()),
              "distances_um": distance[close].tolist(),
              "rms_um": float(np.sqrt(np.mean(distance[close] ** 2))) if close.any() else math.nan,
              "median_um": float(np.median(distance[close])) if close.any() else math.nan,
              "max_um": float(distance[close].max()) if close.any() else math.nan,
              "mean_offset_um": offsets.mean(axis=0).tolist() if close.any() else [math.nan, math.nan]}
    return report


def verify_from_files(calibration, camera_file, scan_file, *, camera_detector=None, scan_detector=None,
                      fast_axis_swept=None, folder=None, tolerance: float = 0.5) -> dict:
    """:func:`verify_bead_calibration` on two recordings, also checking that
    the camera grid is the one calibrated on."""
    from imswitch.imcontrol.model.smartpipeline.calibrations import identity_problems
    from imswitch.imcommon.model.smartpipeline import SpatialTransformRef

    transform = calibration if isinstance(calibration, SpatialTransform) else load_bead_calibration(calibration, folder)
    camera = read_recording(camera_file, camera_detector)
    scan = read_recording(scan_file, scan_detector)
    geometry = scan_geometry_from_recording(scan, fast_axis_swept=fast_axis_swept)
    context, _ = camera_context_from_recording(camera)
    report = verify_bead_calibration(transform, camera.image, scan.image, geometry, tolerance=tolerance)
    ref = SpatialTransformRef.of(transform, "verify")
    report["grid_problems"] = identity_problems(ref, frame_identity_of_context(context))
    return report


# --------------------------------------------------------------------------
# what a file holds (for the Camera -> scan calibration dialog)
# --------------------------------------------------------------------------

#: Detector managers that build their image from a scan.
_SCAN_DETECTOR_MANAGERS = ("APD", "PMT", "TimeTagger", "Swabian", "Photon", "Counter")


def list_detectors(path) -> list[str]:
    """The detector datasets of a recording, without reading the pixels."""
    path = Path(path)
    if path.suffix.lower() in (".h5", ".hdf5"):
        import h5py

        with h5py.File(str(path), "r") as handle:
            return [str(k) for k in handle.keys()]
    if path.suffix.lower() == ".zarr" or path.is_dir():
        import zarr

        return [str(k) for k in zarr.open(str(path), mode="r").keys()]
    raise BeadCalibrationError(f"{path.name}: record the bead images as HDF5 or Zarr")


def _inventory(recording: Recording) -> dict:
    inventory = recording.attrs.get("setup:inventory")
    if isinstance(inventory, (str, bytes)):
        try:
            inventory = json.loads(inventory)
        except ValueError:
            inventory = None
    return inventory if isinstance(inventory, dict) else {}


def is_scan_detector(recording: Recording, detector: str | None = None) -> bool:
    """Whether ``detector`` (default: the recording's) builds its image from
    a scan, by its manager class in the recorded setup inventory."""
    detector = detector or recording.detector
    manager = str(((_inventory(recording).get("detectors") or {}).get(detector) or {}).get("managerClass") or "")
    return any(key.lower() in manager.lower() for key in _SCAN_DETECTOR_MANAGERS)


def preferred_detector(path, role: str) -> tuple[list[str], str | None]:
    """The detectors of a file and the one to pre-select for ``role``
    (``"camera"`` or ``"scan"``): the first of the right kind, else the first."""
    detectors = list_detectors(path)
    if len(detectors) <= 1:
        return detectors, (detectors[0] if detectors else None)
    try:
        probe = read_recording(path, detectors[0])
    except Exception:  # noqa: BLE001 - pre-selection is a convenience
        return detectors, detectors[0]
    for name in detectors:
        if is_scan_detector(probe, name) == (role == "scan"):
            return detectors, name
    return detectors, detectors[0]


def _fmt_um(value) -> str:
    return f"{float(value):.3g} µm"


@dataclass
class RecordingSummary:
    """What a dialog shows of one picked file: ``lines`` (label, value),
    ``warnings``, and the facts compared across files."""

    role: str
    path: Path
    detector: str
    detectors: list
    lines: list
    warnings: list
    setup_name: str | None
    setup_hash: str | None
    recording: Recording
    context: AcquisitionContext | None = None
    geometry: dict | None = None


def summarize_recording(path, role: str, detector: str | None = None) -> RecordingSummary:
    """Read ``path`` and describe it for ``role`` (``"camera"``/``"scan"``):
    file, time, detector, and the camera grid or the scan settings."""
    path = Path(path)
    detectors, preferred = preferred_detector(path, role)
    recording = read_recording(path, detector or preferred)
    attrs = recording.attrs
    lines = [("File", path.name),
             ("Recorded", str(_plain(attrs.get("acquisition:start_time")) or "not recorded")),
             ("Detector", recording.detector),
             ("Recording mode", str(_plain(attrs.get("Rec:Mode")) or "not recorded"))]
    warnings = list(recording.notes)
    setup_name = _plain(attrs.get("setup:name"))
    setup_hash = _plain(attrs.get("setup:hash"))
    lines.append(("Setup", f"{setup_name or 'not recorded'}"
                           + (f" ({str(setup_hash).split(':')[-1][:8]}…)" if setup_hash else "")))
    rows, cols = recording.image.shape
    summary = RecordingSummary(role, path, recording.detector, detectors, lines, warnings, setup_name,
                               setup_hash, recording)
    if role == "camera":
        if is_scan_detector(recording):
            warnings.append(f"{recording.detector} is a scan detector, not a camera")
        context, notes = camera_context_from_recording(recording)
        summary.context = context
        warnings.extend(notes)
        roi = context.roi
        units = (((_inventory(recording).get("detectors") or {}).get(recording.detector) or {})
                 .get("parameters", {}).get("exposure", {}) or {}).get("units", "")
        exposure = attrs.get(f"Detector:{recording.detector}:Param:exposure")
        lines += [("Frame", f"{rows} × {cols} px"),
                  ("ROI offset (x, y)", f"{roi[0]}, {roi[1]}" if roi else "not recorded"),
                  ("Binning", str(context.binning) if context.binning else "not recorded"),
                  ("Pixel size", (f"{context.pixel_size_um[1]:.4g} µm" if context.pixel_size_um else "not recorded")),
                  ("Exposure", f"{_plain(exposure)} {units}".strip() if exposure is not None else "not recorded"),
                  ("Frames averaged", str(recording.frames))]
    else:
        if not is_scan_detector(recording):
            warnings.append(f"{recording.detector} does not look like a scan detector (APD/PMT)")
        mode = str(_plain(attrs.get("Rec:Mode")) or "")
        if mode and mode not in ("ScanOnce", "ScanLapse"):
            warnings.append(f"recorded in mode {mode!r}, not a scan recording (Scan once)")
        scan = _inventory(recording).get("scan") or {}
        panel = str(scan.get("scanWidgetType") or scan.get("controller") or "not recorded")
        if "advanced" not in panel.lower():
            warnings.append(f"the scan panel was {panel!r}, not the Advanced scan")
        try:
            geometry = scan_geometry_from_recording(recording)
            summary.geometry = geometry
            fast, slow = geometry["axes"]
            lengths = dict(zip(_seq(attrs.get("ScanStage:target_device")), _seq(attrs.get("ScanStage:axis_length"))))
            centres = dict(zip(_seq(attrs.get("ScanStage:target_device")), _seq(attrs.get("ScanStage:axis_centerpos"))))
            lines += [("Scan axes", f"fast {fast['device']}, slow {slow['device']}"),
                      ("Field", f"{fast['device']} {_fmt_um(lengths.get(fast['device'], 0))} × "
                                f"{slow['device']} {_fmt_um(lengths.get(slow['device'], 0))}"),
                      ("Step", f"{fast['device']} {_fmt_um(fast['step_um'])}, {slow['device']} {_fmt_um(slow['step_um'])}"),
                      ("Centre", f"{fast['device']} {_fmt_um(centres.get(fast['device'], 0))}, "
                                 f"{slow['device']} {_fmt_um(centres.get(slow['device'], 0))}"),
                      ("Pixels", f"{cols} (fast) × {rows} (slow)")]
        except BeadCalibrationError as error:
            warnings.append(str(error))
            lines.append(("Scan geometry", "cannot be read"))
        dwell = attrs.get("ScanStage:sequence_time")
        lines.append(("Dwell", f"{float(_plain(dwell)) * 1e6:.3g} µs" if dwell is not None else "not recorded"))
        phase = attrs.get("ScanStage:phase_delay")
        lines.append(("Phase delay", f"{_plain(phase)}" if phase is not None else "not recorded"))
        lines.append(("Scan panel", panel))
        lines.append(("Lasers", _lasers_used(attrs)))
        lines.append(("Frames averaged", str(recording.frames)))
    image = recording.image
    lines.append(("Counts", f"min {image.min():.0f}, max {image.max():.0f}"))
    if np.issubdtype(np.dtype(recording.dtype), np.integer) and recording.frames == 1 \
            and image.max() >= np.iinfo(np.dtype(recording.dtype)).max:
        warnings.append(f"saturated pixels (max {image.max():.0f} = the {recording.dtype} maximum): "
                        "saturated beads are left out")
    summary.lines = lines
    return summary


def _lasers_used(attrs: Mapping) -> str:
    enabled = attrs.get("ScanTTL:linestep_enable")
    names = [str(n) for n in _seq(attrs.get("ScanTTL:target_device"))]
    used = []
    for name in names:
        on = True
        if isinstance(enabled, dict) and name in enabled:
            on = any(bool(v) for v in _seq(enabled[name]))
        if on:
            value = attrs.get(f"Laser:{name}:Value")
            units = attrs.get(f"Laser:{name}:Units") or ""
            state = attrs.get(f"Laser:{name}:Enabled")
            text = name
            if value is not None:
                text += f" ({_plain(value)} {_plain(units)}".rstrip() + ")"
            if state is not None and str(state) in ("False", "0"):
                text += " off"
            used.append(text)
    return ", ".join(used) or "none recorded"


def compare_summaries(camera: RecordingSummary | None, scan: RecordingSummary | None, *,
                      running_setup_hash: str | None = None, current_camera_identity=None) -> list[str]:
    """Warnings that need both files, the running setup or the camera's
    current grid."""
    warnings = []
    if camera and scan and camera.setup_hash and scan.setup_hash and camera.setup_hash != scan.setup_hash:
        warnings.append(f"the two files come from different setups ({camera.setup_name} / {scan.setup_name})")
    for summary in (camera, scan):
        if summary and running_setup_hash and summary.setup_hash and summary.setup_hash != running_setup_hash:
            warnings.append(f"{summary.path.name} was recorded with another setup ({summary.setup_name}) "
                            "than the one running")
    if camera and camera.context is not None and current_camera_identity is not None:
        reasons = frame_identity_of_context(camera.context).differs_from(current_camera_identity)
        if reasons:
            warnings.append(f"the camera's grid now differs from the recorded one ({'; '.join(reasons)}): a "
                            "pipeline on the current grid would refuse this calibration at arm")
    return warnings


def plain_reason(error: Exception) -> tuple[str, str]:
    """A refusal in plain words, and what to do about it."""
    text = str(error)
    lowered = text.lower()
    if "too few beads" in lowered:
        return "Too few beads were found.", ("Use a denser bead sample, check focus and exposure, or lower the "
                                             "detection threshold (Advanced).")
    if "ambiguous" in lowered:
        return "The bead pattern is regular, so it matches in more than one way.", (
            "Image a sparse, random bead field (not a grid or rows of beads).")
    if "pair up" in lowered or "no bead pattern" in lowered:
        return "The two images do not show the same beads.", (
            "Record both at the same stage position and focus, and check that you picked the right files.")
    if "residuals too large" in lowered:
        return "The beads do not line up well enough.", (
            "Check for drift between the two recordings, field distortion, or wrong pairs; record again.")
    return "The calibration could not be made.", "See the details below."


def record_path(calibration_id: str, folder=None) -> Path:
    from imswitch.imcontrol.model.smartpipeline.calibrations import _file_name

    return calibrations_root(folder) / TRANSFORMS_DIRNAME / f"{_file_name(calibration_id)}.json"


def archive_calibration(calibration_id: str, folder=None) -> str:
    """Keep an existing calibration under a dated id
    (``<id>_<YYYYmmdd-HHMMSS>``): its record, figure and report are renamed
    and its pipeline entry rewritten to the new id. Returns the new id."""
    from imswitch.imcontrol.model.smartpipeline.calibrations import _file_name

    root = calibrations_root(folder)
    name = _file_name(calibration_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    new_id = f"{name}_{stamp}"
    transforms = root / TRANSFORMS_DIRNAME
    for suffix in (".json", "_qc.png", "_report.txt"):
        old = transforms / f"{name}{suffix}"
        if old.exists():
            old.rename(transforms / f"{new_id}{suffix}")
    entry = root / f"{name}.json"
    if entry.exists():
        payload = json.loads(entry.read_text(encoding="utf-8"))
        payload["id"] = new_id
        if payload.get("path"):
            payload["path"] = f"{TRANSFORMS_DIRNAME}/{new_id}.json"
        (root / f"{new_id}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        entry.unlink()
    return new_id


# --------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------

_HOW_TO_RECORD = """
How to record the two files (same bead sample, same stage position and focus):
  camera: Recording panel, File format HDF5 (or Zarr), select the camera, Snap
          (or record a few frames: they are averaged). Keep the camera ROI and
          binning you will use in the experiment.
  APD:    set the Advanced scan exactly as for the experiment (same axes,
          same phase delay, step, dwell), around the beads; Recording panel,
          select the APD, "Scan once", File format HDF5 (or Zarr), Record.
  Bead sample: sparse (a few um apart), non-regular, visible in both channels.
"""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m imswitch.imcontrol.model.bead_calibration",
        description="Camera pixel -> scan um calibration from two bead recordings (HDF5/Zarr).",
        epilog=_HOW_TO_RECORD, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    cal = sub.add_parser("calibrate", help="fit and save a calibration (the default)")
    cal.add_argument("camera_file")
    cal.add_argument("scan_file")
    cal.add_argument("--id", default="cam_to_scan", help="calibration id (pipeline transform name)")
    cal.add_argument("--camera-detector")
    cal.add_argument("--scan-detector")
    cal.add_argument("--model", default="auto", choices=("auto", "similarity_reflect", "affine"))
    cal.add_argument("--max-residual-um", type=float, default=0.25)
    cal.add_argument("--min-beads", type=int, default=6)
    cal.add_argument("--camera-threshold", type=float, default=6.0)
    cal.add_argument("--scan-threshold", type=float, default=6.0)
    cal.add_argument("--camera-sigma-px", type=float, default=0.0)
    cal.add_argument("--scan-sigma-px", type=float, default=0.0)
    cal.add_argument("--tolerance-um", type=float, default=0.0)
    cal.add_argument("--no-mirror", action="store_true", help="do not consider a mirrored camera")
    cal.add_argument("--fast-axis-swept", choices=("yes", "no"))
    cal.add_argument("--target-frame", default="scanner:galvo")
    cal.add_argument("--folder", help="calibrations folder (default <ImSwitchConfig>/calibrations)")
    cal.add_argument("--overwrite", action="store_true")
    ver = sub.add_parser("verify", help="check a saved calibration on two (new) bead recordings")
    ver.add_argument("calibration", help="calibration id or record path")
    ver.add_argument("camera_file")
    ver.add_argument("scan_file")
    ver.add_argument("--camera-detector")
    ver.add_argument("--scan-detector")
    ver.add_argument("--folder")
    pre = sub.add_parser("predict", help="scan centre for a camera pixel")
    pre.add_argument("calibration")
    pre.add_argument("row", type=float)
    pre.add_argument("col", type=float)
    pre.add_argument("--folder")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("calibrate", "verify", "predict", "-h", "--help"):
        argv.insert(0, "calibrate")
    args = parser.parse_args(argv)
    try:
        if args.command == "verify":
            report = verify_from_files(args.calibration, args.camera_file, args.scan_file,
                                       camera_detector=args.camera_detector, scan_detector=args.scan_detector,
                                       folder=args.folder)
            print(json.dumps({k: v for k, v in report.items() if k != "distances_um"}, indent=2))
            return 0 if not report["grid_problems"] else 2
        if args.command == "predict":
            print(json.dumps(predict_scan_centre(load_bead_calibration(args.calibration, args.folder),
                                                 args.row, args.col)))
            return 0
        if args.command is None:
            parser.print_help()
            return 1
        options = BeadRegistrationOptions(
            camera={"threshold": args.camera_threshold, "sigma_px": args.camera_sigma_px or None},
            scan={"threshold": args.scan_threshold, "sigma_px": args.scan_sigma_px or None},
            model=args.model, max_residual=args.max_residual_um, min_beads=args.min_beads,
            tolerance=args.tolerance_um or None, allow_mirror=not args.no_mirror)
        swept = None if args.fast_axis_swept is None else args.fast_axis_swept == "yes"
        result = calibrate_from_files(args.camera_file, args.scan_file, args.id,
                                      camera_detector=args.camera_detector, scan_detector=args.scan_detector,
                                      options=options, fast_axis_swept=swept, target_frame=args.target_frame,
                                      folder=args.folder, overwrite=args.overwrite)
        print(result.report())
        return 0
    except (BeadRegistrationError, BeadCalibrationError) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    os.environ.setdefault("MPLBACKEND", "Agg")
    raise SystemExit(main())
