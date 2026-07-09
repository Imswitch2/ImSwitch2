# Simulated multicolor nuclei model

This is a sideproject model for generating synthetic image stacks to test a
DAPI segmentation plus dominant coding-channel analysis pipeline.

## Output

The simulated image is written as a BigTIFF/OME-TIFF time series with axes
`TCYX`.
The default channel order is:

1. `dapi`
2. `code1`
3. `code2`
4. `code3`
5. `code4`

The default stack size is 10 timepoints, 5 channels, and 5000 x 5000 pixels.

## Cell geometry and drift

A single base population of circular nuclei is generated once and reused across
all timepoints. Nucleus diameter is drawn from a clipped normal distribution
around a fixed nominal diameter. Cell centers are placed with rejection sampling
so the disks do not substantially overlap.

Each timepoint then applies a small global translation to the same cells, simulating
stage/sample drift. The default drift is linear with a small random-walk term.
The optional ground-truth label stack stores code-class labels shifted by the
timepoint-specific drift: `0` for background, `1` for `code1`, `2` for `code2`,
and so on.

## Channel assignment

Every cell is visible in DAPI. Every cell is also assigned a primary coding
channel from `code1..code4` using configurable ratios. For example:

```text
code1: 40%
code2: 20%
code3: 20%
code4: 20%
```

A small configurable fraction of cells are doubles. The default is 2%. Doubles
receive signal in two coding channels; their strongest simulated coding signal
is marked as the dominant ground-truth channel.

## Intensity model

For every channel, the background is a per-pixel Poisson process:

```text
background_counts[channel]
```

Coding-channel signal amplitudes are drawn per cell from a truncated exponential
distribution. The default coding-channel settings are intentionally difficult:
single timepoints are noisy, and cells should become reasonably apparent mainly
in a maximum projection across the stack. DAPI signal uses the same model with
much brighter defaults.

The simulated photon count for each pixel is:

```text
photons[channel, y, x] ~ Poisson(background[channel] + sum(cell_signal[channel]))
```

The camera offset is then added as a fixed digital offset:

```text
observed[channel, y, x] = camera_offset + photons[channel, y, x]
```

The default camera offset is 100 counts. The final image is clipped to the
chosen integer dtype, normally `uint16`.

## Sidecars

The script writes sidecars next to the OME-TIFF:

- `*_config.json`: all simulation parameters
- `*_cells.csv`: one row per cell observation. The same `cell_id` appears once
  per timepoint with its drifted position, active channels and expected signal
  amplitudes. `label_value` matches the code-class value written into the label
  image.
- `*_labels.tif`: optional integer ground-truth code-class image with axes
  `TYX`; doubles use their dominant simulated coding channel.
