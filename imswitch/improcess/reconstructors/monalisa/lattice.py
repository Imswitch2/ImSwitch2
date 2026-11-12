"""Generalized 2D illumination-lattice representation and detection.

Groundwork for MoNaLISA illumination patterns beyond the axis-aligned
rectangular grid the 1D projection localizer assumes. A :class:`Lattice`
describes any 2D Bravais lattice (two basis vectors plus an offset), with
constructors for the rectangular and hexagonal cases, and
:func:`detect_lattice` recovers one from image data without a period guess by
reading the two dominant reciprocal-space peaks and their phases.

The downstream fast-Gauss *extraction* only needs the enumerated focus
centers (:meth:`Lattice.points_in_frame`) and therefore works for any
lattice. The *reassignment* step (``scan_geometry.get_1d_indices``) still
assumes the axis-aligned rectangular case — :meth:`Lattice.to_grid_params`
bridges to it and raises a descriptive error for anything else, so a
hexagonal pattern fails with its measured geometry in the message instead of
producing a silently scrambled reconstruction.

Spectral detection is bin-limited (0.1-0.25 % of the period), which
accumulates to a sizeable fraction of a pixel across a frame.
:meth:`Lattice.fit_to_points` is the real-space refinement on top of it: a
linear regression of measured focus centres on their integer lattice indices
(``docs/monalisa_optimal_reconstruction.md``, section 2.6).
"""

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter


@dataclass(frozen=True)
class Lattice:
    """A 2D Bravais lattice in pixel coordinates.

    Points are ``offset + m * a1 + n * a2`` for integers ``m, n``. Vectors are
    ``(x, y)`` pairs in pixels (x == column axis, y == row axis).
    """

    a1: tuple[float, float]
    a2: tuple[float, float]
    offset: tuple[float, float] = (0.0, 0.0)

    # ------------------------------------------------------------------ build
    @classmethod
    def rectangular(
        cls, xp: float, yp: float, xo: float = 0.0, yo: float = 0.0
    ) -> "Lattice":
        """Axis-aligned rectangular grid with periods ``xp``/``yp``."""
        if xp <= 0 or yp <= 0:
            raise ValueError("Lattice periods must be positive")
        return cls(
            a1=(float(xp), 0.0),
            a2=(0.0, float(yp)),
            offset=(float(xo) % float(xp), float(yo) % float(yp)),
        )

    @classmethod
    def hexagonal(
        cls,
        spacing: float,
        offset: tuple[float, float] = (0.0, 0.0),
        angle_rad: float = 0.0,
    ) -> "Lattice":
        """Hexagonal lattice with nearest-neighbor distance ``spacing``.

        ``angle_rad`` rotates the whole lattice; at 0 one basis vector points
        along +x.
        """
        if spacing <= 0:
            raise ValueError("Lattice spacing must be positive")
        a1 = spacing * np.array([np.cos(angle_rad), np.sin(angle_rad)])
        second = angle_rad + np.pi / 3
        a2 = spacing * np.array([np.cos(second), np.sin(second)])
        return cls(
            a1=(float(a1[0]), float(a1[1])),
            a2=(float(a2[0]), float(a2[1])),
            offset=(float(offset[0]), float(offset[1])),
        )

    # ------------------------------------------------------------- properties
    @property
    def matrix(self) -> np.ndarray:
        """2x2 matrix with the basis vectors as columns."""
        return np.array(
            [[self.a1[0], self.a2[0]], [self.a1[1], self.a2[1]]], dtype=float
        )

    @property
    def cell_area(self) -> float:
        return float(abs(np.linalg.det(self.matrix)))

    def nearest_spacing(self) -> float:
        """Nearest-neighbor distance (useful to bound extraction footprints)."""
        a1, a2 = self.reduced()._vectors()
        return float(min(np.linalg.norm(a1), np.linalg.norm(a2)))

    def basis_angle_deg(self) -> float:
        """Acute angle between the reduced basis vectors, in (0, 90] degrees.

        Reported acute-normalized because equivalent shortest bases of one
        lattice can enclose either the angle or its supplement (a hexagonal
        lattice reduces to a 60- or 120-degree pair depending on which of its
        three shortest directions the reduction returns).
        """
        a1, a2 = self.reduced()._vectors()
        cosang = np.dot(a1, a2) / (np.linalg.norm(a1) * np.linalg.norm(a2))
        angle = float(np.degrees(np.arccos(np.clip(abs(cosang), 0.0, 1.0))))
        return angle

    def axis_tilt_deg(self) -> float:
        """Largest angular distance of a reduced basis vector to a pixel axis.

        0 for a perfectly axis-aligned grid, ~the rotation angle for a
        slightly rotated one. For strongly non-orthogonal bases (hexagonal
        etc.) the number is large but not a rotation angle.
        """
        tilts = []
        for vector in self.reduced()._vectors():
            theta = np.degrees(np.arctan2(vector[1], vector[0])) % 180.0
            tilts.append(min(theta % 90.0, 90.0 - (theta % 90.0)))
        return float(max(tilts))

    def describe(self) -> str:
        a1, a2 = self._vectors()
        return (
            f"|a1|={np.linalg.norm(a1):.3g} px, |a2|={np.linalg.norm(a2):.3g} px, "
            f"angle={self.basis_angle_deg():.1f} deg, "
            f"a1 tilt={np.degrees(np.arctan2(a1[1], a1[0])):.1f} deg, "
            f"offset=({self.offset[0]:.2f}, {self.offset[1]:.2f})"
        )

    def _vectors(self) -> tuple[np.ndarray, np.ndarray]:
        return np.asarray(self.a1, dtype=float), np.asarray(self.a2, dtype=float)

    # ------------------------------------------------------------- reduction
    def reduced(self) -> "Lattice":
        """Return an equivalent lattice with a Lagrange-reduced basis.

        The two returned basis vectors are (a) shortest possible for the
        lattice, with signs normalized to the upper half-plane and ordered by
        angle from +x, and (b) carry the offset folded into the unit cell.
        Note the *pair* is not unique for high-symmetry lattices — a
        hexagonal lattice has three equally short directions, so equivalent
        inputs may reduce to different (equally valid) pairs. Compare
        lattices through invariants (:meth:`nearest_spacing`,
        :meth:`cell_area`, :meth:`basis_angle_deg`, :meth:`contains_point`)
        rather than matrix equality.
        """
        a1, a2 = self._vectors()
        if np.linalg.norm(a1) > np.linalg.norm(a2):
            a1, a2 = a2, a1
        # Lagrange reduction
        for _ in range(64):
            m = np.round(np.dot(a2, a1) / np.dot(a1, a1))
            a2 = a2 - m * a1
            if np.linalg.norm(a2) >= np.linalg.norm(a1):
                break
            a1, a2 = a2, a1

        # Upper-half-plane representatives (y > 0, or y == 0 and x > 0), then
        # order by angle from +x so the x-most vector comes first.
        def _representative(v: np.ndarray) -> np.ndarray:
            if v[1] < 0 or (v[1] == 0 and v[0] < 0):
                return -v
            return v

        a1 = _representative(a1)
        a2 = _representative(a2)
        if (np.arctan2(a2[1], a2[0]) % np.pi) < (np.arctan2(a1[1], a1[0]) % np.pi):
            a1, a2 = a2, a1

        # Offset into the unit cell of the reduced basis.
        matrix = np.array([[a1[0], a2[0]], [a1[1], a2[1]]])
        fracs = np.linalg.solve(matrix, np.asarray(self.offset, dtype=float))
        fracs = fracs % 1.0
        offset = matrix @ fracs
        return Lattice(
            a1=(float(a1[0]), float(a1[1])),
            a2=(float(a2[0]), float(a2[1])),
            offset=(float(offset[0]), float(offset[1])),
        )

    # ----------------------------------------------------------- conversions
    def _axis_assignment(
        self, tol: float
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """Return the reduced basis as ``(x_like, y_like)`` vectors, or None.

        Tries both assignments, so the result does not depend on how the
        reduced pair happens to be ordered.
        """
        reduced = self.reduced()
        a1, a2 = reduced._vectors()
        for u, v in ((a1, a2), (a2, a1)):
            if (
                abs(u[1]) <= tol * np.linalg.norm(u)
                and abs(v[0]) <= tol * np.linalg.norm(v)
            ):
                return np.abs(u), np.abs(v)
        return None

    def is_axis_aligned_rectangular(self, tol: float = 0.02) -> bool:
        """Whether the (reduced) basis is axis-aligned rectangular.

        ``tol`` bounds the relative off-axis component of each basis vector
        (~ the tilt in radians for small angles).
        """
        return self._axis_assignment(tol) is not None

    def to_grid_params(self, tol: float = 0.02) -> tuple[float, float, float, float]:
        """Return ``(xp, xo, yp, yo)`` for the legacy grid pipeline.

        Raises:
            ValueError: If the lattice is not axis-aligned rectangular — the
                message carries the measured geometry so the failure explains
                itself instead of scrambling the reassignment.
        """
        assignment = self._axis_assignment(tol)
        if assignment is None:
            raise ValueError(
                "Illumination lattice is not an axis-aligned rectangular "
                f"grid ({self.describe()}); the fast-Gauss reassignment "
                "currently supports axis-aligned rectangular patterns only"
            )
        x_like, y_like = assignment
        xp = float(np.linalg.norm(x_like))
        yp = float(np.linalg.norm(y_like))
        offset = self.reduced().offset
        return xp, float(offset[0]) % xp, yp, float(offset[1]) % yp

    # ----------------------------------------------------------- enumeration
    def points_in_frame(
        self, num_rows: int, num_cols: int, margin: float = 0.0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Enumerate lattice points with ``-margin <= x < num_cols + margin``
        (same for y), sorted row-major (by y, then x).

        For an axis-aligned rectangular lattice with the offset inside the
        frame this reproduces ``scan_geometry.get_center_coords`` exactly,
        ordering included.
        """
        matrix = self.matrix
        if abs(np.linalg.det(matrix)) < 1e-12:
            raise ValueError("Lattice basis vectors are collinear")
        inv = np.linalg.inv(matrix)
        offset = np.asarray(self.offset, dtype=float)

        corners = np.array(
            [
                [-margin, -margin],
                [num_cols + margin, -margin],
                [-margin, num_rows + margin],
                [num_cols + margin, num_rows + margin],
            ],
            dtype=float,
        )
        indices = (inv @ (corners - offset).T).T
        m_lo, n_lo = np.floor(indices.min(axis=0)).astype(int) - 1
        m_hi, n_hi = np.ceil(indices.max(axis=0)).astype(int) + 1

        m_range = np.arange(m_lo, m_hi + 1)
        n_range = np.arange(n_lo, n_hi + 1)
        mm, nn = np.meshgrid(m_range, n_range)
        points = (
            offset[None, :]
            + mm.reshape(-1, 1) * matrix[:, 0][None, :]
            + nn.reshape(-1, 1) * matrix[:, 1][None, :]
        )
        eps = 1e-9
        keep = (
            (points[:, 0] >= -margin - eps)
            & (points[:, 0] < num_cols + margin - eps)
            & (points[:, 1] >= -margin - eps)
            & (points[:, 1] < num_rows + margin - eps)
        )
        points = points[keep]
        order = np.lexsort((points[:, 0], points[:, 1]))
        points = points[order]
        return points[:, 0].copy(), points[:, 1].copy()

    def indices_of(
        self, x: np.ndarray, y: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Integer lattice indices ``(m, n)`` of the points nearest to ``(x, y)``.

        Returns ``(indices, residual)``: ``indices`` has shape ``(N, 2)`` and
        ``residual`` is the distance of each point to its lattice point, in
        pixels (the distortion a measured centre carries).
        """
        points = np.stack(
            [np.asarray(x, dtype=float).ravel(), np.asarray(y, dtype=float).ravel()]
        )
        offset = np.asarray(self.offset, dtype=float)[:, None]
        indices = np.rint(np.linalg.solve(self.matrix, points - offset))
        residual = points - (offset + self.matrix @ indices)
        return indices.T.astype(int), np.hypot(residual[0], residual[1])

    def fit_to_points(
        self,
        x: np.ndarray,
        y: np.ndarray,
        weights: np.ndarray | None = None,
        outlier_px: float | None = 0.5,
    ) -> tuple["Lattice", np.ndarray]:
        """Refine basis and offset by regressing measured centres on indices.

        The points are indexed against this lattice (so it must already be
        good to well within half a period over the frame, which the spectral
        detection is), then ``x = o + m a1 + n a2`` is solved by linear least
        squares: six parameters from all foci at once.

        Args:
            x, y: Measured focus centres, px.
            weights: Optional per-point weights (e.g. inverse variances).
            outlier_px: Points further than this from the fitted lattice are
                dropped and the fit repeated once; ``None`` keeps all. When
                the points scatter more than that as a whole, as measured
                centres of real foci do, the limit is three times their
                median distance instead.

        Returns:
            ``(lattice, residuals)``: the refined lattice, expressed in the
            same basis as ``self`` so indices carry over, and the ``(N, 2)``
            residual ``measured - fitted`` of every input point (the
            distortion map).
        """
        x = np.asarray(x, dtype=float).ravel()
        y = np.asarray(y, dtype=float).ravel()
        if x.size < 3:
            raise ValueError("Need at least three points to fit a lattice")
        indices, _ = self.indices_of(x, y)
        design = np.column_stack([np.ones(x.size), indices[:, 0], indices[:, 1]])
        if np.linalg.matrix_rank(design) < 3:
            raise ValueError("Points are collinear in lattice indices")
        targets = np.column_stack([x, y])
        w = (
            np.ones(x.size)
            if weights is None
            else np.asarray(weights, dtype=float).ravel()
        )
        keep = np.isfinite(targets).all(axis=1) & np.isfinite(w) & (w > 0)

        def _solve(selection: np.ndarray) -> np.ndarray:
            root = np.sqrt(w[selection])[:, None]
            coef, *_ = np.linalg.lstsq(
                design[selection] * root, targets[selection] * root, rcond=None
            )
            return coef

        coef = _solve(keep)
        if outlier_px is not None:
            distance = np.linalg.norm(targets - design @ coef, axis=1)
            limit = max(float(outlier_px), 3.0 * float(np.median(distance[keep])))
            inliers = keep & (distance <= limit)
            if 3 <= inliers.sum() < keep.sum():
                coef = _solve(inliers)
        fitted = Lattice(
            a1=(float(coef[1, 0]), float(coef[1, 1])),
            a2=(float(coef[2, 0]), float(coef[2, 1])),
            offset=(float(coef[0, 0]), float(coef[0, 1])),
        )
        return fitted, targets - design @ coef

    def contains_point(self, x: float, y: float, tol: float = 0.05) -> bool:
        """Whether ``(x, y)`` lies on the lattice within ``tol`` (fractional)."""
        fracs = np.linalg.solve(
            self.matrix, np.array([x, y], dtype=float) - np.asarray(self.offset)
        )
        return bool(np.all(np.abs(fracs - np.round(fracs)) <= tol))


def _parabolic_offset(m_minus: float, m_zero: float, m_plus: float) -> float:
    """Sub-bin peak offset from three log-magnitude samples, in (-0.5, 0.5)."""
    denom = m_minus - 2.0 * m_zero + m_plus
    if denom >= 0:  # not a local max in log space; keep the bin center
        return 0.0
    return float(np.clip(0.5 * (m_minus - m_plus) / denom, -0.5, 0.5))


def _dft_phase(image: np.ndarray, freq: np.ndarray) -> float:
    """Phase of the DFT of ``image`` at the (fractional) frequency ``freq``.

    ``freq`` is ``(fx, fy)`` in cycles/px. Evaluated as a direct separable
    sum so sub-bin frequencies from peak refinement are sampled exactly.
    """
    rows, cols = image.shape
    ex = np.exp(-2j * np.pi * freq[0] * np.arange(cols))
    ey = np.exp(-2j * np.pi * freq[1] * np.arange(rows))
    return float(np.angle(ey @ image @ ex))


# The median of a Rayleigh-distributed magnitude over its lower quartile.
_MEDIAN_OVER_QUARTILE = 1.552


def _radial_median(
    magnitude: np.ndarray, radius: np.ndarray, size: int
) -> np.ndarray:
    """Typical ``magnitude`` at every pixel's ``radius``, per pixel.

    The median a spectrum without peaks has over the ring of that radius. It
    is taken from the lower quartile of the ring, because on a small frame
    the peaks of a lattice and their skirts fill a good part of the ring they
    lie on, and a median they have raised hides them. The rings are as wide
    as a quarter of their radius, and at least two frequency bins.
    """
    bins = radius.ravel() * size
    # Rings of equal width on a logarithmic scale beyond eight bins, of two
    # bins below.
    ring = np.where(
        bins < 8.0,
        np.floor(bins / 2.0),
        4.0 + np.floor(np.log(np.maximum(bins, 8.0) / 8.0) / np.log(1.25)),
    ).astype(np.int64)
    order = np.argsort(ring, kind="stable")
    sorted_ring = ring[order]
    sorted_magnitude = magnitude.ravel()[order]
    edges = np.flatnonzero(np.diff(sorted_ring)) + 1
    levels = np.array([
        _MEDIAN_OVER_QUARTILE * np.percentile(part, 25.0)
        for part in np.split(sorted_magnitude, edges)
    ])
    ring_index = np.cumsum(np.concatenate([[0], np.diff(sorted_ring) > 0]))
    out = np.empty(magnitude.size)
    out[order] = levels[ring_index]
    return np.maximum(out, 1e-30).reshape(magnitude.shape)


def _reciprocal_basis(
    candidates: list[tuple[np.ndarray, float]], tolerance: float = 0.1
) -> tuple[np.ndarray, np.ndarray]:
    """The pair of peaks that generates the others.

    Every pair of non-collinear peaks is a candidate basis of the reciprocal
    lattice. The right one explains the other peaks as integer combinations
    of itself; among the pairs that do, the one with the smallest cell is the
    fundamental pair, and the others are made of harmonics. A peak that
    belongs to the specimen or to the sensor's fixed pattern, and not to the
    lattice, is explained by no such pair and explains little itself.
    """
    freqs = np.array([freq for freq, _ in candidates])
    strengths = np.array([strength for _, strength in candidates])
    pairs = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            pair = np.array([freqs[i], freqs[j]])
            norms = np.linalg.norm(pair, axis=1)
            if abs(pair[0] @ pair[1]) >= 0.95 * norms[0] * norms[1]:
                continue
            coords = np.linalg.solve(pair.T, freqs.T).T
            on_lattice = np.all(np.abs(coords - np.rint(coords)) <= tolerance, axis=1)
            explained = float(strengths[on_lattice].sum() / strengths.sum())
            pairs.append((explained, abs(float(np.linalg.det(pair))), i, j))
    if not pairs:
        raise ValueError(
            "Only collinear spectral peaks found — the pattern is 1D-periodic "
            "or too weak along the second lattice direction"
        )
    most = max(explained for explained, *_ in pairs)
    _, _, i, j = min(
        (pair for pair in pairs if pair[0] >= most - 0.05), key=lambda pair: pair[1:]
    )
    return freqs[i], freqs[j]


def _coarsen(q1, q2, peak_at, lowest: float, significance: float):
    """The basis of the coarser lattice, where the spectrum shows one.

    A lattice whose fundamental peaks missed the significance, as they can on
    a frame of a few periods or under a specimen of sparse filaments, is read
    from its harmonics as a lattice of half or a third of the period, and
    nothing in the image it then reconstructs says so. The spectrum does: it
    has a peak at a half or a third of a reciprocal vector of the basis,
    where that of a lattice of this basis has none. The basis is divided as
    long as there is such a peak that is stronger than its own: the spectrum
    of a lattice of spots falls with the frequency, so a fundamental is
    stronger than its harmonics, and what a specimen has there is not.
    """
    for _ in range(6):
        first, second = peak_at(q1)[0], peak_at(q2)[0]
        top = max(first, second)
        # A part of one basis vector has to be stronger than that vector and
        # of the strength of a lattice peak; a part of a sum of the two has
        # to be stronger than both.
        parts = [
            (q1 / 2.0, 0, max(first, 0.5 * top)),
            (q1 / 3.0, 0, max(first, 0.5 * top)),
            (q2 / 2.0, 1, max(second, 0.5 * top)),
            (q2 / 3.0, 1, max(second, 0.5 * top)),
            ((q1 + q2) / 2.0, 1, top),
            ((q1 - q2) / 2.0, 1, top),
        ]
        found = None
        for part, replaced, needed in parts:
            if np.linalg.norm(part) < lowest:
                continue
            strength, standing = peak_at(part)
            if strength >= needed and standing >= significance:
                if found is None or strength > found[0]:
                    found = (strength, part, replaced)
        if found is None:
            return q1, q2
        _, part, replaced = found
        q1, q2 = (part, q2) if replaced == 0 else (q1, part)
    return q1, q2


def detect_lattice(
    image: np.ndarray,
    min_period: float = 3.0,
    max_period: float | None = None,
    num_candidate_peaks: int = 12,
    min_peak_significance: float = 8.0,
) -> Lattice:
    """Detect a 2D Bravais lattice of bright foci in ``image`` — no guess.

    Reads the peaks of the windowed 2D spectrum that stand out against the
    spectrum at their own radius (sub-bin refined), takes the pair that
    generates the others, inverts it into a real-space basis, and
    recovers the lattice offset from the spectral phases at the reduced
    basis' reciprocal vectors. Handles rotated rectangular and hexagonal
    patterns alike; the result is in canonical reduced form.

    Args:
        image: 2D frame (or pre-summed stack) containing the focus pattern.
        min_period: Shortest period considered, px.
        max_period: Longest period considered, px. Defaults to a third of the
            smaller image dimension so at least ~3 repeats support the peak.
        num_candidate_peaks: How many spectral maxima to consider when
            pairing basis vectors.
        min_peak_significance: A local maximum only counts as a lattice peak
            when its magnitude exceeds this multiple of the median magnitude
            at its radius. Noise tops out below ~4x, the structure of a
            specimen at 5-6x, and focus patterns measure 20x and more, so
            the default makes patternless data fail loudly instead of
            returning a junk basis.

    Raises:
        ValueError: If fewer than two significant non-collinear spectral
            peaks exist in the period band.
    """
    image = np.asarray(image, dtype=float)
    if image.ndim != 2:
        raise ValueError(f"Expected a 2D image, got {image.ndim}D")
    rows, cols = image.shape
    if max_period is None:
        max_period = min(rows, cols) / 3.0
    if not (0 < min_period < max_period):
        raise ValueError("Need 0 < min_period < max_period")

    centered = image - image.mean()
    window = np.outer(np.hanning(rows), np.hanning(cols))
    spectrum = np.fft.fftshift(np.fft.fft2(centered * window))
    magnitude = np.abs(spectrum)
    fx = np.fft.fftshift(np.fft.fftfreq(cols))
    fy = np.fft.fftshift(np.fft.fftfreq(rows))
    fxx, fyy = np.meshgrid(fx, fy)
    radius = np.hypot(fxx, fyy)
    in_band = (radius >= 1.0 / max_period) & (radius <= 1.0 / min_period)
    # A specimen puts far more power into the low frequencies than the foci
    # put into their lattice peaks, but it spreads it over all directions.
    # Measured against the typical magnitude at its own radius, a lattice
    # peak stands out and the specimen does not. That decides which maxima
    # count; among those that do, the strongest are the fundamental ones.
    contrast = magnitude / _radial_median(magnitude, radius, max(rows, cols))
    banded = np.where(in_band, magnitude, 0.0)
    is_peak = (
        (banded == maximum_filter(banded, size=3))
        & in_band
        & (contrast > float(min_peak_significance))
    )
    peak_rows, peak_cols = np.nonzero(is_peak)
    if peak_rows.size == 0:
        raise ValueError(
            f"No significant spectral peaks found for periods in "
            f"[{min_period:.3g}, {max_period:.3g}] px — is there a periodic "
            f"focus pattern?"
        )
    strengths = banded[peak_rows, peak_cols]
    order = np.argsort(strengths)[::-1]

    log_mag = np.log(magnitude + 1e-12)
    candidates: list[tuple[np.ndarray, float]] = []
    for index in order:
        iy, ix = int(peak_rows[index]), int(peak_cols[index])
        dx = dy = 0.0
        if 0 < ix < cols - 1:
            dx = _parabolic_offset(
                log_mag[iy, ix - 1], log_mag[iy, ix], log_mag[iy, ix + 1]
            )
        if 0 < iy < rows - 1:
            dy = _parabolic_offset(
                log_mag[iy - 1, ix], log_mag[iy, ix], log_mag[iy + 1, ix]
            )
        freq = np.array([fx[ix] + dx / cols, fy[iy] + dy / rows])
        # The spectrum of a real image is Hermitian: fold q and -q together.
        if freq[0] < 0 or (freq[0] == 0 and freq[1] < 0):
            freq = -freq
        # A specimen modulates the foci, which surrounds every lattice peak
        # with satellites; lattice peaks themselves are a whole reciprocal
        # vector apart.
        if any(
            np.linalg.norm(freq - other)
            < 0.25 * min(np.linalg.norm(freq), np.linalg.norm(other))
            for other, _ in candidates
        ):
            continue
        candidates.append((freq, float(strengths[index])))
        if len(candidates) >= num_candidate_peaks:
            break

    if not candidates:
        raise ValueError("No usable spectral peaks after folding")
    q1, q2 = _reciprocal_basis(candidates)

    def peak_at(freq: np.ndarray) -> tuple[float, float]:
        """Magnitude and contrast of the spectrum at ``freq``, to a bin."""
        col = int(np.rint(freq[0] * cols)) + cols // 2
        row = int(np.rint(freq[1] * rows)) + rows // 2
        if not (1 <= row < rows - 1 and 1 <= col < cols - 1):
            return 0.0, 0.0
        window_mag = magnitude[row - 1:row + 2, col - 1:col + 2]
        window_con = contrast[row - 1:row + 2, col - 1:col + 2]
        best = np.unravel_index(np.argmax(window_mag), window_mag.shape)
        return float(window_mag[best]), float(window_con[best])

    q1, q2 = _coarsen(
        q1, q2, peak_at, 1.0 / max_period, 0.5 * float(min_peak_significance)
    )

    # Real-space basis A solves q_i . a_j = delta_ij: A = Q^-1 with Q rows q_i.
    basis = np.linalg.inv(np.array([q1, q2]))
    lattice = Lattice(
        a1=(float(basis[0, 0]), float(basis[1, 0])),
        a2=(float(basis[0, 1]), float(basis[1, 1])),
    ).reduced()

    # Offset from the phases at the *reduced* basis' reciprocal vectors
    # (integer combinations of the detected peaks, so still true lattice
    # frequencies). For foci at offset + lattice, the component at q_i is
    # |S(q_i)| * exp(-2*pi*i q_i . offset) with a real positive shape factor,
    # hence offset fractions = -phase / 2*pi along each basis vector.
    reciprocal = np.linalg.inv(lattice.matrix)  # rows are q1', q2'
    frac1 = (-_dft_phase(centered, reciprocal[0]) / (2 * np.pi)) % 1.0
    frac2 = (-_dft_phase(centered, reciprocal[1]) / (2 * np.pi)) % 1.0
    offset = lattice.matrix @ np.array([frac1, frac2])
    return Lattice(
        a1=lattice.a1, a2=lattice.a2, offset=(float(offset[0]), float(offset[1]))
    ).reduced()


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
