"""SNOUTY lightsheet deconvolution reconstructor plugin."""

from typing import TYPE_CHECKING

import numpy as np
from qtpy import QtWidgets

from imswitch.imcommon.model import initLogger
from imswitch.improcess.reconstructors.base import Reconstructor
from imswitch.improcess.reconstructors.snouty._pipeline import (
    _validate_geometry,
    load_restack_deskew_timelapse,
)
from imswitch.improcess.reconstructors.snouty.metadata import DEFAULT_PARAMS
from imswitch.improcess.reconstructors.snouty.result import SnoutyResult
from .deconvolve import DeconvolutionProcessorCPU, DeconvolutionProcessorGPU
from .defaults import DECONVOLUTION_DEFAULTS
from .kernel import effective_kernel
from .params_widget import SnoutyDeconvolutionParamsWidget

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

        if context is not None:
            context.report('allocate', 0, 1, 'Building the effective kernel')
        kernel = effective_kernel(params)
        self._logger.info(
            f'Effective kernel {kernel.shape} at {params["sample_vx_size"]} nm voxels'
        )
        if context is not None:
            context.report('allocate', 1, 1, 'Kernel ready')

        geometry = {
            key: params[key]
            for key in ('c_px', 'alpha_deg', 'dy', 'sample_vx_size', 'camera_offset', 'flip_data')
        }
        options = dict(
            normalisation_clip=float(params['normalisation_clip']),
            gradient_consent=bool(params['gradient_consent']),
        )
        deconvolver = None
        counter = {'timepoint': 0, 'total': max(1, int(params.get('n_timepoints', 1)))}

        def per_timepoint(_deskew_processor, tp_stack, use_gpu, cp):
            nonlocal deconvolver
            if deconvolver is None:
                processor_class = (
                    DeconvolutionProcessorGPU if use_gpu else DeconvolutionProcessorCPU
                )
                deconvolver = processor_class(geometry, kernel, **options)

            def progress(done, total):
                if context is not None:
                    context.report(
                        'assemble',
                        counter['timepoint'] * total + done,
                        counter['total'] * total,
                        f'Timepoint {counter["timepoint"] + 1}/{counter["total"]}, '
                        f'iteration {done}/{total}',
                    )

            def cancel():
                if context is not None:
                    context.check_cancelled()

            volume = deconvolver.run(tp_stack, iterations, progress=progress, cancel=cancel)
            counter['timepoint'] += 1
            return cp.asnumpy(volume) if use_gpu else np.asarray(volume)

        volumes = load_restack_deskew_timelapse(
            data_obj, params,
            per_timepoint_fn=per_timepoint,
            logger=self._logger,
        )

        if len(volumes) > 1:
            result_data = np.stack(volumes, axis=0)
        else:
            result_data = volumes[0]
        if context is not None:
            context.report('finalize', 1, 1, 'Deconvolution complete')
        self._logger.info(f'Deconvolution complete: {result_data.shape}')

        data_min = float(np.percentile(result_data, 1))
        data_max = float(np.percentile(result_data, 99.9))
        return SnoutyResult(
            name=f"{data_obj.name}_deconvolved",
            data=result_data.astype(np.float32, copy=False),
            params=params,
            display_levels=(data_min, data_max),
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
