"""Generate synthetic multicolor nuclei stacks for pipeline testing.

This is intentionally a standalone sideproject script. Edit the parameter block
inside ``main()`` and run the file directly.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

import numpy as np
import tifffile


@dataclass(frozen=True)
class SimulationParams:
    output_path: Path = Path("simulated_multicolor_stack.ome.tif")
    seed: int = 1

    planes: int = 10
    height: int = 5000
    width: int = 5000
    channel_names: tuple[str, ...] = ("dapi", "code1", "code2", "code3", "code4")

    dtype: str = "uint16"
    camera_offset: float = 100.0
    background_counts: tuple[float, ...] = (20.0, 5.0, 5.0, 5.0, 5.0)

    cells_per_plane: int = 2500
    nucleus_diameter_px: float = 28.0
    nucleus_diameter_jitter_px: float = 3.0
    nucleus_edge_softness_px: float = 1.0
    minimum_gap_px: float = 2.0
    max_placement_attempts_per_cell: int = 500

    code_ratios: tuple[float, ...] = (0.40, 0.20, 0.20, 0.20)
    double_fraction: float = 0.02

    code_signal_min: float = 5.0
    code_signal_max: float = 500.0
    code_signal_exponential_scale: float = 80.0

    dapi_signal_min: float = 300.0
    dapi_signal_max: float = 4000.0
    dapi_signal_exponential_scale: float = 700.0

    write_label_stack: bool = True
    overwrite: bool = True
    print_progress: bool = True


@dataclass(frozen=True)
class SimulationResult:
    image_path: Path
    config_path: Path
    cell_table_path: Path
    label_path: Path | None
    shape: tuple[int, int, int, int]
    cell_count: int


@dataclass(frozen=True)
class SimulatedCell:
    cell_id: int
    plane: int
    y: float
    x: float
    radius: float
    primary_code_index: int
    secondary_code_index: int | None
    dominant_code_index: int
    dapi_signal: float
    code_signals: tuple[float, ...]

    @property
    def is_double(self) -> bool:
        return self.secondary_code_index is not None


def main() -> SimulationResult:
    params = SimulationParams(
        output_path=Path("simulated_multicolor_stack.ome.tif"),
        seed=1,
        planes=10,
        height=5000,
        width=5000,
        channel_names=("dapi", "code1", "code2", "code3", "code4"),
        dtype="uint16",
        camera_offset=100.0,
        background_counts=(20.0, 5.0, 5.0, 5.0, 5.0),
        cells_per_plane=2500,
        nucleus_diameter_px=28.0,
        nucleus_diameter_jitter_px=3.0,
        nucleus_edge_softness_px=1.0,
        minimum_gap_px=2.0,
        code_ratios=(0.40, 0.20, 0.20, 0.20),
        double_fraction=0.02,
        code_signal_min=5.0,
        code_signal_max=500.0,
        code_signal_exponential_scale=80.0,
        dapi_signal_min=300.0,
        dapi_signal_max=4000.0,
        dapi_signal_exponential_scale=700.0,
        write_label_stack=True,
        overwrite=True,
        print_progress=True,
    )
    return simulate_multicolor_nuclei(params)


def simulate_multicolor_nuclei(params: SimulationParams) -> SimulationResult:
    """Generate a synthetic stack and ground-truth sidecars."""
    prepared = _validate_params(params)
    rng = np.random.default_rng(params.seed)

    image_path = Path(params.output_path)
    config_path = _sidecar_path(image_path, "_config.json")
    cell_table_path = _sidecar_path(image_path, "_cells.csv")
    label_path = _sidecar_path(image_path, "_labels.tif") if params.write_label_stack else None

    output_paths = [image_path, config_path, cell_table_path]
    if label_path is not None:
        output_paths.append(label_path)
    _prepare_output_paths(output_paths, overwrite=params.overwrite)

    dtype = np.dtype(params.dtype)
    shape = (params.planes, len(params.channel_names), params.height, params.width)
    metadata = {
        "axes": "ZCYX",
        "Channel": {"Name": list(params.channel_names)},
    }
    image_stack = tifffile.memmap(
        image_path,
        shape=shape,
        dtype=dtype,
        bigtiff=True,
        ome=True,
        metadata=metadata,
    )

    label_stack = None
    if label_path is not None:
        label_stack = tifffile.memmap(
            label_path,
            shape=(params.planes, params.height, params.width),
            dtype=np.uint32,
            bigtiff=True,
            ome=True,
            metadata={"axes": "ZYX"},
        )

    records: list[SimulatedCell] = []
    next_cell_id = 1
    if params.print_progress:
        gib = np.prod(shape) * dtype.itemsize / 1024**3
        print(f"Writing {shape} {dtype} image stack to {image_path} ({gib:.2f} GiB)")

    for plane_index in range(params.planes):
        plane_cells = _generate_plane_cells(
            params=params,
            prepared=prepared,
            rng=rng,
            plane_index=plane_index,
            first_cell_id=next_cell_id,
        )
        records.extend(plane_cells)
        next_cell_id += len(plane_cells)

        if params.print_progress:
            print(f"Plane {plane_index + 1}/{params.planes}: {len(plane_cells)} cells")

        if label_stack is not None:
            label_stack[plane_index] = _render_label_plane(
                params.height,
                params.width,
                plane_cells,
            )
            label_stack.flush()

        for channel_index, background in enumerate(prepared["background_counts"]):
            plane = rng.poisson(background, size=(params.height, params.width)).astype(
                np.uint32,
                copy=False,
            )
            _add_cell_signals(
                plane,
                plane_cells,
                channel_index=channel_index,
                params=params,
                rng=rng,
            )
            image_stack[plane_index, channel_index] = _apply_camera_offset_and_clip(
                plane,
                offset=params.camera_offset,
                dtype=dtype,
            )
            image_stack.flush()

    image_stack.flush()
    if label_stack is not None:
        label_stack.flush()

    _write_config(config_path, params, shape=shape, cell_count=len(records))
    _write_cell_table(cell_table_path, records, params.channel_names)

    return SimulationResult(
        image_path=image_path,
        config_path=config_path,
        cell_table_path=cell_table_path,
        label_path=label_path,
        shape=shape,
        cell_count=len(records),
    )


def _validate_params(params: SimulationParams) -> dict[str, np.ndarray]:
    channel_count = len(params.channel_names)
    if channel_count < 2:
        raise ValueError("channel_names must include DAPI plus at least one code channel")
    if params.channel_names[0].lower() != "dapi":
        raise ValueError("channel_names[0] must be 'dapi'")
    if params.planes < 1 or params.height < 1 or params.width < 1:
        raise ValueError("planes, height, and width must be positive")
    if params.cells_per_plane < 0:
        raise ValueError("cells_per_plane must be non-negative")
    if params.camera_offset < 0:
        raise ValueError("camera_offset must be non-negative")

    dtype = np.dtype(params.dtype)
    if not np.issubdtype(dtype, np.integer):
        raise ValueError("dtype must be an integer dtype")
    if np.iinfo(dtype).min < 0:
        raise ValueError("dtype must be an unsigned integer dtype")

    background = np.asarray(params.background_counts, dtype=np.float64)
    if background.shape != (channel_count,):
        raise ValueError(
            f"background_counts must have {channel_count} values, got {background.size}"
        )
    if np.any(background < 0):
        raise ValueError("background_counts must be non-negative")

    code_count = channel_count - 1
    code_ratios = np.asarray(params.code_ratios, dtype=np.float64)
    if code_ratios.shape != (code_count,):
        raise ValueError(f"code_ratios must have {code_count} values, got {code_ratios.size}")
    if np.any(code_ratios < 0) or not np.any(code_ratios > 0):
        raise ValueError("code_ratios must contain at least one positive value")
    code_ratios = code_ratios / code_ratios.sum()

    if not 0 <= params.double_fraction <= 1:
        raise ValueError("double_fraction must be between 0 and 1")
    if params.double_fraction > 0 and code_count < 2:
        raise ValueError("double_fraction requires at least two code channels")

    _validate_signal_distribution(
        params.code_signal_min,
        params.code_signal_max,
        params.code_signal_exponential_scale,
        name="code signal",
    )
    _validate_signal_distribution(
        params.dapi_signal_min,
        params.dapi_signal_max,
        params.dapi_signal_exponential_scale,
        name="DAPI signal",
    )
    if params.nucleus_diameter_px <= 0:
        raise ValueError("nucleus_diameter_px must be positive")
    if params.nucleus_diameter_jitter_px < 0:
        raise ValueError("nucleus_diameter_jitter_px must be non-negative")
    if params.nucleus_edge_softness_px < 0:
        raise ValueError("nucleus_edge_softness_px must be non-negative")
    if params.minimum_gap_px < 0:
        raise ValueError("minimum_gap_px must be non-negative")
    if params.max_placement_attempts_per_cell < 1:
        raise ValueError("max_placement_attempts_per_cell must be positive")

    max_radius = _max_radius(params)
    if params.height <= 2 * max_radius or params.width <= 2 * max_radius:
        raise ValueError("image dimensions must be larger than the maximum nucleus diameter")

    return {
        "background_counts": background,
        "code_ratios": code_ratios,
    }


def _validate_signal_distribution(minimum: float, maximum: float, scale: float, *, name: str) -> None:
    if minimum < 0:
        raise ValueError(f"{name} minimum must be non-negative")
    if maximum < minimum:
        raise ValueError(f"{name} maximum must be greater than or equal to minimum")
    if maximum > minimum and scale <= 0:
        raise ValueError(f"{name} exponential scale must be positive")


def _prepare_output_paths(paths: list[Path | None], *, overwrite: bool) -> None:
    existing = [path for path in paths if path is not None and path.exists()]
    if existing and not overwrite:
        names = ", ".join(str(path) for path in existing)
        raise FileExistsError(f"Output file(s) already exist: {names}")
    for path in paths:
        if path is None:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and overwrite:
            path.unlink()


def _generate_plane_cells(
    *,
    params: SimulationParams,
    prepared: dict[str, np.ndarray],
    rng: np.random.Generator,
    plane_index: int,
    first_cell_id: int,
) -> list[SimulatedCell]:
    geometries = _place_nuclei(params, rng)
    cells = []
    for local_index, (y, x, radius) in enumerate(geometries):
        primary = int(rng.choice(len(prepared["code_ratios"]), p=prepared["code_ratios"]))
        secondary = None
        if rng.random() < params.double_fraction:
            secondary = _choose_secondary_code(primary, prepared["code_ratios"], rng)

        dapi_signal = float(
            _truncated_exponential(
                rng,
                minimum=params.dapi_signal_min,
                maximum=params.dapi_signal_max,
                scale=params.dapi_signal_exponential_scale,
            )
        )
        code_signals = np.zeros(len(prepared["code_ratios"]), dtype=np.float64)
        active = [primary] if secondary is None else [primary, secondary]
        for code_index in active:
            code_signals[code_index] = _truncated_exponential(
                rng,
                minimum=params.code_signal_min,
                maximum=params.code_signal_max,
                scale=params.code_signal_exponential_scale,
            )
        dominant = int(np.argmax(code_signals))

        cells.append(
            SimulatedCell(
                cell_id=first_cell_id + local_index,
                plane=plane_index,
                y=y,
                x=x,
                radius=radius,
                primary_code_index=primary,
                secondary_code_index=secondary,
                dominant_code_index=dominant,
                dapi_signal=dapi_signal,
                code_signals=tuple(float(value) for value in code_signals),
            )
        )
    return cells


def _place_nuclei(params: SimulationParams, rng: np.random.Generator) -> list[tuple[float, float, float]]:
    if params.cells_per_plane == 0:
        return []

    max_radius = _max_radius(params)
    grid_size = max(1.0, 2 * max_radius + params.minimum_gap_px)
    spatial_index: dict[tuple[int, int], list[int]] = {}
    placed: list[tuple[float, float, float]] = []

    for _ in range(params.cells_per_plane):
        accepted = False
        for _attempt in range(params.max_placement_attempts_per_cell):
            radius = _sample_radius(params, rng)
            y = float(rng.uniform(radius, params.height - radius))
            x = float(rng.uniform(radius, params.width - radius))
            if _can_place(y, x, radius, placed, spatial_index, grid_size, params.minimum_gap_px):
                placed.append((y, x, radius))
                key = _grid_key(y, x, grid_size)
                spatial_index.setdefault(key, []).append(len(placed) - 1)
                accepted = True
                break
        if not accepted:
            raise RuntimeError(
                f"Could place only {len(placed)} of {params.cells_per_plane} nuclei. "
                "Reduce cells_per_plane, nucleus_diameter_px, or minimum_gap_px."
            )
    return placed


def _sample_radius(params: SimulationParams, rng: np.random.Generator) -> float:
    mean = params.nucleus_diameter_px / 2
    if params.nucleus_diameter_jitter_px == 0:
        return float(mean)
    sigma = params.nucleus_diameter_jitter_px / 2
    radius = float(rng.normal(mean, sigma))
    return float(np.clip(radius, max(1.0, mean - 3 * sigma), mean + 3 * sigma))


def _max_radius(params: SimulationParams) -> float:
    return params.nucleus_diameter_px / 2 + 1.5 * params.nucleus_diameter_jitter_px


def _can_place(
    y: float,
    x: float,
    radius: float,
    placed: list[tuple[float, float, float]],
    spatial_index: dict[tuple[int, int], list[int]],
    grid_size: float,
    minimum_gap_px: float,
) -> bool:
    gy, gx = _grid_key(y, x, grid_size)
    for yy in range(gy - 1, gy + 2):
        for xx in range(gx - 1, gx + 2):
            for index in spatial_index.get((yy, xx), []):
                other_y, other_x, other_radius = placed[index]
                min_distance = radius + other_radius + minimum_gap_px
                if (y - other_y) ** 2 + (x - other_x) ** 2 < min_distance**2:
                    return False
    return True


def _grid_key(y: float, x: float, grid_size: float) -> tuple[int, int]:
    return int(y // grid_size), int(x // grid_size)


def _choose_secondary_code(
    primary: int,
    ratios: np.ndarray,
    rng: np.random.Generator,
) -> int:
    secondary_ratios = ratios.copy()
    secondary_ratios[primary] = 0
    if secondary_ratios.sum() <= 0:
        secondary_ratios[:] = 1
        secondary_ratios[primary] = 0
    secondary_ratios = secondary_ratios / secondary_ratios.sum()
    return int(rng.choice(len(ratios), p=secondary_ratios))


def _truncated_exponential(
    rng: np.random.Generator,
    *,
    minimum: float,
    maximum: float,
    scale: float,
) -> float:
    if maximum == minimum:
        return float(minimum)
    upper_cdf = 1.0 - math.exp(-(maximum - minimum) / scale)
    u = rng.random() * upper_cdf
    return float(minimum - scale * math.log1p(-u))


def _add_cell_signals(
    plane: np.ndarray,
    cells: list[SimulatedCell],
    *,
    channel_index: int,
    params: SimulationParams,
    rng: np.random.Generator,
) -> None:
    for cell in cells:
        signal = _cell_signal_for_channel(cell, channel_index)
        if signal <= 0:
            continue
        y0, y1, x0, x1, profile = _nucleus_profile(
            cell.y,
            cell.x,
            cell.radius,
            params.height,
            params.width,
            params.nucleus_edge_softness_px,
        )
        counts = rng.poisson(signal * profile).astype(np.uint32, copy=False)
        plane[y0:y1, x0:x1] += counts


def _cell_signal_for_channel(cell: SimulatedCell, channel_index: int) -> float:
    if channel_index == 0:
        return cell.dapi_signal
    return cell.code_signals[channel_index - 1]


def _render_label_plane(
    height: int,
    width: int,
    cells: list[SimulatedCell],
) -> np.ndarray:
    labels = np.zeros((height, width), dtype=np.uint32)
    for cell in cells:
        y0, y1, x0, x1, profile = _nucleus_profile(
            cell.y,
            cell.x,
            cell.radius,
            height,
            width,
            edge_softness_px=0.0,
        )
        mask = profile > 0
        labels[y0:y1, x0:x1][mask] = cell.cell_id
    return labels


def _nucleus_profile(
    y: float,
    x: float,
    radius: float,
    height: int,
    width: int,
    edge_softness_px: float,
) -> tuple[int, int, int, int, np.ndarray]:
    y0 = max(0, int(math.floor(y - radius - edge_softness_px)))
    y1 = min(height, int(math.ceil(y + radius + edge_softness_px)) + 1)
    x0 = max(0, int(math.floor(x - radius - edge_softness_px)))
    x1 = min(width, int(math.ceil(x + radius + edge_softness_px)) + 1)

    yy, xx = np.ogrid[y0:y1, x0:x1]
    distance = np.sqrt((yy - y) ** 2 + (xx - x) ** 2)
    if edge_softness_px <= 0:
        return y0, y1, x0, x1, (distance <= radius).astype(np.float32)

    profile = np.clip((radius + edge_softness_px - distance) / edge_softness_px, 0.0, 1.0)
    return y0, y1, x0, x1, profile.astype(np.float32, copy=False)


def _apply_camera_offset_and_clip(
    plane: np.ndarray,
    *,
    offset: float,
    dtype: np.dtype,
) -> np.ndarray:
    dtype_info = np.iinfo(dtype)
    if offset:
        plane = plane + np.uint32(round(offset))
    return np.clip(plane, dtype_info.min, dtype_info.max).astype(dtype, copy=False)


def _write_config(
    path: Path,
    params: SimulationParams,
    *,
    shape: tuple[int, int, int, int],
    cell_count: int,
) -> None:
    payload = {
        "model": "simulated_multicolor_nuclei",
        "model_version": 1,
        "shape_zcyx": shape,
        "cell_count": cell_count,
        "parameters": _jsonable(params),
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


def _write_cell_table(
    path: Path,
    records: list[SimulatedCell],
    channel_names: tuple[str, ...],
) -> None:
    code_names = channel_names[1:]
    fieldnames = [
        "cell_id",
        "plane",
        "y",
        "x",
        "radius",
        "is_double",
        "primary_channel",
        "secondary_channel",
        "dominant_channel",
        "active_channels",
        "dapi_signal",
        *[f"{name}_signal" for name in code_names],
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for cell in records:
            active_indices = [index for index, value in enumerate(cell.code_signals) if value > 0]
            row = {
                "cell_id": cell.cell_id,
                "plane": cell.plane,
                "y": f"{cell.y:.3f}",
                "x": f"{cell.x:.3f}",
                "radius": f"{cell.radius:.3f}",
                "is_double": int(cell.is_double),
                "primary_channel": code_names[cell.primary_code_index],
                "secondary_channel": (
                    "" if cell.secondary_code_index is None else code_names[cell.secondary_code_index]
                ),
                "dominant_channel": code_names[cell.dominant_code_index],
                "active_channels": ";".join(code_names[index] for index in active_indices),
                "dapi_signal": f"{cell.dapi_signal:.6g}",
            }
            for name, signal in zip(code_names, cell.code_signals, strict=True):
                row[f"{name}_signal"] = f"{signal:.6g}"
            writer.writerow(row)


def _sidecar_path(image_path: Path, suffix: str) -> Path:
    name = image_path.name
    lower_name = name.lower()
    for extension in (".ome.tiff", ".ome.tif", ".tiff", ".tif"):
        if lower_name.endswith(extension):
            return image_path.with_name(name[: -len(extension)] + suffix)
    return image_path.with_name(image_path.stem + suffix)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


if __name__ == "__main__":
    main()
