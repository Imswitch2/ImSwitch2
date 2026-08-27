# ISM reassignment integration test path

The validated xrecon-style ISM reassignment backend is currently available
through two paths during the integration phase:

- **MoNaLISA → ISM reassignment**: the preferred integrated path. It reuses the
  MoNaLISA pattern parameters, scan metadata, automatic scan-orientation
  detection, timepoint handling, result display, and saving. The existing
  **CPU/GPU** selector chooses the reconstruction backend.
- **ISM reassignment (GPU)** as a standalone reconstructor: retained temporarily
  as an A/B reference for the previously validated CuPy implementation.

The pre-existing **Enhanced confocal (ISM)** MoNaLISA method is unrelated and
has intentionally not been changed.

The ISM mode always auto-detects the scan orientation from the first timepoint.
It uses the same rectangular Fast Gauss orientation detector and converts the
detected orientation to the xrecon scan convention used by the ISM backend.

The Fast Gauss and xrecon sign conventions are not identical. The conversion is
handled internally by the integrated path; users do not need to set the xrecon
orientation manually.

The **Unidirectional scan** value from the scanning parameters is still used to
distinguish ordinary raster scanning from bidirectional/snake acquisition.

For acquisitions with multiple timepoints, the orientation is detected once
from the first timepoint and reused for all remaining timepoints.

Multiple timepoints are reconstructed independently and returned as a standard
`MonalisaProcessingResult`, so the T axis appears as a normal Napari slider.

The reconstruction runs on ImProcess's worker thread. Each timepoint is passed
to the shared ISM reconstruction backend as one `(M*M, Y, X)` stack.

## CPU and GPU backends

The integrated **ISM reassignment** method supports both backends:

- **GPU** uses CuPy.
- **CPU** uses NumPy/SciPy.

Both paths use the same reconstruction algorithm and shared implementation.
The CPU backend is primarily useful as a fallback and as a numerical reference;
it is expected to be slower than the GPU backend.

The standalone **ISM reassignment (GPU)** reconstructor remains GPU-only so it
continues to provide a stable reference against the originally validated CuPy
path.

CuPy must be available only when the GPU backend is selected. For a CUDA 12 pip
environment this is typically:

```bash
pip install cupy-cuda12x
```

Use the CuPy build matching the machine's CUDA/driver setup.

## ISM reassignment options

- **Oversampling**: xrecon microlens-grid oversampling; default 2.
- **ISM shift**: Fourier reassignment fraction; default 0.5.
- **Subtract patch mean**: preserve the validated per-patch mean subtraction.
- **Frame batch**: 0 processes all frames of one scan together. Reduce it if
  temporary memory becomes limiting, particularly on GPU.

## Current intentional limitations of the integrated path

- square XY scan;
- one Z slice;
- one line-step condition;
- square camera frames;
- axis-aligned rectangular illumination pattern;
- multiple timepoints are supported.

## A/B validation

Until the integrated path has been sufficiently validated on real acquisitions,
keep the standalone `ism-reassign` reconstructor enabled.

For a one-timepoint dataset, compare the standalone
**ISM reassignment (GPU)** output with **MoNaLISA → ISM reassignment** using the
same:

- pattern parameters;
- pixel size;
- PSF FWHM;
- oversampling;
- ISM shift;
- mean-subtraction setting.

When comparing against the standalone reconstructor, use the standalone xrecon
scan orientation known to be correct for the acquisition. The integrated path
detects the Fast Gauss orientation automatically and converts it internally to
the corresponding xrecon convention.

For CPU/GPU parity checks within the integrated path, use identical
reconstruction parameters. Small floating-point differences are expected, but
the reconstructed images should otherwise agree numerically.
