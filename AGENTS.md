# AGENTS.md — AI Agent Coding Rules for ImSwitch2

This document defines the rules, constraints, and boundaries for all AI agents working on ImSwitch2. Every agent (OpenHands, Claude, Microsoft Agent Framework agents, or any future agent) MUST follow these rules.

## Core Principles

1. **Human-in-the-loop is mandatory.** No agent may merge code, push to main, or deploy without human review and approval.
2. **Safety first.** ImSwitch2 controls real microscope hardware. Incorrect changes can damage equipment or create unsafe conditions.
3. **Bounded tasks only.** Agents work on narrowly scoped, well-defined tasks — never open-ended refactors.

## Coding Rules

### Mandatory for All Changes

- **No API-breaking changes** without explicit maintainer approval.
- **No hardware timing modifications** unless explicitly requested and approved by someone with physical hardware access.
- **All changes require tests.** No PR will be accepted without corresponding test coverage.
- **No direct hardware execution.** Agents must never trigger physical hardware actions (laser firing, stage movement, DAQ acquisition).
- **All PRs require risk explanations.** Every pull request must include a description of what could go wrong.

### Code Quality

- Follow PEP 8 and existing project conventions.
- Add type hints to all new functions and methods.
- Add docstrings to all public classes and functions.
- Keep changes small and focused — one concern per PR.
- Run linting (`ruff`) before submitting.

### Branch Discipline

- Agents work ONLY on isolated feature branches, never on `main`.
- Branch naming: `agent/<agent-type>/<short-description>` (e.g., `agent/openhands/cleanup-imports`).
- Agents must never force-push or rewrite history.

## Red-Zone Files

The following code areas are considered **red-zone** — they require **mandatory human review** and **explicit maintainer approval** before any modification. Agents should avoid touching these unless the task specifically requires it.

### Hardware Control
- **DAQ timing** — Any code controlling data acquisition timing, synchronization, or triggering.
- **Laser control** — Laser power, enable/disable, modulation, safety interlocks.
- **Galvo scan generation** — Scan patterns, voltage generation, waveform construction.
- **TTL generation** — Digital pulse timing, trigger sequences, synchronization signals.
- **Stage movement** — Positioning commands, velocity, acceleration, limit handling.
- **Hardware initialization** — Device discovery, connection, configuration, calibration.

### Identification Patterns

Red-zone files typically include (but are not limited to):
- Files containing `DAQ`, `NI`, `nidaq` in their names or imports
- Files in `laser`, `positioner`, `stage`, `scanner`, `galvo` directories
- Files with `TTL`, `trigger`, `pulse`, `waveform` in their names
- Hardware manager initialization code
- Any file importing `nidaqmx`, `pyvisa`, or direct serial communication libraries

### What Agents Must Do with Red-Zone Files

1. **Flag the file** as red-zone in the PR description.
2. **Explain the risk** of every change in detail.
3. **Never modify timing constants** or hardware parameters without explicit values provided by a maintainer.
4. **Request review** from someone with physical access to the relevant hardware.

## Agent Workflow

The expected workflow for agent-generated changes:

```
GitHub Issue (scoped task)
  → Agent plans approach
  → Agent creates isolated branch
  → Agent implements changes
  → Agent runs tests
  → Agent opens PR with risk explanation
  → Human reviews PR
  → Human merges or rejects
```

Automatic merges are **never allowed**.

## Microscope Knowledge Base

Before modifying hardware-related code, agents MUST consult the microscope knowledge base in `microscope-kb/`. This provides context about hardware limits, safety procedures, and configuration constraints.

## Cost Tracking

Each agent task should track:
- Estimated cost before starting
- Actual cost after completion
- Whether the output was useful

Budget limits (configured in `.env`):
- Monthly budget: ~$100
- Per-task budget: $2–10
- Daily soft limit: $10–20

## Recent Additions

### ViewerToolManager (2026-05-12)

A new `ViewerToolManager` class was added to `imswitch/imcommon/view/guitools/naparitools.py` to provide napari Shape layer-based viewer interaction tools.

**What it does:**
- Manages a napari Shapes layer for ROI drawing, line drawing, and other annotation tools
- Provides simplified API: `set_mode('rectangle')`, `set_mode('line')`, `set_mode('pan')`
- Lazy initialization (layer only created when first used)
- Qt signals for integration: `sigShapesChanged`, `sigModeChanged`
- Helper methods: `get_rectangle_bounds()`, `get_line_endpoints()`, `get_shapes_data()`

**Why it was added:**
- Enable modern napari-native interaction tools
- Provide foundation for future line profiles, multi-ROI analysis, etc.
- Completely backward compatible (Vispy overlays unchanged)

**Location:**
- Implementation: `imswitch/imcommon/view/guitools/naparitools.py` (lines 981-1190)
- Usage docs: `VIEWER_TOOL_MANAGER_USAGE.md`
- Architecture analysis: `NAPARI_VIEWER_TOOLS_ARCHITECTURE.md`
- Example: `examples/viewer_tool_manager_demo.py`

**Status:** Fully implemented and integrated into ImSwitch. Safe to use, no risk to existing functionality.

### ViewerToolsWidget (2026-05-12)

A new UI widget was added to provide toolbar buttons for switching napari viewer modes.

**What it does:**
- Provides 4 mutually exclusive buttons: Pan, Select, Rectangle ROI, Line
- Emits `sigToolSelected(str)` signal with mode name: 'pan', 'select', 'rectangle', 'line'
- API methods: `setActiveTool(mode)`, `getActiveTool()`
- Follows standard ImSwitch widget conventions (inherits from `Widget`)

**Why it was added:**
- User interface component for ViewerToolManager
- Enables manual mode switching via toolbar
- Self-contained, minimal design

**Location:**
- Implementation: `imswitch/imcontrol/view/widgets/ViewerToolsWidget.py`
- Documentation: `VIEWER_TOOLS_WIDGET_README.md`

**Status:** Fully implemented and wired. Available but disabled by default.

### ViewerToolsWidget Wiring (2026-05-12)

The ViewerToolsWidget is now fully wired to the ImageWidget.toolManager through ViewerToolsController.

**Architecture:**
```
ViewerToolsWidget.sigToolSelected(str)
    ↓
ViewerToolsController.__init__
    ↓ connects signal to
ImageWidget.toolManager.set_mode(str)
    ↓
napari Shapes layer mode change
```

**Implementation:**
- `ViewerToolsController` (new): Minimal controller that connects widget signals to toolManager
- `ImConMainView`: Passes `ImageWidget.toolManager` to factory as `imageToolManager` argument
- `ImConMainView`: Registers ViewerTools dock at yPosition=2 (left panel)
- All existing Vispy overlays remain unchanged

**Files modified:**
1. `imswitch/imcontrol/controller/controllers/ViewerToolsController.py` - **NEW**
2. `imswitch/imcontrol/controller/controllers/__init__.py` - Export ViewerToolsController
3. `imswitch/imcontrol/view/widgets/__init__.py` - Export ViewerToolsWidget
4. `imswitch/imcontrol/view/ImConMainView.py` - Register dock and pass toolManager

**Configuration:**
- Disabled by default (not in example configs)
- Enable by adding to `availableWidgets`: `"ViewerTools"`
- No breaking changes to existing setups

**Documentation:**
- Wiring summary: `VIEWER_TOOLS_WIRING_SUMMARY.md`
- Wiring diagram: `VIEWER_TOOLS_WIRING_DIAGRAM.txt`
- Widget API: `VIEWER_TOOLS_WIDGET_README.md`
- ToolManager API: `VIEWER_TOOL_MANAGER_USAGE.md`

**Status:** Complete and ready for testing. Zero risk to existing functionality.
