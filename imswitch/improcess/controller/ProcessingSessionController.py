from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from qtpy import QtCore, QtGui, QtWidgets

from imswitch.improcess.controller.basecontrollers import ImProcessWidgetController
from imswitch.improcess.model.DataObj import DataObj
from imswitch.improcess.model.processing_session import ProcessingSession
from imswitch.improcess.model.provenance import graph_of, output_node
from imswitch.improcess.model.saved_result import load_saved_result, read_saved_result_metadata


_FORMAT_SUFFIX = {
    "tiff": ".ome.tif",
    "hdf5": ".h5",
    "zarr": ".ome.zarr",
}


def _clean_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "result")).strip("_.")
    return text or "result"


def _same_path(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except Exception:
        return str(a) == str(b)


class ProcessingSessionController(ImProcessWidgetController):
    """Prototype persistent Session, independent of Directory watcher."""

    def __init__(
        self,
        *args,
        mainController=None,
        fileIOController=None,
        reconstructionController=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._main = mainController
        self._fileIO = fileIOController
        self._reconstruction = reconstructionController
        self._session: ProcessingSession | None = None
        self._selectedDatasetId: str | None = None
        self._selectedReconKey: str | None = None
        self._currentDatasetId: str | None = None
        self._loadingDatasetId: str | None = None
        self._followActiveReconstruction = True
        self._syncingFromActiveReconstruction = False
        self._autoSave = True
        self._saveFormat = "tiff"
        self._addQueue: list[tuple[object, object]] | None = None
        self._addTotal = 0
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        # sigResultsChanged also fires for selection-only changes. Track the
        # actual loaded set so Session only rebuilds its catalog when membership
        # changes; active-result navigation is handled by sigCurrentResultChanged.
        self._loadedResultSignature = self._loaded_result_signature()

        self._widget.sigStartSessionRequested.connect(self.startSession)
        self._widget.sigSettingsRequested.connect(self.editSessionSettings)
        self._widget.sigRefresh.connect(self.refresh)
        self._widget.sigDatasetSelectionChanged.connect(self.datasetSelected)
        self._widget.sigSavedSelectionChanged.connect(self.reconstructionSelected)
        self._widget.sigLoadRawRequested.connect(self.loadSelectedRaw)
        self._widget.sigLoadSelectedAsMultiDataRequested.connect(self.loadSelectedAsMultiData)
        self._widget.sigRevealRawRequested.connect(self.revealSelectedRaw)
        self._widget.sigCopyRawPathRequested.connect(self.copySelectedRawPath)
        self._widget.sigDeleteRawRequested.connect(self.deleteSelectedRaw)
        self._widget.sigFavoriteDatasetRequested.connect(self.setDatasetFavorite)
        self._widget.sigSetSelectedFavoriteRequested.connect(self.setSelectedDatasetsFavorite)
        self._widget.sigReconstructionWorkspaceToggleRequested.connect(
            self.toggleReconstructionWorkspace
        )
        self._widget.sigDiscardReconstructionRequested.connect(self.discardReconstruction)
        self._widget.sigDiscardSelectedRequested.connect(self.discardSelected)
        self._widget.sigReuseCalibrationRequested.connect(self.reuseReconstructionCalibration)
        self._widget.sigAddSelectedReconstructionsRequested.connect(
            self.addSelectedDatasetReconstructions
        )
        self._widget.sigCloseSelectedDatasetReconstructionsRequested.connect(
            self.closeSelectedDatasetReconstructions
        )
        self._widget.sigFollowActiveReconstructionChanged.connect(
            self.setFollowActiveReconstruction
        )
        self._widget.sigCancelAddingRequested.connect(self.cancelAddingReconstructions)
        self._widget.sigCloseSessionReconstructionsRequested.connect(self.closeSessionReconstructions)
        self._widget.sigCloseNonFavoriteReconstructionsRequested.connect(
            self.closeNonFavoriteReconstructions
        )
        self._widget.sigCleanupNonFavoritesRequested.connect(self.cleanupNonFavorites)
        self._widget.sigCloseSessionRequested.connect(self.closeSession)
        self._widget.sigEmptyTrashRequested.connect(self.emptySessionTrash)
        self._widget.sigRevealRawFolderRequested.connect(self.revealRawFolder)
        self._widget.sigRevealResultsFolderRequested.connect(self.revealResultsFolder)

        self._commChannel.sigResultProduced.connect(self._resultProduced)
        self._commChannel.sigResultsChanged.connect(self._loadedResultsChanged)
        if hasattr(self._commChannel, "sigResultSaved"):
            self._commChannel.sigResultSaved.connect(self._resultSaved)
        self._commChannel.sigCurrentDataChanged.connect(self._currentDataChanged)
        self._commChannel.sigCurrentResultChanged.connect(self._currentResultChanged)

    @property
    def session(self):
        return self._session

    @staticmethod
    def _session_origin_id(session) -> str | None:
        if session is None:
            return None
        try:
            return str(session.manifest_path.resolve())
        except Exception:
            return str(session.manifest_path)

    def _tag_session_origin(self, result, session=None) -> None:
        if result is None:
            return
        origin = self._session_origin_id(session or self._session)
        if origin:
            result._session_origin = origin

    def _results_associated_with_session(self, session=None) -> list[object]:
        session = session or self._session
        if session is None:
            return []
        matches = []
        seen = set()
        for _name, result in self._commChannel.getAllResults():
            if result is None or id(result) in seen or not self._is_session_reconstruction(result):
                continue
            if session.dataset_for_result(result) is None:
                continue
            seen.add(id(result))
            matches.append(result)
        return matches

    def _reset_add_queue(self) -> None:
        self._addQueue = None
        self._addTotal = 0
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        self._widget.setAddingProgress(False)

    def _deactivate_session(self, *, status: str | None = None) -> None:
        self._reset_add_queue()
        self._session = None
        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._currentDatasetId = None
        self._loadingDatasetId = None
        self._loadedResultSignature = self._loaded_result_signature()
        self._widget.setSessionActive(False)
        self._widget.setDatasets([])
        self._widget.setSelectedDataset("", "")
        self._widget.setSavedReconstructions([])
        if status:
            self._widget.setStatus(status)

    def _request_configuration(self) -> dict | None:
        session = self._session
        raw = str(session.raw_root) if session is not None else ""
        results = str(session.results_root) if session is not None else ""
        return self._widget.requestSessionConfiguration(
            raw_root=raw,
            results_root=results,
            auto_save=self._autoSave,
            save_format=self._saveFormat,
            active_raw_root=raw,
        )

    def startSession(self) -> None:
        config = self._request_configuration()
        if config is None:
            return
        self._apply_configuration(config)

    def editSessionSettings(self) -> None:
        config = self._request_configuration()
        if config is None:
            return
        self._apply_configuration(config)

    def _apply_configuration(self, config: dict) -> None:
        raw_text = str(config.get("raw_root") or "").strip()
        if not raw_text:
            return
        raw = Path(raw_text).expanduser()
        results_text = str(config.get("results_root") or "").strip()
        results = Path(results_text).expanduser() if results_text else raw / "results"
        self._autoSave = bool(config.get("auto_save", True))
        fmt = str(config.get("save_format") or "tiff")
        self._saveFormat = fmt if fmt in _FORMAT_SUFFIX else "tiff"

        restore_mode = str(config.get("restore_mode") or "none")
        if (
            self._session is not None
            and _same_path(self._session.raw_root, raw)
            and _same_path(self._session.results_root, results)
        ):
            self._widget.setStatus("Session settings updated")
            return
        if not self.openSession(raw, results):
            return
        if restore_mode == "all":
            self._start_add_reconstructions(list(self._session.datasets))
        elif restore_mode == "favorites":
            self._start_add_reconstructions(
                [dataset for dataset in self._session.datasets if dataset.favorite]
            )

    def _prepare_session(self, raw_root, results_root) -> ProcessingSession | None:
        raw = Path(raw_root).expanduser()
        if not raw.is_dir():
            self._widget.setStatus(f"Raw-data folder does not exist: {raw}")
            return None
        try:
            session = ProcessingSession(raw, results_root)
            session.refresh()
        except Exception as exc:
            self._logger.exception("Could not open processing session")
            self._widget.setStatus(f"Could not open session: {exc}")
            return None
        return session

    def _activate_session(self, session: ProcessingSession) -> None:
        self._session = session
        self._loadedResultSignature = self._loaded_result_signature()
        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._widget.setSessionActive(True, session.raw_root.name)
        self._widget.setStatus(
            f"Session active · {len(session.datasets)} raw dataset(s) · {session.results_root}"
        )
        self._sync_current_dataset(getattr(self._main, "_currentDataObj", None))
        self._render()
        if self._followActiveReconstruction and self._reconstruction is not None:
            self._currentResultChanged(self._reconstruction.getActiveResult())

    def openSession(self, raw_root, results_root) -> bool:
        # Build/refresh the candidate before touching the active Session. A bad
        # target path therefore cannot close a perfectly usable current Session.
        candidate = self._prepare_session(raw_root, results_root)
        if candidate is None:
            return False

        current = self._session
        changing = bool(
            current is not None
            and (
                not _same_path(current.raw_root, candidate.raw_root)
                or not _same_path(current.results_root, candidate.results_root)
            )
        )
        if changing and not self.closeSession(for_switch=True):
            return False

        self._activate_session(candidate)
        return True

    def closeSession(self, *, for_switch: bool = False) -> bool:
        session = self._session
        if session is None:
            return True

        # Explicitly finishing a Session is the natural point to offer curation.
        # Switching Sessions stays lightweight and never suggests destructive cleanup.
        if not for_switch:
            favorite_count = sum(1 for dataset in session.datasets if dataset.favorite)
            nonfavorite_count = len(session.datasets) - favorite_count
            finish = self._widget.requestSessionFinishChoice(
                favorite_count=favorite_count, nonfavorite_count=nonfavorite_count
            )
            if finish == "cancel":
                return False
            if finish == "reconstructions":
                if not self._cleanup_nonfavorite_datasets(include_raw=False, ask_raw=False):
                    return False
            elif finish == "raw":
                if not self._cleanup_nonfavorite_datasets(include_raw=True, ask_raw=True):
                    return False

        associated = self._results_associated_with_session(session)
        choice = "keep"
        if associated:
            choice = self._widget.requestSessionCloseChoice(
                len(associated), switching=bool(for_switch)
            )
            if choice == "cancel":
                return False
            if choice == "close":
                # Session "Close" is non-destructive just like the per-result
                # Close action. Persist any reconstruction that only exists in
                # memory before removing its right-panel object.
                for result in associated:
                    if getattr(result, "_session_saved_path", None):
                        continue
                    dataset = session.dataset_for_result(result)
                    if dataset is None:
                        continue
                    try:
                        receipt = self._save_result(
                            result, dataset, str(getattr(result, "name", "reconstruction"))
                        )
                    except Exception as exc:
                        self._logger.exception(
                            "Could not preserve reconstruction before closing Session"
                        )
                        self._widget.setStatus(
                            f"Could not close Session; preserving a reconstruction failed: {exc}"
                        )
                        return False
                    result._session_saved_path = str(receipt.primary)
                    self._tag_session_origin(result, session)
                session.refresh()
                for result in list(associated):
                    self._remove_loaded_result(result)

        name = session.raw_root.name
        self._deactivate_session(
            status=None if for_switch else f"Session closed · {name}"
        )
        return True

    def refresh(self) -> None:
        if self._session is None:
            self.startSession()
            return
        try:
            self._session.refresh()
        except Exception as exc:
            self._logger.exception("Could not refresh processing session")
            self._widget.setStatus(f"Refresh failed: {exc}")
            return
        self._render()

    # ------------------------------------------------------------------ UI

    def _loaded_by_saved_path(self) -> dict[str, object]:
        loaded = {}
        for _name, result in self._commChannel.getAllResults():
            path = getattr(result, "_session_saved_path", None)
            if path:
                try:
                    loaded[str(Path(path).resolve())] = result
                except Exception:
                    loaded[str(path)] = result
        return loaded

    @staticmethod
    def _is_session_reconstruction(result) -> bool:
        """Whether a result is a reconstruction artifact Session may own.

        A normal reconstruction is a ``reconstruct`` node.  Reconstructing
        several MultiData members with consolidation produces a final
        ``consolidate`` node whose ancestors are those reconstruction nodes;
        it is also a reconstruction artifact for Session purposes.  Generic
        downstream processing nodes remain excluded.
        """
        node = output_node(result)
        return isinstance(node, dict) and node.get("op") in {"reconstruct", "consolidate"}

    def _loaded_reconstructions_for_dataset(self, dataset) -> list[tuple[str, object]]:
        """Session reconstruction objects currently present in the right-hand list."""
        if self._session is None or dataset is None:
            return []
        matches = []
        for display_name, result in self._commChannel.getAllResults():
            if not self._is_session_reconstruction(result):
                continue
            owner = self._session.dataset_for_result(result)
            if owner is not None and owner.id == dataset.id:
                matches.append((str(display_name or getattr(result, "name", "reconstruction")), result))
        return matches

    @staticmethod
    def _loaded_key(result) -> str:
        uid = getattr(result, "result_uid", None)
        return f"loaded:{uid or id(result)}"

    def _reconstruction_entries(self, dataset):
        """Session rows for one raw dataset.

        Saved and currently loaded reconstruction objects are merged here, so
        Session immediately reflects a reconstruction even when auto-save is
        disabled.  Downstream processor results are intentionally excluded.
        """
        loaded_by_saved = self._loaded_by_saved_path()
        loaded_for_dataset = self._loaded_reconstructions_for_dataset(dataset)
        loaded_ids = {id(result) for _name, result in loaded_for_dataset}
        entries = []
        represented_loaded: set[int] = set()

        for saved in dataset.saved_reconstructions:
            resolved = str(Path(saved.path).resolve())
            result = loaded_by_saved.get(resolved)
            if result is not None and id(result) in loaded_ids:
                represented_loaded.add(id(result))
            entries.append({
                "key": f"saved:{resolved}",
                "name": saved.name,
                "tooltip": saved.tooltip or saved.name,
                "label": saved.name,
                "saved": saved,
                "loaded": result,
            })

        for display_name, result in loaded_for_dataset:
            if id(result) in represented_loaded:
                continue
            graph = graph_of(result)
            session_name, _object_name, tooltip = ProcessingSession.reconstruction_presentation(
                graph, fallback_name=str(display_name or getattr(result, "name", "reconstruction"))
            )
            entries.append({
                "key": self._loaded_key(result),
                "name": session_name,
                "tooltip": tooltip or session_name,
                "label": session_name,
                "saved": None,
                "loaded": result,
            })
        return entries

    @staticmethod
    def _dataset_timestamp(dataset) -> float | None:
        """Best lightweight acquisition timestamp for Session ordering.

        For multi-file time lapses use the newest member so newly arriving
        timepoints keep the logical acquisition near the top in Latest mode.
        No pixel data is opened.
        """
        values = []
        for ref in getattr(dataset, "source_refs", ()) or ():
            try:
                values.append(os.path.getmtime(ref.path))
            except OSError:
                continue
        if not values:
            try:
                values.append(os.path.getmtime(dataset.primary_path))
            except OSError:
                pass
        return max(values) if values else None

    @staticmethod
    def _dataset_search_text(dataset) -> str:
        parts = [
            str(getattr(dataset, "label", "") or ""),
            str(getattr(dataset, "primary_path", "") or ""),
            str(getattr(dataset, "primary_dataset", "") or ""),
        ]
        for ref in getattr(dataset, "source_refs", ()) or ():
            parts.append(str(getattr(ref, "path", "") or ""))
            parts.append(str(getattr(ref, "dataset", "") or ""))
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _format_bytes(value) -> str:
        try:
            value = int(value)
        except (TypeError, ValueError):
            return ""
        units = ("B", "KB", "MB", "GB", "TB")
        amount = float(max(0, value))
        for unit in units:
            if amount < 1024.0 or unit == units[-1]:
                if unit == "B":
                    return f"{int(amount)} {unit}"
                return f"{amount:.1f} {unit}" if amount < 10 else f"{amount:.0f} {unit}"
            amount /= 1024.0
        return ""

    @classmethod
    def _dataset_info(cls, dataset) -> tuple[str, str]:
        bits = []
        tooltip = []
        dimension_tag = str(getattr(dataset, "dimension_tag", "") or "")
        spatial_label = str(getattr(dataset, "spatial_display_label", "") or "")
        if spatial_label:
            bits.append(spatial_label)
        if dimension_tag:
            tooltip.append(f"Dimensions: {dimension_tag}")
        if dataset.kind == "time-lapse" and dataset.planned_timepoints:
            bits.append(f"T={dataset.planned_timepoints}")
            tooltip.append(f"Timepoints: {dataset.planned_timepoints}")
        decoded = cls._format_bytes(getattr(dataset, "decoded_bytes", None))
        if decoded:
            bits.append(decoded)
            tooltip.append(f"Estimated decoded size: {decoded}")
        if getattr(dataset, "raw_deleted", False):
            tooltip.append("Raw acquisition has been removed; saved reconstructions are retained")
        return " · ".join(bits), "\n".join(tooltip)

    def _sync_current_dataset(self, data_obj) -> None:
        self._currentDatasetId = None
        if self._session is None or data_obj is None:
            return
        path = getattr(data_obj, "dataPath", None)
        dataset_name = getattr(data_obj, "datasetName", None)
        for dataset in self._session.datasets:
            if dataset.matches_source(path, dataset_name):
                self._currentDatasetId = dataset.id
                return

    def _currentDataChanged(self, data_obj=None) -> None:
        self._sync_current_dataset(data_obj)
        if self._session is not None:
            self._render()

    def setFollowActiveReconstruction(self, enabled: bool) -> None:
        self._followActiveReconstruction = bool(enabled)
        self._widget.setFollowActiveReconstruction(self._followActiveReconstruction)
        if not self._followActiveReconstruction or self._session is None:
            return
        current = self._reconstruction.getActiveResult() if self._reconstruction is not None else None
        self._currentResultChanged(current)

    def _currentResultChanged(self, result) -> None:
        """Follow the active right-panel result back to its Session source."""
        if not self._followActiveReconstruction or self._session is None or result is None:
            return
        dataset = self._session.dataset_for_result(result)
        if dataset is None:
            return

        # Keep the exact reconstruction identity as well as the raw-dataset link.
        # This makes an already-expanded Reconstructions section follow the active
        # object on the right, including the ambiguous multi-reconstruction case.
        entry = next(
            (item for item in self._reconstruction_entries(dataset) if item["loaded"] is result),
            None,
        )
        selected_key = str(entry["key"]) if entry is not None else None

        # Deliberately replace Session selection, matching an ordinary user click.
        # Guard the resulting currentItemChanged signal so right -> left following
        # does not immediately trigger the left -> right navigation path again.
        self._syncingFromActiveReconstruction = True
        try:
            self._widget.selectDataset(dataset.id)
        finally:
            self._syncingFromActiveReconstruction = False

        # A filter/search may hide the source row. Keep controller context aligned
        # without changing the user's filter unexpectedly.  In either case restore
        # the exact reconstruction selection after datasetSelected() has run.
        self._selectedDatasetId = dataset.id
        self._selectedReconKey = selected_key
        # If the active result changes between two reconstructions of the same
        # raw dataset, the raw row itself does not change.  Clear the previous
        # reconstruction selection so the exact active reconstruction becomes the sole
        # selected row after rerendering.
        self._widget.clearReconstructionSelection()
        self._renderSelected()

    def _render(self) -> None:
        session = self._session
        if session is None:
            self._widget.setDatasets([])
            self._renderSelected()
            return
        entries = []
        for dataset in session.datasets:
            recon_entries = self._reconstruction_entries(dataset)
            total_count = len(recon_entries)
            loaded_count = sum(1 for item in recon_entries if item["loaded"] is not None)
            if self._loadingDatasetId == dataset.id:
                status = "Loading…"
                state = "loading"
            elif total_count == 0:
                status = "— No recon"
                state = "raw"
            else:
                status = f"✓ Recon ({total_count})"
                state = "reconstructed"
            if getattr(dataset, "raw_deleted", False):
                status += " · data deleted"
            info, info_tooltip = self._dataset_info(dataset)
            search = self._dataset_search_text(dataset)
            tooltip = search
            if info_tooltip:
                tooltip += f"\n{info_tooltip}"
            entries.append({
                "id": dataset.id,
                "label": dataset.label,
                "info": info,
                "info_tooltip": info_tooltip,
                "dimension": str(getattr(dataset, "dimension_tag", "") or ""),
                "dimension_label": str(
                    getattr(dataset, "dimension_filter_label", "") or ""
                ),
                "status": status,
                "state": state,
                "timestamp": self._dataset_timestamp(dataset),
                "search": search,
                "tooltip": tooltip,
                "current": dataset.id == self._currentDatasetId,
                "raw_deleted": bool(getattr(dataset, "raw_deleted", False)),
                "favorite": bool(getattr(dataset, "favorite", False)),
                "saved_count": len(dataset.saved_reconstructions),
                "panel_count": loaded_count,
                "panel_link": bool(loaded_count),
            })
        self._widget.setDatasets(entries)
        if self._selectedDatasetId and session.dataset_by_id(self._selectedDatasetId) is None:
            self._selectedDatasetId = None
        self._renderSelected()

    def _renderSelected(self) -> None:
        session = self._session
        dataset = session.dataset_by_id(self._selectedDatasetId) if session else None
        if dataset is None:
            self._widget.setSelectedDataset("", "")
            self._widget.setSavedReconstructions([])
            return

        entries = self._reconstruction_entries(dataset)
        loaded_count = sum(1 for item in entries if item["loaded"] is not None)
        saved_count = len(dataset.saved_reconstructions)
        total_count = len(entries)
        summary = f"{total_count} recon · {loaded_count} in panel · {saved_count} saved"
        self._widget.setSelectedDataset(dataset.label, summary)

        selected = self._selectedReconKey
        if selected and not any(item["key"] == selected for item in entries):
            selected = None
            self._selectedReconKey = None
        self._widget.setSavedReconstructions(
            [
                {
                    "key": item["key"],
                    "name": item.get("name", item["label"]),
                    "loaded": item["loaded"] is not None,
                    "saved": item["saved"] is not None,
                    "tooltip": item.get("tooltip", item.get("name", "")),
                }
                for item in entries
            ],
            selected,
        )

    def datasetSelected(self, dataset_id) -> None:
        self._selectedDatasetId = str(dataset_id) if dataset_id else None
        self._selectedReconKey = None

        if (
            self._syncingFromActiveReconstruction
            or self._session is None
            or self._reconstruction is None
            or not self._selectedDatasetId
        ):
            self._renderSelected()
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            self._renderSelected()
            return

        loaded = [result for _name, result in self._loaded_reconstructions_for_dataset(dataset)]
        if len(loaded) == 1:
            # The common one-reconstruction case is unambiguous: selecting the
            # raw Session row also selects its reconstruction object on the right.
            entry = next(
                (
                    item
                    for item in self._reconstruction_entries(dataset)
                    if item["loaded"] is loaded[0]
                ),
                None,
            )
            self._selectedReconKey = str(entry["key"]) if entry is not None else None

        self._renderSelected()

        if len(loaded) == 1:
            self._reconstruction.selectResult(loaded[0])
        elif len(loaded) > 1:
            # Do not choose arbitrarily.  The reconstruction list is the explicit
            # ambiguity resolver and already supports selecting the exact object.
            self._widget.setReconstructionsExpanded(True)

    def reconstructionSelected(self, entry_key) -> None:
        self._selectedReconKey = str(entry_key) if entry_key else None
        entry = self._selected_reconstruction_entry()
        if entry is not None and entry["loaded"] is not None and self._reconstruction is not None:
            self._reconstruction.selectResult(entry["loaded"])

    def reuseReconstructionCalibration(self, entry_key, mode: str) -> None:
        entry = self._reconstruction_entry_by_key(str(entry_key or ""))
        if entry is None:
            return
        snapshot = None
        result = entry.get("loaded")
        if result is not None:
            metadata = dict(getattr(result, "metadata", {}) or {})
            snapshot = metadata.get("monalisa_calibration")
            if not isinstance(snapshot, dict):
                snapshot = dict(getattr(result, "params", {}) or {}).get("calibration_snapshot")
        if not isinstance(snapshot, dict) and entry.get("saved") is not None:
            try:
                metadata = read_saved_result_metadata(entry["saved"].path)
                snapshot = metadata.get("monalisa_calibration")
            except Exception as exc:
                self._logger.warning(f"Could not read saved reconstruction calibration: {exc}")

        if not isinstance(snapshot, dict) or not snapshot:
            QtWidgets.QMessageBox.information(
                self._widget, "Reuse MoNaLISA calibration",
                "This reconstruction does not contain a MoNaLISA calibration snapshot.",
            )
            return
        if isinstance(snapshot.get("per_data_item"), list):
            QtWidgets.QMessageBox.information(
                self._widget, "Reuse MoNaLISA calibration",
                "This consolidated reconstruction contains different calibration values "
                "for its data items, so there is no single calibration to reuse.",
            )
            return

        manager = getattr(self._main, "reconstructorManager", None)
        reuse = getattr(manager, "reuseMonalisaCalibration", None)
        if not callable(reuse):
            QtWidgets.QMessageBox.warning(
                self._widget, "Reuse MoNaLISA calibration",
                "The MoNaLISA calibration controls are unavailable.",
            )
            return
        try:
            message = reuse(
                snapshot, mode=str(mode), source_label=str(entry.get("name") or "reconstruction")
            )
        except Exception as exc:
            self._logger.warning(f"Could not reuse MoNaLISA calibration: {exc}")
            QtWidgets.QMessageBox.warning(
                self._widget, "Reuse MoNaLISA calibration", str(exc)
            )
            return
        if message:
            self._widget.setStatus(str(message))

    def _reconstruction_entry_by_key(self, key):
        if self._session is None or not self._selectedDatasetId or not key:
            return None
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return None
        key = str(key)
        return next(
            (item for item in self._reconstruction_entries(dataset) if item["key"] == key),
            None,
        )

    def _selected_reconstruction_entry(self):
        return self._reconstruction_entry_by_key(self._selectedReconKey)

    def _selected_saved(self):
        entry = self._selected_reconstruction_entry()
        return entry["saved"] if entry is not None else None

    # --------------------------------------------------------------- actions

    def loadSelectedRaw(self) -> None:
        if self._session is None or self._fileIO is None:
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None or getattr(dataset, "raw_deleted", False):
            return
        # Selection stays metadata-only; double click / Load raw is the explicit
        # transition into the familiar FileIO materialisation path.
        self._loadingDatasetId = dataset.id
        self._widget.setStatus(f"Loading {dataset.label}…")
        self._render()
        QtWidgets.QApplication.processEvents()
        try:
            outcome = self._fileIO._loadFromPath(dataset.primary_path, prefer_as_current=True)
            if outcome not in {"current", "multidata"}:
                self._widget.setStatus(f"Could not load {dataset.label}")
            else:
                self._widget.setStatus(f"Loaded {dataset.label}")
        finally:
            self._loadingDatasetId = None
            self._render()

    def _selected_dataset_ids(self) -> list[str]:
        ids = list(self._widget.selectedDatasetIds())
        if not ids and self._selectedDatasetId:
            ids = [self._selectedDatasetId]
        return list(dict.fromkeys(str(item) for item in ids if item))

    @staticmethod
    def _multidata_source(
        path: str, dataset_name: str | None
    ) -> tuple[str, str | None] | None:
        """Return a concrete ordinary-image source for MultiData.

        Multi-file lapse discovery can leave the dataset name unset for
        sibling files when each file contains exactly one image dataset.  A
        MultiData ``DataObj`` should still carry that real dataset name so its
        provenance later matches the Session source precisely.  Never guess
        when a container has several choices.
        """
        if dataset_name not in (None, ""):
            return str(path), str(dataset_name)
        try:
            names = list(DataObj.getDatasetNames(str(path)))
        except Exception:
            return None
        if len(names) != 1:
            return None
        return str(path), str(names[0])

    def loadSelectedAsMultiData(self) -> None:
        if self._session is None or self._fileIO is None:
            return
        datasets = [
            self._session.dataset_by_id(dataset_id)
            for dataset_id in self._selected_dataset_ids()
        ]
        datasets = [d for d in datasets if d is not None and not d.raw_deleted]
        added = 0
        skipped = 0
        for dataset in datasets:
            if dataset.kind == "time-lapse":
                sources = [
                    (ref.path, ref.dataset)
                    for ref in dataset.source_refs
                ]
            else:
                sources = [(dataset.primary_path, dataset.primary_dataset)]

            for source_path, source_dataset in sources:
                resolved = self._multidata_source(source_path, source_dataset)
                if resolved is None:
                    skipped += 1
                    self._logger.warning(
                        "Could not add Session source to MultiData without "
                        f"guessing a dataset: {source_path}::{source_dataset}"
                    )
                    continue
                resolved_path, resolved_dataset = resolved
                self._fileIO.multiDataFrameController.makeAndAddDataObj(
                    Path(resolved_path).name,
                    resolved_dataset,
                    path=resolved_path,
                )
                added += 1
        if added:
            self._fileIO._widget.raiseMultiDataDock()
        message = f"Added {added} dataset{'s' if added != 1 else ''} to MultiData"
        if skipped:
            message += f" · skipped {skipped} ambiguous source{'s' if skipped != 1 else ''}"
        self._widget.setStatus(message)

    def _current_selected_dataset(self):
        if self._session is None:
            return None
        dataset_id = self._widget.currentDatasetId() or self._selectedDatasetId
        return self._session.dataset_by_id(dataset_id)

    @staticmethod
    def _reveal_path(path: str | Path) -> None:
        path = Path(path)
        target = path if path.is_dir() else path.parent
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(target)))

    def revealSelectedRaw(self) -> None:
        dataset = self._current_selected_dataset()
        if dataset is not None:
            self._reveal_path(dataset.primary_path)

    def copySelectedRawPath(self) -> None:
        dataset = self._current_selected_dataset()
        if dataset is not None:
            self._widget.copyText(dataset.primary_path)
            self._widget.setStatus("Raw-data path copied")

    def setDatasetFavorite(self, dataset_id, favorite: bool) -> None:
        if self._session is None or not dataset_id:
            return
        self._session.set_favorite([str(dataset_id)], bool(favorite))
        self._render()

    def setSelectedDatasetsFavorite(self, favorite: bool) -> None:
        if self._session is None:
            return
        dataset_ids = self._selected_dataset_ids()
        if not dataset_ids:
            return
        self._session.set_favorite(dataset_ids, bool(favorite))
        self._widget.setStatus(
            f"{'Favorited' if favorite else 'Removed favorite from'} "
            f"{len(dataset_ids)} dataset{'s' if len(dataset_ids) != 1 else ''}"
        )
        self._render()

    def _nonfavorite_datasets(self):
        if self._session is None:
            return []
        return [dataset for dataset in self._session.datasets if not dataset.favorite]

    def _reconstruction_count_for_datasets(self, datasets) -> int:
        return sum(len(self._reconstruction_entries(dataset)) for dataset in datasets)

    def _close_reconstructions_for_datasets(self, datasets) -> bool:
        """Non-destructive Close: persist in-memory-only results before removing them."""
        if self._session is None:
            return True
        results = []
        seen = set()
        for dataset in datasets:
            for _name, result in self._loaded_reconstructions_for_dataset(dataset):
                if id(result) in seen:
                    continue
                seen.add(id(result))
                results.append((dataset, result))

        # Preserve every unsaved result before closing any, so one failed save
        # cannot leave the operation half destructive.
        saved_any = False
        for dataset, result in results:
            if getattr(result, "_session_saved_path", None):
                continue
            try:
                receipt = self._save_result(
                    result, dataset, str(getattr(result, "name", "reconstruction"))
                )
            except Exception as exc:
                self._logger.exception("Could not preserve reconstruction before closing")
                self._widget.setStatus(f"Could not close reconstructions: {exc}")
                return False
            result._session_saved_path = str(receipt.primary)
            self._tag_session_origin(result)
            saved_any = True
        if saved_any:
            self._session.refresh()
        for _dataset, result in results:
            self._remove_loaded_result(result)
        return True

    def _raw_cleanup_plan(self, datasets):
        if self._session is None:
            return [], [], []
        return self._session.raw_cleanup_plan(dataset.id for dataset in datasets)

    def _discard_reconstructions_for_datasets(self, datasets) -> None:
        """Destructive cleanup: remove loaded objects and trash saved artifacts."""
        if self._session is None:
            return
        seen_results = set()
        for dataset in list(datasets):
            for entry in list(self._reconstruction_entries(dataset)):
                result = entry.get("loaded")
                if result is not None and id(result) not in seen_results:
                    seen_results.add(id(result))
                    self._remove_loaded_result(result)
                saved = entry.get("saved")
                if saved is not None:
                    try:
                        self._session.trash_reconstruction(dataset.id, saved.path)
                    except Exception:
                        self._logger.exception("Could not move non-favorite reconstruction to Session trash")
        # A deleted-raw tombstone with no reconstruction left represents
        # nothing after cleanup; remove it from the manifest as well.
        empty_deleted = [
            dataset.id for dataset in datasets
            if dataset.raw_deleted and not dataset.saved_reconstructions
        ]
        if empty_deleted:
            self._session.forget_datasets(empty_deleted)

    def _cleanup_nonfavorite_datasets(self, *, include_raw: bool, ask_raw: bool) -> bool:
        if self._session is None:
            return True
        datasets = self._nonfavorite_datasets()
        if not datasets:
            self._widget.setStatus("No non-favorite datasets to clean up")
            return True

        deletable, raw_paths, skipped = self._raw_cleanup_plan(datasets)
        physical_deletable = [dataset for dataset in deletable if not dataset.raw_deleted]
        if include_raw and ask_raw and raw_paths:
            if not self._widget.requestRawCleanupConfirmation(
                dataset_count=len(physical_deletable),
                raw_file_count=len(raw_paths),
                skipped_count=len(skipped),
            ):
                return False

        # Cleanup intentionally does not preserve unsaved non-favorite results.
        self._discard_reconstructions_for_datasets(datasets)

        if include_raw:
            # Re-resolve IDs after reconstruction cleanup may have removed raw-deleted
            # tombstones from the manifest. Physical paths are from the safe plan above.
            deletable_ids = [dataset.id for dataset in physical_deletable]
            if raw_paths:
                self._session.mark_raw_deleted(deletable_ids)
                try:
                    release = getattr(self._fileIO, "releasePaths", None)
                    if callable(release):
                        release(raw_paths)
                    from send2trash import send2trash
                    for path in raw_paths:
                        source = Path(path)
                        if source.exists():
                            send2trash(str(source))
                except Exception as exc:
                    self._logger.exception("Could not clean up non-favorite raw data")
                    self._widget.setStatus(f"Raw-data cleanup failed: {exc}")
                    self._session.refresh()
                    self._render()
                    return False
            forget_ids = [dataset.id for dataset in deletable]
            if forget_ids:
                self._session.forget_datasets(forget_ids)

        self._session.refresh()
        if include_raw:
            message = "Cleaned up non-favorite reconstructions and raw data"
            if skipped:
                message += (
                    f" · kept {len(skipped)} dataset{'s' if len(skipped) != 1 else ''} "
                    "whose raw files are shared with kept data"
                )
        else:
            message = "Moved non-favorite reconstructions to Session trash"
        self._widget.setStatus(message)
        self._selectedReconKey = None
        self._render()
        return True

    def cleanupNonFavorites(self) -> None:
        if self._session is None:
            return
        datasets = self._nonfavorite_datasets()
        if not datasets:
            self._widget.setStatus("No non-favorite datasets to clean up")
            return
        choice = self._widget.requestNonFavoriteCleanupChoice(
            len(datasets), self._reconstruction_count_for_datasets(datasets)
        )
        if choice == "cancel":
            return
        self._cleanup_nonfavorite_datasets(
            include_raw=(choice == "raw"), ask_raw=(choice == "raw")
        )

    def _affected_datasets_for_raw_delete(self, dataset_ids: list[str]):
        if self._session is None:
            return [], []
        selected = [self._session.dataset_by_id(item) for item in dataset_ids]
        selected = [item for item in selected if item is not None and not item.raw_deleted]
        paths = {
            os.path.normcase(os.path.abspath(ref.path))
            for dataset in selected
            for ref in dataset.source_refs
            if ref.path
        }
        affected = [
            dataset
            for dataset in self._session.datasets
            if not dataset.raw_deleted
            and any(
                os.path.normcase(os.path.abspath(ref.path)) in paths
                for ref in dataset.source_refs
                if ref.path
            )
        ]
        return affected, sorted(paths)

    def deleteSelectedRaw(self) -> None:
        if self._session is None:
            return
        affected, raw_paths = self._affected_datasets_for_raw_delete(
            self._selected_dataset_ids()
        )
        if not affected or not raw_paths:
            return
        reconstruction_entries = {
            dataset.id: list(self._reconstruction_entries(dataset))
            for dataset in affected
        }
        recon_count = sum(len(entries) for entries in reconstruction_entries.values())
        names = ", ".join(dataset.label for dataset in affected[:3])
        if len(affected) > 3:
            names += f" and {len(affected) - 3} more"

        box = QtWidgets.QMessageBox(self._widget)
        box.setWindowTitle("Delete raw data")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(
            f"Move raw data for {len(affected)} dataset{'s' if len(affected) != 1 else ''} to the system Trash?"
        )
        box.setInformativeText(names)
        delete_recons_btn = None
        if recon_count:
            delete_recons_btn = box.addButton(
                f"Delete raw + {recon_count} reconstruction{'s' if recon_count != 1 else ''}",
                QtWidgets.QMessageBox.DestructiveRole,
            )
            keep_recons_btn = box.addButton(
                "Delete raw only, keep reconstructions",
                QtWidgets.QMessageBox.ActionRole,
            )
        else:
            keep_recons_btn = box.addButton(
                "Delete raw data", QtWidgets.QMessageBox.DestructiveRole
            )
        cancel_btn = box.addButton(QtWidgets.QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn or clicked is None:
            return
        delete_recons = bool(delete_recons_btn is not None and clicked is delete_recons_btn)

        # "Keep reconstructions" must also preserve results that currently
        # exist only in the reconstruction-object list. Persist those before
        # the raw source disappears.
        if recon_count and not delete_recons:
            for dataset in affected:
                for entry in reconstruction_entries.get(dataset.id, ()):
                    if entry["saved"] is not None or entry["loaded"] is None:
                        continue
                    try:
                        receipt = self._save_result(
                            entry["loaded"],
                            dataset,
                            str(getattr(entry["loaded"], "name", "reconstruction")),
                        )
                        entry["loaded"]._session_saved_path = str(receipt.primary)
                    except Exception as exc:
                        self._logger.exception("Could not preserve reconstruction before raw deletion")
                        self._widget.setStatus(f"Could not preserve reconstruction: {exc}")
                        return
            self._session.refresh()

        affected_ids = [dataset.id for dataset in affected]
        # Persist the tombstone before touching disk. If an OS-trash operation
        # fails halfway, refresh can still retain the logical Session entry.
        self._session.mark_raw_deleted(affected_ids)
        try:
            release = getattr(self._fileIO, "releasePaths", None)
            if callable(release):
                release(raw_paths)
            from send2trash import send2trash
            for path in raw_paths:
                source = Path(path)
                if source.exists():
                    send2trash(str(source))
        except Exception as exc:
            self._logger.exception("Could not delete raw Session data")
            self._widget.setStatus(f"Raw-data deletion failed: {exc}")
            self._session.refresh()
            self._render()
            return

        if delete_recons:
            for dataset in affected:
                for entry in reconstruction_entries.get(dataset.id, ()):
                    if entry["loaded"] is not None:
                        self._remove_loaded_result(entry["loaded"])
                    if entry["saved"] is not None:
                        try:
                            self._session.trash_reconstruction(dataset.id, entry["saved"].path)
                        except Exception:
                            self._logger.exception("Could not trash associated reconstruction")
            self._session.forget_datasets(affected_ids)
        elif not recon_count:
            self._session.forget_datasets(affected_ids)

        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._session.refresh()
        self._widget.setStatus(
            "Raw data moved to system Trash"
            + ("; reconstructions moved to Session trash" if delete_recons else "")
        )
        self._render()

    def closeSelectedDatasetReconstructions(self) -> None:
        if self._session is None:
            return
        results = []
        seen = set()
        for dataset_id in self._selected_dataset_ids():
            dataset = self._session.dataset_by_id(dataset_id)
            if dataset is None:
                continue
            for _name, result in self._loaded_reconstructions_for_dataset(dataset):
                if id(result) in seen:
                    continue
                seen.add(id(result))
                results.append(result)
        for result in results:
            self._remove_loaded_result(result)
        if results:
            self._widget.setStatus(
                f"Closed {len(results)} reconstruction{'s' if len(results) != 1 else ''} from the panel"
            )
        self._render()

    def addSelectedDatasetReconstructions(self) -> None:
        if self._session is None:
            return
        wanted = set(self._selected_dataset_ids())
        datasets = [dataset for dataset in self._session.datasets if dataset.id in wanted]
        self._start_add_reconstructions(datasets)

    def _start_add_reconstructions(self, datasets) -> None:
        if self._session is None:
            return
        if self._addQueue is not None:
            self._widget.setStatus("Already adding reconstructions")
            return

        loaded_by_saved = self._loaded_by_saved_path()
        queue = []
        for dataset in datasets:
            for saved in dataset.saved_reconstructions:
                try:
                    resolved = str(Path(saved.path).resolve())
                except Exception:
                    resolved = str(saved.path)
                if resolved in loaded_by_saved:
                    continue
                queue.append((dataset, saved))

        if not queue:
            self._widget.setStatus("No matching saved reconstructions to add")
            return

        self._addQueue = queue
        self._addTotal = len(queue)
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        self._widget.setAddingProgress(True, 0, self._addTotal)
        self._widget.setStatus(
            f"Adding {self._addTotal} reconstruction{'s' if self._addTotal != 1 else ''}…"
        )
        QtCore.QTimer.singleShot(0, self._process_next_add_reconstruction)

    def cancelAddingReconstructions(self) -> None:
        if self._addQueue is not None:
            self._addCancelled = True
            self._widget.setStatus("Cancelling after current reconstruction…")

    def _process_next_add_reconstruction(self) -> None:
        queue = self._addQueue
        if queue is None:
            return
        if self._addCancelled or not queue:
            self._finish_add_reconstructions(cancelled=self._addCancelled)
            return

        _dataset, saved = queue.pop(0)
        try:
            result = load_saved_result(
                saved.path, session_origin=self._session_origin_id(self._session)
            )
        except Exception:
            self._logger.exception("Could not restore saved Session reconstruction")
            self._addFailed += 1
        else:
            self._commChannel.sigResultProduced.emit(
                result,
                str(getattr(result, "name", "") or saved.display_name or saved.name),
            )
            self._addLastResult = result
            self._addAdded += 1
        self._addProcessed += 1
        self._widget.setAddingProgress(True, self._addProcessed, self._addTotal)
        QtCore.QTimer.singleShot(0, self._process_next_add_reconstruction)

    def _finish_add_reconstructions(self, *, cancelled: bool) -> None:
        added = self._addAdded
        total = self._addTotal
        processed = self._addProcessed
        failed = self._addFailed
        last_result = self._addLastResult
        self._addQueue = None
        self._addTotal = 0
        self._addProcessed = 0
        self._addAdded = 0
        self._addFailed = 0
        self._addCancelled = False
        self._addLastResult = None
        self._widget.setAddingProgress(False)

        if last_result is not None and self._reconstruction is not None:
            self._reconstruction.selectResult(last_result)

        if cancelled:
            message = f"Cancelled — added {added} of {total} reconstructions"
        else:
            message = f"Added {added} reconstruction{'s' if added != 1 else ''}"
        if failed:
            message += f" · {failed} failed"
        elif cancelled and processed < total:
            message += f" · {total - processed} not opened"
        self._widget.setStatus(message)
        self._render()

    def closeSessionReconstructions(self) -> None:
        if self._session is None:
            return
        seen = set()
        results = []
        for dataset in self._session.datasets:
            for _name, result in self._loaded_reconstructions_for_dataset(dataset):
                if id(result) not in seen:
                    seen.add(id(result))
                    results.append(result)
        for result in results:
            self._remove_loaded_result(result)
        self._widget.setStatus(
            f"Closed {len(results)} Session reconstruction{'s' if len(results) != 1 else ''}"
        )
        self._render()

    def closeNonFavoriteReconstructions(self) -> None:
        datasets = self._nonfavorite_datasets()
        if not datasets:
            self._widget.setStatus("No non-favorite datasets")
            return
        before = sum(
            len(self._loaded_reconstructions_for_dataset(dataset)) for dataset in datasets
        )
        if not self._close_reconstructions_for_datasets(datasets):
            return
        self._widget.setStatus(
            f"Closed {before} reconstruction{'s' if before != 1 else ''} from non-favorites"
        )
        self._render()

    def emptySessionTrash(self) -> None:
        if self._session is None or not self._session.trash_root.exists():
            self._widget.setStatus("Session trash is empty")
            return
        answer = QtWidgets.QMessageBox.question(
            self._widget,
            "Empty Session trash",
            "Permanently delete everything in Session trash? This cannot be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if answer != QtWidgets.QMessageBox.Yes:
            return
        try:
            self._session.empty_trash()
        except Exception as exc:
            self._logger.exception("Could not empty Session trash")
            self._widget.setStatus(f"Could not empty Session trash: {exc}")
            return
        self._widget.setStatus("Session trash emptied")

    def revealRawFolder(self) -> None:
        if self._session is not None:
            self._reveal_path(self._session.raw_root)

    def revealResultsFolder(self) -> None:
        if self._session is not None:
            self._session.results_root.mkdir(parents=True, exist_ok=True)
            self._reveal_path(self._session.results_root)

    def addSelectedToReconstructions(self) -> None:
        entry = self._selected_reconstruction_entry()
        if entry is None:
            return
        if entry["loaded"] is not None:
            if self._reconstruction is not None:
                self._reconstruction.selectResult(entry["loaded"])
            return
        saved = entry["saved"]
        if saved is None:
            return
        resolved = str(Path(saved.path).resolve())
        try:
            result = load_saved_result(
                saved.path, session_origin=self._session_origin_id(self._session)
            )
        except Exception as exc:
            self._logger.exception("Could not restore saved reconstruction")
            self._widget.setStatus(f"Could not add reconstruction: {exc}")
            return
        self._commChannel.sigResultProduced.emit(
            result, str(getattr(result, "name", "") or saved.display_name or saved.name)
        )
        if self._reconstruction is not None:
            self._reconstruction.selectResult(result)
        self._widget.setStatus(f"Added {saved.name} to reconstructions")
        self._renderSelected()

    def toggleReconstructionWorkspace(self, entry_key) -> None:
        entry = self._reconstruction_entry_by_key(entry_key)
        if entry is None:
            return
        self._selectedReconKey = str(entry_key)

        if entry["loaded"] is not None:
            # Close is explicitly non-destructive. If this result only exists
            # in memory, save it to the Session first so removing the
            # ProcessingResult does not destroy the only copy.
            loaded = entry["loaded"]
            if entry["saved"] is None:
                saved = self._ensure_selected_saved()
                if saved is None:
                    return
            self._remove_loaded_result(loaded)
            self._widget.setStatus("Closed reconstruction object; Session result preserved")
            self._render()
            return

        saved = entry["saved"]
        if saved is None:
            return
        try:
            result = load_saved_result(
                saved.path, session_origin=self._session_origin_id(self._session)
            )
        except Exception as exc:
            self._logger.exception("Could not restore saved reconstruction")
            self._widget.setStatus(f"Could not add reconstruction: {exc}")
            return
        self._commChannel.sigResultProduced.emit(
            result, str(getattr(result, "name", "") or saved.display_name or saved.name)
        )
        if self._reconstruction is not None:
            self._reconstruction.selectResult(result)
        self._widget.setStatus(f"Added {saved.name} to reconstructions")
        self._render()

    def _ensure_selected_saved(self):
        """Return the selected saved record, saving an in-memory result if needed."""
        entry = self._selected_reconstruction_entry()
        if (
            entry is None
            or self._session is None
            or self._selectedDatasetId is None
        ):
            return None
        if entry["saved"] is not None:
            return entry["saved"]
        result = entry["loaded"]
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if result is None or dataset is None:
            return None
        try:
            receipt = self._save_result(
                result, dataset, str(getattr(result, "name", "reconstruction"))
            )
        except Exception as exc:
            self._logger.exception("Could not save reconstruction into Session")
            self._widget.setStatus(f"Could not keep reconstruction: {exc}")
            return None
        result._session_saved_path = str(receipt.primary)
        self._tag_session_origin(result)
        wanted = str(Path(receipt.primary).resolve())
        self._session.refresh()
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return None
        saved = next(
            (item for item in dataset.saved_reconstructions if _same_path(item.path, wanted)),
            None,
        )
        if saved is not None:
            self._selectedReconKey = f"saved:{wanted}"
        return saved

    def _remove_loaded_result(self, result) -> None:
        if result is None or self._reconstruction is None:
            return
        remover = getattr(self._reconstruction, "removeResult", None)
        if callable(remover):
            # ReconstructionViewController owns result-list resource release, so
            # Session and the generic Remove current/Remove all paths behave the
            # same way for lazy Session-restored results.
            remover(result)

    def discardReconstruction(self, entry_key) -> None:
        key = str(entry_key or "")
        if key:
            self._discard_reconstruction_keys([key])

    def discardSelected(self) -> None:
        keys = list(dict.fromkeys(self._widget.selectedReconstructionKeys()))
        if not keys and self._selectedReconKey:
            keys = [self._selectedReconKey]
        self._discard_reconstruction_keys(keys)

    def _discard_reconstruction_keys(self, keys) -> None:
        if self._session is None or self._selectedDatasetId is None:
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return
        entries = [self._reconstruction_entry_by_key(str(key)) for key in keys if key]
        entries = [entry for entry in entries if entry is not None]
        if not entries:
            return

        discarded = 0
        for entry in entries:
            if entry["loaded"] is not None:
                self._remove_loaded_result(entry["loaded"])
            if entry["saved"] is not None:
                try:
                    self._session.trash_reconstruction(dataset.id, entry["saved"].path)
                except Exception as exc:
                    self._logger.exception("Could not move reconstruction to Session trash")
                    self._widget.setStatus(
                        f"Could not discard {entry['saved'].name}: {exc}"
                    )
                    self._session.refresh()
                    self._render()
                    return
            discarded += 1

        self._selectedReconKey = None
        self._session.refresh()
        self._widget.setStatus(
            f"Discarded {discarded} reconstruction{'s' if discarded != 1 else ''} to Session trash"
        )
        self._render()

    # ------------------------------------------------------------ result sync

    def _loaded_result_signature(self) -> tuple[int, ...]:
        try:
            return tuple(sorted(id(result) for _name, result in self._commChannel.getAllResults()))
        except Exception:
            return ()

    def _loadedResultsChanged(self) -> None:
        signature = self._loaded_result_signature()
        if signature == self._loadedResultSignature:
            return
        self._loadedResultSignature = signature
        if self._session is not None:
            self._render()

    def _resultProduced(self, result, _display_name: str) -> None:
        if self._session is None or result is None:
            return
        # A restored saved result is already persistent.  Publishing it through
        # the normal viewer signal must not cause a duplicate save.
        if getattr(result, "_session_restored", False):
            QtCore.QTimer.singleShot(0, self._render)
            return
        if not self._is_session_reconstruction(result):
            return
        dataset = self._session.dataset_for_result(result)
        if dataset is None:
            return

        # Recognition and persistence are deliberately independent: even with
        # auto-save disabled, Session must immediately show that this raw
        # dataset has a reconstruction object in the right-hand list.  Defer
        # one event-loop turn because ReconstructionViewController is another
        # listener of the same signal and may not have inserted its item yet.
        QtCore.QTimer.singleShot(0, self._render)

        if not self._autoSave or getattr(result, "_session_saved_path", None):
            return
        try:
            receipt = self._save_result(result, dataset, _display_name)
        except Exception as exc:
            self._logger.exception("Session auto-save failed")
            self._widget.setStatus(f"Auto-save failed: {exc}")
            return
        result._session_saved_path = str(receipt.primary)
        self._tag_session_origin(result)
        self._session.refresh()
        self._widget.setStatus(f"Auto-saved {receipt.primary.name}")
        self._render()

    @staticmethod
    def _short_component(value: str, limit: int) -> str:
        value = _clean_name(value)
        if len(value) <= limit:
            return value
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]
        keep = max(1, limit - len(digest) - 1)
        return f"{value[:keep].rstrip('._-')}-{digest}"

    @classmethod
    def _autosave_stem(cls, dataset, result, display_name: str = "") -> str:
        source_path = Path(dataset.primary_path)
        source_stem = _clean_name(source_path.stem)
        label = str(display_name or getattr(result, "name", "reconstruction") or "reconstruction")

        # Reconstructors commonly name a result ``<raw filename>_<algorithm>``.
        # Do not concatenate that whole name with the raw stem again: besides
        # being redundant it makes the staged-save path exceed Windows' common
        # path-length limit because SaveProtocol repeats the filename in the
        # staging-directory name.
        lowered = label.lower()
        for prefix in (source_path.name, source_path.stem):
            if lowered.startswith(prefix.lower()):
                label = label[len(prefix):].lstrip(" ._-:")
                break
        label = _clean_name(label or "recon")
        source_part = cls._short_component(source_stem, 36)
        result_part = cls._short_component(label, 18)
        return f"{source_part}_{result_part}"

    def _save_result(self, result, dataset, display_name: str = ""):
        fmt = self._saveFormat if self._saveFormat in _FORMAT_SUFFIX else "tiff"
        suffix = _FORMAT_SUFFIX[fmt]
        stem = self._autosave_stem(dataset, result, display_name)
        base = self._session.results_root / f"{stem}{suffix}"
        candidate = base
        index = 1
        while candidate.exists():
            candidate = base.with_name(f"{stem}_{index}{suffix}")
            index += 1
        return result.save(candidate, fmt, overwrite=False)

    def _resultSaved(self, result, receipt) -> None:
        if self._session is None or result is None or receipt is None:
            return
        try:
            primary = Path(receipt.primary).resolve()
            primary.relative_to(self._session.results_root)
        except Exception:
            return
        if self._session.dataset_for_result(result) is None:
            return
        result._session_saved_path = str(primary)
        self._tag_session_origin(result)
        self._session.refresh()
        self._render()


__all__ = ["ProcessingSessionController"]
