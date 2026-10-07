"""Estimate lattice translation for a candidate linear geometry.

Template correlation provides the main translation initializer.  Lightweight
nearest-neighbour hypotheses are retained as numerical fallbacks when template
correlation is unavailable or ambiguous.
"""

from __future__ import annotations

import numpy as np
from scipy.fft import irfft2,next_fast_len,rfft2
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree

class _CorrelationWorkspace:
    """Reuse the measured-image FFT while scoring lattice templates."""

    def __init__(self,image):
        array = np.asarray(image,dtype=np.float64)
        if array.ndim != 2:
            raise ValueError("Correlation image must be two-dimensional")
        self.image = array - float(np.mean(array))
        self.h,self.w = array.shape
        self.fft_shape = (
            next_fast_len(2*self.h-1),
            next_fast_len(2*self.w-1),
        )
        self.image_fft = rfft2(self.image,s=self.fft_shape)
        self.image_norm = max(float(np.linalg.norm(self.image)),1e-12)
        self.base_translation = np.array([
            0.5*(self.w-1),0.5*(self.h-1),
        ],dtype=np.float64)

    def translation_for(self,logical,linear):
        template,valid_count = _render_lattice_template(
            (self.h,self.w),logical,linear,self.base_translation,
        )
        if valid_count < max(3,int(np.ceil(0.5*logical.shape[1]))):
            return None

        template -= float(np.mean(template))
        template_norm = float(np.linalg.norm(template))
        if template_norm <= 1e-12:
            return None

        template_fft = rfft2(
            template[::-1,::-1],s=self.fft_shape,
        )
        convolution = irfft2(
            self.image_fft*template_fft,s=self.fft_shape,
        )
        full = convolution[:2*self.h-1,:2*self.w-1]
        peak_index = int(np.argmax(full))
        py,px = np.unravel_index(peak_index,full.shape)
        sub_x = _quadratic_peak_offset(full[py,:],px)
        sub_y = _quadratic_peak_offset(full[:,px],py)
        lag_x = (float(px)+sub_x) - float(self.w-1)
        lag_y = (float(py)+sub_y) - float(self.h-1)
        translation = self.base_translation + np.array(
            [lag_x,lag_y],dtype=np.float64,
        )
        score = float(full[py,px]) / (self.image_norm*template_norm)
        return translation,score

def _render_lattice_template(shape,logical,linear,translation):
    """Render the known finite logical lattice for phase/offset correlation."""
    h,w = int(shape[0]),int(shape[1])
    logical = np.asarray(logical,dtype=np.float64)
    linear = np.asarray(linear,dtype=np.float64)
    translation = np.asarray(translation,dtype=np.float64)
    positions = linear.dot(logical) + translation[:,None]
    template = np.zeros((h,w),dtype=np.float64)
    valid = (
        (positions[0] >= 0.0) & (positions[0] <= w-1.0)
        & (positions[1] >= 0.0) & (positions[1] <= h-1.0)
    )
    for x,y in positions[:,valid].T:
        x0 = int(np.floor(x)); y0 = int(np.floor(y))
        x1 = min(w-1,x0+1); y1 = min(h-1,y0+1)
        dx = float(x-x0); dy = float(y-y0)
        template[y0,x0] += (1.0-dx)*(1.0-dy)
        template[y0,x1] += dx*(1.0-dy)
        template[y1,x0] += (1.0-dx)*dy
        template[y1,x1] += dx*dy
    if np.any(template):
        template = gaussian_filter(template,0.9)
    return template,int(np.count_nonzero(valid))

def _quadratic_peak_offset(values,index):
    if index <= 0 or index >= len(values)-1:
        return 0.0
    left = float(values[index-1])
    center = float(values[index])
    right = float(values[index+1])
    denominator = left - 2.0*center + right
    if abs(denominator) <= 1e-12:
        return 0.0
    offset = 0.5*(left-right)/denominator
    return float(np.clip(offset,-0.5,0.5))

def _best_period_neighbor_translation(
    logical,points,linear,translation,options,tree,
):
    """Resolve the one-period finite-lattice ambiguity cheaply before fitting."""
    logical = np.asarray(logical,dtype=np.float64)
    points = np.asarray(points,dtype=np.float64)
    linear = np.asarray(linear,dtype=np.float64)
    periods = np.linalg.norm(linear,axis=0)
    gate = max(
        1.5,float(options.matching_gate_fraction)*float(np.min(periods)),
    )
    base = linear.dot(logical)
    best = None
    for candidate in _period_neighbor_translations(translation,linear):
        predicted = base + candidate[:,None]
        distances,nearest = tree.query(
            predicted.T,k=1,distance_upper_bound=float(gate),
        )
        valid = np.isfinite(distances) & (nearest < points.shape[1])
        if not np.any(valid):
            continue
        count = len(set(int(value) for value in nearest[valid]))
        median = float(np.median(distances[valid]))
        score = (count,-median)
        if best is None or score > best[0]:
            best = (score,candidate)
    return (
        np.asarray(translation,dtype=np.float64)
        if best is None else np.asarray(best[1],dtype=np.float64)
    )

def _period_neighbor_translations(translation,linear):
    translation = np.asarray(translation,dtype=np.float64)
    linear = np.asarray(linear,dtype=np.float64)
    result = []
    # Center first so the common case still costs one refinement.
    order = (
        (0,0),(1,0),(-1,0),(0,1),(0,-1),
        (1,1),(1,-1),(-1,1),(-1,-1),
    )
    for ix,iy in order:
        result.append(
            translation + linear.dot(
                np.asarray([ix,iy],dtype=np.float64)
            )
        )
    return result

def _translation_hypotheses(logical,points,linear,options,max_hypotheses=6):
    """Return robust initial translations for a finite/partially missing lattice.

    A centroid initializer is fast but can be displaced by one full lattice
    period when edge spots are missing.  We therefore align a few central model
    points with central detections, score those hypotheses with a nearest-neighbor
    gate, and retain only the best small set for iterative affine fitting.
    """
    logical = np.asarray(logical,dtype=np.float64)
    points = np.asarray(points,dtype=np.float64)
    linear = np.asarray(linear,dtype=np.float64)
    periods = np.linalg.norm(linear,axis=0)
    gate = max(1.5,float(options.matching_gate_fraction)*float(np.min(periods)))
    tree = cKDTree(points.T)

    hypotheses = [_initial_translation(logical,points,linear)]
    model_order = np.argsort(np.sum(logical*logical,axis=0))[:min(9,logical.shape[1])]
    center = np.median(points,axis=1)
    point_order = np.argsort(np.sum((points-center[:,None])**2,axis=0))[
        :min(30,points.shape[1])
    ]
    for model_index in model_order:
        base = linear.dot(logical[:,model_index])
        for point_index in point_order:
            hypotheses.append(points[:,point_index]-base)

    # Coarse de-duplication avoids repeatedly testing translations that differ
    # only by subpixel noise.
    quantization = max(0.5,0.25*gate)
    unique = {}
    for translation in hypotheses:
        key = tuple(np.round(np.asarray(translation)/quantization).astype(int))
        unique.setdefault(key,np.asarray(translation,dtype=np.float64))

    scored = []
    predicted_base = linear.dot(logical)
    for translation in unique.values():
        predicted = predicted_base + translation[:,None]
        distances,nearest = tree.query(
            predicted.T,k=1,distance_upper_bound=float(gate),
        )
        valid = np.isfinite(distances) & (nearest < points.shape[1])
        if not np.any(valid):
            continue
        # Unique detections approximate the greedy one-to-one match count.
        count = len(set(int(v) for v in nearest[valid]))
        median_distance = float(np.median(distances[valid]))
        scored.append((count,-median_distance,translation))
    scored.sort(key=lambda item:(item[0],item[1]),reverse=True)
    return [item[2] for item in scored[:int(max_hypotheses)]]

def _initial_translation(logical,points,linear):
    predicted = linear.dot(logical)
    return np.median(points,axis=1) - np.median(predicted,axis=1)
