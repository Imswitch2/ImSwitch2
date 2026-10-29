"""Lightweight persistent processing-session catalog for ImProcess.

The session is deliberately *not* a raw-data loader and not another result
provider.  It records which acquisition sources exist on disk and which saved
reconstruction artifacts belong to each source.  Pixel arrays are only opened
when the user explicitly asks the normal FileIO path to load raw data or asks
the Session controller to add a saved reconstruction to the existing
Reconstruction list.

The first implementation scans HDF5 and Zarr acquisitions (plus TIFF/HDF5/Zarr
saved reconstruction artifacts) and understands the existing ImProcess
single-file and multi-file time-lapse metadata through ``discover_time_lapse``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.lapse_source import NotATimeLapse, discover_time_lapse
from imswitch.improcess.model.provenance_io import ProvenanceReadError, read_provenance


SESSION_FILENAME = "session.improcess.json"
_RAW_SUFFIXES = (".h5", ".hdf5", ".hdf", ".zarr")
_RESULT_SUFFIXES = (
    ".h5", ".hdf5", ".hdf", ".zarr", ".ome.zarr",
    ".tif", ".tiff", ".ome.tif", ".ome.tiff",
)


def _norm_path(path: str | Path) -> str:
    """Stable comparison form without requiring the path to exist."""
    text = os.path.abspath(os.path.expanduser(str(path)))
    return os.path.normcase(os.path.normpath(text))


def _stable_id(*parts: str) -> str:
    text = "\x1f".join(str(part) for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _is_supported_raw(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _RAW_SUFFIXES)


def _is_supported_result(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _RESULT_SUFFIXES)


@dataclass(frozen=True)
class SourceRef:
    path: str
    dataset: str | None = None

    @property
    def normalized_path(self) -> str:
        return _norm_path(self.path)


@dataclass
class SavedReconstruction:
    path: str
    name: str
    final: bool = False


@dataclass
class SessionDataset:
    id: str
    label: str
    kind: str  # "scan" | "time-lapse"
    primary_path: str
    primary_dataset: str | None
    source_refs: list[SourceRef] = field(default_factory=list)
    planned_timepoints: int | None = None
    saved_reconstructions: list[SavedReconstruction] = field(default_factory=list)

    @property
    def final_reconstruction(self) -> SavedReconstruction | None:
        return next((item for item in self.saved_reconstructions if item.final), None)

    @property
    def status(self) -> str:
        if self.final_reconstruction is not None:
            return "final"
        if self.saved_reconstructions:
            return "draft"
        return "raw"

    def matches_source(self, path: str | None, dataset: str | None = None) -> bool:
        if not path:
            return False
        wanted_path = _norm_path(path)
        wanted_dataset = str(dataset) if dataset not in (None, "") else None
        for ref in self.source_refs:
            if ref.normalized_path != wanted_path:
                continue
            # Time-lapse provenance currently identifies the anchor file but
            # uses a synthetic dataset label ("time-lapse").  Path identity is
            # enough there because source_refs contains every member file.
            if self.kind == "time-lapse":
                return True
            if wanted_dataset is None or ref.dataset is None:
                return True
            if str(ref.dataset) == wanted_dataset:
                return True
        return False


class ProcessingSession:
    """Persistent catalog of raw acquisitions and saved reconstructions."""

    schema = 1

    def __init__(self, raw_root: str | Path, results_root: str | Path):
        self.raw_root = Path(raw_root).expanduser().resolve()
        self.results_root = Path(results_root).expanduser().resolve()
        self.datasets: list[SessionDataset] = []

    @property
    def manifest_path(self) -> Path:
        return self.results_root / SESSION_FILENAME

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the lightweight catalog without materialising image pixels."""
        remembered_final = self._read_final_paths()
        self.datasets = self._discover_raw_datasets()
        self._discover_saved_reconstructions(remembered_final)
        self.save_manifest()

    def _candidate_raw_sources(self) -> Iterable[Path]:
        if not self.raw_root.exists():
            return []
        out: list[Path] = []
        try:
            entries = sorted(self.raw_root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        for entry in entries:
            if _is_supported_raw(entry):
                out.append(entry)
                continue
            # Existing live acquisition convention: root/<job>/<store>.
            # One level is enough for the prototype and avoids accidentally
            # crawling large experiment trees.
            if entry.is_dir():
                try:
                    for child in sorted(entry.iterdir(), key=lambda p: p.name.lower()):
                        if _is_supported_raw(child):
                            out.append(child)
                except OSError:
                    continue
        return out

    def _discover_raw_datasets(self) -> list[SessionDataset]:
        datasets: list[SessionDataset] = []
        seen_logical: set[str] = set()
        candidates = list(self._candidate_raw_sources())

        for path in candidates:
            try:
                names = list(DataObj.getDatasetNames(str(path)))
            except Exception:
                continue
            for dataset_name in names:
                # Prefer the existing time-lapse resolver.  It reads headers
                # and sibling names, not image pixels.
                try:
                    lapse = discover_time_lapse(path, dataset_name)
                except (NotATimeLapse, ValueError, RuntimeError, OSError):
                    lapse = None
                except Exception:
                    lapse = None

                if lapse is not None:
                    channel = lapse.channel(lapse.anchor_detector)
                    logical = "|".join((
                        "lapse",
                        _norm_path(lapse.root),
                        str(lapse.name),
                        str(lapse.anchor_detector),
                    ))
                    if logical in seen_logical:
                        continue
                    seen_logical.add(logical)
                    refs = [
                        SourceRef(str(item.path), item.dataset)
                        for item in channel.candidates
                    ]
                    label = str(lapse.name)
                    if lapse.anchor_detector and lapse.anchor_detector not in label:
                        label = f"{label} · {lapse.anchor_detector}"
                    datasets.append(SessionDataset(
                        id=_stable_id(logical),
                        label=label,
                        kind="time-lapse",
                        primary_path=str(lapse.anchor.path),
                        primary_dataset=lapse.anchor.dataset,
                        source_refs=refs,
                        planned_timepoints=int(lapse.planned_count),
                    ))
                    continue

                logical = f"scan|{_norm_path(path)}|{dataset_name}"
                if logical in seen_logical:
                    continue
                seen_logical.add(logical)
                label = path.name if len(names) == 1 else f"{path.name}::{dataset_name}"
                datasets.append(SessionDataset(
                    id=_stable_id(logical),
                    label=label,
                    kind="scan",
                    primary_path=str(path),
                    primary_dataset=str(dataset_name),
                    source_refs=[SourceRef(str(path), str(dataset_name))],
                ))

        return datasets

    def _result_candidates(self) -> Iterable[Path]:
        if not self.results_root.exists():
            return []
        out: list[Path] = []
        try:
            entries = sorted(self.results_root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        for entry in entries:
            if entry.name == SESSION_FILENAME:
                continue
            if _is_supported_result(entry):
                out.append(entry)
        return out

    @staticmethod
    def _source_descriptions(document) -> list[dict]:
        graph = getattr(document, "graph", None)
        if not isinstance(graph, dict):
            return []
        descriptions = []
        for node in (graph.get("nodes") or {}).values():
            if not isinstance(node, dict) or node.get("op") != "source":
                continue
            source = node.get("source")
            if isinstance(source, dict):
                descriptions.append(source)
        return descriptions

    def _discover_saved_reconstructions(self, remembered_final: set[str]) -> None:
        for path in self._result_candidates():
            try:
                document = read_provenance(path)
            except (ProvenanceReadError, OSError, ValueError):
                continue
            sources = self._source_descriptions(document)
            dataset = self._dataset_for_sources(sources)
            if dataset is None:
                continue
            normalized = _norm_path(path)
            dataset.saved_reconstructions.append(SavedReconstruction(
                path=str(path.resolve()),
                name=self._result_display_name(path, document),
                final=normalized in remembered_final,
            ))

    def _dataset_for_sources(self, sources: Iterable[dict]) -> SessionDataset | None:
        for source in sources:
            path = source.get("path")
            dataset_name = source.get("dataset")
            for dataset in self.datasets:
                if dataset.matches_source(path, dataset_name):
                    return dataset
        return None

    @staticmethod
    def _result_display_name(path: Path, document) -> str:
        graph = getattr(document, "graph", None)
        artifact = getattr(document, "artifact", None) or {}
        node_id = artifact.get("node")
        if not node_id and isinstance(graph, dict):
            node_id = (graph.get("output") or {}).get("node")
        if node_id and isinstance(graph, dict):
            node = (graph.get("nodes") or {}).get(node_id) or {}
            label = node.get("label")
            if label:
                return str(label)
        name = path.name
        for suffix in (".ome.zarr", ".ome.tiff", ".ome.tif", ".hdf5", ".tiff", ".zarr", ".h5", ".tif"):
            if name.lower().endswith(suffix):
                return name[: -len(suffix)]
        return path.stem

    # ------------------------------------------------------------------
    # lookup / mutation
    # ------------------------------------------------------------------

    def dataset_by_id(self, dataset_id: str | None) -> SessionDataset | None:
        return next((item for item in self.datasets if item.id == dataset_id), None)

    def dataset_for_result(self, result) -> SessionDataset | None:
        from imswitch.improcess.model.provenance import graph_of

        graph = graph_of(result)
        if not isinstance(graph, dict):
            return None
        document = type("_Doc", (), {"graph": graph})()
        return self._dataset_for_sources(self._source_descriptions(document))

    def set_final(self, dataset_id: str, result_path: str | Path) -> bool:
        dataset = self.dataset_by_id(dataset_id)
        if dataset is None:
            return False
        wanted = _norm_path(result_path)
        changed = False
        found = False
        for result in dataset.saved_reconstructions:
            is_final = _norm_path(result.path) == wanted
            found = found or is_final
            if result.final != is_final:
                result.final = is_final
                changed = True
        if found and changed:
            self.save_manifest()
        return found

    # ------------------------------------------------------------------
    # manifest
    # ------------------------------------------------------------------

    def _read_final_paths(self) -> set[str]:
        if not self.manifest_path.exists():
            return set()
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        paths: set[str] = set()
        for dataset in payload.get("datasets", []) if isinstance(payload, dict) else []:
            for result in dataset.get("saved_reconstructions", []) if isinstance(dataset, dict) else []:
                if isinstance(result, dict) and result.get("final") and result.get("path"):
                    path = Path(result["path"])
                    if not path.is_absolute():
                        path = self.results_root / path
                    paths.add(_norm_path(path))
        return paths

    def save_manifest(self) -> None:
        self.results_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": self.schema,
            "raw_root": str(self.raw_root),
            "results_root": str(self.results_root),
            "datasets": [],
        }
        for dataset in self.datasets:
            entry = asdict(dataset)
            # Keep the manifest movable with the result directory when possible.
            for result in entry["saved_reconstructions"]:
                try:
                    result["path"] = str(Path(result["path"]).resolve().relative_to(self.results_root))
                except ValueError:
                    pass
            payload["datasets"].append(entry)
        tmp = self.manifest_path.with_suffix(self.manifest_path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.manifest_path)


__all__ = [
    "ProcessingSession",
    "SavedReconstruction",
    "SessionDataset",
    "SourceRef",
    "SESSION_FILENAME",
]
