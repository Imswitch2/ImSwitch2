# Plans

Integration plans, feature rollouts, and historical "what we were going
to do" documents.  Some are active, most are historical — kept here so
the rationale behind the current code stays discoverable.

## Index

| File | Status | Topic |
|---|---|---|
| [ws-integration.md](ws-integration.md) | Active — Phase 5 landed 2026-05-24, hardware verification pending | Porting device managers from WidefieldStarss + Teensy pulse generator |
| [communication-backbone-cleanup.md](communication-backbone-cleanup.md) | Active — Phase 4 started 2026-05-24 | CommunicationChannel and controller backbone cleanup |
| [widget-usability-improvements.md](widget-usability-improvements.md) | Active — scrollability baseline started 2026-05-25 | Widget responsiveness and UI cleanup |
| [time-resolved-detector-workflows.md](time-resolved-detector-workflows.md) | Implementation in progress — software foundation complete, hardware validation pending | Photon-arrival cubes, gated-STED, and tau-STED workflows |
| [improcess-graph-and-wfs-analysis.md](improcess-graph-and-wfs-analysis.md) | Active — graph/WFS first slice shipped; FRC + ROI panels added | General ImProcess plotting surface, WidefieldSTARSS analysis, and generic analysis widgets |
| [improcess-analysis-widgets.md](improcess-analysis-widgets.md) | Active — Phases 1-5 generic analysis widgets implemented | ImProcess projections, ROI manager, segmentation, PSF/bead resolution, and colocalization |
| [improcess-fiji-like-toolbar.md](improcess-fiji-like-toolbar.md) | Proposed | Fiji-like persistent image toolbar, brightness/contrast dialog, stack and channel operations |
| [beadrec-2.0.md](beadrec-2.0.md) | Active — Phase 1 started 2026-05-25 | Bead reconstruction controller/widget rewrite |
| [etsted-2-0.md](etsted-2-0.md) | Active | EtSTED scan controller rewrite |
| [etmonalisa-2-0.md](etmonalisa-2-0.md) | Active | EtMonalisa rewrite |
| [smart-microscopy-mode-switching.md](smart-microscopy-mode-switching.md) | Active — Phases 1-6 implemented in-repo, external Snouty validation pending | Role-based setup-mode switching for event-triggered smart microscopy |
| [tiled-target-timelapse-smart-events.md](tiled-target-timelapse-smart-events.md) | Active — T1-T4 workflow foundations implemented, T5 UI/controller adapters pending | Tiled target timelapse, smart modes, and event-gated acquisition |
| [config-editor-discovery-and-schema.md](config-editor-discovery-and-schema.md) | Proposed | Registry-backed config editor discovery, plugin templates, and shared setup validation |
| [acquisition-layout-contract.md](acquisition-layout-contract.md) | Active — all seven PRs implemented (schema through every reconstructor); direction audit 2026-09-03 sets 7 conditions before rig validation | Versioned frame-layout metadata from scan producers through recording and reconstruction |
| [magic-number-audit.md](magic-number-audit.md) | Reference — 2026-09-10/12; 32 verified, 10 refuted, 12 unchallenged leads; ALL 44 fixed on the branch | Constants and defaults that are right for the case they were written for and silently wrong for a neighbour; the shape, and where else the tree has it |
| [magic-number-audit-fixes.md](magic-number-audit-fixes.md) | Review — 2026-09-12; every audit finding: defect, fix, where, test; plus the behaviour changes to look at on a rig | Companion to the audit: what was done about each finding |
| [memory-budgets.md](memory-budgets.md) | IMPLEMENTED 2026-09-21 on feat/memory-limits (stacked on PR #29): three named limits, diagnostics, safer automatic work, four-shape stall suite; Phase A regression fix d648967d on PR #29 itself; Open keeps materialising (deviation recorded) | One declared memory budget per module with derived shares, instead of a preference per byte constant; why VRAM waits |
| [acquisition-layout-direction-audit.md](acquisition-layout-direction-audit.md) | Reference — 2026-09-03; conditions 1–7 done + pre-rig probe (11 verified defects fixed) 2026-09-04, cleared for rig validation | Verdict on whether the layout contract is the final answer to scan geometry/order: direction right, five verified defects, ordered conditions checklist |
| [scan-acquisition-order-spec.md](scan-acquisition-order-spec.md) | Draft 4 for review — review #2 (8 findings) resolved | Portable description of how a scan was performed, complementing OME |
| [acquisition-metadata-channel-retirement.md](acquisition-metadata-channel-retirement.md) | Active — P1/P2 implemented, P3/P4 release-gated | Retiring the duplicate acquisition-metadata channels the layout contract superseded |
| [scanner-calibration-widget.md](scanner-calibration-widget.md) | Proposed — revision 3, awaiting second review; P-0 (manual per-axis voltage entry + actuator reservation) is the first deliverable | Manual scanner DAC control and camera-based measurement of scanner calibration constants |
| [tis-camera-ic4-migration.md](tis-camera-ic4-migration.md) | Active — shipped as the `imswitch-device-tis` plugin (mock-complete), rig validation pending | Replacing the vendored `pyicic`/IC3 wrapper with the IC4 Python library for per-trigger TIS frame capture |
| [dynamic-layer-lifecycle.md](dynamic-layer-lifecycle.md) | Historical | Napari layer lifecycle redesign |
| [widget-state-persistence.md](widget-state-persistence.md) | Historical (shipped 2026-05-14) | Save/load of widget controller states |
| [manager-audit-todo.md](manager-audit-todo.md) | Reference | Per-manager audit notes |

When a plan is done and its content is fully reflected elsewhere (e.g.
in `ARCHITECTURE.md` or a how-to), mark it Historical here rather than
deleting — the rationale is often more useful than the outcome.
