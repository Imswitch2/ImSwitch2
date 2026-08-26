"""
Aberration phase patterns.
"""

import logging
import numpy as np

from ..engine.parameters import EditorKind,ParamSpec
from ..engine.registry import register_aberration
from ..engine.section import SectionContext

_logger = logging.getLogger(__name__)

# list of available aberrations
ZERNIKE_DEFS = {
    "tilt_x":     {"noll": 2},
    "tilt_y":     {"noll": 3},
    "defocus":    {"noll": 4},
    "astig_obl":  {"noll": 5},
    "astig_vert": {"noll": 6},
    "coma_vert":  {"noll": 7},
    "coma_horiz": {"noll": 8},
    "trefoil_y":  {"noll": 9},
    "trefoil_x":  {"noll": 10},
    "spherical":  {"noll": 11}
}

ZERNIKE_PARAMS = {
    key:ParamSpec(
        0.0,float,step=0.01,decimals=2,
        editor=EditorKind.DOUBLE_SPIN_BOX,
    )
    for key in ZERNIKE_DEFS
}

ZERNIKE_NOLL_BY_KEY = {
    key:data["noll"]
    for key,data in ZERNIKE_DEFS.items()
}


class ZernikeGenerator:
    """
    Helper class to generate wavefronts from Zernike-based phase modes efficiently.

    Formalism:
    ----------
    1. Indexing: **Noll Indices** (j = 1, 2, 3...)
       - j=1: Piston (ignored usually)
       - j=2: Tilt X
       - j=3: Tilt Y
       - j=4: Defocus
       ...

    2. Normalization: **Noll / RMS Normalization**
       - Most modes use the standard Noll-normalized Zernike definitions.
       - The normalization factors (e.g., sqrt(3) for defocus, sqrt(6) for
         astigmatism) are included in the calculation.
       - Coma and spherical aberration intentionally use modified radial terms
         rather than the standard orthogonal Noll polynomials to avoid lateral
         shift happening with standard radial terms.

        NOTE: For standard Noll modes, the coefficient represents the RMS
        (Root Mean Square) error and not the Peak-to-Valley amplitude. This
        exact RMS interpretation does not necessarily apply to the modified
        coma and spherical modes.

       Equation examples:
       - Defocus (j=4): Z = sqrt(3) * (2*rho^2 - 1)
       - Astig (j=5):   Z = sqrt(6) * rho^2 * sin(2*theta)

    3. Coordinates:
       - Defined on the unit circle (rho <= 1.0).
       - Coordinates are normalized by the pupil radius provided through
         `SectionContext`.
    """

    def __init__(self):
        self._grid_cache = {}

    def _get_grid(self,context: SectionContext):
        """
        Retrieves or generates the polar coordinate grid.
        Grid is cached to improve performance on repeated calls with same geometry.
        NOTE: cx and cy are relative offsets from the geometric center of the image.
        """
        h,w = context.shape
        radius = context.pupil_radius_px

        if radius <= 0:
            radius = min(w,h) / 2.0

        cx = context.center_offset_x_px
        cy = context.center_offset_y_px
        key = (w,h,radius,cx,cy)
        if key not in self._grid_cache:
            y, x = np.indices((h, w))
            # Normalize coordinates so the pupil_radius is 1.0 unit
            y = (y - h/2 - cy) / radius
            x = (x - w/2 - cx) / radius

            rho = np.sqrt(x**2 + y**2)
            theta = np.arctan2(y, x)

            self._grid_cache[key] = (rho, theta)

        return self._grid_cache[key]

    def compute_wavefront(
        self,
        context: SectionContext,
        coeffs,
        unit="waves",
        test_zernike_values=False,
    ):
        """
        Compute the complex field ``exp(1j * phase)`` from Zernike coefficients.

        Parameters
        ----------
        context : SectionContext
            Section-wide computation context. It provides the output shape, pupil
            radius, pupil center offsets and wavelength required by the calculation.
            If the pupil radius is zero or negative, it defaults to half the
            smallest section dimension.
        coeffs : dict
            Dictionary mapping Noll indices to coefficient values:
            ``{noll_index: value}``.
        unit : str, optional
            Unit used for the coefficients:

            - ``"waves"``: coefficient 1.0 corresponds to one wavelength of
            aberration, or ``2*pi`` radians of phase.
            - ``"rad"``: coefficient 1.0 corresponds to one radian of phase.
            - ``"um"``: coefficient 1.0 corresponds to one micrometre of optical
            path difference and uses ``context.wavelength_nm`` for conversion.

            Default is ``"waves"``.
        test_zernike_values : bool, optional
            Print sanity checks of the generated phase values, including RMS,
            minimum, maximum and Peak-to-Valley values.
        """
        height,width = context.shape
        if not coeffs:
            return np.ones((height, width), dtype=complex)

        radius = context.pupil_radius_px
        if radius is None or radius <= 0:
            radius = min(width, height) / 2.0

        rho, theta = self._get_grid(context)
        total_phase = np.zeros((height, width), dtype=np.float64)


        scale_factor = 1.0
        if unit == "waves":
            # 1 Wave RMS -> 2*pi Radians RMS
            scale_factor = 2 * np.pi
        elif unit == "um":
             wavelength_um = context.wavelength_nm/1000.0
             # 1 micron OPD -> (2*pi / lambda) Radians
             if wavelength_um is None or wavelength_um == 0:
                 # Fallback to avoid division by zero
                 wavelength_um = 1.0
             scale_factor = (2 * np.pi) / wavelength_um
        # If unit == "rad", scale_factor stays 1.0


        for noll, val in coeffs.items():

            if val == 0:
                continue

            c = val * scale_factor
            term = 0.0

            # Noll Formalism Definitions
            if noll == 1:   term = 1.0
            elif noll == 2: term = 2 * rho * np.sin(theta)                          # Tilt X
            elif noll == 3: term = 2 * rho * np.cos(theta)                          # Tilt Y
            elif noll == 4: term = np.sqrt(3) * (2 * rho**2 - 1)                    # Defocus
            elif noll == 5: term = np.sqrt(6) * rho**2 * np.sin(2*theta)            # Astigmatism (Oblique)
            elif noll == 6: term = np.sqrt(6) * rho**2 * np.cos(2*theta)            # Astigmatism (Vertical)
            # We remove the -2*rho term so we only apply the cubic 'shape'
            elif noll == 7: term = np.sqrt(8) * (3 * rho ** 3) * np.sin(theta)       # Modified Coma
            elif noll == 8: term = np.sqrt(8) * (3 * rho ** 3) * np.cos(theta)       # Modified Coma
            elif noll == 9: term = np.sqrt(8) * rho**3 * np.sin(3 * theta)          # Trefoil Y
            elif noll == 10: term = np.sqrt(8) * rho**3 * np.cos(3 * theta)         # Trefoil X
            elif noll == 11:term = np.sqrt(5) * (6*rho**4 + 1)           # Spherical

            total_phase += c * term

            if test_zernike_values:
                self.check_zernike(total_phase,context)

        return np.exp(1j * total_phase)


    def check_zernike(self, phase, context:SectionContext):
        """
        Calculates statistics (RMS, Peak-to-Valley) of the phase pattern,
        strictly masking pixels inside the unit circle (rho <= 1).

        This is critical because Zernike polynomials are mathematically undefined
        (or explode to infinity) in the corners of a rectangular grid where rho > 1.

        Usage:
            Call this immediately after generating the aberration to verify scaling.
            params:
                phase: 2D numpy array of phase values (in Radians).
                r: The normalization radius used during generation (e.g., 540).
                cx, cy: Center offsets used during generation.

        Expected Result Example:
            If you input a coefficient of 0.5 waves (Noll normalization):
            - The RMS should be exactly ~0.50 waves (~3.14 radians).
            - The Peak-to-Valley will depend on the specific term (e.g., Defocus ~1.73 waves).
        """
        h, w = phase.shape
        y, x = np.indices((h, w))

        r = context.pupil_radius_px
        cx=context.center_offset_x_px
        cy=context.center_offset_y_px

        # Re-calculate the grid locally just for the mask
        y = (y - h/2 - cy) / r
        x = (x - w/2 - cx) / r
        rho = np.sqrt(x**2 + y**2)

        # Create a Boolean Mask for the valid unit disk
        mask = rho <= 1.0

        # Extract only valid pixels
        valid_phase = phase[mask]

        if valid_phase.size == 0:
            _logger.warning("No pixels inside the Zernike unit disk")
            return

        # Calculate Stats
        min_val = np.min(valid_phase)
        max_val = np.max(valid_phase)
        pv = max_val - min_val

        # RMS calculation
        mean_val = np.mean(valid_phase)
        rms = np.sqrt(np.mean((valid_phase - mean_val)**2))

        _logger.debug("Zernike statistics inside unit circle")
        _logger.debug("Min phase: %.2f rad",min_val)
        _logger.debug("Max phase: %.2f rad",max_val)
        _logger.debug("Peak-to-valley: %.2f rad (%.2f waves)",pv,pv / (2 * np.pi))
        _logger.debug("Calculated RMS: %.2f rad (%.2f waves)",rms,rms / (2 * np.pi))



_ZERNIKE_GENERATOR = ZernikeGenerator()


@register_aberration(
    "zernike",
    params=ZERNIKE_PARAMS,
    noll_by_key=ZERNIKE_NOLL_BY_KEY,
)
def zernike_aberration(context: SectionContext,**params):
    """
    Apply Zernike aberration modes over the section pupil.

    Coefficients are expressed in waves and mapped to their registered
    Noll indices before generating the combined aberration wavefront.
    """
    coeffs = {
        noll:params.get(key,ZERNIKE_PARAMS[key].default)
        for key,noll in ZERNIKE_NOLL_BY_KEY.items()
    }

    return _ZERNIKE_GENERATOR.compute_wavefront(
        context=context,
        coeffs=coeffs,
        unit="waves",
    )
