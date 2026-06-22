"""Factory for creating LiveSource instances by format."""

from pathlib import Path
from typing import Any

from .sources import Hdf5LiveSource, LiveSource, ZarrLiveSource


def make_live_source(
    path: str | Path,
    *,
    fmt: str | None = None,
    detector_name: str | None = None,
    chunk_size: int | None = None,
) -> LiveSource:
    """
    Create a LiveSource instance for the given path and format.

    Args:
        path: Path to the live source (file or directory).
        fmt: Explicit format override. If None, inferred from path suffix.
             Supported: 'zarr', 'hdf5', 'h5'.
        detector_name: Optional detector name for structured layouts.
        chunk_size: Optional chunk size override for polling.

    Returns:
        LiveSource instance appropriate for the format.

    Raises:
        ValueError: If the format cannot be determined or is not supported.
        NotImplementedError: If the format is TIFF (out of scope for live sources).
    """
    path_obj = Path(path)

    if fmt is None:
        suffix = path_obj.suffix.lower()
        if suffix == '.zarr':
            fmt = 'zarr'
        elif suffix in {'.h5', '.hdf5', '.hdf'}:
            fmt = 'hdf5'
        elif suffix in {'.tif', '.tiff'}:
            raise NotImplementedError(
                "TIFF live sources are not supported. Use the batch watcher "
                "for TIFF file monitoring."
            )
        else:
            raise ValueError(
                f"Cannot determine format from path suffix '{suffix}'. "
                f"Provide an explicit fmt parameter ('zarr' or 'hdf5')."
            )

    fmt_lower = fmt.lower()

    if fmt_lower == 'zarr':
        return ZarrLiveSource(detector_name=detector_name, chunk_size=chunk_size)
    elif fmt_lower in {'hdf5', 'h5'}:
        return Hdf5LiveSource(detector_name=detector_name, chunk_size=chunk_size)
    else:
        raise ValueError(
            f"Unsupported format '{fmt}'. Supported formats: 'zarr', 'hdf5', 'h5'."
        )
