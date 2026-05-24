# Device reference

Per-device documentation for every hardware manager in ImSwitch.
Each file in this directory covers one device category and contains
one "card" per manager class, with:

- the JSON shape expected in your setup file,
- every `managerProperties` field with its type and default,
- which low-level managers it depends on (NI-DAQ, RS232, pulse
  generator, …),
- which vendor library it lazy-imports + what mock fallback exists.

| File | Category |
|---|---|
| [detectors.rst](detectors.rst) | Cameras, photon counters, photodiodes |
| [lasers.rst](lasers.rst) | Laser sources and illumination |
| [positioners.rst](positioners.rst) | Stages and piezos |
| [rotators.rst](rotators.rst) | Rotation mounts |

These pages are rendered as part of the official Sphinx docs site
(see `../index.rst`).  For "how do I add a new device manager
class?", see [`../adding-device-support.rst`](../adding-device-support.rst).
For task-oriented recipes, see [`../how-to/`](../how-to/).

## Maintenance

Every claim in these files is grounded in source code (specifically:
the `managerProperties[...]` and `managerProperties.get(...)` calls in
the manager `.py` files).  When a manager's config schema changes, the
corresponding card here must be updated.  Adding a new manager means
adding a new card to the right category file.
