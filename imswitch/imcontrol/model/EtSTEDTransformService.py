import importlib.util
import json
from pathlib import Path
from types import ModuleType

import numpy as np
from scipy.optimize import least_squares


class EtSTEDTransformService:
    """Load, validate, calibrate, and apply etSTED coordinate transforms."""

    expected_coefficients = 20

    def __init__(self) -> None:
        self.name: str | None = None
        self.function = None
        self.coefficients = np.zeros(self.expected_coefficients)

    def load(self, transform_dir: str, transform_name: str, coefficients_name: str) -> None:
        """Load a transform function and coefficient file from the transform directory."""
        self.name = transform_name
        self.function = self._load_transform_function(Path(transform_dir), transform_name)
        self.coefficients = self.load_coefficients(Path(transform_dir) / f'{coefficients_name}.csv')

    def apply(self, coords, etsted_info=None) -> np.ndarray:
        """Apply the loaded transform to one coordinate pair."""
        self._ensure_loaded()
        coords = self.prepare_input_coords(coords, etsted_info)
        return np.asarray(self.function(coords, self.coefficients))

    def set_coefficients(self, coefficients) -> None:
        """Set active transform coefficients after validation."""
        self.coefficients = self.validate_coefficients(coefficients)

    def prepare_input_coords(self, coords, etsted_info=None) -> np.ndarray:
        """Apply setup-level coordinate flags before transform application."""
        coords = np.asarray(coords, dtype=float).copy()
        if coords.size < 2:
            raise ValueError('etSTED coordinate transforms require at least two coordinate values.')

        if etsted_info is not None:
            if getattr(etsted_info, 'swapXY', False):
                coords[[0, 1]] = coords[[1, 0]]
            if getattr(etsted_info, 'invertX', False):
                coords[0] = -coords[0]
            if getattr(etsted_info, 'invertY', False):
                coords[1] = -coords[1]
        return coords

    def calibrate(self, low_res_coords, high_res_coords) -> np.ndarray:
        """Fit third-order polynomial coefficients from paired coordinates."""
        xdata = np.asarray(low_res_coords, dtype=np.float32)
        ydata = np.asarray(high_res_coords, dtype=np.float32)
        if xdata.shape != ydata.shape:
            raise ValueError('Low- and high-resolution coordinate arrays must have the same shape.')
        if xdata.ndim != 2 or xdata.shape[1] != 2:
            raise ValueError('Calibration coordinates must have shape (n, 2).')

        initguess = np.zeros(self.expected_coefficients, dtype=np.float32)
        res_lsq = least_squares(self.poly_thirdorder_residuals, initguess, args=(xdata, ydata), method='lm')
        self.coefficients = self.validate_coefficients(res_lsq.x)
        return self.coefficients

    def save_calibration_metadata(
        self,
        path: str | Path,
        low_res_coords,
        high_res_coords,
        coefficients=None,
    ) -> None:
        """Persist calibration inputs and resulting coefficients as JSON metadata."""
        coefficients = self.coefficients if coefficients is None else coefficients
        metadata = {
            'model': 'third_order_polynomial',
            'coefficient_count': self.expected_coefficients,
            'low_res_coords': np.asarray(low_res_coords, dtype=float).tolist(),
            'high_res_coords': np.asarray(high_res_coords, dtype=float).tolist(),
            'coefficients': self.validate_coefficients(coefficients).tolist(),
        }
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(metadata, indent=2))

    @classmethod
    def load_coefficients(cls, path: Path) -> np.ndarray:
        """Load and validate one third-order transform coefficient vector."""
        try:
            coefficients = np.loadtxt(path, dtype=float)
        except OSError as e:
            raise FileNotFoundError(f'Transform coefficient file not found: {path}') from e
        except ValueError as e:
            raise ValueError(f'Transform coefficient file is not numeric: {path}') from e
        return cls.validate_coefficients(coefficients)

    @classmethod
    def validate_coefficients(cls, coefficients) -> np.ndarray:
        """Validate that coefficients are a flat third-order transform vector."""
        coefficients = np.asarray(coefficients, dtype=float).reshape(-1)
        if coefficients.size != cls.expected_coefficients:
            raise ValueError(
                f'etSTED coordinate transforms require {cls.expected_coefficients} coefficients; '
                f'got {coefficients.size}.'
            )
        return coefficients

    @staticmethod
    def poly_thirdorder_residuals(a, x, y) -> list[float]:
        """Residuals for third-order polynomial coordinate transform fitting."""
        res = []
        for i in range(0, len(x)):
            transformed = EtSTEDTransformService.poly_thirdorder_transform(a, x[i])
            res.append(transformed[0] - y[i, 0])
            res.append(transformed[1] - y[i, 1])
        return res

    @staticmethod
    def poly_thirdorder_transform(a, x) -> tuple[float, float]:
        """Apply third-order polynomial coefficients to one coordinate pair."""
        c1 = x[0]
        c2 = x[1]
        x_i1 = (
            a[0]*c1**3 + a[1]*c2**3 + a[2]*c2*c1**2 + a[3]*c1*c2**2
            + a[4]*c1**2 + a[5]*c2**2 + a[6]*c1*c2 + a[7]*c1 + a[8]*c2 + a[9]
        )
        x_i2 = (
            a[10]*c1**3 + a[11]*c2**3 + a[12]*c2*c1**2 + a[13]*c1*c2**2
            + a[14]*c1**2 + a[15]*c2**2 + a[16]*c1*c2 + a[17]*c1 + a[18]*c2 + a[19]
        )
        return (x_i1, x_i2)

    def _load_transform_function(self, transform_dir: Path, transform_name: str):
        path = transform_dir / f'{transform_name}.py'
        module = self._load_module_from_path(transform_name, path)
        try:
            return getattr(module, transform_name)
        except AttributeError as e:
            raise RuntimeError(f'Transform module {path} does not define {transform_name}().') from e

    def _load_module_from_path(self, module_name: str, path: Path) -> ModuleType:
        if not path.is_file():
            raise FileNotFoundError(f'Transform module not found: {path}')
        spec = importlib.util.spec_from_file_location(f'imswitch_etsted_transform_{module_name}', path)
        if spec is None or spec.loader is None:
            raise ImportError(f'Could not load transform module from {path}')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _ensure_loaded(self) -> None:
        if self.function is None:
            raise RuntimeError('No etSTED coordinate transform is loaded.')
