"""Session-scoped MoNaLISA calibration registry and crash-recovery journal.

The registry is deliberately UI-neutral.  It stores explicit user-locked
localizations and detector-specific orientations for the current ImProcess
session, while transient Auto results remain separate until the user locks
them.  A small atomic JSON journal outside the recording directory makes an
interrupted session recoverable without turning session calibration into a
permanent library.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid
from typing import Any, Callable

import numpy as np

from imswitch.imcommon.model import dirtools

from .calibration import PatternLocalization, ScanOrientation


JOURNAL_FILENAME = "monalisa-calibration.json"
JOURNAL_FORMAT = "imswitch-monalisa-session-calibration"
JOURNAL_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def detector_identity(data_obj=None, attrs: dict | None = None) -> str:
    """Stable detector label available from a source without pixel inference."""
    attrs = dict(attrs or getattr(data_obj, "attrs", None) or {})
    for key in (
        "recording:detector_name",
        "detector_name",
        "detector",
        "Detector",
    ):
        value = attrs.get(key)
        if value not in (None, ""):
            return str(value)
    if data_obj is not None:
        try:
            layout = getattr(data_obj, "acquisition_layout", None)
            value = getattr(getattr(layout, "layout", None), "detector", None)
            if value:
                return str(value)
        except Exception:
            pass
        for name in ("datasetName", "_datasetName"):
            value = getattr(data_obj, name, None)
            if value:
                return str(value)
    return "unknown"


@dataclass(frozen=True)
class LocalizationProfile:
    id: str
    name: str
    detector: str
    localization: PatternLocalization
    source: str
    source_dataset: str = ""
    source_path: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "detector": self.detector,
            "localization": self.localization.to_dict(),
            "source": self.source,
            "source_dataset": self.source_dataset,
            "source_path": self.source_path,
            "diagnostics": _jsonable(self.diagnostics),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "LocalizationProfile":
        localization = PatternLocalization.from_dict(value.get("localization"))
        if localization is None:
            raise ValueError("Locked localization profile has no localization")
        return cls(
            id=str(value["id"]),
            name=str(value.get("name") or "Localization"),
            detector=str(value.get("detector") or "unknown"),
            localization=localization,
            source=str(value.get("source") or "unknown"),
            source_dataset=str(value.get("source_dataset") or ""),
            source_path=str(value.get("source_path") or ""),
            diagnostics=dict(value.get("diagnostics") or {}),
            created_at=str(value.get("created_at") or _now()),
            updated_at=str(value.get("updated_at") or value.get("created_at") or _now()),
        )


@dataclass(frozen=True)
class OrientationLock:
    detector: str
    orientation: ScanOrientation
    source: str
    source_dataset: str = ""
    source_path: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "detector": self.detector,
            "orientation": self.orientation.to_literal(),
            "source": self.source,
            "source_dataset": self.source_dataset,
            "source_path": self.source_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "OrientationLock":
        return cls(
            detector=str(value.get("detector") or "unknown"),
            orientation=ScanOrientation.from_literal(str(value["orientation"])),
            source=str(value.get("source") or "unknown"),
            source_dataset=str(value.get("source_dataset") or ""),
            source_path=str(value.get("source_path") or ""),
            created_at=str(value.get("created_at") or _now()),
            updated_at=str(value.get("updated_at") or value.get("created_at") or _now()),
        )


@dataclass(frozen=True)
class TransientLocalization:
    detector: str
    localization: PatternLocalization
    source: str
    source_dataset: str = ""
    source_path: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransientOrientation:
    detector: str
    orientation: ScanOrientation
    source: str
    source_dataset: str = ""
    source_path: str = ""


@dataclass(frozen=True)
class RecoveryCandidate:
    path: Path
    session_id: str
    updated_at: str
    localization_count: int
    orientation_count: int


class MonalisaSessionRegistry:
    """Source of truth for session-locked MoNaLISA calibration."""

    def __init__(self, sessions_root: str | os.PathLike | None = None):
        root = sessions_root or getattr(
            dirtools.UserFileDirs,
            "Sessions",
            os.path.join(dirtools.UserFileDirs.Root, "sessions"),
        )
        self.sessions_root = Path(root)
        self.session_id = uuid.uuid4().hex
        self.session_dir = self.sessions_root / self.session_id
        self.journal_path = self.session_dir / JOURNAL_FILENAME
        self._lock = threading.RLock()
        self._localizations: dict[str, LocalizationProfile] = {}
        self._orientations: dict[str, OrientationLock] = {}
        self._current_localizations: dict[str, TransientLocalization] = {}
        self._current_orientations: dict[str, TransientOrientation] = {}
        self._listeners: list[Callable[[], None]] = []
        self._journal_exists = False
        self._recovery_prompted = False
        self._completed = False
        atexit.register(self.complete_session)

    # --------------------------------------------------------------- listeners
    def add_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _notify(self) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception:
                pass

    # --------------------------------------------------------- transient state
    def set_current_localization(
        self,
        detector: str,
        localization: PatternLocalization,
        *,
        source: str,
        source_dataset: str = "",
        source_path: str = "",
        diagnostics: dict | None = None,
    ) -> None:
        detector = str(detector or "unknown")
        with self._lock:
            self._current_localizations[detector] = TransientLocalization(
                detector=detector,
                localization=localization,
                source=str(source),
                source_dataset=str(source_dataset or ""),
                source_path=str(source_path or ""),
                diagnostics=dict(diagnostics or {}),
            )

    def current_localization(self, detector: str) -> TransientLocalization | None:
        with self._lock:
            return self._current_localizations.get(str(detector or "unknown"))

    def set_current_orientation(
        self,
        detector: str,
        orientation: ScanOrientation | str,
        *,
        source: str,
        source_dataset: str = "",
        source_path: str = "",
    ) -> None:
        detector = str(detector or "unknown")
        canonical = (
            orientation
            if isinstance(orientation, ScanOrientation)
            else ScanOrientation.from_literal(str(orientation))
        )
        with self._lock:
            self._current_orientations[detector] = TransientOrientation(
                detector=detector,
                orientation=canonical,
                source=str(source),
                source_dataset=str(source_dataset or ""),
                source_path=str(source_path or ""),
            )

    def current_orientation(self, detector: str) -> TransientOrientation | None:
        with self._lock:
            return self._current_orientations.get(str(detector or "unknown"))

    # -------------------------------------------------------------- locked API
    def localization_profiles(self, detector: str | None = None) -> tuple[LocalizationProfile, ...]:
        with self._lock:
            values = tuple(self._localizations.values())
        if detector is not None:
            detector = str(detector)
            values = tuple(value for value in values if value.detector == detector)
        return tuple(sorted(values, key=lambda value: (value.detector, value.name.lower(), value.created_at)))

    def localization_profile(self, profile_id: str | None) -> LocalizationProfile | None:
        if not profile_id:
            return None
        with self._lock:
            return self._localizations.get(str(profile_id))

    def lock_localization(
        self,
        *,
        name: str,
        detector: str,
        localization: PatternLocalization,
        source: str,
        source_dataset: str = "",
        source_path: str = "",
        diagnostics: dict | None = None,
    ) -> LocalizationProfile:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Locked localization needs a name")
        detector = str(detector or "unknown")
        timestamp = _now()
        profile = LocalizationProfile(
            id=uuid.uuid4().hex,
            name=name,
            detector=detector,
            localization=localization,
            source=str(source),
            source_dataset=str(source_dataset or ""),
            source_path=str(source_path or ""),
            diagnostics=dict(diagnostics or {}),
            created_at=timestamp,
            updated_at=timestamp,
        )
        with self._lock:
            self._localizations[profile.id] = profile
            self._write_active_locked()
        self._notify()
        return profile

    def rename_localization(self, profile_id: str, name: str) -> LocalizationProfile:
        name = str(name or "").strip()
        if not name:
            raise ValueError("Locked localization needs a name")
        with self._lock:
            old = self._localizations[str(profile_id)]
            profile = LocalizationProfile(
                id=old.id,
                name=name,
                detector=old.detector,
                localization=old.localization,
                source=old.source,
                source_dataset=old.source_dataset,
                source_path=old.source_path,
                diagnostics=old.diagnostics,
                created_at=old.created_at,
                updated_at=_now(),
            )
            self._localizations[profile.id] = profile
            self._write_active_locked()
        self._notify()
        return profile

    def delete_localization(self, profile_id: str) -> None:
        with self._lock:
            self._localizations.pop(str(profile_id), None)
            self._write_active_locked()
        self._notify()

    def locked_orientation(self, detector: str) -> OrientationLock | None:
        with self._lock:
            return self._orientations.get(str(detector or "unknown"))

    def lock_orientation(
        self,
        detector: str,
        orientation: ScanOrientation | str,
        *,
        source: str,
        source_dataset: str = "",
        source_path: str = "",
    ) -> OrientationLock:
        detector = str(detector or "unknown")
        canonical = (
            orientation
            if isinstance(orientation, ScanOrientation)
            else ScanOrientation.from_literal(str(orientation))
        )
        timestamp = _now()
        with self._lock:
            previous = self._orientations.get(detector)
            entry = OrientationLock(
                detector=detector,
                orientation=canonical,
                source=str(source),
                source_dataset=str(source_dataset or ""),
                source_path=str(source_path or ""),
                created_at=previous.created_at if previous else timestamp,
                updated_at=timestamp,
            )
            self._orientations[detector] = entry
            self._write_active_locked()
        self._notify()
        return entry

    def clear_orientation(self, detector: str) -> None:
        with self._lock:
            self._orientations.pop(str(detector or "unknown"), None)
            self._write_active_locked()
        self._notify()

    # --------------------------------------------------------------- recovery
    @property
    def recovery_prompted(self) -> bool:
        return self._recovery_prompted

    def mark_recovery_prompted(self) -> None:
        self._recovery_prompted = True

    def recovery_candidates(self) -> tuple[RecoveryCandidate, ...]:
        if not self.sessions_root.exists():
            return ()
        found = []
        for path in self.sessions_root.glob(f"*/{JOURNAL_FILENAME}"):
            if path == self.journal_path:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("format") != JOURNAL_FORMAT or payload.get("version") != JOURNAL_VERSION:
                continue
            if payload.get("status") != "active":
                continue
            found.append(RecoveryCandidate(
                path=path,
                session_id=str(payload.get("session_id") or path.parent.name),
                updated_at=str(payload.get("updated_at") or ""),
                localization_count=len(payload.get("localizations") or []),
                orientation_count=len(payload.get("orientations") or []),
            ))
        return tuple(sorted(found, key=lambda value: value.updated_at, reverse=True))

    def restore(self, candidate: RecoveryCandidate | str | os.PathLike) -> None:
        path = candidate.path if isinstance(candidate, RecoveryCandidate) else Path(candidate)
        payload = json.loads(path.read_text(encoding="utf-8"))
        localizations = {
            entry.id: entry
            for entry in (
                LocalizationProfile.from_dict(item)
                for item in (payload.get("localizations") or [])
            )
        }
        orientations = {
            entry.detector: entry
            for entry in (
                OrientationLock.from_dict(item)
                for item in (payload.get("orientations") or [])
            )
        }
        with self._lock:
            self._localizations.update(localizations)
            self._orientations.update(orientations)
            self._write_active_locked()
        self._mark_external(path, "restored")
        self._notify()

    def discard(self, candidate: RecoveryCandidate | str | os.PathLike) -> None:
        path = candidate.path if isinstance(candidate, RecoveryCandidate) else Path(candidate)
        self._mark_external(path, "discarded")

    # --------------------------------------------------------------- journal
    def _payload_locked(self, status: str) -> dict:
        return {
            "format": JOURNAL_FORMAT,
            "version": JOURNAL_VERSION,
            "status": str(status),
            "session_id": self.session_id,
            "updated_at": _now(),
            "localizations": [value.to_dict() for value in self._localizations.values()],
            "orientations": [value.to_dict() for value in self._orientations.values()],
        }

    @staticmethod
    def _atomic_write(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_jsonable(payload), handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    def _write_active_locked(self) -> None:
        if self._completed:
            return
        # Do not create empty journals merely because ImProcess was opened.
        if not self._localizations and not self._orientations and not self._journal_exists:
            return
        self._atomic_write(self.journal_path, self._payload_locked("active"))
        self._journal_exists = True

    def _mark_external(self, path: Path, status: str) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["status"] = str(status)
            payload["updated_at"] = _now()
            self._atomic_write(path, payload)
        except Exception:
            pass

    def complete_session(self) -> None:
        with self._lock:
            if self._completed:
                return
            self._completed = True
            if self._journal_exists or self.journal_path.exists():
                try:
                    self._atomic_write(self.journal_path, self._payload_locked("completed"))
                except Exception:
                    pass


__all__ = [
    "JOURNAL_FILENAME",
    "LocalizationProfile",
    "MonalisaSessionRegistry",
    "OrientationLock",
    "RecoveryCandidate",
    "TransientLocalization",
    "TransientOrientation",
    "detector_identity",
]
