"""Interactive runs: what the editor keeps between runs, and what to run next. Qt-free.

A :class:`RunSession` holds one document's results in a cache keyed by
**fingerprint**: a hash of everything a step's output depends on -- its kind,
plugin and plugin version, effective parameters (a Python step's code is one),
ROI restriction, and the fingerprints of what it reads, port by port. Step
ids are not part of it, so a rename outdates nothing. A source's fingerprint
is its spec with the path resolved, plus the file's size and modification
time (a folder's modification time, for Zarr: a chunk rewritten in place
without touching the folder goes unnoticed, which *Re-run from here* is for).

From that, everything else follows without bookkeeping of its own:

- a step is **done** when the cache holds results for its current
  fingerprint, **outdated** when it holds them for an older one, **not run**
  when it holds none; editing a parameter changes that step's fingerprint and
  so every fingerprint after it;
- **Run to here** binds the cached ancestors and runs the rest
  (:meth:`plan`), and the cache stores what the run made under the
  fingerprints computed when it was *planned*, never recomputed, so an edit
  made during the run cannot make an old result look fresh;
- a step declared ``deterministic = False`` (the Python step) gets a new
  nonce in its fingerprint each time it is recomputed, which outdates every
  cached result after it: a partial re-run never mixes two realisations.

**Open files.** A cached result may read lazily from the source handle its
run opened. The session takes the handles from every report
(``detach_sources``) and gives each cache entry the handles of its own run
and of the entries it was computed from; a handle is closed when no entry
needs it any more. Evicting an entry keeps its preview (so an outdated box
still shows its old picture) and releases its data and handles.

**Memory.** Results the cache holds count against ``workflowCacheBytes``
(``memory.workflowCacheMB``); above it, outdated entries go first, then the
least recently used. The selected step's results and pinned ones are
exempt, and a lazily read result counts only what it holds in memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from imswitch.improcess.model.workfloweditor.graph import ancestors, descendants, refs_of
from imswitch.improcess.model.workfloweditor.previews import make_preview, nbytes_in_memory
from imswitch.improcess.workflows.steps import (
    Consolidate,
    Process,
    Reconstruct,
    Save,
    Source,
    Workflow,
)

#: The limit's default when none is configured: 4 GiB.
DEFAULT_CACHE_BYTES = 4 * 1024 ** 3
CACHE_LIMIT = "workflowCacheBytes"


def cache_budget() -> int:
    from imswitch.imcommon.model import memory_limits

    try:
        return memory_limits.effectiveBytes(CACHE_LIMIT, DEFAULT_CACHE_BYTES)
    except KeyError:
        return DEFAULT_CACHE_BYTES


@dataclass
class Entry:
    """One step's results for one fingerprint."""

    key: str
    step_id: str
    ports: dict | None
    previews: dict
    seconds: float
    nbytes: int = 0
    handles: list = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)
    pinned: set = field(default_factory=set)
    run_id: int = 0

    @property
    def held(self) -> bool:
        return self.ports is not None


@dataclass
class StepState:
    """What a box says about a step."""

    status: str = ""                # "" | done | outdated | running | waiting | failed | would-write
    entry: Entry | None = None
    error: dict | None = None
    would_write: Path | None = None
    wrote: tuple = ()
    cached: bool = False            # the last run took it from the cache
    bound: str = ""                 # what a source or a step is bound to in this session


@dataclass
class RunPlan:
    """What one run will do: a workflow to hand the runner, and what binds it."""

    run_id: int
    workflow: Workflow
    bindings: dict
    keys: dict
    to_run: list
    cached: list
    targets: list
    out_dir: Path
    overwrite: bool
    write_saves: bool
    source_stem: str | None
    source_root: str | None

    @property
    def empty(self) -> bool:
        """Nothing to run: everything asked for is already in the cache."""
        return not self.to_run


@dataclass
class RunSummary:
    """What the last run did, for the status bar."""

    ran: list = field(default_factory=list)
    cached: list = field(default_factory=list)
    seconds: float = 0.0
    would_write: dict = field(default_factory=dict)
    wrote: dict = field(default_factory=dict)
    failure: dict | None = None
    error: str | None = None
    cancelled: bool = False


class RunSession:
    """See the module docstring."""

    def __init__(self, registry=None, *, budget: int | None = None):
        self.registry = registry
        self._budget = budget
        self._cache: dict[str, Entry] = {}
        self._last_key: dict[str, str] = {}
        self._nonces: dict[str, str] = {}
        self._handle_refs: dict[int, list] = {}         # id(handle) -> [handle, count]
        self._failed: dict[str, tuple[str, dict]] = {}  # step id -> (key, failure)
        self._running: str | None = None
        self._waiting: set[str] = set()
        self._cached_last: set[str] = set()
        self._would_write: dict[str, tuple[str, Path]] = {}
        self._wrote: dict[str, tuple[str, tuple]] = {}
        self._run_id = 0
        self._plan: RunPlan | None = None
        self._created: list[str] = []
        self.source_bindings: dict[str, str] = {}
        self.result_bindings: dict[str, object] = {}
        self.summary: RunSummary | None = None
        self.closed = False

    # -- fingerprints ------------------------------------------------------------------

    @property
    def budget(self) -> int:
        return int(self._budget) if self._budget is not None else cache_budget()

    def _plugin(self, step):
        registry = self.registry
        if registry is None:
            return None
        if isinstance(step, (Reconstruct, Consolidate)):
            return registry.get_reconstructor(step.reconstructor, raise_on_missing=False)
        if isinstance(step, Process):
            return registry.get_processor(step.processor, raise_on_missing=False)
        return None

    def deterministic(self, step) -> bool:
        plugin = self._plugin(step)
        return bool(getattr(plugin, "deterministic", True)) if plugin is not None else True

    def _source_path(self, step: Source, source_root) -> str | None:
        bound = self.source_bindings.get(step.id)
        if bound:
            return str(bound)
        spec = step.source.resolved(source_root) if source_root else step.source
        return str(spec.path) if spec.path else None

    def _source_print(self, step: Source, source_root) -> dict:
        path = self._source_path(step, source_root)
        spec = step.source
        stat = None
        if path:
            try:
                info = os.stat(path)
                stat = [int(info.st_size), int(info.st_mtime_ns)]
            except OSError:
                stat = None
        return {"kind": "source", "path": os.path.abspath(path) if path else None, "dataset": spec.dataset,
                "source_kind": spec.source_kind, "stat": stat}

    def fingerprints(self, document, source_root=None) -> dict[str, str]:
        """Every step's fingerprint, with the nonces assigned so far."""
        prints: dict[str, str] = {}
        for step in document.steps:
            prints[step.id] = self._fingerprint(document, step, prints, source_root)
        return prints

    def _base(self, document, step, prints, source_root) -> dict:
        if isinstance(step, Source):
            return self._source_print(step, source_root)
        if step.id in self.result_bindings:
            result = self.result_bindings[step.id]
            return {"kind": "bound", "uid": str(getattr(result, "result_uid", "") or id(result))}
        inputs = [[prints.get(ref.step, f"missing:{ref.step}"), ref.port or ""] for ref in refs_of(step)]
        if isinstance(step, Save):
            return {"kind": "save", "fmt": step.fmt, "template": step.path_template, "inputs": inputs}
        entry = document.entry_for(step)
        plugin = getattr(step, "processor", None) or getattr(step, "reconstructor", "")
        return {
            "kind": step.kind, "plugin": plugin, "version": entry.version if entry is not None else "",
            "params": document.effective_params(step.id) if document.has(step.id) else dict(step.params),
            "restriction": getattr(step, "restriction", None), "inputs": inputs,
        }

    def _fingerprint(self, document, step, prints, source_root) -> str:
        base = _digest(self._base(document, step, prints, source_root))
        if isinstance(step, (Source, Save)) or step.id in self.result_bindings or self.deterministic(step):
            return base
        return _digest({"base": base, "nonce": self._nonces.get(base, "unrun")})

    def _base_key(self, document, step, prints, source_root) -> str:
        return _digest(self._base(document, step, prints, source_root))

    # -- states ------------------------------------------------------------------------

    def states(self, document, source_root=None, *, write_saves: bool = False) -> dict[str, StepState]:
        prints = self.fingerprints(document, source_root)
        found: dict[str, StepState] = {}
        for step in document.steps:
            key = prints[step.id]
            state = StepState(cached=step.id in self._cached_last)
            if isinstance(step, Source):
                state.bound = self.source_bindings.get(step.id, "")
            elif step.id in self.result_bindings:
                state.bound = str(getattr(self.result_bindings[step.id], "name", "result"))
            entry = self._cache.get(key)
            last = self._cache.get(self._last_key.get(step.id, ""))
            if step.id == self._running:
                state.status = "running"
                state.entry = entry or last
            elif step.id in self._waiting:
                state.status = "waiting"
                state.entry = entry or last
            elif isinstance(step, Save):
                wrote = self._wrote.get(step.id)
                would = self._would_write.get(step.id)
                if wrote is not None and wrote[0] == key:
                    state.status, state.wrote = "done", wrote[1]
                elif would is not None:
                    state.status, state.would_write = "would-write", would[1]
            elif entry is not None and (entry.held or isinstance(step, Source)):
                state.status, state.entry = "done", entry
            elif last is not None and last.key != key:
                # An older result, kept (perhaps only as its preview) until evicted.
                state.status, state.entry = "outdated", last
            if step.id in self._failed and state.status not in ("running", "waiting"):
                failed_key, failure = self._failed[step.id]
                if failed_key == key:
                    state.status, state.error = "failed", failure
            found[step.id] = state
        return found

    def outdated(self, document, source_root=None) -> list[str]:
        return [sid for sid, state in self.states(document, source_root).items() if state.status == "outdated"]

    def entry_for(self, document, step_id: str, source_root=None) -> Entry | None:
        """The entry a box shows: its current one, else its last (outdated) one."""
        prints = self.fingerprints(document, source_root)
        key = prints.get(step_id)
        entry = self._cache.get(key) if key else None
        return entry or self._cache.get(self._last_key.get(step_id, ""))

    # -- planning ----------------------------------------------------------------------

    def plan(self, document, targets=None, *, write_saves: bool = False, out_dir=None, overwrite: bool = False,
             source_root=None) -> RunPlan:
        """What to run so that ``targets`` (default: every step) are done."""
        steps = list(document.steps)
        if targets:
            wanted = set(targets)
            for target in targets:
                wanted |= ancestors(steps, target)
        else:
            wanted = {step.id for step in steps}
            targets = [step.id for step in steps if not descendants(steps, step.id)]
        sub = [step for step in steps if step.id in wanted]
        renewed: set[str] = set()
        while True:
            prints = self.fingerprints(document, source_root)
            cached = {step.id for step in sub if self._bindable(step) and self._held(prints[step.id])}
            cached |= {step.id for step in sub if step.id in self.result_bindings}
            runs = self._runs(sub, set(targets), cached)
            fresh = [step for step in sub if step.id in runs and step.id not in renewed
                     and not isinstance(step, (Source, Save)) and step.id not in self.result_bindings
                     and not self.deterministic(step)]
            if not fresh:
                break
            for step in fresh:
                base = self._base_key(document, step, prints, source_root)
                self._nonces[base] = uuid.uuid4().hex
                renewed.add(step.id)
        bindings = {}
        for step in sub:
            if step.id in self.result_bindings:
                bindings[step.id] = self.result_bindings[step.id]
            elif step.id in cached:
                entry = self._cache[prints[step.id]]
                entry.last_used = time.monotonic()
                bindings[step.id] = dict(entry.ports)
            elif isinstance(step, Source) and step.id in self.source_bindings:
                bindings[step.id] = str(self.source_bindings[step.id])
        workflow = Workflow.from_dict({**document.workflow.to_dict(), "steps": [s.to_dict() for s in sub]})
        self._run_id += 1
        return RunPlan(
            run_id=self._run_id, workflow=workflow, bindings=bindings, keys=dict(prints),
            to_run=[step.id for step in sub if step.id in runs],
            cached=[step.id for step in sub if step.id in cached],
            targets=list(targets),
            out_dir=Path(out_dir) if out_dir else Path(tempfile.gettempdir()) / "imswitch-workflow-preview",
            overwrite=bool(overwrite), write_saves=bool(write_saves),
            source_stem=self._stem(document, source_root), source_root=source_root,
        )

    @staticmethod
    def _bindable(step) -> bool:
        return not isinstance(step, (Source, Save))

    def _held(self, key: str) -> bool:
        entry = self._cache.get(key)
        return entry is not None and entry.held

    @staticmethod
    def _runs(sub, targets: set, cached: set) -> set:
        """Steps the run must compute: not cached, and needed by a target
        through steps that are not cached either."""
        needed: set[str] = set()
        consumers: dict[str, set[str]] = {}
        for step in sub:
            for ref in refs_of(step):
                consumers.setdefault(ref.step, set()).add(step.id)
        for step in reversed(sub):
            if step.id in cached:
                continue
            if step.id in targets or consumers.get(step.id, set()) & needed:
                needed.add(step.id)
        return needed

    def _stem(self, document, source_root) -> str | None:
        """Name the run's files: after the result a step was started from, as
        *Run on selected results* does, else after the first recording whether
        or not the run opens it (a cached reconstruction does not)."""
        from imswitch.improcess.workflows.runner import source_stem_of

        for result in self.result_bindings.values():
            return str(getattr(result, "name", "") or "") or None
        for step in document.steps:
            if isinstance(step, Source):
                path = self._source_path(step, source_root)
                return source_stem_of(path) if path else None
        return None

    # -- a run -------------------------------------------------------------------------

    def begin(self, plan: RunPlan) -> None:
        self._plan = plan
        self._created = []
        self._running = None
        self._waiting = set(plan.to_run)
        self._cached_last = set()
        for step_id in plan.to_run:
            self._failed.pop(step_id, None)
        self.summary = RunSummary()

    def step_started(self, step_id: str) -> None:
        if self._plan is None:
            return
        self._waiting.discard(step_id)
        self._running = step_id if step_id in self._plan.to_run else None

    def step_done(self, step_id: str, ports: dict, seconds: float, cached: bool, previews: dict | None = None) -> None:
        """A step finished (on the worker); store what it made under the
        fingerprint it had when the run was planned."""
        plan = self._plan
        if plan is None or step_id not in plan.keys:
            return
        self._waiting.discard(step_id)
        if self._running == step_id:
            self._running = None
        key = plan.keys[step_id]
        self._last_key[step_id] = key
        summary = self.summary
        if cached:
            self._cached_last.add(step_id)
            if summary is not None:
                summary.cached.append(step_id)
            entry = self._cache.get(key)
            if entry is not None:
                entry.last_used = time.monotonic()
            return
        if summary is not None:
            summary.ran.append(step_id)
            summary.seconds += float(seconds)
        step = plan.workflow.step(step_id)
        if previews is None:
            previews = {port: make_preview(result) for port, result in ports.items()}
        keep = None if isinstance(step, Source) else dict(ports)
        old = self._cache.get(key)
        if old is not None:
            self._drop(old)
        entry = Entry(key=key, step_id=step_id, ports=keep, previews=dict(previews), seconds=float(seconds),
                      nbytes=sum(nbytes_in_memory(r) for r in ports.values()) if keep is not None else 0,
                      run_id=plan.run_id)
        self._cache[key] = entry
        if keep is not None:
            self._created.append(key)

    def finish(self, report, error: str | None = None, document=None, keep=()) -> RunSummary:
        """The run ended: adopt its open files, note what Saves wrote or would
        have, and what failed."""
        plan, self._plan = self._plan, None
        summary = self.summary or RunSummary()
        self._running = None
        self._waiting = set()
        if plan is None:
            return summary
        summary.error = error
        handles = list(report.detach_sources()) if report is not None else []
        inherited = []
        for step_id in plan.cached:
            entry = self._cache.get(plan.keys.get(step_id, ""))
            if entry is not None:
                inherited.extend(entry.handles)
        created = [self._cache[key] for key in self._created if key in self._cache]
        for entry in created:
            for handle in _unique(handles + inherited):
                self._hold(entry, handle)
        if handles and not created:
            for handle in handles:
                self._close(handle)
        if report is not None:
            for step_id, path in getattr(report, "would_write", {}).items():
                self._would_write[step_id] = (plan.keys.get(step_id, ""), Path(path))
                summary.would_write[step_id] = Path(path)
            written = [sid for sid in report.steps_run if isinstance(_step_or_none(plan.workflow, sid), Save)
                       and sid not in report.would_write]
            for step_id, receipt in zip(written, report.receipts):
                files = tuple(Path(f) for f in getattr(receipt, "files", ()) or ())
                self._wrote[step_id] = (plan.keys.get(step_id, ""), files)
                summary.wrote[step_id] = files
                self._would_write.pop(step_id, None)
            failure = getattr(report, "failure", None)
            if failure:
                summary.failure = dict(failure)
                summary.cancelled = failure.get("type") == "OperationCancelled" or failure.get("message") == "cancelled"
                step_id = failure.get("step")
                if step_id in plan.keys and not summary.cancelled:
                    self._failed[step_id] = (plan.keys[step_id], dict(failure))
        elif error:
            summary.cancelled = error == "cancelled"
        if error and not summary.failure and "cancelled" in str(error):
            summary.cancelled = True
        self.summary = summary
        if document is not None:
            self.trim(document, keep=keep)
        return summary

    # -- what the user asks for -------------------------------------------------------

    def rerun_from(self, document, step_id: str, source_root=None) -> list[str]:
        """Forget the cached results of ``step_id`` and everything after it;
        returns the step ids whose results were dropped."""
        prints = self.fingerprints(document, source_root)
        doomed = {step_id} | descendants(document.steps, step_id)
        dropped = []
        for sid in doomed:
            entry = self._cache.get(prints.get(sid, ""))
            if entry is not None and not entry.pinned:
                self._evict(entry, keep_preview=True)
                dropped.append(sid)
        step = document.step(step_id) if document.has(step_id) else None
        if step is not None and not isinstance(step, (Source, Save)) and not self.deterministic(step):
            self._nonces.pop(self._base_key(document, step, prints, source_root), None)
        return sorted(dropped)

    def bind_source(self, document, step_id: str, path: str | None, source_root=None) -> None:
        """Run ``step_id`` on ``path`` in this session (``None`` unbinds); the
        file keeps its own path. Results of the old binding are released."""
        before = self.fingerprints(document, source_root)
        if path:
            self.source_bindings[step_id] = str(path)
        else:
            self.source_bindings.pop(step_id, None)
        self._release_below(document, step_id, before)

    def bind_result(self, document, step_id: str, result, source_root=None) -> None:
        """Start ``step_id`` from an in-memory result (``None`` unbinds), as
        *Run on selected results* does: the step and what fed it are not run."""
        before = self.fingerprints(document, source_root)
        if result is not None:
            self.result_bindings[step_id] = result
        else:
            self.result_bindings.pop(step_id, None)
        self._release_below(document, step_id, before)

    def _release_below(self, document, step_id: str, before: dict) -> None:
        for sid in {step_id} | descendants(document.steps, step_id):
            entry = self._cache.get(before.get(sid, ""))
            if entry is not None and not entry.pinned:
                self._evict(entry, keep_preview=True)

    def pin(self, document, step_id: str, port: str | None = None, source_root=None):
        """The result to publish for ``step_id`` (its first port, or ``port``),
        marked so the cache never drops it; ``None`` when it is not held."""
        entry = self._cache.get(self.fingerprints(document, source_root).get(step_id, ""))
        if entry is None or not entry.held or not entry.ports:
            return None
        port = port if port in entry.ports else next(iter(entry.ports))
        entry.pinned.add(port)
        return entry.ports[port]

    def result(self, document, step_id: str, port: str | None = None, source_root=None):
        entry = self._cache.get(self.fingerprints(document, source_root).get(step_id, ""))
        if entry is None or not entry.held or not entry.ports:
            return None
        return entry.ports.get(port) if port else next(iter(entry.ports.values()))

    # -- memory ------------------------------------------------------------------------

    def cache_bytes(self) -> int:
        return sum(entry.nbytes for entry in self._cache.values() if entry.held)

    def held_entries(self) -> int:
        return sum(1 for entry in self._cache.values() if entry.held)

    def trim(self, document, keep=(), source_root=None) -> list[str]:
        """Drop results until the cache fits its limit: outdated ones first,
        then the least recently used; never ``keep`` (step ids) or pinned ones."""
        prints = self.fingerprints(document, source_root)
        current = set(prints.values())
        protected = {prints[sid] for sid in keep if sid in prints}
        budget = self.budget
        dropped = []
        candidates = sorted(
            (entry for entry in self._cache.values() if entry.held and not entry.pinned and entry.key not in protected),
            key=lambda entry: (entry.key in current, entry.last_used),
        )
        for entry in candidates:
            if self.cache_bytes() <= budget:
                break
            if entry.nbytes <= 0 and entry.key in current:
                continue
            self._evict(entry, keep_preview=True)
            dropped.append(entry.step_id)
        return dropped

    def _evict(self, entry: Entry, *, keep_preview: bool) -> None:
        entry.ports = None
        entry.nbytes = 0
        for handle in list(entry.handles):
            self._release(handle)
        entry.handles = []
        if not keep_preview:
            self._cache.pop(entry.key, None)

    def _drop(self, entry: Entry) -> None:
        self._evict(entry, keep_preview=False)

    def _hold(self, entry: Entry, handle) -> None:
        if any(h is handle for h in entry.handles):
            return
        entry.handles.append(handle)
        slot = self._handle_refs.setdefault(id(handle), [handle, 0])
        slot[1] += 1

    def _release(self, handle) -> None:
        slot = self._handle_refs.get(id(handle))
        if slot is None:
            return
        slot[1] -= 1
        if slot[1] <= 0:
            del self._handle_refs[id(handle)]
            self._close(handle)

    @staticmethod
    def _close(handle) -> None:
        from imswitch.improcess.workflows.sources import close_source

        try:
            close_source(handle)
        except Exception:  # noqa: BLE001 - closing must not fail the editor
            pass

    def open_handles(self) -> list:
        return [slot[0] for slot in self._handle_refs.values()]

    def close(self) -> list[tuple[frozenset, list]]:
        """Release everything; returns ``(uids, handles)`` for the pinned
        results, whose handles the caller must keep open as long as they are
        loaded (the controller's retained list). Every other handle is closed."""
        handover = []
        kept: set[int] = set()
        for entry in self._cache.values():
            if entry.pinned and entry.held:
                uids = frozenset(str(getattr(entry.ports[port], "result_uid", "") or id(entry.ports[port]))
                                 for port in entry.pinned if port in entry.ports)
                handles = list(entry.handles)
                if handles:
                    handover.append((uids, handles))
                    kept |= {id(h) for h in handles}
        for slot in list(self._handle_refs.values()):
            if id(slot[0]) not in kept:
                self._close(slot[0])
        self._handle_refs = {}
        self._cache = {}
        self.closed = True
        return handover


def execute(plan: RunPlan, registry, *, on_step=None, progress=None, cancel=None, preview_size: int = 192):
    """Run ``plan`` on the calling thread; returns ``(report, error)``.

    ``on_step(step_id, ports, seconds, cached, previews)`` is called as each
    step finishes, with previews made here (a worker thread, in the editor).
    """
    from imswitch.improcess.workflows.runner import RunError, run

    def done(step_id, ports, seconds, cached):
        if on_step is None:
            return
        previews = {} if cached else {port: make_preview(result, preview_size) for port, result in ports.items()}
        on_step(step_id, ports, seconds, cached, previews)

    try:
        report = run(
            plan.workflow, registry=registry, bindings=plan.bindings, out_dir=plan.out_dir,
            overwrite=plan.overwrite, write_saves=plan.write_saves, source_stem=plan.source_stem,
            source_root=plan.source_root, progress=progress, cancel=cancel, on_step_done=done,
        )
    except RunError as exc:
        return getattr(exc, "report", None), str(exc)
    except Exception as exc:  # noqa: BLE001 - reported in the editor
        return None, str(exc)
    return report, None


def _digest(payload) -> str:
    text = json.dumps(payload, sort_keys=True, default=_jsonable)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _jsonable(value):
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:  # pragma: no cover
        pass
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    return repr(value)


def _unique(items: list) -> list:
    seen, found = set(), []
    for item in items:
        if id(item) not in seen:
            seen.add(id(item))
            found.append(item)
    return found


def _step_or_none(workflow, step_id):
    try:
        return workflow.step(step_id)
    except KeyError:
        return None


__all__ = ["CACHE_LIMIT", "DEFAULT_CACHE_BYTES", "Entry", "RunPlan", "RunSession", "RunSummary", "StepState",
           "cache_budget", "execute"]
