import importlib
import importlib.util
import sys
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from inspect import signature
from pathlib import Path

import numpy as np


@dataclass
class EtSTEDPipelineResult:
    """Normalized result returned by an etSTED analysis pipeline."""

    coords_detected: np.ndarray
    exinfo: object
    analysis_image: np.ndarray | None = None


class EtSTEDPipelineRunner:
    """Load, validate, parse parameters for, and execute one etSTED pipeline."""

    _required_params = ('img', 'prev_frames', 'binary_mask', 'testmode', 'exinfo')

    def __init__(self) -> None:
        self.name: str | None = None
        self.function: Callable | None = None
        self.parameters: dict = {}

    def load(self, name: str) -> dict:
        """Load a pipeline by module/function name and return its signature parameters."""
        pipeline = getattr(importlib.import_module(name), name)
        parameters = signature(pipeline).parameters
        self._validate_signature(name, parameters)
        self.name = name
        self.function = pipeline
        self.parameters = parameters
        return parameters

    def load_file(self, path) -> dict:
        """Load a pipeline from its file (``<name>.py`` defining ``<name>``).

        What :meth:`load` does for a module found on ``sys.path``, for a caller
        that has the file: the module gets a private name (so two folders can
        hold a pipeline of the same name, and an edited file is read again),
        and the file's folder is put on ``sys.path`` as etSTED does, for a
        pipeline that imports a helper next to it. The signature check and
        everything after are the same.
        """
        path = Path(path)
        name = path.stem
        if not name.isidentifier():
            raise RuntimeError(f'{path.name}: the file name must be the pipeline function name '
                               f'(a valid Python name), not {name!r}.')
        folder = str(path.resolve().parent)
        if folder not in sys.path:
            sys.path.append(folder)
        module_name = f'_etsted_pipeline_{abs(hash(str(path.resolve())))}_{name}'
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f'Cannot load the pipeline file {path}.')
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        pipeline = getattr(module, name, None)
        if not callable(pipeline):
            raise RuntimeError(f'{path.name} must define a function called {name!r} '
                               f'(the file name without .py).')
        parameters = signature(pipeline).parameters
        self._validate_signature(name, parameters)
        self.name = name
        self.function = pipeline
        self.parameters = parameters
        return parameters

    def parse_parameter_values(self, param_edits: Sequence) -> list[float]:
        """Parse user-editable pipeline parameters from widget fields."""
        self._ensure_loaded()
        pipeline_param_names = self.get_user_parameter_names()
        if len(param_edits) != len(pipeline_param_names):
            raise RuntimeError(
                f'Pipeline {self.name} expects {len(pipeline_param_names)} user parameters, '
                f'but the widget contains {len(param_edits)} fields.'
            )

        param_values = []
        for param_name, item in zip(pipeline_param_names, param_edits):
            try:
                param_values.append(float(item.text()))
            except ValueError as e:
                raise ValueError(f'Invalid value for pipeline parameter {param_name}: {item.text()}') from e
        return param_values

    def get_user_parameter_names(self) -> list[str]:
        """Return parameter names that should be supplied from the UI."""
        return [
            pipeline_param_name
            for pipeline_param_name in self.parameters
            if pipeline_param_name not in self._required_params
        ]

    def execute(
        self,
        img: np.ndarray,
        prev_frames: deque,
        binary_mask: np.ndarray | None,
        testmode: bool,
        exinfo: object,
        param_values: Sequence[float],
    ) -> EtSTEDPipelineResult:
        """Run the loaded pipeline and normalize its output contract."""
        self._ensure_loaded()
        raw_result = self.function(
            img,
            prev_frames,
            binary_mask,
            testmode,
            exinfo,
            *param_values
        )

        if testmode:
            if not isinstance(raw_result, tuple) or len(raw_result) != 3:
                raise RuntimeError(
                    f'Pipeline {self.name} must return '
                    '(coords_detected, exinfo, analysis_image) in test modes.'
                )
            coords_detected, exinfo, analysis_image = raw_result
        else:
            if not isinstance(raw_result, tuple) or len(raw_result) != 2:
                raise RuntimeError(
                    f'Pipeline {self.name} must return (coords_detected, exinfo) in experiment mode.'
                )
            coords_detected, exinfo = raw_result
            analysis_image = None

        return EtSTEDPipelineResult(
            coords_detected=self._normalize_coords(coords_detected),
            exinfo=exinfo,
            analysis_image=analysis_image
        )

    def _validate_signature(self, name: str, parameters: dict) -> None:
        missing_params = [
            param_name for param_name in self._required_params
            if param_name not in parameters
        ]
        if missing_params:
            raise RuntimeError(
                f'Pipeline {name} is missing required parameters: {", ".join(missing_params)}'
            )

    def _normalize_coords(self, coords_detected) -> np.ndarray:
        if coords_detected is None:
            return np.empty((0, 2))
        coords = np.asarray(coords_detected, dtype=float)
        if coords.size == 0:
            return np.empty((0, 2))
        if coords.shape == (2,):
            return coords.reshape(1, 2)
        if coords.ndim != 2 or coords.shape[1] != 2:
            raise RuntimeError(
                f'Pipeline {self.name} returned coordinates with shape {coords.shape}; '
                'expected (2,) or (N, 2).'
            )
        return coords

    def _ensure_loaded(self) -> None:
        if self.function is None:
            raise RuntimeError('No etSTED analysis pipeline is loaded.')
