"""SNOUTY lightsheet deconvolution reconstructor plugin."""

from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.snouty._pipeline import (
    _validate_geometry,
    display_levels,
    load_restack_deskew_timelapse,
    stack_timepoints,
)
from imswitch.improcess.reconstructors.snouty.metadata import (
    DEFAULT_PARAMS,
    recorded_snouty_geometry,
)
from imswitch.improcess.reconstructors.snouty.result import SnoutyResult
from .deconvolve import (
    DeconvolutionProcessorCPU,
    DeconvolutionProcessorGPU,
    _to_host,
)
from .defaults import DECONVOLUTION_DEFAULTS
from .kernel import effective_kernel
from .params_widget import SnoutyDeconvolutionParamsWidget
from .strips import CameraStrip, camera_strips, describe_strips, parse_wavelengths

if TYPE_CHECKING:
    from imswitch.improcess.model import DataObj


class SnoutyDeconvolutionReconstructor(Reconstructor):
    """
    Richardson–Lucy deconvolution of SNOUTY / OPM / MS-RESOLFT raw stacks.

    Where the SNOUTY deskew only re-grids the camera voxels, this plugin fits a
    sample volume on the same output grid whose blur with the effective
    light-sheet kernel, read along the tilted scan geometry, reproduces the
    camera stack. It is the ``Deconvolve`` method of the Deconvolution_GUI
    tool as an ImProcess plugin, with CPU and GPU (CuPy) backends.

    Shares the deskew plugin's loading, restacking, timepoint splitting and
    result type, so a deconvolved volume lines up voxel for voxel with the
    deskewed one.

    A multicolour frame, with the colour channels side by side along the
    camera columns, is deconvolved strip by strip with one wavelength each
    when a multicolor alignment file names the strips; the result is still
    one full-width volume, so Multicolor Apply handles it like a deskew.

    *Sweep iterations* outputs one volume per iteration of the first
    timepoint as a stack, for choosing the iteration count.
    """

    name = "SNOUTY deconvolution"
    id = "snouty-deconvolution"
    file_extensions = ["hdf5", "h5", "tiff"]
    description = "Richardson-Lucy deconvolution through the SNOUTY/OPM/MS-RESOLFT scan geometry"
    default_save_subdir = "deconvolved"

    @classmethod
    def default_params(cls) -> dict:
        return {
            'device': 'CPU',
            'n_timepoints': 1,
            **DEFAULT_PARAMS,
            **DECONVOLUTION_DEFAULTS,
        }

    def __init__(self):
        self._logger = initLogger('SnoutyDeconvolutionReconstructor')

    def make_param_widget(self, parent: QtWidgets.QWidget) -> QtWidgets.QWidget:
        return SnoutyDeconvolutionParamsWidget(parent)

    def make_metadata_dialog(self, parent: QtWidgets.QWidget) -> QtWidgets.QDialog | None:
        """Geometry lives in the parameter widget, as for the deskew plugin."""
        return None

    # ----------------------------------------------------------------- plan
    def _plan_strips(self, params: dict, n_columns: int) -> list[CameraStrip]:
        """The colour strips to deconvolve: one per alignment ROI, or the whole frame."""
        alignment_path = str(params.get('alignment_path', '') or '').strip()
        wavelengths = parse_wavelengths(params.get('wavelengths_nm', ''))
        if not alignment_path:
            if len(wavelengths) > 1:
                raise ValueError(
                    f"{len(wavelengths)} wavelengths were given but no alignment file names "
                    f"the colour strips they belong to"
                )
            wavelength = wavelengths[0] if wavelengths else float(params['wavelength_nm'])
            return [CameraStrip(0, 0, int(n_columns), wavelength)]
        from imswitch.improcess.analysis.multicolor import load_alignment

        alignment = load_alignment(Path(alignment_path))
        strips = camera_strips(alignment, int(n_columns), wavelengths, float(params['wavelength_nm']))
        self._logger.info(f'Colour strips from {alignment_path}: {describe_strips(strips)}')
        return strips

    def _build_processors(
        self, params: dict, strips: list[CameraStrip], n_columns: int, use_gpu: bool, context
    ):
        processor_class = DeconvolutionProcessorGPU if use_gpu else DeconvolutionProcessorCPU
        geometry = {
            key: params[key]
            for key in ('c_px', 'alpha_deg', 'dy', 'sample_vx_size', 'camera_offset', 'flip_data')
        }
        options = dict(
            normalisation_clip=float(params['normalisation_clip']),
            gradient_consent=bool(params['gradient_consent']),
        )
        processors = []
        for number, strip in enumerate(strips):
            if context is not None:
                context.report('allocate', number, len(strips),
                               f'Building the effective kernel at {strip.wavelength_nm:g} nm')
            kernel = effective_kernel({**params, 'wavelength_nm': strip.wavelength_nm})
            self._logger.info(
                f'Effective kernel {kernel.shape} at {params["sample_vx_size"]} nm voxels, '
                f'{strip.wavelength_nm:g} nm'
            )
            processors.append(
                processor_class(
                    geometry, kernel, column_origin=strip.start,
                    frame_columns=int(n_columns), **options,
                )
            )
        if context is not None:
            context.report('allocate', len(strips), len(strips), 'Kernels ready')
        return processors

    # -------------------------------------------------------------- process
    def process(
        self, data_obj: 'DataObj', params: dict, context=None
    ) -> SnoutyResult:
        """Deconvolve every timepoint of ``data_obj`` into a ``SnoutyResult``.

        ``params`` holds the SNOUTY deskew keys plus those of
        :data:`DECONVOLUTION_DEFAULTS`. Progress is reported per iteration
        through ``context`` when one is given, and cancellation is honoured
        between iterations.
        """
        params = {**self.default_params(), **params}
        _validate_geometry(params)
        iterations = int(params['iterations'])
        if iterations < 1:
            raise ValueError(f"Iterations must be at least 1, got {iterations}")
        sweep = bool(params.get('sweep_iterations', False))
        # Fail on a malformed wavelength list before any data is read.
        parse_wavelengths(params.get('wavelengths_nm', ''))

        total_timepoints = max(1, int(params.get('n_timepoints', 1)))
        recorded = recorded_snouty_geometry(data_obj)
        if recorded is not None and recorded[2] is not None:
            total_timepoints = max(1, int(recorded[2]))
        if sweep:
            total_timepoints = 1
        counter = {'timepoint': 0, 'total': total_timepoints}
        plan = {}

        def per_timepoint(_deskew_processor, tp_stack, use_gpu, cp):
            if not plan:
                plan['strips'] = self._plan_strips(params, tp_stack.shape[2])
                plan['processors'] = self._build_processors(
                    params, plan['strips'], tp_stack.shape[2], use_gpu, context
                )
            strips, processors = plan['strips'], plan['processors']
            full_shape = processors[0].output_shape(tuple(tp_stack.shape))
            out = np.zeros(((iterations,) if sweep else ()) + full_shape, dtype=np.float32)
            steps_per_timepoint = len(strips) * iterations

            for number, (strip, processor) in enumerate(zip(strips, processors)):
                def progress(done, total, number=number):
                    if context is not None:
                        context.report(
                            'assemble',
                            counter['timepoint'] * steps_per_timepoint + number * total + done,
                            counter['total'] * steps_per_timepoint,
                            f'Timepoint {counter["timepoint"] + 1}/{counter["total"]}, '
                            f'strip {number + 1}/{len(strips)}, iteration {done}/{total}',
                        )

                def cancel():
                    if context is not None:
                        context.check_cancelled()

                def on_iteration(iteration, volume, processor=processor):
                    offset = processor.column_offset
                    out[iteration - 1, :, :, offset:offset + volume.shape[2]] = _to_host(volume)

                sub_stack = tp_stack[:, :, strip.start:strip.stop]
                volume = processor.run(
                    sub_stack, iterations, progress=progress, cancel=cancel,
                    on_iteration=on_iteration if sweep else None,
                )
                if not sweep:
                    offset = processor.column_offset
                    out[:, :, offset:offset + volume.shape[2]] = _to_host(volume)
                del volume
            counter['timepoint'] += 1
            return out

        volumes = load_restack_deskew_timelapse(
            data_obj, params,
            per_timepoint_fn=per_timepoint,
            logger=self._logger,
            timepoint_limit=1 if sweep else None,
        )

        if sweep:
            result_data = volumes[0]
            leading_axis = 'Iteration'
            name = f"{data_obj.name}_deconvolved_sweep"
        else:
            result_data = stack_timepoints(volumes)
            leading_axis = 'T'
            name = f"{data_obj.name}_deconvolved"
        if context is not None:
            context.report('finalize', 1, 1, 'Deconvolution complete')
        self._logger.info(f'Deconvolution complete: {result_data.shape}')

        data_min, data_max = display_levels(result_data)
        return SnoutyResult(
            name=name,
            data=result_data.astype(np.float32, copy=False),
            params=params,
            display_levels=(data_min, data_max),
            leading_axis=leading_axis,
        )


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
