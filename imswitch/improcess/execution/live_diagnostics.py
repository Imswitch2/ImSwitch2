"""Temporary, opt-in diagnostics for testing live-to-finite error recovery.

Configured from the Session GUI at runtime. The one-shot fault is claimed only
by an explicitly live-following job; logging continues until stopped so a
subsequent *finite* retry appears in the same trace. No settings are persisted.
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
from threading import RLock

_MODES = {"off", "begin", "reader", "after_t0"}
_lock = RLock()
_logger = logging.getLogger("imswitch.improcess.live_failure_diagnostics")
_logger.propagate = False
_logger.setLevel(logging.INFO)
_handler: logging.FileHandler | None = None
_path: Path | None = None
_last_path: Path | None = None
_armed = "off"
_last_claim = ""


def state() -> dict:
    with _lock:
        return {
            "active": _handler is not None,
            "path": str(_path or _last_path or ""),
            "armed": _armed,
            "last_claim": _last_claim,
        }


def start() -> str:
    """Start a new diagnostic log; never write to a raw acquisition folder."""
    global _handler, _path, _last_path, _armed, _last_claim
    with _lock:
        if _handler is not None:
            return str(_path)
        folder = Path.home() / "Documents" / "ImSwitchDiagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"improcess_live_{datetime.now():%Y%m%d_%H%M%S_%f}.log"
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d [%(threadName)s] %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        _logger.addHandler(handler)
        _handler = handler
        _path = _last_path = path
        _armed = "off"
        _last_claim = ""
        _logger.info("diagnostics.started path=%s", path)
        return str(path)


def arm(mode: str) -> None:
    global _armed
    mode = str(mode)
    if mode not in _MODES:
        raise ValueError(f"Unsupported diagnostic mode: {mode}")
    with _lock:
        if _handler is None:
            raise RuntimeError("Start a diagnostic log first")
        _armed = mode
        _logger.info("fault.configured next_live_job=%s", mode)


def stop() -> None:
    global _handler, _path, _armed
    with _lock:
        _armed = "off"
        if _handler is None:
            return
        _logger.info("diagnostics.stopped")
        _logger.removeHandler(_handler)
        _handler.close()
        _handler = None
        _path = None


def claim_live(job_id: str) -> str:
    """Consume an armed fault ONCE, only for the selected live-follow runner."""
    global _armed, _last_claim
    with _lock:
        mode = _armed if _handler is not None else "off"
        _armed = "off"
        if mode != "off":
            _last_claim = f"{mode} for job {job_id}"
            _logger.info("fault.claimed job=%s mode=%s", job_id, mode)
        return mode


def event(name: str, **fields) -> None:
    """Low-volume, thread-safe lifecycle event; no raw pixels/params logged."""
    with _lock:
        if _handler is None:
            return
        detail = " ".join(f"{k}={str(v)!r}" for k, v in fields.items())
        _logger.info("%s %s", name, detail)


def exception(name: str, exc: BaseException, **fields) -> None:
    with _lock:
        if _handler is None:
            return
        detail = " ".join(f"{k}={str(v)!r}" for k, v in fields.items())
        _logger.error("%s %s", name, detail,
                      exc_info=(type(exc), exc, exc.__traceback__))


def injected(mode: str, job_id: str) -> None:
    event("fault.triggered", mode=mode, job=job_id)


def log_lapse_inventory(source, *, scope=None, units=()):
    """Compare the Session's cached lapse index with actual scanN folders.

    Diagnostic-only, single shallow directory listing per finite job. This
    never changes the source, discovery, scheduling, or readiness decisions.
    """
    if not state()["active"]:
        return
    try:
        index = getattr(source, "sourceMetadata", None)
        planned = getattr(index, "planned_count", None)
        if planned is None:
            planned = getattr(source, "planned_timepoints", None)
        candidates = []
        candidate_paths = []
        if index is not None:
            try:
                ch = index.channel(index.anchor_detector)
                for item in ch.candidates:
                    candidates.append(int(item.ordinal) - int(index.anchor.ordinal)
                                      + int(index.anchor_index))
                    candidate_paths.append(Path(str(item.path)))
            except Exception as exc:
                event("finite.inventory_catalog_error", reason=str(exc))
        for ref in getattr(source, "source_refs", ()) or ():
            if getattr(ref, "timepoint_index", None) is not None:
                candidates.append(int(ref.timepoint_index))
            if getattr(ref, "path", None):
                candidate_paths.append(Path(str(ref.path)))
        if getattr(source, "dataPath", None):
            candidate_paths.append(Path(str(source.dataPath)))
        roots = set()
        for path in candidate_paths:
            for candidate in (path, *path.parents):
                if candidate.suffix.lower() == ".zarr":
                    roots.add(candidate)
                    break
        physical = {}
        for root in sorted(roots, key=str):
            try:
                physical[str(root)] = sorted(
                    child.name for child in root.iterdir()
                    if child.is_dir() and child.name.startswith("scan")
                    and child.name[4:].isdigit()
                )
            except OSError as exc:
                physical[str(root)] = f"UNREADABLE: {exc}"
        event("finite.lapse_inventory", scope=scope, planned=planned,
              catalog_indices=sorted(set(candidates)), physical_scan_groups=physical,
              selected_units=[str(getattr(obj, "_improcess_timepoint_scope", ""))
                              for obj in units])
    except Exception as exc:
        exception("finite.lapse_inventory_failed", exc)
