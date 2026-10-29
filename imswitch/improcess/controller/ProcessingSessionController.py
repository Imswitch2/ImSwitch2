from __future__ import annotations

import hashlib
import re
from pathlib import Path

from qtpy import QtCore

from imswitch.improcess.controller.basecontrollers import ImProcessWidgetController
from imswitch.improcess.model.processing_session import ProcessingSession
from imswitch.improcess.model.provenance import output_node
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
        self._widget.sigAddToReconstructionsRequested.connect(self.addSelectedToReconstructions)
        self._widget.sigSetFinalRequested.connect(self.setSelectedFinal)
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
            marker = "●" if result is not None else "○"
            bits = ["FINAL" if saved.final else "DRAFT"]
            bits.append("In reconstructions" if result is not None else "Saved only")
            entries.append({
                "key": f"saved:{resolved}",
                "label": f"{marker} {saved.name}    {' · '.join(bits)}",
                "saved": saved,
                "loaded": result,
            })

        for display_name, result in loaded_for_dataset:
            if id(result) in represented_loaded:
                continue
            entries.append({
                "key": self._loaded_key(result),
                "label": f"● {display_name}    In reconstructions · Not saved",
                "saved": None,
                "loaded": result,
            })
        return entries

    def _render(self) -> None:
        session = self._session
        if session is None:
            self._widget.setDatasets([])
            self._renderSelected()
            return
        entries = []
        for dataset in session.datasets:
            in_memory = len(self._loaded_reconstructions_for_dataset(dataset))
            if dataset.status == "final":
                status = "✓ FINAL"
            elif dataset.status == "draft":
                status = f"● DRAFT · {len(dataset.saved_reconstructions)} saved"
            elif in_memory:
                status = f"● RECONSTRUCTED · {in_memory} in reconstructions"
            else:
                status = "— RAW"
            if dataset.kind == "time-lapse" and dataset.planned_timepoints:
                status += f" · T={dataset.planned_timepoints}"
            entries.append((dataset.id, dataset.label, status))
        self._widget.setDatasets(entries)
        if self._selectedDatasetId and session.dataset_by_id(self._selectedDatasetId) is None:
            self._selectedDatasetId = None
        self._renderSelected()

    def _renderSelected(self) -> None:
        session = self._session
        dataset = session.dataset_by_id(self._selectedDatasetId) if session else None
        if dataset is None:
            self._widget.setSelectedDataset("No dataset selected", "")
            self._widget.setSavedReconstructions([])
            self._widget.setActionsEnabled(False, False, False)
            return

        final = dataset.final_reconstruction
        entries = self._reconstruction_entries(dataset)
        loaded_count = sum(1 for item in entries if item["loaded"] is not None)
        saved_count = len(dataset.saved_reconstructions)
        summary = (
            f"{loaded_count} reconstruction(s) in list · {saved_count} saved\n"
            f"Final: {final.name if final else 'none'}"
        )
        if dataset.kind == "time-lapse" and dataset.planned_timepoints:
            summary += f"\nTime lapse: {dataset.planned_timepoints} planned timepoints"
        self._widget.setSelectedDataset(dataset.label, summary)

        selected = self._selectedReconKey
        if selected and not any(item["key"] == selected for item in entries):
            selected = None
            self._selectedReconKey = None
        self._widget.setSavedReconstructions(
            [(item["key"], item["label"]) for item in entries], selected
        )
        selected_entry = self._selected_reconstruction_entry()
        can_add = bool(selected_entry and selected_entry["saved"] is not None and selected_entry["loaded"] is None)
        can_final = bool(
            selected_entry
            and selected_entry["saved"] is not None
            and not selected_entry["saved"].final
        )
        self._widget.setActionsEnabled(True, can_add, can_final)

    def datasetSelected(self, dataset_id) -> None:
        # Raw selection is deliberately local to Session.  It never changes
        # Current data and never switches the active reconstruction.
        self._selectedDatasetId = str(dataset_id) if dataset_id else None
        self._selectedReconKey = None
        self._renderSelected()

    def reconstructionSelected(self, entry_key) -> None:
        self._selectedReconKey = str(entry_key) if entry_key else None
        entry = self._selected_reconstruction_entry()
        can_add = bool(entry and entry["saved"] is not None and entry["loaded"] is None)
        can_final = bool(entry and entry["saved"] is not None and not entry["saved"].final)
        self._widget.setActionsEnabled(
            self._selectedDatasetId is not None, can_add, can_final
        )
        if entry is not None and entry["loaded"] is not None and self._reconstruction is not None:
            self._reconstruction.selectResult(entry["loaded"])

    def _selected_reconstruction_entry(self):
        if self._session is None or not self._selectedDatasetId or not self._selectedReconKey:
            return None
        dataset = self._session.dataset_by_id(self._selectedDatasetId)
        if dataset is None:
            return None
        return next(
            (item for item in self._reconstruction_entries(dataset) if item["key"] == self._selectedReconKey),
            None,
        )

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
        self._commChannel.sigResultProduced.emit(result, saved.name)
        if self._reconstruction is not None:
            self._reconstruction.selectResult(result)
        self._widget.setStatus(f"Added {saved.name} to reconstructions")
        self._renderSelected()

    def setSelectedFinal(self) -> None:
        saved = self._selected_saved()
        if saved is None or self._session is None or self._selectedDatasetId is None:
            return
        if self._session.set_final(self._selectedDatasetId, saved.path):
            self._widget.setStatus(f"Final reconstruction: {saved.name}")
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
