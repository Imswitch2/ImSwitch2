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
| [tis-camera-ic4-migration.md](tis-camera-ic4-migration.md) | Active — shipped as the `imswitch-device-tis` plugin (mock-complete), rig validation pending | Replacing the vendored `pyicic`/IC3 wrapper with the IC4 Python library for per-trigger TIS frame capture |
| [dynamic-layer-lifecycle.md](dynamic-layer-lifecycle.md) | Historical | Napari layer lifecycle redesign |
| [widget-state-persistence.md](widget-state-persistence.md) | Historical (shipped 2026-05-14) | Save/load of widget controller states |
| [manager-audit-todo.md](manager-audit-todo.md) | Reference | Per-manager audit notes |

When a plan is done and its content is fully reflected elsewhere (e.g.
in `ARCHITECTURE.md` or a how-to), mark it Historical here rather than
deleting — the rationale is often more useful than the outcome.
