# CI Improvement Plan

**Status:** Tier A implemented (2026-06-30, uncommitted) · Tier B/C pending — see §5
**Scope:** `.github/workflows/` + pytest config + test marker hygiene

---

## 1. Current state

### Workflows
| File | Trigger | What it does |
|------|---------|--------------|
| `ci.yml` | push/PR to `main` | `lint` (ruff) · `test` (single no-hardware job) · `build` (package) |
| `imswitch-test.yml` | manual | Full suite incl. napari GUI, across macOS/Windows/Linux, hardware extras best-effort |
| `imswitch-pypi.yml` | (publish) | PyPI release |
| `imswitch-bundle.yml` | **disabled** | PyInstaller bundle — uses deprecated `upload-artifact@v2`, fails at validation |

### The per-push `test` job (the thing to fix)
- Runs **~1,700 tests in a single Ubuntu job**, serially.
- Selected by a **hardcoded path list**, not by markers:
  `test_no_hardware_profile.py`, `imcontrol/_test/unit`, `imcontrol/controller/_test`, `improcess/_test`.
- `snouty` excluded via `--ignore` + a TODO; `imcontrol/_test/ui` (34 napari/Qt tests) excluded **by omission** — they only run in the manual workflow, so **UI tests do not gate PRs**.
- One file (`test_recording.py`, 37 tests) = **30s**; full run is several minutes with a single pass/fail signal.

### Findings
1. **No pip caching.** `actions/setup-python` is used without `cache: pip` → full dependency reinstall every run. Likely the largest single time cost.
2. **No parallelism.** No `pytest-xdist`; the suite is almost entirely isolated unit tests that would shard cleanly.
3. **Dead/incomplete marker taxonomy.** `pyproject.toml` defines `nohardware`, `hardware`, `redzone`, `ui`, but marker coverage is incomplete for the current PR-gated suite and CI ignores markers anyway. The intended tiering exists on paper only, and switching to marker-only selection before completing the tags would silently drop coverage.
4. **Selection drifts via paths.** Exclusions (`snouty`, `ui/`) are encoded in the workflow YAML, not declared on the tests. New test directories must be manually added to the path list.
5. **Cross-workflow duplication.** `imswitch-test.yml` re-implements install + lint + a superset test run. Lint and install logic are copy-pasted across both files and will drift.
6. **No `concurrency` guard.** Rapid pushes pile up redundant runs.

### On "lots of tests checking the same thing"
Investigated — it is **not** mostly literal duplication. The `*_component_state.py` (×10), `*_smart_modes`, and `*_workflow` families are legitimate coverage from recent refactors (state-persistence unification, smart-microscopy mode switching, etc.). The real issue is they each re-bootstrap managers/app fixtures and all land in **one slow, undifferentiated bucket** with no caching and no parallelism — so it *feels* redundant because feedback is slow and lumped together. The fix is tiering + speed, not deleting tests.

---

## 2. Proposed changes

### Tier A — speed wins (no test-logic changes, lowest risk)
1. **Enable pip caching:** add `cache: pip` to the `actions/setup-python` step in every workflow.
2. **Parallelize:** add `pytest-xdist` to the `test` extra in `setup.cfg`; run with `-n auto`.
   - Current CI sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, so the pytest command must either explicitly load xdist (`-p xdist.plugin`) alongside `pytestqt`/`pytest_timeout`, or deliberately remove that autoload guard after checking plugin side effects.
3. **Concurrency guard:** add to `ci.yml`:
   ```yaml
   concurrency:
     group: ci-${{ github.ref }}
     cancel-in-progress: true
   ```

> Expected: wall-clock cut by more than half, dominated by the caching + xdist combination. Zero changes to test files, but the workflow command must prove `-n auto` is actually active under the disabled-plugin-autoload policy.

### Tier B — split the monolith into parallel lanes without losing coverage
4. **Establish collection parity first.** Before changing selection semantics, compare the baseline CI command against the proposed matrix with `pytest --collect-only` and keep the collected test set equal except for explicitly documented additions/removals (`snouty`, UI).
5. **Activate the markers before relying on them.** Apply:
   - `nohardware` to every currently PR-gated test that is safe without hardware, not only the files already tagged today.
   - `ui` to `imcontrol/_test/ui/*`.
   - `redzone` to the safety-critical DAQ/laser/stage/TTL/scan tests.
   Do not use `-m "nohardware ..."` as the primary selector until the parity check proves no current CI-selected tests are dropped.
6. **Split `test` into concurrent matrix jobs, initially path-scoped for safety:**
   - `unit` — `test_no_hardware_profile.py`, `imcontrol/_test/unit`, and `imcontrol/controller/_test` (`-n auto`, optional marker filter only after parity is proven)
   - `improcess` — `improcess/_test` (`-n auto`)
   - `ui` — `imcontrol/_test/ui` once the display strategy is proven in CI
   Failures localize per lane; the slow lane no longer blocks signal from the fast lanes.
7. **Handle Snouty before removing the ignore.** Either fix `improcess/_test/test_snouty.py` or mark the known CPU-deskew all-zero-output case `xfail(strict=False)` with a tracking reference before the `improcess` lane stops passing the existing `--ignore`.
8. **Make the UI lane explicit.** Do not assume `QT_QPA_PLATFORM=offscreen` alone is enough. Start from the manual Linux setup (`xvfb`, XCB libraries, `xvfb-run`) or prove pure offscreen Qt works for napari/vispy on `ubuntu-latest`; gate PRs only after that lane is stable.

### Tier C — de-duplicate & make selection declarative
9. **Shared setup.** Extract install + lint into a composite action (`.github/actions/setup/`) or a reusable workflow, consumed by both `ci.yml` and `imswitch-test.yml` — one source of truth for deps/lint.
10. **Declarative selection.** Once marker coverage and collection parity are proven, move from path-scoped matrix lanes to marker-only selection where practical. Convert residual YAML exclusions into `@pytest.mark.skip(reason=...)` / `xfail` / marker selection on the tests themselves, so CI selection is expressed in code, not stale workflow comments.

### Housekeeping (independent)
11. Either fix `imswitch-bundle.yml` (bump `checkout@v4`/`setup-python@v5`/`upload-artifact@v4`, Python 3.11+) or delete it — it currently can only fail.

---

## 3. Suggested sequencing
1. **Tier A** — single PR, immediate speedup, safe to merge first. Include the explicit xdist plugin load or a tested autoload-policy change.
2. **Tier B1** — marker-completion PR with a `--collect-only` parity report against the current CI command.
3. **Tier B2** — path-scoped workflow matrix PR. Keep the current Snouty ignore unless the known failure is fixed/xfail'd in the same PR.
4. **Tier B3** — UI lane PR. Start as non-ambiguous Ubuntu CI coverage with documented Xvfb/offscreen setup; make it required after it is stable.
5. **Tier C** — cleanup once A/B are stable.
6. Housekeeping item 11 whenever convenient.

## 4. Acceptance signals
- Per-push CI wall-clock materially down (target: caching removes the reinstall cost; xdist removes serial cost).
- `pytest -n auto` works in CI with plugin autoload disabled, or the autoload policy change is intentional and documented.
- Matrix collection matches the previous PR-gated collection unless a difference is explicitly approved.
- The known Snouty failure is fixed or tracked as an `xfail` before the workflow-level `--ignore` is removed.
- napari UI tests run on every PR with a documented display strategy (`xvfb`/XCB packages or proven pure offscreen Qt).
- No hardcoded test-path list or `--ignore` in `ci.yml` once marker coverage has parity; interim path lanes are acceptable during rollout.
- Install/lint logic defined once and shared.
- Redundant in-flight runs auto-cancelled.

---

## 5. Implementation status

### Tier A — DONE (2026-06-30, uncommitted)
Files: `.github/workflows/ci.yml`, `.github/workflows/imswitch-test.yml`, `setup.cfg`.

- **pip caching** — `cache: pip` + `cache-dependency-path: [setup.cfg, pyproject.toml]` on every `setup-python` step in `ci.yml` (lint/test/build) and `imswitch-test.yml`.
- **xdist** — `pytest-xdist>=3` added to the `test` extra; `ci.yml` test command now runs `-n auto` and loads the plugin explicitly via `-p xdist.plugin` (required because `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` is set; `-p` flags propagate to workers). Not added to `imswitch-test.yml` (full GUI suite — out of Tier A scope).
- **concurrency** — `cancel-in-progress` guard keyed on `${{ github.workflow }}-${{ github.ref }}`.

Verification (local, mirroring the CI invocation):
- Proved `-p xdist.plugin … -n auto` activates under disabled autoload: `created: 2/2 workers`; `test_recording.py` 30.5s serial → 16.2s parallel.
- Ran the full `imcontrol/_test/unit` lane (122 files) under `-n auto`: **no parallel-safety regression**. The 2 failures (`test_stores::test_tiff_storer`, `test_workflow_provenance` recording-metadata) reproduce identically in serial mode (pre-existing in the working tree), and the 27 collection errors are a local numpy-2 → matplotlib-stub artifact absent in CI.
- Static scan for parallel-unsafe shared state (hardcoded `/tmp` paths, fixed ports, module-relative writes): only mock attributes / params that already passed under `-n auto`. No real cross-worker collisions; improcess has none.

Not yet validated in real CI (needs a push/PR): cache hit-rate and wall-clock delta, and the improcess lane under `-n auto` (segfaults locally for an unrelated env reason, so it could only be checked statically).

### Tier B / Tier C — not started.
