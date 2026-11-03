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
import shutil
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


_SPATIAL_DISPLAY_ALIASES = {
    "XY": "2D",
    "XYZ": "3D",
}


def _canonical_spatial_tag(value: str | None) -> str:
    """Normalize legacy Session scan labels to compact axis tags."""
    text = str(value or "").strip().upper()
    return {"2D": "XY", "3D": "XYZ"}.get(text, text)


def _spatial_display_label(value: str | None) -> str:
    """Human-facing spatial label while keeping axis tags canonical internally."""
    spatial = _canonical_spatial_tag(value)
    return _SPATIAL_DISPLAY_ALIASES.get(spatial, spatial)


def _dimension_filter_label(tag: str | None) -> str:
    """Compact filter text with familiar aliases shown only as annotations."""
    canonical = str(tag or "").strip().upper()
    timed = canonical.endswith("T")
    spatial = canonical[:-1] if timed else canonical
    alias = _SPATIAL_DISPLAY_ALIASES.get(spatial)
    return f"{canonical} ({alias})" if canonical and alias else canonical


def _is_supported_raw(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _RAW_SUFFIXES)


def _is_supported_result(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _RESULT_SUFFIXES)


def _result_role_from_graph(graph: dict | None) -> str:
    """Semantic result role recorded in provenance, with migration fallback.

    New provenance records ``result_role`` on the producing plugin node.  Old
    View-only / Time-lapse artifacts predate that field, so recognize those two
    historical inspection plugin ids explicitly when reopening old Sessions.
    """
    if not isinstance(graph, dict):
        return "derived"
    output = graph.get("output") or {}
    node = (graph.get("nodes") or {}).get(output.get("node")) or {}
    role = str(node.get("result_role") or "").strip().lower()
    if role:
        return role
    if str(node.get("plugin_id") or "") in {"view-only", "time-lapse"}:
        return "inspection"
    return "derived"


def result_role_for_result(result) -> str:
    """Return the Session semantic role of an in-memory result."""
    from imswitch.improcess.model.provenance import graph_of

    role = str(getattr(result, "_improcess_result_role", "") or "").strip().lower()
    if role:
        return role
    return _result_role_from_graph(graph_of(result))


@dataclass(frozen=True)
class SourceRef:
    path: str
    dataset: str | None = None
    relative_path: str | None = None

    @property
    def normalized_path(self) -> str:
        return _norm_path(self.path)


@dataclass
class SavedReconstruction:
    path: str
    # Compact Session-facing description (for example ``MoNaLISA - Fast``).
    name: str
    # Reconstruction-object display name to restore on the right-hand list.
    display_name: str = ""
    # Rich but lightweight provenance summary for the Session tooltip.
    tooltip: str = ""
    # Reconstruction recipe fingerprints discovered from provenance. Kept
    # metadata-only in this pass; future Auto UX may explicitly compare these
    # against the current recipe without reopening result pixels.
    recipe_ids: tuple[str, ...] = ()


@dataclass
class SessionDataset:
    id: str
    label: str
    kind: str  # "scan" | "time-lapse"
    primary_path: str
    primary_dataset: str | None
    # Stable Session-relative logical identity, separate from the legacy ``id``.
    # This is intentionally introduced before the tree/subtree UX so later
    # watcher/queue code does not need to use absolute paths as identity.
    source_key: str = ""
    source_refs: list[SourceRef] = field(default_factory=list)
    planned_timepoints: int | None = None
    # Lightweight source metadata used by the Session list.  These are derived
    # from array headers/layout metadata and never require materialising pixels.
    scan_label: str = ""
    decoded_bytes: int | None = None
    # A raw source may deliberately be removed while kept reconstructions stay
    # in the Session.  Such entries are restored from the manifest on refresh.
    raw_deleted: bool = False
    # User curation state: this acquisition is worth keeping/revisiting.
    favorite: bool = False
    # Explicit Session policy override. Manual processing remains available;
    # only automatic submission skips this logical dataset.
    auto_process_ignored: bool = False
    saved_reconstructions: list[SavedReconstruction] = field(default_factory=list)

    @property
    def status(self) -> str:
        """Compact compatibility state: reconstructed or raw only."""
        return "reconstructed" if self.saved_reconstructions else "raw"

    @property
    def spatial_display_label(self) -> str:
        """Concise human-facing spatial label (for example XY -> 2D)."""
        return _spatial_display_label(self.scan_label)

    @property
    def dimension_tag(self) -> str:
        """Canonical acquisition tag used by the Session filter.

        The persisted ``scan_label`` remains purely spatial.  A temporal ``T``
        suffix is added only when the acquisition actually contains more than
        one planned timepoint.
        """
        spatial = _canonical_spatial_tag(self.scan_label)
        try:
            timed = int(self.planned_timepoints or 0) > 1
        except (TypeError, ValueError):
            timed = False
        if timed:
            return f"{spatial}T" if spatial else "T"
        return spatial

    @property
    def dimension_filter_label(self) -> str:
        """Human-facing filter text while preserving ``dimension_tag`` as data."""
        return _dimension_filter_label(self.dimension_tag)

    def matches_source(self, path: str | None, dataset: str | None = None) -> bool:
        if not path:
            return False
        wanted_path = _norm_path(path)
        wanted_dataset = str(dataset) if dataset not in (None, "") else None
        for ref in self.source_refs:
            if ref.normalized_path != wanted_path:
                continue
            # A whole-lapse DataObj deliberately uses the synthetic dataset
            # name ``time-lapse``.  In that one case the anchor path identifies
            # the logical Session dataset.  Ordinary MultiData members keep
            # their real dataset identity and must match it as well; otherwise
            # two lapses stored in one HDF5 container can be confused.
            if self.kind == "time-lapse" and wanted_dataset == "time-lapse":
                return True
            if wanted_dataset is None or ref.dataset is None:
                return True
            if str(ref.dataset) == wanted_dataset:
                return True
        return False


class ProcessingSession:
    """Persistent catalog of raw acquisitions and saved reconstructions."""

    schema = 6

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
        """Compatibility wrapper for :meth:`reconcile_all`."""
        self.reconcile_all()

    def reconcile_all(self) -> None:
        """Reconcile the complete lightweight catalog with disk.

        This remains the correctness fallback used on Session open/manual refresh.
        Incremental watcher updates use :meth:`reconcile_path`, but both paths feed
        the same discovery and result-association logic.
        """
        payload = self._read_manifest_payload()
        remembered_favorites = self._favorite_ids_from_payload(payload)
        remembered_favorite_keys = self._favorite_source_keys_from_payload(payload)
        remembered_auto_ignored = self._auto_ignored_ids_from_payload(payload)
        remembered_auto_ignored_keys = self._auto_ignored_source_keys_from_payload(payload)
        remembered_deleted = self._deleted_datasets_from_payload(payload)
        self.datasets = self._discover_raw_datasets()
        for dataset in self.datasets:
            dataset.favorite = (
                dataset.id in remembered_favorites
                or bool(dataset.source_key and dataset.source_key in remembered_favorite_keys)
            )
            dataset.auto_process_ignored = (
                dataset.id in remembered_auto_ignored
                or bool(
                    dataset.source_key
                    and dataset.source_key in remembered_auto_ignored_keys
                )
            )
        discovered_ids = {dataset.id for dataset in self.datasets}
        self.datasets.extend(
            dataset for dataset in remembered_deleted if dataset.id not in discovered_ids
        )
        self._discover_saved_reconstructions()
        self.save_manifest()

    def _relative_source_path(self, path: str | Path) -> str:
        """Session-relative path when possible, otherwise a normalized absolute one."""
        candidate = Path(path).expanduser()
        try:
            relative = candidate.resolve().relative_to(self.raw_root)
        except (ValueError, OSError):
            return _norm_path(candidate)
        text = relative.as_posix()
        return text or "."

    def _source_ref(self, path: str | Path, dataset: str | None) -> SourceRef:
        return SourceRef(
            str(path),
            dataset,
            relative_path=self._relative_source_path(path),
        )

    def _entry_is_results(self, path: str | Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.results_root)
            return True
        except (ValueError, OSError):
            return False

    def _candidate_raw_sources_for_entry(self, entry: Path) -> list[Path]:
        """Supported raw stores represented by one immediate Session-root entry."""
        if self._entry_is_results(entry):
            return []
        if _is_supported_raw(entry):
            return [entry]
        if not entry.is_dir():
            return []
        out: list[Path] = []
        try:
            children = sorted(entry.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        for child in children:
            if self._entry_is_results(child):
                continue
            if _is_supported_raw(child):
                out.append(child)
        return out

    def _candidate_raw_sources(self) -> Iterable[Path]:
        if not self.raw_root.exists():
            return []
        out: list[Path] = []

        try:
            entries = sorted(self.raw_root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return []
        for entry in entries:
            out.extend(self._candidate_raw_sources_for_entry(entry))
        return out

    @staticmethod
    def _scan_label_from_layout(resolved) -> str:
        try:
            loops = tuple(resolved.layout.event_loops)
        except Exception:
            return ""
        kinds = {str(loop.kind) for loop in loops if int(getattr(loop, "count", 1) or 1) > 1}
        axes = {axis for axis in ("x", "y", "z") if f"scan_{axis}" in kinds}
        if axes == {"x", "y", "z"}:
            return "XYZ"
        if axes == {"x", "y"}:
            return "XY"
        if axes == {"y", "z"}:
            return "YZ"
        if axes == {"x", "z"}:
            return "XZ"
        if axes == {"z"}:
            return "Z"
        if axes == {"y"}:
            return "Y"
        if axes == {"x"}:
            return "X"
        return ""

    @classmethod
    def _source_metadata(
        cls, path: Path, dataset_name: str | None, *, timepoints: int | None = None
    ) -> tuple[str, int | None]:
        """Read compact scan semantics/decoded size without loading image pixels."""
        data_obj = DataObj(path.name, dataset_name, path=str(path))
        try:
            data_obj.checkAndOpenData()
            decoded = data_obj.decodedBytes()
            if decoded is not None and timepoints and timepoints > 1:
                decoded *= int(timepoints)
            try:
                scan_label = cls._scan_label_from_layout(data_obj.acquisition_layout)
            except Exception:
                scan_label = ""
            return scan_label, decoded
        except Exception:
            return "", None
        finally:
            try:
                data_obj.checkAndUnloadData()
            except Exception:
                pass

    def _discover_raw_datasets(
        self, candidates: Iterable[Path] | None = None
    ) -> list[SessionDataset]:
        datasets: list[SessionDataset] = []
        seen_logical: set[str] = set()
        candidates = list(self._candidate_raw_sources() if candidates is None else candidates)

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
                    relative_root = self._relative_source_path(lapse.root)
                    source_key = "|".join((
                        "lapse",
                        relative_root,
                        str(lapse.name),
                        str(lapse.anchor_detector),
                    ))
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
                        self._source_ref(item.path, item.dataset)
                        for item in channel.candidates
                    ]
                    label = str(lapse.name)
                    if lapse.anchor_detector and lapse.anchor_detector not in label:
                        label = f"{label} · {lapse.anchor_detector}"
                    scan_label, decoded_bytes = self._source_metadata(
                        Path(lapse.anchor.path),
                        lapse.anchor.dataset,
                        timepoints=int(lapse.planned_count),
                    )
                    datasets.append(SessionDataset(
                        id=_stable_id(logical),
                        label=label,
                        kind="time-lapse",
                        primary_path=str(lapse.anchor.path),
                        primary_dataset=lapse.anchor.dataset,
                        source_key=source_key,
                        source_refs=refs,
                        planned_timepoints=int(lapse.planned_count),
                        scan_label=scan_label,
                        decoded_bytes=decoded_bytes,
                    ))
                    continue

                relative_path = self._relative_source_path(path)
                source_key = f"scan|{relative_path}|{dataset_name}"
                logical = f"scan|{_norm_path(path)}|{dataset_name}"
                if logical in seen_logical:
                    continue
                seen_logical.add(logical)
                label = path.name if len(names) == 1 else f"{path.name}::{dataset_name}"
                scan_label, decoded_bytes = self._source_metadata(path, str(dataset_name))
                datasets.append(SessionDataset(
                    id=_stable_id(logical),
                    label=label,
                    kind="scan",
                    primary_path=str(path),
                    primary_dataset=str(dataset_name),
                    source_key=source_key,
                    source_refs=[self._source_ref(path, str(dataset_name))],
                    scan_label=scan_label,
                    decoded_bytes=decoded_bytes,
                ))

        return datasets

    @staticmethod
    def _path_contains(parent: Path, child: str | Path) -> bool:
        try:
            Path(child).resolve().relative_to(parent.resolve())
            return True
        except (ValueError, OSError):
            return False

    def _dataset_touches_entry(self, dataset: SessionDataset, entry: Path) -> bool:
        if self._path_contains(entry, dataset.primary_path):
            return True
        return any(self._path_contains(entry, ref.path) for ref in dataset.source_refs)

    def reconcile_path(
        self, path: str | Path, *, reconcile_saved_results: bool = True
    ) -> bool:
        """Incrementally reconcile one immediate Session-root entry.

        The watcher deals in physical root entries; this method resolves that hint
        back into logical datasets (including a whole time lapse).  If an entry is
        temporarily unreadable while being written, the existing catalog is left
        untouched and a later watcher change/manual refresh can retry safely.

        Returns ``True`` when the catalog was updated.
        """
        entry = Path(path).expanduser()
        try:
            entry.resolve().relative_to(self.raw_root)
        except (ValueError, OSError):
            return False
        if self._entry_is_results(entry):
            return False
        if not entry.exists():
            # Removal can affect a logical time lapse spanning sibling stores;
            # use the complete correctness path rather than guessing locally.
            self.reconcile_all()
            return True

        candidates = self._candidate_raw_sources_for_entry(entry)
        discovered = self._discover_raw_datasets(candidates)
        if not discovered:
            return False

        existing_by_key = {
            dataset.source_key: dataset
            for dataset in self.datasets
            if dataset.source_key
        }
        discovered_keys = {dataset.source_key for dataset in discovered if dataset.source_key}
        for dataset in discovered:
            previous = existing_by_key.get(dataset.source_key)
            if previous is not None:
                dataset.favorite = previous.favorite
                dataset.auto_process_ignored = previous.auto_process_ignored

        retained = [
            dataset
            for dataset in self.datasets
            if dataset.source_key not in discovered_keys
            and not self._dataset_touches_entry(dataset, entry)
        ]
        self.datasets = retained + discovered
        self.datasets.sort(key=lambda item: (item.label.lower(), item.id))

        if reconcile_saved_results:
            self.reconcile_results(save_manifest=True)
        else:
            # Hot watcher path: a newly discovered raw dataset normally has no
            # pre-existing result to attach. Avoid rereading every historical
            # result merely because one new acquisition appeared. Explicit
            # refresh/open still performs the complete correctness reconciliation.
            self.save_manifest()
        return True

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

    def _discover_saved_reconstructions(self) -> None:
        for path in self._result_candidates():
            try:
                document = read_provenance(path)
            except (ProvenanceReadError, OSError, ValueError):
                continue
            if _result_role_from_graph(getattr(document, "graph", None)) == "inspection":
                continue
            sources = self._source_descriptions(document)
            dataset = self._dataset_for_sources(sources)
            if dataset is None:
                continue
            session_name, display_name, tooltip = self._result_presentation(path, document)
            from imswitch.improcess.model.provenance import recipe_ids_from_graph

            dataset.saved_reconstructions.append(SavedReconstruction(
                path=str(path.resolve()),
                name=session_name,
                display_name=display_name,
                tooltip=tooltip,
                recipe_ids=recipe_ids_from_graph(getattr(document, "graph", None)),
            ))

    def reconcile_results(self, *, save_manifest: bool = True) -> None:
        """Rebuild only saved-result associations, leaving raw discovery alone."""
        for dataset in self.datasets:
            dataset.saved_reconstructions = []
        self._discover_saved_reconstructions()
        if save_manifest:
            self.save_manifest()

    def register_saved_result(
        self,
        dataset_id: str,
        result_path: str | Path,
        *,
        result=None,
        display_name: str = "",
        save_manifest: bool = True,
    ) -> SavedReconstruction | None:
        """Register one known saved result without rescanning ``results_root``.

        Session-owned saves already know both the logical dataset and the result
        that was written. Re-reading every previous result's provenance after
        each save made a long Auto run progressively slower. Full
        :meth:`reconcile_results` remains the explicit/open-session correctness
        path for externally-created files.
        """
        dataset = self.dataset_by_id(str(dataset_id))
        if dataset is None:
            return None
        path = Path(result_path).resolve()
        wanted = _norm_path(path)
        existing = next(
            (item for item in dataset.saved_reconstructions if _norm_path(item.path) == wanted),
            None,
        )
        if existing is not None:
            return existing

        from imswitch.improcess.model.provenance import graph_of

        graph = graph_of(result) if result is not None else None
        if isinstance(graph, dict):
            from imswitch.improcess.model.provenance import recipe_ids_from_graph
            from imswitch.improcess.model.session_result_presentation import (
                filename_display_name,
                reconstruction_presentation,
            )

            fallback = str(display_name or getattr(result, "name", "") or filename_display_name(path))
            session_name, object_name, tooltip = reconstruction_presentation(
                graph, fallback_name=fallback
            )
            recipe_ids = recipe_ids_from_graph(graph)
        else:
            try:
                document = read_provenance(path)
            except (ProvenanceReadError, OSError, ValueError):
                return None
            if _result_role_from_graph(getattr(document, "graph", None)) == "inspection":
                return None
            session_name, object_name, tooltip = self._result_presentation(path, document)
            from imswitch.improcess.model.provenance import recipe_ids_from_graph

            recipe_ids = recipe_ids_from_graph(getattr(document, "graph", None))

        saved = SavedReconstruction(
            path=str(path),
            name=session_name,
            display_name=object_name,
            tooltip=tooltip,
            recipe_ids=recipe_ids,
        )
        dataset.saved_reconstructions.append(saved)
        if save_manifest:
            self.save_manifest()
        return saved

    def _dataset_for_sources(self, sources: Iterable[dict]) -> SessionDataset | None:
        """Resolve provenance sources to one logical Session dataset.

        A consolidated reconstruction is Session-manageable only when *every*
        provenance source resolves unambiguously to the same Session dataset.
        This is what makes a logical time lapse safe: all of its physical
        timepoint sources collapse to one ``SessionDataset``.  An arbitrary
        MultiData consolidation spanning unrelated raw datasets deliberately
        returns ``None`` instead of attaching the result to whichever source
        happens to be visited first.
        """
        owner: SessionDataset | None = None
        saw_source = False
        for source in sources:
            if not isinstance(source, dict):
                return None
            saw_source = True
            path = source.get("path")
            dataset_name = source.get("dataset")
            matches = [
                dataset
                for dataset in self.datasets
                if dataset.matches_source(path, dataset_name)
            ]
            # Missing sources and ambiguous sources both fail closed.
            if len(matches) != 1:
                return None
            match = matches[0]
            if owner is None:
                owner = match
            elif owner.id != match.id:
                return None
        return owner if saw_source else None

    @classmethod
    def reconstruction_presentation(
        cls, graph: dict | None, *, artifact: dict | None = None, fallback_name: str = ""
    ) -> tuple[str, str, str]:
        """Compatibility wrapper; presentation lives outside the Session catalog."""
        from imswitch.improcess.model.session_result_presentation import (
            reconstruction_presentation,
        )

        return reconstruction_presentation(
            graph, artifact=artifact, fallback_name=fallback_name
        )

    @staticmethod
    def _result_presentation(path: Path, document) -> tuple[str, str, str]:
        from imswitch.improcess.model.session_result_presentation import result_presentation

        return result_presentation(path, document)

    # ------------------------------------------------------------------
    # lookup / mutation
    # ------------------------------------------------------------------

    def dataset_by_id(self, dataset_id: str | None) -> SessionDataset | None:
        return next((item for item in self.datasets if item.id == dataset_id), None)

    def dataset_by_source_key(self, source_key: str | None) -> SessionDataset | None:
        if not source_key:
            return None
        return next((item for item in self.datasets if item.source_key == source_key), None)

    def dataset_for_result(self, result) -> SessionDataset | None:
        """Return the logical Session dataset that owns ``result``.

        Queue-driven processing already knows ownership before reconstruction
        starts, so prefer that explicit transient claim. Provenance remains the
        durable fallback used for restored/external results after reopening a
        Session. Keeping these two paths separate avoids reverse-engineering a
        runtime relationship that the queue already knows exactly.
        """
        explicit_id = getattr(result, "_session_dataset_id", None)
        if explicit_id not in (None, ""):
            explicit_origin = getattr(result, "_session_origin", None)
            if explicit_origin not in (None, ""):
                try:
                    own_origin = str(self.manifest_path.resolve())
                except Exception:
                    own_origin = str(self.manifest_path)
                if str(explicit_origin) != own_origin:
                    return None
            dataset = self.dataset_by_id(str(explicit_id))
            if dataset is not None:
                return dataset

        from imswitch.improcess.model.provenance import graph_of

        graph = graph_of(result)
        if not isinstance(graph, dict):
            return None
        document = type("_Doc", (), {"graph": graph})()
        return self._dataset_for_sources(self._source_descriptions(document))

    def trash_reconstruction(
        self, dataset_id: str, result_path: str | Path
    ) -> Path | None:
        """Move one saved result into Session trash and remove it from catalog.

        Trash is deliberately inside ``results_root`` but hidden from normal
        result discovery.  This makes the curation action noticeable/reversible
        without permanently deleting data while the feature is still evolving.
        """
        dataset = self.dataset_by_id(dataset_id)
        if dataset is None:
            return None
        wanted = _norm_path(result_path)
        saved = next(
            (item for item in dataset.saved_reconstructions if _norm_path(item.path) == wanted),
            None,
        )
        if saved is None:
            return None

        source = Path(saved.path)
        trash_dir = self.results_root / ".trash" / dataset.id
        trash_dir.mkdir(parents=True, exist_ok=True)
        target = trash_dir / source.name
        index = 1
        while target.exists():
            target = trash_dir / f"{source.stem}_{index}{source.suffix}"
            index += 1
        if source.exists():
            shutil.move(str(source), str(target))
        dataset.saved_reconstructions = [
            item for item in dataset.saved_reconstructions if item is not saved
        ]
        self.save_manifest()
        return target

    # ------------------------------------------------------------------
    # manifest
    # ------------------------------------------------------------------

    def _read_manifest_payload(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _favorite_ids_from_payload(payload: dict) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        return {
            str(entry.get("id"))
            for entry in payload.get("datasets", [])
            if isinstance(entry, dict)
            and entry.get("id")
            and bool(entry.get("favorite", False))
        }

    @staticmethod
    def _favorite_source_keys_from_payload(payload: dict) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        return {
            str(entry.get("source_key"))
            for entry in payload.get("datasets", [])
            if isinstance(entry, dict)
            and entry.get("source_key")
            and bool(entry.get("favorite", False))
        }

    @staticmethod
    def _auto_ignored_ids_from_payload(payload: dict) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        return {
            str(entry.get("id"))
            for entry in payload.get("datasets", [])
            if isinstance(entry, dict)
            and entry.get("id")
            and bool(entry.get("auto_process_ignored", False))
        }

    @staticmethod
    def _auto_ignored_source_keys_from_payload(payload: dict) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        return {
            str(entry.get("source_key"))
            for entry in payload.get("datasets", [])
            if isinstance(entry, dict)
            and entry.get("source_key")
            and bool(entry.get("auto_process_ignored", False))
        }

    def _deleted_datasets_from_payload(self, payload: dict) -> list[SessionDataset]:
        out: list[SessionDataset] = []
        for entry in payload.get("datasets", []) if isinstance(payload, dict) else []:
            if not isinstance(entry, dict) or not entry.get("raw_deleted"):
                continue
            try:
                refs = [
                    SourceRef(
                        str(ref.get("path", "")),
                        ref.get("dataset"),
                        relative_path=(
                            str(ref.get("relative_path"))
                            if ref.get("relative_path") not in (None, "")
                            else self._relative_source_path(str(ref.get("path", "")))
                        ),
                    )
                    for ref in entry.get("source_refs", [])
                    if isinstance(ref, dict) and ref.get("path")
                ]
                source_key = str(entry.get("source_key") or "")
                if not source_key and refs:
                    if str(entry.get("kind") or "scan") == "scan":
                        source_key = (
                            f"scan|{refs[0].relative_path or self._relative_source_path(refs[0].path)}|"
                            f"{entry.get('primary_dataset') or refs[0].dataset or ''}"
                        )
                out.append(SessionDataset(
                    id=str(entry["id"]),
                    label=str(entry.get("label") or "deleted raw data"),
                    kind=str(entry.get("kind") or "scan"),
                    primary_path=str(entry.get("primary_path") or (refs[0].path if refs else "")),
                    primary_dataset=entry.get("primary_dataset"),
                    source_key=source_key,
                    source_refs=refs,
                    planned_timepoints=entry.get("planned_timepoints"),
                    scan_label=_canonical_spatial_tag(entry.get("scan_label")),
                    decoded_bytes=entry.get("decoded_bytes"),
                    raw_deleted=True,
                    favorite=bool(entry.get("favorite", False)),
                    auto_process_ignored=bool(entry.get("auto_process_ignored", False)),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def raw_cleanup_plan(
        self, dataset_ids: Iterable[str]
    ) -> tuple[list[SessionDataset], list[str], list[SessionDataset]]:
        """Plan raw deletion without ever splitting a shared physical container.

        A logical dataset is deletable only when every physical path it uses is
        referenced exclusively by datasets in the requested deletion set. This
        makes favorites safe when several acquisitions share one HDF5/Zarr path.
        """
        wanted = {str(item) for item in dataset_ids}
        candidates = [dataset for dataset in self.datasets if dataset.id in wanted]
        owners: dict[str, set[str]] = {}
        original_paths: dict[str, str] = {}
        for dataset in self.datasets:
            if dataset.raw_deleted:
                continue
            for ref in dataset.source_refs:
                if not ref.path:
                    continue
                normalized = _norm_path(ref.path)
                owners.setdefault(normalized, set()).add(dataset.id)
                original_paths.setdefault(normalized, str(ref.path))

        deletable: list[SessionDataset] = []
        skipped: list[SessionDataset] = []
        raw_paths: set[str] = set()
        for dataset in candidates:
            if dataset.raw_deleted:
                deletable.append(dataset)
                continue
            paths = {_norm_path(ref.path) for ref in dataset.source_refs if ref.path}
            safe = bool(paths) and all(owners.get(path, set()) <= wanted for path in paths)
            if not safe:
                skipped.append(dataset)
                continue
            deletable.append(dataset)
            raw_paths.update(paths)
        return (
            deletable,
            [original_paths[path] for path in sorted(raw_paths)],
            skipped,
        )

    def set_favorite(self, dataset_ids: Iterable[str], favorite: bool) -> None:
        wanted = {str(item) for item in dataset_ids}
        changed = False
        for dataset in self.datasets:
            if dataset.id in wanted and dataset.favorite != bool(favorite):
                dataset.favorite = bool(favorite)
                changed = True
        if changed:
            self.save_manifest()

    def set_auto_process_ignored(
        self, dataset_ids: Iterable[str], ignored: bool
    ) -> None:
        wanted = {str(item) for item in dataset_ids}
        changed = False
        for dataset in self.datasets:
            if (
                dataset.id in wanted
                and dataset.auto_process_ignored != bool(ignored)
            ):
                dataset.auto_process_ignored = bool(ignored)
                changed = True
        if changed:
            self.save_manifest()

    def favorite_datasets(self) -> list[SessionDataset]:
        return [dataset for dataset in self.datasets if dataset.favorite]

    def nonfavorite_datasets(self) -> list[SessionDataset]:
        return [dataset for dataset in self.datasets if not dataset.favorite]

    def mark_raw_deleted(self, dataset_ids: Iterable[str]) -> None:
        wanted = {str(item) for item in dataset_ids}
        for dataset in self.datasets:
            if dataset.id in wanted:
                dataset.raw_deleted = True
        self.save_manifest()

    def forget_datasets(self, dataset_ids: Iterable[str]) -> None:
        wanted = {str(item) for item in dataset_ids}
        self.datasets = [dataset for dataset in self.datasets if dataset.id not in wanted]
        self.save_manifest()

    @property
    def trash_root(self) -> Path:
        return self.results_root / ".trash"

    def empty_trash(self) -> None:
        if self.trash_root.exists():
            shutil.rmtree(self.trash_root)

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
    "result_role_for_result",
]
