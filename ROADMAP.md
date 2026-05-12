# ImSwitch2 Migration Roadmap

This roadmap tracks the major milestones for the ImSwitch2 migration. Each milestone should be created as a GitHub Milestone with associated issues.

## Milestone 1: Stabilization ✅

**Goal:** Establish a stable, tested baseline from the imported ImSwitch code.

- ✅ Import existing ImSwitch codebase and tag baseline
- ✅ Set up CI pipeline (linting, tests, build)
- ✅ Critical lint errors resolved (`E9/F63/F7/F82` clean)
- ✅ First unit tests passing in CI (`test_stores.py`)
- ⬜ Ensure the application launches without errors on a clean install
- ⬜ Document current state and known issues

## Milestone 2: Packaging Cleanup 🔄

**Goal:** Clean up the project structure and packaging for easy installation.

- ✅ Modernize `setup.cfg`: `python_requires = >=3.10`, relaxed version pins
- ✅ Split hardware packages into optional extras (`[hardware]`, `[full]`)
- ✅ Core install (`pip install imswitch`) no longer requires NI-DAQ, Lantz, napari
- ⬜ Remove dead code and unused imports (bare-except / blank-import cleanup deferred)
- ⬜ Establish clear package boundaries
- ⬜ Consolidate or remove `setup.py` in favour of `pyproject.toml` only

## Milestone 3: Hardware Abstraction Cleanup

**Goal:** Improve the hardware abstraction layer for clarity and safety.

- Document all hardware interfaces
- Standardize manager/controller patterns
- Add type hints to hardware interfaces
- Improve error handling in hardware communication
- Add timeout mechanisms where missing

## Milestone 4: Detector Manager Refactor

**Goal:** Modernize and simplify detector management.

- Map current detector manager dependencies
- Define clean detector interface
- Refactor with backward compatibility
- Add comprehensive tests
- Document the new architecture

## Milestone 5: DAQ Safety Layer

**Goal:** Add a safety layer around all DAQ operations.

- Implement voltage limit enforcement
- Add scan parameter validation against `microscope-kb/limits.yaml`
- Implement graceful error recovery for DAQ failures
- Add logging for all DAQ operations
- Create integration tests with mock hardware

## Milestone 6: Documentation Improvements

**Goal:** Comprehensive documentation for developers, agents, and users.

- Generate architecture diagrams from codebase analysis
- Document all configuration options
- Write developer onboarding guide
- Fill in microscope knowledge base with real hardware values
- Create agent task templates for common operations
