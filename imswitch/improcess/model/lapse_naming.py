"""Pure helpers for ImControl multi-file lapse naming.

Shared by the catalog parser and finite Auto timepoint discovery; does not open
any HDF5/Zarr store or import their runtime libraries.
"""

import re

_MULTI_FILE_TOKENS = ("time", "scan")

def _index_token(index: int, planned: int) -> str:
    """How ImControl pads the lapse index in a file name: to the planned count."""
    return str(int(index)).zfill(len(str(int(planned))))


def _split_multi_file_name(name: str, index: int, planned: int):
    """``(prefix, token, suffix)`` around the anchor's own index, or None."""
    padded = _index_token(index, planned)
    best = None
    for token in _MULTI_FILE_TOKENS:
        marker = f"_{token}{padded}"
        position = name.rfind(marker)
        if position < 0:
            continue
        rest = name[position + len(marker):]
        # The index is followed by the detector suffix or the extension, never
        # by another digit -- ``_time07`` must not match inside ``_time070``.
        if rest[:1].isdigit():
            continue
        if best is None or position > best[0]:
            best = (position, token, rest)
    if best is None:
        return None
    position, token, rest = best
    return name[:position], token, rest
