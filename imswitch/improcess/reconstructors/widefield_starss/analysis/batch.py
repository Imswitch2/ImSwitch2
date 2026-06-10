"""Batch helpers for WidefieldSTARSS H/V pair analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import h5py
import numpy as np
import pandas as pd
import tifffile as tiff

from imswitch.improcess.model.plotting import PlotPayload, PlotSeries

from .pipeline import WidefieldStarssAnalysis, WidefieldStarssParams, analyze_widefield_starss_pair

BatchProgressCallback = Callable[[dict[str, object]], None]
BatchCancelCallback = Callable[[], bool]


class WidefieldStarssBatchCancelled(RuntimeError):
    """Raised when a batch run is cancelled between file pairs."""


@dataclass(frozen=True)
class WidefieldStarssPair:
    """One matched H/V file pair."""

    sample_id: str
    h_path: Path
    v_path: Path


@dataclass
class WidefieldStarssBatchResult:
    """Aggregated output from a WFS batch run."""

    pairs: list[WidefieldStarssPair]
    analyses: list[WidefieldStarssAnalysis]
    regions: pd.DataFrame
    summary: pd.DataFrame
    unmatched: list[Path]
    params: WidefieldStarssParams

    def save_csv(self, output_dir: str | Path) -> tuple[Path, Path]:
        """Write consolidated region and per-sample summary CSV files."""
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        regions_path = output / "batch_regions.csv"
        summary_path = output / "batch_summary.csv"
        self.regions.to_csv(regions_path, index=False)
        self.summary.to_csv(summary_path, index=False)
        return regions_path, summary_path

    def save_hdf5(self, path: str | Path) -> Path:
        """Write consolidated tables and batch metadata to HDF5."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with h5py.File(str(path), "w") as h5:
            _write_dataframe_group(h5.create_group("regions"), self.regions)
            _write_dataframe_group(h5.create_group("summary"), self.summary)
            pairs = h5.create_group("pairs")
            dtype = h5py.string_dtype(encoding="utf-8")
            pairs.create_dataset("sample_id", data=np.asarray([p.sample_id for p in self.pairs], dtype=dtype))
            pairs.create_dataset("h_path", data=np.asarray([str(p.h_path) for p in self.pairs], dtype=dtype))
            pairs.create_dataset("v_path", data=np.asarray([str(p.v_path) for p in self.pairs], dtype=dtype))
            h5.attrs["pair_count"] = len(self.pairs)
            h5.attrs["region_count"] = len(self.regions)
            h5.attrs["unmatched_count"] = len(self.unmatched)
            h5.attrs["anisotropy_mode"] = self.params.anisotropy_mode
            h5.attrs["segmentation_mode"] = self.params.segmentation_mode
        return path

    def plot_payloads(self) -> list[PlotPayload]:
        """Return aggregate plots for the generic ImProcess graph widget."""
        payloads: list[PlotPayload] = []

        anisotropy = _finite_column(self.regions, "anisotropy_direct")
        if anisotropy.size:
            payloads.append(
                PlotPayload(
                    title="Batch region anisotropy",
                    x_label="Anisotropy",
                    y_label="Region count",
                    series=[
                        PlotSeries(
                            name="Regions",
                            y=anisotropy,
                            kind="histogram",
                            style={"bins": 50},
                        )
                    ],
                    metadata={"pair_count": len(self.pairs), "region_count": len(self.regions)},
                )
            )

        area = _finite_column(self.regions, "area_pixels")
        if area.size and anisotropy.size:
            size = min(area.size, anisotropy.size)
            payloads.append(
                PlotPayload(
                    title="Batch area vs anisotropy",
                    x_label="Area (px)",
                    y_label="Anisotropy",
                    series=[
                        PlotSeries(
                            name="Regions",
                            x=area[:size],
                            y=anisotropy[:size],
                            kind="scatter",
                        )
                    ],
                    metadata={"pair_count": len(self.pairs), "region_count": len(self.regions)},
                )
            )

        pair_index = _finite_column(self.summary, "pair_index")
        mean_anisotropy = _finite_column(self.summary, "mean_anisotropy_direct")
        if pair_index.size and mean_anisotropy.size:
            size = min(pair_index.size, mean_anisotropy.size)
            payloads.append(
                PlotPayload(
                    title="Batch mean anisotropy per sample",
                    x_label="Pair index",
                    y_label="Mean anisotropy",
                    series=[
                        PlotSeries(
                            name="Mean anisotropy",
                            x=pair_index[:size],
                            y=mean_anisotropy[:size],
                            kind="line",
                        )
                    ],
                    metadata={"pair_count": len(self.pairs)},
                )
            )

        region_count = _finite_column(self.summary, "region_count")
        if pair_index.size and region_count.size:
            size = min(pair_index.size, region_count.size)
            payloads.append(
                PlotPayload(
                    title="Batch regions per sample",
                    x_label="Pair index",
                    y_label="Region count",
                    series=[
                        PlotSeries(
                            name="Regions",
                            x=pair_index[:size],
                            y=region_count[:size],
                            kind="line",
                        )
                    ],
                    metadata={"pair_count": len(self.pairs)},
                )
            )

        return payloads


def discover_widefield_starss_pairs(
    paths: Iterable[str | Path],
    h_suffix: str = "_h",
    v_suffix: str = "_v",
) -> tuple[list[WidefieldStarssPair], list[Path]]:
    """
    Match files whose stems end in ``h_suffix`` / ``v_suffix`` into WFS pairs.

    Returns sorted pairs and unmatched input paths.
    """
    by_sample: dict[str, dict[str, Path]] = {}
    unmatched_candidates: list[Path] = []

    for raw_path in paths:
        path = Path(raw_path)
        if path.suffix.lower() not in (".tif", ".tiff"):
            unmatched_candidates.append(path)
            continue
        match = _role_from_path(path, h_suffix, v_suffix)
        if match is None:
            unmatched_candidates.append(path)
            continue
        role, matched_suffix = match
        sample_id = path.stem[:-len(matched_suffix)]
        by_sample.setdefault(sample_id, {})[role] = path

    pairs: list[WidefieldStarssPair] = []
    unmatched = list(unmatched_candidates)
    for sample_id in sorted(by_sample):
        roles = by_sample[sample_id]
        if "H" in roles and "V" in roles:
            pairs.append(WidefieldStarssPair(sample_id=sample_id, h_path=roles["H"], v_path=roles["V"]))
        else:
            unmatched.extend(roles.values())
    return pairs, sorted(unmatched, key=lambda p: str(p))


def discover_widefield_starss_pairs_in_folder(
    folder: str | Path,
    h_suffix: str = "_h",
    v_suffix: str = "_v",
) -> tuple[list[WidefieldStarssPair], list[Path]]:
    """Discover WFS pairs from TIFF files directly inside ``folder``."""
    folder = Path(folder)
    paths = [*folder.glob("*.tif"), *folder.glob("*.tiff")]
    return discover_widefield_starss_pairs(paths, h_suffix, v_suffix)


def run_widefield_starss_batch(
    pairs: Iterable[WidefieldStarssPair],
    params: WidefieldStarssParams | None = None,
    unmatched: Iterable[str | Path] | None = None,
    progress_callback: BatchProgressCallback | None = None,
    cancel_callback: BatchCancelCallback | None = None,
) -> WidefieldStarssBatchResult:
    """Analyze each H/V pair and aggregate per-region and per-sample tables."""
    params = params or WidefieldStarssParams()
    pair_list = list(pairs)
    analyses: list[WidefieldStarssAnalysis] = []
    region_tables: list[pd.DataFrame] = []
    summary_rows: list[dict[str, object]] = []

    for pair_index, pair in enumerate(pair_list):
        if cancel_callback is not None and cancel_callback():
            raise WidefieldStarssBatchCancelled("WidefieldSTARSS batch cancelled")
        if progress_callback is not None:
            progress_callback(_progress_payload("processing", pair_index, len(pair_list), pair))
        stack_h = tiff.imread(str(pair.h_path))
        stack_v = tiff.imread(str(pair.v_path))
        analysis = analyze_widefield_starss_pair(stack_h, stack_v, params)
        analyses.append(analysis)

        region_table = analysis.regions.copy()
        region_table.insert(0, "pair_index", pair_index)
        region_table.insert(1, "sample_id", pair.sample_id)
        region_table.insert(2, "source_h_path", str(pair.h_path))
        region_table.insert(3, "source_v_path", str(pair.v_path))
        region_tables.append(region_table)

        summary_rows.append(_summary_row(pair_index, pair, analysis))
        if progress_callback is not None:
            progress_callback(_progress_payload("completed", pair_index, len(pair_list), pair))
        if cancel_callback is not None and cancel_callback():
            raise WidefieldStarssBatchCancelled("WidefieldSTARSS batch cancelled")

    regions = (
        pd.concat(region_tables, ignore_index=True)
        if region_tables
        else pd.DataFrame()
    )
    summary = pd.DataFrame(summary_rows)
    return WidefieldStarssBatchResult(
        pairs=pair_list,
        analyses=analyses,
        regions=regions,
        summary=summary,
        unmatched=[Path(path) for path in (unmatched or [])],
        params=params,
    )


def run_widefield_starss_batch_from_folder(
    folder: str | Path,
    params: WidefieldStarssParams | None = None,
    progress_callback: BatchProgressCallback | None = None,
    cancel_callback: BatchCancelCallback | None = None,
    h_suffix: str = "_h",
    v_suffix: str = "_v",
) -> WidefieldStarssBatchResult:
    """Discover and analyze all WFS pairs in ``folder``."""
    pairs, unmatched = discover_widefield_starss_pairs_in_folder(folder, h_suffix, v_suffix)
    return run_widefield_starss_batch(
        pairs,
        params=params,
        unmatched=unmatched,
        progress_callback=progress_callback,
        cancel_callback=cancel_callback,
    )


def _progress_payload(
    state: str,
    pair_index: int,
    pair_count: int,
    pair: WidefieldStarssPair,
) -> dict[str, object]:
    completed = pair_index + 1 if state == "completed" else pair_index
    return {
        "state": state,
        "pair_index": pair_index,
        "pair_count": pair_count,
        "completed": completed,
        "sample_id": pair.sample_id,
        "h_path": str(pair.h_path),
        "v_path": str(pair.v_path),
    }


def _role_from_path(
    path: Path,
    h_suffix: str = "_h",
    v_suffix: str = "_v",
) -> tuple[str, str] | None:
    """Return (role, matched suffix) for a path, or None if neither matches.

    Matching is case-insensitive on the file stem. If both suffixes match
    (e.g. 'h' and '_h'), the longer one wins so the more specific suffix
    cannot be shadowed by the shorter.
    """
    lower = path.stem.lower()
    matches = [
        (role, suffix)
        for role, suffix in (("H", h_suffix), ("V", v_suffix))
        if suffix and lower.endswith(suffix.lower())
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[1]))


def _summary_row(pair_index: int, pair: WidefieldStarssPair, analysis: WidefieldStarssAnalysis) -> dict[str, object]:
    regions = analysis.regions

    def stat(column: str, fn, default=np.nan):
        if column not in regions or len(regions) == 0:
            return default
        values = regions[column].to_numpy(dtype=float)
        values = values[np.isfinite(values)]
        if values.size == 0:
            return default
        return float(fn(values))

    return {
        "pair_index": pair_index,
        "sample_id": pair.sample_id,
        "source_h_path": str(pair.h_path),
        "source_v_path": str(pair.v_path),
        "region_count": int(len(regions)),
        "total_area_pixels": stat("area_pixels", np.sum, default=0.0),
        "mean_area_pixels": stat("area_pixels", np.mean),
        "mean_anisotropy_direct": stat("anisotropy_direct", np.mean),
        "median_anisotropy_direct": stat("anisotropy_direct", np.median),
        "std_anisotropy_direct": stat("anisotropy_direct", np.std),
        "mean_anisotropy_fit": stat("anisotropy_fit", np.mean),
        "mean_intensity_h_total": stat("H_total_mean_signal", np.mean),
        "mean_intensity_v_total": stat("V_total_mean_signal", np.mean),
        "mean_ellipticity": stat("ellipticity", np.mean),
        "anisotropy_mode": analysis.anis_maps.anisotropy_mode,
        "segmentation_mode": analysis.params.segmentation_mode if analysis.params is not None else None,
    }


def _write_dataframe_group(group, dataframe: pd.DataFrame) -> None:
    dtype = h5py.string_dtype(encoding="utf-8")
    group.attrs["row_count"] = len(dataframe)
    for column in dataframe.columns:
        values = dataframe[column].to_numpy()
        if values.dtype.kind in ("O", "U", "S"):
            values = np.asarray([str(value) for value in values], dtype=dtype)
        group.create_dataset(str(column), data=values)


def _finite_column(dataframe: pd.DataFrame, column: str) -> np.ndarray:
    if column not in dataframe:
        return np.asarray([], dtype=float)
    values = dataframe[column].to_numpy(dtype=float)
    return values[np.isfinite(values)]
