# ImProcess M2D-B checkpoint — canonical MoNaLISA result

Baseline: `feat/improcess-generalized-session` at `8d629828`, with all M1–M2D-A patches applied. This is the M2D-B incremental change. It does not change the Session queue/UI, the reconstruction algorithms, or the timepoint publication policy.

## Contract

- All **new** MoNaLISA Fast, finite Full/Robust/lattice, and Legacy results have the same Python type: `imswitch.improcess.reconstructors.monalisa.canonical_result.MonalisaResult`, a `ProcessingResult`.
- Existing imports `monalisa.result.MonalisaProcessingResult` and `monalisa_lattice.result.MonalisaLatticeResult` remain usable aliases and constructor signatures. Saved pickle objects under the historical module and class names resolve to `MonalisaResult`, with the original fields and per-format save policy preserved.
- The original fixed-6D and flexible-image implementations remain **private format adapters** until the saved-result/export/GUI parity gate is satisfied. This is intentional transitional compatibility, not a second public result type.
- `MonalisaResult.monalisa_representation` identifies the storage/display layout explicitly: `coefficients_6d` or `flexible_image`. It does *not* select how processing runs or how Session results are assembled.
- Historical `T` may fold acquisition conditions; `scientific_axis_metadata` and `metadata['monalisa_result']` retain the semantic projection where known, without guessing from array dimensions. Flexible results keep explicit axis labels and condition labels.
- The old 6D result retains `getCoeffs`, `updateScanParams`, `updateImages`, Base-separated layers and the `TZCYX` disk view. The flexible result retains optional sharpening/background/pinhole layers, diagnostics tables and its own named-axis serialization. No image-array copy is required to wrap either result.
- `MonalisaReconstructor.consolidate` dispatches by **representation**, not `isinstance(MonalisaLatticeResult)`, because both legacy names now refer to the canonical type. The old consolidation algorithms remain for compatibility; M2D-C will move generic timepoint and Dataset combination to the semantic assembler.
- The canonical object exposes the standard `ProcessingResult` interface used by the future PR #79 content summaries. PR #79 is not merged or modified in this patch.

## Validation

- Python compilation and Git diff whitespace check.
- Isolated result and semantic-assembler tests: **27 passed**, including real historical-class pickle byte-stream compatibility for both old result classes, classic/lattice display layers, named axes, output formatting, coefficient rerendering, and assembly contracts.
- Existing MoNaLISA display-layer tests in the dependency-isolated harness: **43 passed, 2 deselected** (Qt view controller and the coefficient-to-image implementation intentionally stubbed by the harness). The complete Qt test suite was **not** run in this environment.
- **Required on Windows:** run `test_monalisa_canonical_result.py`, `test_monalisa_display_layers.py`, `test_monalisa_coeffs_per_base.py`, `test_monalisa_result_formats.py`, `test_monalisa_lattice_reconstructor.py`, `test_monalisa_live_session.py`, and a small Session Fast/Full/Legacy save/reopen smoke test. Include ImageJ and HDF5/Zarr exports, update-reconstruction and optional outputs.

## Deliberately deferred to M2D-C/M4

- Progressive offline Full/Robust publication and semantic Multi Data assembly.
- Removing the original format adapters, historical `consolidate` implementations or old result import paths.
- Any reconstruction algorithm merger, new UI control, or PR #79 code changes.
