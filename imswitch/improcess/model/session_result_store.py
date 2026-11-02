"""Persistence boundary for derived results owned by a ProcessingSession.

The Session catalog decides *which raw dataset* a result belongs to.  This
store decides *where/how a derived result is written*.  Keeping that boundary
outside the Session UI controller gives normal and live processing one place to
converge in the next integration patches.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from imswitch.improcess.model.processing_session import result_role_for_result


FORMAT_SUFFIX = {
    "tiff": ".ome.tif",
    "hdf5": ".h5",
    "zarr": ".ome.zarr",
}


def _clean_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "result")).strip("_.")
    return text or "result"


class SessionResultStore:
    """Write derived results into one Session's results directory."""

    def __init__(self, session, *, save_format: str = "tiff") -> None:
        self.session = session
        self.save_format = save_format if save_format in FORMAT_SUFFIX else "tiff"

    @staticmethod
    def _short_component(value: str, limit: int) -> str:
        value = _clean_name(value)
        if len(value) <= limit:
            return value
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]
        keep = max(1, limit - len(digest) - 1)
        return f"{value[:keep].rstrip('._-')}-{digest}"

    @classmethod
    def autosave_stem(cls, dataset, result, display_name: str = "") -> str:
        source_path = Path(dataset.primary_path)
        source_stem = _clean_name(source_path.stem)
        label = str(
            display_name
            or getattr(result, "name", "reconstruction")
            or "reconstruction"
        )

        lowered = label.lower()
        for prefix in (source_path.name, source_path.stem):
            if lowered.startswith(prefix.lower()):
                label = label[len(prefix):].lstrip(" ._-:")
                break
        label = _clean_name(label or "recon")
        source_part = cls._short_component(source_stem, 36)
        result_part = cls._short_component(label, 18)
        return f"{source_part}_{result_part}"

    def save(self, result, dataset, display_name: str = ""):
        """Persist one derived Session result and return its save receipt."""
        if result_role_for_result(result) == "inspection":
            raise ValueError("Inspection results are not persisted as Session results")
        fmt = self.save_format if self.save_format in FORMAT_SUFFIX else "tiff"
        suffix = FORMAT_SUFFIX[fmt]
        stem = self.autosave_stem(dataset, result, display_name)
        base = self.session.results_root / f"{stem}{suffix}"
        candidate = base
        index = 1
        while candidate.exists():
            candidate = base.with_name(f"{stem}_{index}{suffix}")
            index += 1
        return result.save(candidate, fmt, overwrite=False)
