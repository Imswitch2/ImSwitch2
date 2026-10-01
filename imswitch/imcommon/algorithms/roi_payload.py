"""Compact, immutable storage for an ROI's pixel mask.

A segmentation region used to be stored as one Python tuple per pixel, which
for a megapixel region is roughly a million two-element tuples — enough to make
pushing a handful of regions into the ROI manager a memory event, and enough to
bloat every export that serialised them.

Two encodings are kept, because neither wins everywhere:

``rle``
    run lengths over the ROI's own bounding box, C order, alternating off/on
    and starting with an off run.  A solid blob is a handful of integers.
``bits``
    ``np.packbits`` of the same mask, optionally zlib-compressed
    (``bits-zlib``).  A fragmented or noisy mask has more runs than pixels/8,
    and this is then much smaller.

The encoder measures both and keeps the smaller; the codec is recorded, never
inferred, so adding a third later changes nothing that already exists.

Deliberately free of any package import beyond numpy: the record type imports
this, and the geometry helpers import the record, so anything else here would
be a cycle.
"""

from __future__ import annotations

import base64
import zlib
from dataclasses import dataclass
from typing import Literal

import numpy as np

Codec = Literal["rle", "bits", "bits-zlib"]

#: Refuse to decode a mask larger than this many pixels. Guards against a
#: malformed or hostile payload claiming a shape that would exhaust memory.
MAX_MASK_PIXELS = 1 << 28  # 268M pixels, ~256 MB as bool


class MaskPayloadError(ValueError):
    """A payload is malformed, inconsistent, or implausibly large."""


@dataclass(frozen=True)
class MaskPayload:
    """An encoded boolean mask over an ROI's own bounding box.

    ``shape`` is *local* — the bounding box, not the image — so a payload is
    independent of the image it was drawn on and stays small.
    """

    codec: Codec
    shape: tuple[int, int]
    data: tuple[int, ...] | bytes
    nbytes: int = 0

    def __post_init__(self):
        rows, cols = (int(n) for n in self.shape)
        if rows < 0 or cols < 0:
            raise MaskPayloadError(f"negative mask shape {self.shape!r}")
        object.__setattr__(self, "shape", (rows, cols))

    @property
    def size(self) -> int:
        return int(self.shape[0]) * int(self.shape[1])

    @property
    def is_empty(self) -> bool:
        """True for a mask with no pixels.

        A legitimate outcome, not an error: intersecting two disjoint ROIs
        produces one, as does clipping an ROI entirely outside its image.
        """
        return self.size == 0

    def to_json(self) -> dict:
        """JSON-ready form; bytes are base64 only at this boundary."""
        data = (
            list(self.data)
            if self.codec == "rle"
            else base64.b64encode(self.data).decode("ascii")
        )
        return {
            "codec": self.codec,
            "shape": list(self.shape),
            "data": data,
            "nbytes": int(self.nbytes),
        }

    @classmethod
    def from_json(cls, payload: dict) -> "MaskPayload":
        codec = str(payload.get("codec", "rle"))
        if codec not in ("rle", "bits", "bits-zlib"):
            raise MaskPayloadError(f"unknown mask codec {codec!r}")
        shape = tuple(int(v) for v in payload.get("shape", (0, 0)))
        raw = payload.get("data", [])
        data: tuple[int, ...] | bytes
        if codec == "rle":
            data = tuple(int(v) for v in raw)
        else:
            data = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
        return cls(
            codec=codec,  # type: ignore[arg-type]
            shape=shape,  # type: ignore[arg-type]
            data=data,
            nbytes=int(payload.get("nbytes", 0)),
        )


def _encode_rle(mask: np.ndarray) -> tuple[int, ...]:
    flat = np.asarray(mask, dtype=bool).reshape(-1)
    if flat.size == 0:
        return ()
    # Boundaries between runs, then the run lengths between them.
    change = np.flatnonzero(np.diff(flat)) + 1
    edges = np.concatenate(([0], change, [flat.size]))
    lengths = np.diff(edges).astype(np.int64)
    runs = lengths.tolist()
    # By convention the first run is "off"; prepend a zero-length one when the
    # mask starts set, so a decoder never has to be told which way to start.
    if bool(flat[0]):
        runs = [0, *runs]
    return tuple(int(v) for v in runs)


def _decode_rle(runs, size: int) -> np.ndarray:
    flat = np.zeros(size, dtype=bool)
    values = [int(v) for v in runs]
    if any(value < 0 for value in values):
        # A negative run would rewind the write position and corrupt the mask
        # rather than fail, so it is rejected outright.
        raise MaskPayloadError("run lengths must be non-negative")
    total = int(sum(values))
    if total != size:
        raise MaskPayloadError(
            f"run lengths sum to {total}, expected {size}"
        )
    position = 0
    value = False
    for run in runs:
        run = int(run)
        if run:
            flat[position:position + run] = value
            position += run
        value = not value
    return flat


def encode_mask(mask: np.ndarray) -> MaskPayload:
    """Encode a local boolean mask, choosing the codec by measured size.

    Both candidates are built and compared rather than picking by a run-count
    rule of thumb: the thing that matters is how big the payload ends up, and
    that can simply be measured.
    """
    arr = np.asarray(mask, dtype=bool)
    if arr.ndim != 2:
        raise MaskPayloadError(f"mask must be 2D, got shape {arr.shape}")
    shape = (int(arr.shape[0]), int(arr.shape[1]))
    size = shape[0] * shape[1]
    nbytes = int(size)

    if size == 0:
        return MaskPayload(codec="rle", shape=shape, data=(), nbytes=0)

    runs = _encode_rle(arr)
    packed = np.packbits(arr.reshape(-1)).tobytes()
    compressed = zlib.compress(packed, 6)

    candidates = [
        MaskPayload("rle", shape, runs, nbytes),
        MaskPayload("bits", shape, packed, nbytes),
        MaskPayload("bits-zlib", shape, compressed, nbytes),
    ]
    # Compared on what each actually serialises to rather than a per-run
    # estimate: the estimate was the thing being guessed at, and the real size
    # is cheap to measure.
    return min(candidates, key=serialised_size)


def serialised_size(payload: MaskPayload) -> int:
    """Bytes ``payload`` occupies once written out (JSON form for RLE)."""
    if payload.codec == "rle":
        # Comma-separated decimal integers, which is what to_json produces.
        return sum(len(str(int(run))) + 1 for run in payload.data)
    # base64 inflates bytes by 4/3 at the JSON boundary.
    return ((len(payload.data) + 2) // 3) * 4


def decode_mask(payload: MaskPayload) -> np.ndarray:
    """Decode a payload back to a local boolean mask, validating as it goes."""
    if not isinstance(payload, MaskPayload):
        raise MaskPayloadError(f"expected a MaskPayload, got {type(payload).__name__}")
    size = payload.size
    if size > MAX_MASK_PIXELS:
        raise MaskPayloadError(
            f"mask of {size} pixels exceeds the {MAX_MASK_PIXELS} limit"
        )
    if payload.nbytes and int(payload.nbytes) != size:
        # The declared size and the shape must agree, or one of them is lying
        # about what this payload contains.
        raise MaskPayloadError(
            f"payload declares {payload.nbytes} pixels but its shape holds {size}"
        )
    if size == 0:
        return np.zeros(payload.shape, dtype=bool)

    if payload.codec == "rle":
        flat = _decode_rle(payload.data, size)
    else:
        raw = payload.data
        if not isinstance(raw, (bytes, bytearray)):
            raise MaskPayloadError("packed payloads must carry bytes")
        expected = (size + 7) // 8
        if payload.codec == "bits-zlib":
            # Bounded so a malformed or hostile payload cannot expand without
            # limit, and checked for a clean end so trailing junk is not
            # quietly accepted.
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(bytes(raw), expected + 1)
            if len(raw) > expected:
                raise MaskPayloadError("compressed mask expands beyond its shape")
            if not decompressor.eof:
                raise MaskPayloadError("compressed mask is truncated")
            if decompressor.unused_data:
                raise MaskPayloadError("compressed mask has trailing data")
        if len(raw) != expected:
            raise MaskPayloadError(
                f"packed mask has {len(raw)} bytes, expected {expected}"
            )
        flat = np.unpackbits(np.frombuffer(raw, dtype=np.uint8))[:size].astype(bool)

    return flat.reshape(payload.shape)


def payload_from_pixels(pixels, bounds) -> MaskPayload:
    """Encode a legacy ``((row, col), ...)`` pixel list over ``bounds``."""
    r0, r1, c0, c1 = (int(v) for v in bounds)
    shape = (max(0, r1 - r0), max(0, c1 - c0))
    mask = np.zeros(shape, dtype=bool)
    coords = np.asarray(tuple(pixels), dtype=np.int64).reshape((-1, 2))
    if coords.size:
        rows = coords[:, 0] - r0
        cols = coords[:, 1] - c0
        inside = (rows >= 0) & (rows < shape[0]) & (cols >= 0) & (cols < shape[1])
        mask[rows[inside], cols[inside]] = True
    return encode_mask(mask)


__all__ = [
    "MAX_MASK_PIXELS",
    "Codec",
    "MaskPayload",
    "MaskPayloadError",
    "decode_mask",
    "encode_mask",
    "payload_from_pixels",
]
