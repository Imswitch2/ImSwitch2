"""Zernike polynomials in the Noll convention, normalised to unit RMS over
the unit disk (so a wavefront's RMS is ``sqrt(sum c_j**2)`` without piston).

One basis for the whole of ImSwitch: the bead-stack aberration fit
(``improcess.analysis.psf_aberrations``) and adaptive optics
(:mod:`.shack_hartmann`, :mod:`.adaptive_optics`) both use it. Coordinates
are image coordinates: ``x`` grows along the columns, ``y`` along the rows
(downwards), ``theta = atan2(y, x)``.

The SLM pattern designer (``aberrationPatterns.ZernikeGenerator``) defines
modes 2/3, 7/8 and 11 differently.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

MODE_NAMES = {
    1: "piston",
    2: "tip",
    3: "tilt",
    4: "defocus",
    5: "astigmatism (oblique)",
    6: "astigmatism (vertical)",
    7: "coma (vertical)",
    8: "coma (horizontal)",
    9: "trefoil (vertical)",
    10: "trefoil (oblique)",
    11: "spherical (primary)",
    12: "secondary astigmatism (vertical)",
    13: "secondary astigmatism (oblique)",
    22: "spherical (secondary)",
}


def noll_to_nm(j: int) -> tuple[int, int]:
    """Noll index to ``(n, m)``; ``m > 0`` is the cosine, ``m < 0`` the sine term."""
    if j < 1:
        raise ValueError("Noll indices start at 1")
    n, j1 = 0, j - 1
    while j1 > n:
        n += 1
        j1 -= n
    m = (-1) ** j * ((n % 2) + 2 * int((j1 + ((n + 1) % 2)) / 2))
    return n, m


def zernike(j: int, rho: np.ndarray, theta: np.ndarray) -> np.ndarray:
    """Noll-normalised Zernike polynomial (unit RMS over the unit disk).

    Outside the disk (``rho > 1``) it is the same polynomial continued."""
    n, m = noll_to_nm(j)
    am = abs(m)
    rho = np.asarray(rho, dtype=np.float64)
    radial = np.zeros_like(rho, dtype=np.float64)
    for k in range((n - am) // 2 + 1):
        coefficient = (-1) ** k * math.factorial(n - k) / (
            math.factorial(k)
            * math.factorial((n + am) // 2 - k)
            * math.factorial((n - am) // 2 - k)
        )
        radial += coefficient * rho ** (n - 2 * k)
    if m == 0:
        return math.sqrt(n + 1) * radial
    angular = np.cos(am * theta) if m > 0 else np.sin(am * theta)
    return math.sqrt(2 * (n + 1)) * radial * angular


def zernike_xy(j: int, x, y) -> np.ndarray:
    """:func:`zernike` at Cartesian pupil coordinates (unit disk radius 1)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    return zernike(j, np.hypot(x, y), np.arctan2(y, x))


def mode_name(j: int) -> str:
    """``"Z5 astigmatism (oblique)"``, or ``"Z14"`` for a mode without a name."""
    name = MODE_NAMES.get(int(j))
    return f"Z{int(j)} {name}" if name else f"Z{int(j)}"


def wavefront(coefficients: dict, x, y) -> np.ndarray:
    """``sum c_j Z_j`` at the pupil coordinates ``x``, ``y``."""
    x = np.asarray(x, dtype=np.float64)
    total = np.zeros(np.broadcast(x, np.asarray(y)).shape)
    for j, c in coefficients.items():
        total = total + float(c) * zernike_xy(int(j), x, y)
    return total


#: Gauss-Legendre nodes and weights on [-1, 1]; 8 points integrate a
#: polynomial of degree 15 exactly, Zernike radial orders up to 15.
_GL_NODES, _GL_WEIGHTS = np.polynomial.legendre.leggauss(8)


def mean_gradients(modes: Sequence[int], x_centers, y_centers,
                   half_width: float) -> tuple[np.ndarray, np.ndarray]:
    """The mean of ``dZ/dx`` and ``dZ/dy`` over squares centred on
    ``(x_centers, y_centers)`` with half side ``half_width`` (pupil units):
    what a Shack-Hartmann lenslet of that size measures.

    Exact for polynomials (a mean of ``dZ/dx`` over the square is the
    difference of ``Z`` along its two vertical edges, averaged by
    Gauss-Legendre quadrature). Returns two ``(len(centers), len(modes))``
    arrays.
    """
    xc = np.asarray(x_centers, dtype=np.float64).ravel()[:, None]
    yc = np.asarray(y_centers, dtype=np.float64).ravel()[:, None]
    h = float(half_width)
    if not h > 0:
        raise ValueError("half_width must be positive")
    t = h * _GL_NODES[None, :]
    w = _GL_WEIGHTS / 2.0                     # weights of a mean over [-1, 1]
    gx = np.empty((xc.shape[0], len(modes)))
    gy = np.empty_like(gx)
    for column, j in enumerate(modes):
        right = zernike_xy(j, xc + h, yc + t)
        left = zernike_xy(j, xc - h, yc + t)
        gx[:, column] = ((right - left) @ w) / (2 * h)
        bottom = zernike_xy(j, xc + t, yc + h)
        top = zernike_xy(j, xc + t, yc - h)
        gy[:, column] = ((bottom - top) @ w) / (2 * h)
    return gx, gy


def modes_up_to(last: int, first: int = 2) -> list[int]:
    """Noll modes ``first`` .. ``last`` (tip to ``last`` by default)."""
    return list(range(int(first), int(last) + 1))


def as_mode_list(modes: Iterable[int]) -> list[int]:
    out = [int(j) for j in modes]
    if any(j < 1 for j in out):
        raise ValueError("Noll indices start at 1")
    if len(set(out)) != len(out):
        raise ValueError(f"a mode is listed twice: {out}")
    return out
