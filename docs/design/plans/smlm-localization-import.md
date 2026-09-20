# SMLM localization import — scope

**Status:** implemented on `feat/napari-storm-viewer` (2026-09): §0 precision
columns, §1 formats (ThunderSTORM CSV, Picasso HDF5, generic CSV via mapping
dialog) and §2 layout as written. User docs: `docs/improcess.rst`,
"Importing localization tables".

Opening a coordinate file that ImProcess did not produce. Today this is not
possible: `read_picasso_hdf5()` exists in `analysis/smlm_export.py` but nothing
outside the tests calls it, and there is no CSV path at all.

The value is disproportionate to the effort. It decouples ImProcess from its
own localizer — people can fit in Picasso, ThunderSTORM, SMAP or DECODE and
still filter, drift-correct, group, render and view here, with napari-storm on
top. The set of SMLM data ImProcess did *not* generate is much larger than the
set it did.

---

## 0. One decision blocks the rest: what `sigma_*_nm` means

Our schema calls `sigma_x_nm` a *fitted width* and our localizer writes the PSF
second moment there. But `read_picasso_hdf5()` maps Picasso's **`lpx`/`lpy`**
onto it — and those are *localization precision*, not PSF width. Picasso stores
both (`sx`/`sy` are the widths). So the same column already means two different
physical quantities depending on where the data came from, and nothing says so.

Import multiplies this across every format. ThunderSTORM CSV carries both
`sigma [nm]` and `uncertainty [nm]`; a generic CSV may carry either.

It matters because **precision is the physically correct rendering width.** An
SMLM reconstruction draws each molecule as a Gaussian of its positional
uncertainty; drawing it at the PSF width just reproduces a diffraction-limited
image. napari-storm's variable-width mode is therefore asking for precision,
and we are currently feeding it PSF width for our own data and precision for
imported Picasso data.

**Proposed:** keep `sigma_*_nm` as PSF width, add `lp_x_nm` / `lp_y_nm` /
`lp_z_nm` for precision, and have the napari-storm adapter prefer precision
when present and fall back to width. Consequences:

* `NAPARI_STORM_TABLE_KWARGS` (committed in `00db129b`) points `sigma_columns`
  at the precision columns instead. This changes *which column we declare*, not
  the interface — no renegotiation with the napari-storm side needed.
* `read_picasso_hdf5()` gets corrected: `lpx` → `lp_x_nm`, `sx` → `sigma_x_nm`.
* Our own localizer should fill precision from the Thompson/Mortensen closed
  form, which needs only photons, width, background and pixel size — all
  already computed. This also closes the "no uncertainty estimate" gap noted in
  the fitter review, cheaply and without an optimiser.
* Filtering gets the criterion the field actually uses.

`LOCALIZATION_DTYPE` grows by three `f4` columns. That is a schema change, so
it wants doing before import spreads the ambiguity, not after.

---

## 1. Formats

**In scope:**

| Format | Notes |
|---|---|
| **ThunderSTORM CSV** | The de-facto interchange format. Self-describing units in the header (`x [nm]`, `sigma [nm]`, `intensity [photon]`); **1-based frames**. Parse units from the bracket notation rather than hardcoding a column list — the header varies by fitter and version. |
| **Picasso HDF5** | ~90% written. Needs the `lp*`/`s*` correction above, the `.yaml` sidecar pixel size it already reads, and wiring. |
| **Generic CSV** | Anything else, via a column-mapping dialog. The escape hatch that stops the format list from being a treadmill. |

**Out of scope for now:** vendor binaries (Nikon `.bin`, Zeiss `.txt`),
RapidSTORM XML-header `.txt`, SMLM-challenge format. Add on demand — each is a
reader function once the layer exists.

## 2. Where the code goes

There is already a precedent for a non-image source: `TILING_MANIFEST_SPEC`.
A localization table is exactly parallel — a file that opens to something other
than an array — so it follows the same route rather than inventing one.

| Layer | Change |
|---|---|
| `model/dataset_sources.py` | `LOCALIZATIONS_SPEC` (`.csv`, plus `.hdf5` disambiguated by content); extend `source_kind_for()` with a `"localizations"` kind next to `"tiling-manifest"` |
| `analysis/smlm_import.py` | **new** — pure readers, one per format, ndarray/`Path` in, `LocalizationResult` out. No Qt, testable headlessly, mirrors `smlm_export.py` |
| `analysis/smlm_export.py` | move `read_picasso_hdf5` here, corrected; re-export for back-compat |
| `controller/FileIOController.py` | third branch in `_loadFromPath`, beside the `TILING_MANIFEST_SPEC` one, routing to `ReconstructionView.addNewData()` |
| `view/LocalizationImportDialog.py` | **new** — column mapping + pixel size, generic CSV only |

The `.hdf5` collision needs care: an HDF5 may be an image stack *or* a Picasso
table. Sniff for a `locs` dataset and route accordingly rather than asking.

## 3. Phasing

**Phase 0 — schema.** Precision columns, Thompson/Mortensen precision in the
localizer, corrected Picasso mapping, adapter points at precision. Self-
contained, unblocks everything, and improves rendering on its own.

**Phase 1 — readers.** ThunderSTORM CSV + Picasso HDF5 as pure functions, with
round-trip tests. No UI. Usable from scripts immediately.

**Phase 2 — wiring.** Source spec, `_loadFromPath` branch, result appears in
the results list. This is the phase where "open a CSV" starts working.

**Phase 3 — generic CSV dialog.** Column mapping, unit choice, pixel size.
The only real UI work.

**Phase 4 — fixtures.** A corpus of real files from each tool. Worth saying
plainly: without real exports from actual ThunderSTORM and Picasso versions,
the readers are guesses with tests that agree with the guess.

## 4. Traps

* **Pixel size may be absent.** `LocalizationResult` requires
  `pixel_size_nm > 0`, but ThunderSTORM CSV stores nm and carries no pixel
  size. It is only needed for the preview histogram floor and the Picasso
  export. Either make it optional with a sentinel, or prompt on import —
  prompting is honest, but it must not block a headless read.
* **Frame base differs.** ThunderSTORM is 1-based, Picasso 0-based. Silent
  off-by-one that no test catches unless it is written for it.
* **Y-axis origin and handedness** differ between tools; a flipped
  reconstruction looks plausible. Worth a known-answer fixture per format.
* **Astigmatic 3D sigmas.** ThunderSTORM emits `sigma1`/`sigma2` for elliptical
  fits, which are not `sigma_x`/`sigma_y` — they are the major/minor axes.
  Mapping them positionally is wrong.
* **Large files.** A multi-million-row CSV read naively is slow and memory
  hungry; `pandas.read_csv` with an explicit dtype map is the cheap fix, and
  pandas is already a transitive dependency.

## 5. Relationship to the localizer question

This scope deliberately does **not** replace our fitter. The review found real
defects there — `gausslq` is a centroid, not a least-squares fit; `photons`
holds the ROI sum for one method and the peak amplitude for the other; MLE
fits one symmetric sigma so astigmatic 3D is impossible — and those are worth
fixing on their own terms, keeping it scoped as the live/quick-look path.

Adopting Picasso as a *library* was investigated and rejected: `picassosr`
hard-depends on PyQt6 (base requirements, not an extra), which would put a
second Qt binding beside our PyQt5, and pulls streamlit, sqlalchemy, nd2 and
playsound3 besides. It is MIT licensed, so vendoring specific numba fitting
routines with attribution stays open if in-process gold-standard fitting is
ever wanted. Import is the cheaper and more useful half of that goal.
