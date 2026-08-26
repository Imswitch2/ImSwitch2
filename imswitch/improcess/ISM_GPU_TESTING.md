# GPU ISM reassignment test path

This branch adds an isolated **ISM reassignment (GPU)** reconstructor. It does
not use the experimental `Enhanced confocal (ISM)` code inside the MoNaLISA
reconstructor.

## Enable the plugin

If your setup JSON has an explicit `processing.reconstructors` list, add:

```json
"ism-reassign"
```

For example:

```json
"processing": {
  "reconstructors": ["view-only", "monalisa", "ism-reassign"]
}
```

CuPy must be available in the ImSwitch environment. The existing ImProcess GPU
paths use the same dependency. For a CUDA 12 pip environment this is typically:

```bash
pip install cupy-cuda12x
```

Use the CuPy build matching the CUDA/driver setup of the machine.

## Test workflow

1. Open the raw ISM acquisition in ImProcess.
2. Select **ISM reassignment (GPU)** in the reconstructor picker.
3. Set **Pixel size** and, if needed, the initial Row/Col period guesses.
4. Click **Pattern → Find pattern**.
5. Check the detected lattice on the raw mean image. `Show pattern` is enabled
   automatically after a successful localization.
6. Set the scan orientation. The current default is `X+Y-`; enable
   `Bidirectional scan` if every second fast-scan row must be reversed.
7. Click **Reconstruct current**.

The reconstruction is dispatched through ImProcess's worker thread. The heavy
path uploads the raw stack once, performs linear microlens centering,
reassignment, mean subtraction, FFT shift and Gaussian+constant fitting on the
GPU, then copies only the final 2-D reconstruction back to NumPy.

## Current intentional limitations

- one 3-D acquisition with shape `(M*M, Y, X)`;
- square scan (`M*M` frames);
- square camera frames;
- axis-aligned rectangular illumination lattice;
- GPU/CuPy only;
- linear microlens centering and fused FFT reassignment only.

`GPU frame batch = 0` is the fastest path and processes all scan frames in one
batch. Reduce it only if a large acquisition runs out of temporary GPU memory.

The output metadata records the PatternFinder parameters, converted xrecon
period/phase, reconstruction settings, output pixel size and measured GPU
reconstruction time.
