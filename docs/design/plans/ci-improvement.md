# CI improvement plan (open tail)

**Status (2026-10-17):** Tier A (pip cache, `pytest-xdist` via `-p xdist.plugin -n auto`,
`concurrency` cancel-in-progress) and Tier B2 (path-scoped lanes `unit`, `improcess`,
`core` in `.github/workflows/ci.yml`, `fail-fast: false`) are on main. The
bundle workflow was repaired and now ships the installers (`imswitch-bundle.yml`).
Still open:

1. **B1 - marker completion.** `pyproject.toml` defines `nohardware`, `hardware`,
   `redzone`, `ui`, `glviewer`, but CI selects by path, not marker, so the
   taxonomy is incomplete. Tag every currently PR-gated test `nohardware`, tag
   `imcontrol/_test/ui/*` `ui` and the safety-critical DAQ/laser/stage/TTL/scan
   tests `redzone`. Prove it with a `pytest --collect-only` parity check against
   the lane union before changing selection.
2. **B3 - UI lane.** `imswitch/imcontrol/_test/ui` (napari/Qt) is excluded from
   every PR lane and only runs in the manual `imswitch-test.yml`. Add a `ui` lane
   with a proven display strategy (xvfb + XCB libraries, or demonstrably pure
   offscreen Qt on `ubuntu-latest`), non-required until stable.
3. **Snouty ignore.** `improcess` lane still passes
   `--ignore=imswitch/improcess/_test/test_snouty.py` (CPU deskew all-zero-output).
   Fix it or mark the case `xfail(strict=False)` with a tracking reference, then
   drop the workflow-level ignore.
4. **Tier C - shared setup and declarative selection.** Extract install + lint
   into a composite action (`.github/actions/setup/`, does not exist yet) used by
   `ci.yml` and `imswitch-test.yml`; once markers have parity, replace the path
   lanes and YAML exclusions with marker selection and `skip`/`xfail` on the
   tests themselves. Guard the partition: union of lanes must equal the
   whole-tree no-hardware collection.

Acceptance: no hardcoded path list or `--ignore` in `ci.yml`, UI tests gate PRs,
install/lint defined once.
