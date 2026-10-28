"""Frame normalisation for images popped from MMCore's circular buffer.

The bridge is mono-only (plan section 4.4): ImSwitch2's chunk contract is
``(numFrames, height, width)``. ``popNextImageAndMD`` returns ``(h, w)`` for
mono cameras and ``(h, w, 3)`` for 4-component (BGRA) cameras, and tags every
frame with ``PixelType`` (P0 probe: ``GRAY8`` / ``GRAY16``, not the property
value ``16bit``). The dtype therefore comes from that tag, never from the
array and never from a camera property.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional, Sequence

import numpy as np

#: The Micro-Manager property that selects the pixel format of a camera.
PROPERTY_PIXEL_TYPE = 'PixelType'

#: ``PixelType`` metadata tag -> numpy dtype, for the mono types MMCore delivers.
#: Anything else (``RGB32``, ``RGB64``, 32-bit float, ...) is refused.
_DTYPE_BY_PIXEL_TYPE = {
    'GRAY8': np.dtype(np.uint8),
    'GRAY16': np.dtype(np.uint16),
}

#: Bytes per pixel -> dtype, for a camera with one component (MMCore's
#: ``getBytesPerPixel`` / ``getNumberOfComponents``), used before any frame
#: exists. Consistent with the table above: GRAY8 = 1 byte, GRAY16 = 2 bytes.
_DTYPE_BY_BYTES_PER_PIXEL = {dtype.itemsize: dtype for dtype in _DTYPE_BY_PIXEL_TYPE.values()}


class FrameFormatError(ValueError):
    """A frame does not match what the bridge can carry or what was declared."""


class UnsupportedPixelTypeError(FrameFormatError):
    """The camera delivers colour or a pixel type the mono-only bridge refuses."""


def dtype_for_pixel_type(pixel_type) -> np.dtype:
    """dtype of the ``PixelType`` tag (``GRAY8`` -> uint8, ``GRAY16`` -> uint16)."""
    try:
        return _DTYPE_BY_PIXEL_TYPE[str(pixel_type)]
    except KeyError:
        raise UnsupportedPixelTypeError(
            f"pixel type {pixel_type!r} is not supported: the Micro-Manager bridge "
            f"carries mono cameras only ({', '.join(sorted(_DTYPE_BY_PIXEL_TYPE))}); "
            f"colour cameras need a per-component split, which is not implemented"
        ) from None


#: The ways an adapter spells a mono ``PixelType`` property value: ``8bit`` / ``16bit`` (the
#: DemoCamera and most adapters), ``8-bit`` / ``16 bit``, and the metadata tag's own ``GRAY8`` /
#: ``GRAY16``. Group 1 or 2 is the bit depth.
_PIXEL_TYPE_PROPERTY_TEXT = re.compile(r'^\s*(?:(\d+)\s*-?\s*bit|gray\s*(\d+))\s*$', re.IGNORECASE)


def dtype_for_pixel_type_property(text) -> Optional[np.dtype]:
    """The dtype a ``PixelType`` *property value* selects, or ``None`` if it cannot be told.

    Unlike :func:`dtype_for_pixel_type` (the metadata *tag*, ``GRAY8``) this reads what a user
    writes to the property (``8bit``). ``None`` means "do not predict": colour (``32bitRGB``)
    or an adapter's own wording; the caller then writes the value and reads the format back.
    """
    match = _PIXEL_TYPE_PROPERTY_TEXT.match(str(text))
    if match is None:
        return None
    bits = int(match.group(1) or match.group(2))
    if bits % 8:
        return None
    return _DTYPE_BY_BYTES_PER_PIXEL.get(bits // 8)


#: Words of a ``PixelType`` property value that mean several components per pixel (colour): the
#: DemoCamera's ``32bitRGB`` / ``64bitRGB`` and the spellings other adapters use.
_COLOUR_PIXEL_TYPE_TEXT = re.compile(r'rgb|bgr|colou?r', re.IGNORECASE)


def predict_pixel_type_property(text) -> Optional[np.dtype]:
    """What writing the ``PixelType`` property value ``text`` does to the frame format, before it is written.

    Three answers, which is what the caller's rule needs:

    * the dtype (8-bit or 16-bit mono): the format is **predictable and supported**;
    * :class:`UnsupportedPixelTypeError`: the format is **predictable and unsupported** -- colour
      wording, or a whole number of bytes per pixel the bridge has no dtype for (``32bit``): refuse
      before the write, because the bridge cannot carry those frames whatever else is streaming;
    * ``None``: the adapter's own wording (``12bit`` delivers 16-bit frames on some cameras, and
      other adapters invent names): the effect **cannot be told** and has to be read back, which is
      only possible while the camera is MMCore's current one or nothing else streams.
    """
    value = str(text)
    if _COLOUR_PIXEL_TYPE_TEXT.search(value):
        raise UnsupportedPixelTypeError(
            f"PixelType {value!r} selects colour frames: the Micro-Manager bridge carries mono "
            f"cameras only ({', '.join(sorted(_DTYPE_BY_PIXEL_TYPE))})")
    match = _PIXEL_TYPE_PROPERTY_TEXT.match(value)
    if match is None:
        return None
    bits = int(match.group(1) or match.group(2))
    if bits % 8:
        return None
    dtype = _DTYPE_BY_BYTES_PER_PIXEL.get(bits // 8)
    if dtype is None:
        raise UnsupportedPixelTypeError(
            f"PixelType {value!r} is {bits // 8} bytes per pixel: the Micro-Manager bridge carries "
            f"8-bit and 16-bit mono cameras only")
    return dtype


def dtype_for_bytes_per_pixel(bytes_per_pixel: int, n_components: int = 1) -> np.dtype:
    """dtype of a camera that has not delivered a frame yet.

    ``bytes_per_pixel`` and ``n_components`` are MMCore's ``getBytesPerPixel()``
    and ``getNumberOfComponents()``.
    """
    if int(n_components) != 1:
        raise UnsupportedPixelTypeError(
            f"the camera delivers {n_components} components per pixel: the "
            f"Micro-Manager bridge carries mono cameras only"
        )
    try:
        return _DTYPE_BY_BYTES_PER_PIXEL[int(bytes_per_pixel)]
    except KeyError:
        raise UnsupportedPixelTypeError(
            f"{bytes_per_pixel} bytes per pixel is not supported: the bridge "
            f"carries 8-bit and 16-bit mono cameras only"
        ) from None


def frame_bytes(shape: Sequence[int], dtype) -> int:
    """Pixel bytes of one frame of ``shape`` and ``dtype``."""
    count = 1
    for extent in shape:
        count *= int(extent)
    return count * np.dtype(dtype).itemsize


def normalize_frame(img, md: Mapping) -> np.ndarray:
    """Return ``img`` as a 2-D ``(h, w)`` array with the dtype of its ``PixelType`` tag.

    Raises :class:`UnsupportedPixelTypeError` for colour / unknown pixel types
    and :class:`FrameFormatError` when the array contradicts its own metadata
    (dtype, ``Width`` / ``Height``) -- a frame is never converted silently.
    """
    try:
        pixel_type = md['PixelType']
    except KeyError:
        raise FrameFormatError("frame metadata has no 'PixelType' tag") from None
    dtype = dtype_for_pixel_type(pixel_type)

    frame = np.asarray(img)
    if frame.ndim == 3 and frame.shape[-1] == 1:
        frame = frame[..., 0]
    if frame.ndim != 2:
        raise UnsupportedPixelTypeError(
            f"frame has shape {frame.shape} for PixelType {pixel_type!r}: expected (height, width); "
            f"colour frames are not supported by the Micro-Manager bridge"
        )
    if frame.dtype != dtype:
        raise FrameFormatError(
            f"PixelType {pixel_type!r} means {dtype} but the frame is {frame.dtype}"
        )
    for tag, extent in (('Height', frame.shape[0]), ('Width', frame.shape[1])):
        declared = md.get(tag) if hasattr(md, 'get') else None
        if declared is not None and int(declared) != extent:
            raise FrameFormatError(
                f"metadata {tag}={declared} contradicts the frame shape {frame.shape} (height, width)"
            )
    return frame
