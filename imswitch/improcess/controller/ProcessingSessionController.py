from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from qtpy import QtCore

from imswitch.improcess.controller.basecontrollers import ImProcessWidgetController
from imswitch.improcess.model.processing_session import ProcessingSession
from imswitch.improcess.model.provenance import graph_of, output_node
from imswitch.improcess.model.saved_result import load_saved_result


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
        self._autoSave = bool(self._widget.autoSaveCheck.isChecked())
        self._saveFormat = str(self._widget.saveFormatCombo.currentData() or "tiff")

        self._widget.sigOpenSession.connect(self.openSessionFromFields)
        self._widget.sigRefresh.connect(self.refresh)
        self._widget.sigDatasetSelectionChanged.connect(self.datasetSelected)
        self._widget.sigSavedSelectionChanged.connect(self.reconstructionSelected)
        self._widget.sigLoadRawRequested.connect(self.loadSelectedRaw)
        self._widget.sigReconstructionFinalToggleRequested.connect(
            self.toggleReconstructionFinal
        )
        self._widget.sigReconstructionWorkspaceToggleRequested.connect(
            self.toggleReconstructionWorkspace
        )
        self._widget.sigDiscardSelectedRequested.connect(self.discardSelected)
        self._widget.sigDiscardCandidatesRequested.connect(self.discardCandidates)
        self._widget.sigAutoSaveChanged.connect(self._setAutoSave)
        self._widget.sigSaveFormatChanged.connect(self._setSaveFormat)

        self._commChannel.sigResultProduced.connect(self._resultProduced)
        self._commChannel.sigResultsChanged.connect(self._loadedResultsChanged)
        if hasattr(self._commChannel, "sigResultSaved"):
            self._commChannel.sigResultSaved.connect(self._resultSaved)

    @property
    def session(self):
        return self._session

    def _setAutoSave(self, enabled: bool) -> None:
        self._autoSave = bool(enabled)

    def _setSaveFormat(self, fmt: str) -> None:
        self._saveFormat = str(fmt or "tiff")

    def openSessionFromFields(self) -> None:
        raw_text = self._widget.rawRoot()
        if not raw_text:
            self._widget.setStatus("Choose a raw-data folder first")
            return
        raw = Path(raw_text).expanduser()
        results_text = self._widget.resultsRoot()
        results = (
            Path(results_text).expanduser()
            if results_text
            else raw.with_name(raw.name + "_recon")
        )
        self.openSession(raw, results)

    def openSession(self, raw_root, results_root) -> None:
        raw = Path(raw_root).expanduser()
        if not raw.is_dir():
            self._widget.setStatus(f"Raw-data folder does not exist: {raw}")
            return
        try:
            session = ProcessingSession(raw, results_root)
            session.refresh()
        except Exception as exc:
            self._logger.exception("Could not open processing session")
            self._widget.setStatus(f"Could not open session: {exc}")
            return
        self._session = session
        self._selectedDatasetId = None
        self._selectedReconKey = None
        self._widget.setPaths(str(session.raw_root), str(session.results_root))
        self._widget.setSetupExpanded(False)
        self._widget.setStatus(
            f"Session open · {len(session.datasets)} raw dataset(s) · {session.results_root}"
        )
        self._render()

    def refresh(self) -> None:
        if self._session is None:
            self.openSessionFromFields()
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
    def _is_direct_reconstruction(result) -> bool:
        node = output_node(result)
        return isinstance(node, dict) and node.get("op") == "reconstruct"

    def _loaded_reconstructions_for_dataset(self, dataset) -> list[tuple[str, object]]:
        """Direct reconstruction objects currently present in the right-hand list."""
        if self._session is None or dataset is None:
            return []
        matches = []
        for display_name, result in self._commChannel.getAllResults():
            if not self._is_direct_reconstruction(result):
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
            marker = "★" if saved.final else ("●" if result is not None else "○")
            bits = ["FINAL" if saved.final else "CANDIDATE"]
            bits.append("In reconstructions" if result is not None else "Saved only")
            entries.append({
                "key": f"saved:{resolved}",
                "name": saved.name,
                "tooltip": saved.tooltip or saved.name,
                "label": f"{marker} {saved.name}    {' · '.join(bits)}",
                "final": bool(saved.final),
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
                "label": f"● {session_name}    CANDIDATE · In reconstructions · Not saved",
                "final": False,
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

    def _render(self) -> None:
        session = self._session
        if session is None:
            self._widget.setDatasets([])
            self._renderSelected()
            return
        entries = []
        for dataset in session.datasets:
            recon_entries = self._reconstruction_entries(dataset)
            candidate_count = sum(
                1
                for item in recon_entries
                if item["saved"] is None or not item["saved"].final
            )
            kept_count = sum(
                1
                for item in recon_entries
                if item["saved"] is not None and item["saved"].final
            )
            if candidate_count:
                status = f"! REVIEW · {candidate_count} candidate"
                if candidate_count != 1:
                    status += "s"
            elif kept_count:
                status = "✓ FINAL"
                if kept_count > 1:
                    status += f" · {kept_count} kept"
            else:
                status = "— RAW"
            if dataset.kind == "time-lapse" and dataset.planned_timepoints:
                status += f" · T={dataset.planned_timepoints}"
            state = "review" if candidate_count else ("final" if kept_count else "raw")
            entries.append({
                "id": dataset.id,
                "label": dataset.label,
                "status": status,
                "state": state,
                "timestamp": self._dataset_timestamp(dataset),
                "search": self._dataset_search_text(dataset),
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
            self._widget.setActionsEnabled(False, False, False)
            return

        entries = self._reconstruction_entries(dataset)
        loaded_count = sum(1 for item in entries if item["loaded"] is not None)
        saved_count = len(dataset.saved_reconstructions)
        kept_count = sum(
            1 for item in entries
            if item["saved"] is not None and item["saved"].final
        )
        candidate_count = len(entries) - kept_count
        total_count = len(entries)
        summary = (
            f"{total_count} reconstructed · {loaded_count} in list · "
            f"{kept_count} kept · {candidate_count} candidate"
            f"{'s' if candidate_count != 1 else ''}"
        )
        if dataset.kind == "time-lapse" and dataset.planned_timepoints:
            summary += f" · T={dataset.planned_timepoints}"
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
                    "final": bool(item.get("final", False)),
                    "loaded": item["loaded"] is not None,
                    "saved": item["saved"] is not None,
                    "tooltip": item.get("tooltip", item.get("name", "")),
                }
                for item in entries
            ],
            selected,
        )
        can_discard = bool(self._widget.selectedReconstructionKeys())
        can_discard_candidates = any(
            item["saved"] is None or not item["saved"].final for item in entries
        )
        self._widget.setActionsEnabled(True, can_discard, can_discard_candidates)

    def datasetSelected(self, dataset_id) -> None:
        # Raw selection is deliberately local to Session.  It never changes
        # Current data and never switches the active reconstruction.
        self._selectedDatasetId = str(dataset_id) if dataset_id else None
        self._selectedReconKey = None
        self._renderSelected()

    def reconstructionSelected(self, entry_key) -> None:
        self._selectedReconKey = str(entry_key) if entry_key else None
        entry = self._selected_reconstruction_entry()
        dataset = (
            self._session.dataset_by_id(self._selectedDatasetId)
            if self._session is not None and self._selectedDatasetId
            else None
        )
        entries = self._reconstruction_entries(dataset) if dataset is not None else []
        self._widget.setActionsEnabled(
            self._selectedDatasetId is not None,
            bool(self._widget.selectedReconstructionKeys()),
            any(item["saved"] is None or not item["saved"].final for item in entries),
        )
        if entry is not None and entry["loaded"] is not None and self._reconstruction is not None:
            self._reconstruction.selectResult(entry["loaded"])

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
        if dataset is None:
            return
        # This is the *only* Session operation that intentionally enters the
        # familiar raw-data Load path. Selection above remains metadata-only.
        self._fileIO._loadFromPath(dataset.primary_path, prefer_as_current=True)

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
            result = load_saved_result(saved.path)
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
            # Remove is explicitly non-destructive. If this candidate only
            # exists in memory, save it to the Session first so removing the
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
            result = load_saved_result(saved.path)
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

    def toggleReconstructionFinal(self, entry_key) -> None:
        entry = self._reconstruction_entry_by_key(entry_key)
        if entry is None or self._session is None or self._selectedDatasetId is None:
            return
        self._selectedReconKey = str(entry_key)
        if entry["saved"] is not None and entry["saved"].final:
            if self._session.set_final(self._selectedDatasetId, entry["saved"].path, False):
                self._widget.setStatus(f"Marked {entry['saved'].name} as candidate")
                self._render()
            return

        saved = self._ensure_selected_saved()
        if saved is None:
            return
        if self._session.set_final(self._selectedDatasetId, saved.path, True):
            self._widget.setStatus(f"Keeping {saved.name} as final")
            self._render()

    def _ensure_selected_saved(self):
        """Return the selected saved record, saving an in-memory candidate if needed."""
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

    @staticmethod
    def _close_restored_backing(result) -> None:
        backing = getattr(result, "_session_backing_data_obj", None)
        closer = getattr(backing, "checkAndUnloadData", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def _remove_loaded_result(self, result) -> None:
        if result is None or self._reconstruction is None:
            return
        remover = getattr(self._reconstruction, "removeResult", None)
        if callable(remover):
            remover(result)
        self._close_restored_backing(result)

    def discardSelected(self) -> None:
        if self._session is None or self._selectedDatasetId is None:
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return
        keys = list(dict.fromkeys(self._widget.selectedReconstructionKeys()))
        if not keys and self._selectedReconKey:
            keys = [self._selectedReconKey]
        entries = [self._reconstruction_entry_by_key(key) for key in keys]
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

    def discardCandidates(self) -> None:
        if self._session is None or self._selectedDatasetId is None:
            return
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return
        entries = [
            entry
            for entry in self._reconstruction_entries(dataset)
            if entry["saved"] is None or not entry["saved"].final
        ]
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
            f"Discarded {discarded} candidate{'s' if discarded != 1 else ''} to Session trash"
        )
        self._render()

    # ------------------------------------------------------------ result sync

    def _loadedResultsChanged(self) -> None:
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
        if not self._is_direct_reconstruction(result):
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
        self._session.refresh()
        self._render()


__all__ = ["ProcessingSessionController"]
