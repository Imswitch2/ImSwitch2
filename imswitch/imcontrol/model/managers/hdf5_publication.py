"""Publication boundary for disk HDF5 timelapses.

A staged file is an ordinary HDF5 file whose *last* suffix is ``.partial``;
Session's normal HDF5 discovery must not open it while acquisition is active.
Only the writer's owner publishes files, after all HDF5 handles are closed.
"""

from __future__ import annotations

import os
import threading


class Hdf5PublicationError(OSError):
    """The recording finalized, but its public filename could not be published."""


class Hdf5PublicationRegistry:
    """Per-RecordingManager public/staged paths and finalized-file ownership.

    Single-file lapses reuse one staged path across recording generations.
    Aborting a later generation must not discard an earlier finalized one.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._working: dict[str, str] = {}
        self._claims: dict[str, str] = {}
        self._finalized: set[str] = set()
        self._deferred: set[str] = set()

    @staticmethod
    def working_name(public_path: str) -> str:
        return f"{public_path}.partial"

    @classmethod
    def claim_name(cls, public_path: str) -> str:
        return f"{cls.working_name(public_path)}.claim"

    def available(self, public_path: str) -> bool:
        """Do not reuse either a published name or an abandoned stage."""
        public_path = str(public_path)
        with self._lock:
            return (
                public_path not in self._working
                and not os.path.lexists(public_path)
                and not os.path.lexists(self.working_name(public_path))
                and not os.path.lexists(self.claim_name(public_path))
            )

    def reserve(self, public_path: str) -> str:
        public_path = str(public_path)
        with self._lock:
            if not self.available(public_path):
                raise FileExistsError(f"HDF5 output already reserved: {public_path}")
            working = self.working_name(public_path)
            # Atomic cross-process reservation: two ImSwitch instances must
            # never append to the same growing single-file lapse. This tiny
            # sidecar also occupies no HDF5 handle and is ignored by Session.
            claim = self.claim_name(public_path)
            fd = os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            self._working[public_path] = working
            self._claims[public_path] = claim
            return working

    def working_path(self, public_path: str) -> str:
        with self._lock:
            return self._working[str(public_path)]

    def pending(self, public_path: str) -> bool:
        with self._lock:
            return str(public_path) in self._working

    def is_finalized(self, public_path: str) -> bool:
        with self._lock:
            return str(public_path) in self._finalized

    def mark_finalized(self, public_paths, *, deferred=False) -> None:
        with self._lock:
            for public_path in set(map(str, public_paths)):
                if public_path in self._working:
                    self._finalized.add(public_path)
                    if deferred:
                        self._deferred.add(public_path)

    @staticmethod
    def _publish_one(working: str, public: str) -> None:
        if os.path.lexists(public):
            raise FileExistsError(f"HDF5 destination already exists: {public}")
        if not os.path.isfile(working):
            raise FileNotFoundError(f"Finalized HDF5 working file missing: {working}")
        if os.name == "nt":
            # Windows rename is atomic on the same volume and refuses to
            # replace an existing target, unlike os.replace().
            os.rename(working, public)
        else:
            # POSIX rename would overwrite an existing target. Link with
            # O_EXCL semantics first, then remove the staged name. This
            # publishes atomically without permitting a competing overwrite.
            os.link(working, public)
            os.unlink(working)

    def publish(self, public_paths) -> tuple[str, ...]:
        published = []
        with self._lock:
            for public_path in dict.fromkeys(map(str, public_paths)):
                working = self._working.get(public_path)
                if working is None:
                    continue
                if public_path not in self._finalized:
                    raise Hdf5PublicationError(
                        f"Cannot publish HDF5 before writer finalization: {public_path}"
                    )
                try:
                    self._publish_one(working, public_path)
                except OSError as exc:
                    raise Hdf5PublicationError(
                        f"Finalized HDF5 retained at {working!r}; "
                        f"could not publish {public_path!r}: {exc}"
                    ) from exc
                del self._working[public_path]
                self._finalized.remove(public_path)
                self._deferred.discard(public_path)
                claim = self._claims.pop(public_path, None)
                if claim is not None:
                    try:
                        os.unlink(claim)
                    except OSError:
                        # The public HDF5 was successfully published. A stale
                        # claim is harmless and must not reclassify success.
                        pass
                published.append(public_path)
        return tuple(published)

    def publish_pending_finalized(self, public_paths=None) -> tuple[str, ...]:
        """Publish only this operation's finalized, shared single-file lapse."""
        with self._lock:
            pending = self._finalized & self._deferred
            if public_paths is not None:
                pending &= set(map(str, public_paths))
            return self.publish(tuple(sorted(pending)))

    def discard_missing_unfinalized(self) -> None:
        """Forget an aborted first point whose working file was deleted."""
        with self._lock:
            for public_path, working in tuple(self._working.items()):
                if public_path not in self._finalized and not os.path.lexists(working):
                    del self._working[public_path]
                    claim = self._claims.pop(public_path, None)
                    if claim is not None:
                        try:
                            os.unlink(claim)
                        except OSError:
                            pass
