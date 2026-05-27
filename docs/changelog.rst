*********
Changelog
*********

Unreleased
==========

**New Features**

- FLIM histogram widget gained a "Decay" mode that plots the aggregated TCSPC photon-arrival histogram with a red marker showing the global single-τ fit for the selected method; the original per-pixel lifetime distribution remains as "Lifetime dist." mode
- Added ``laser_rep_rate_mhz`` parameter to ``SwabianTimeTaggerManager`` so the phasor fit uses the true ω = 2π·f_rep instead of assuming the histogram window equals one laser period
- Added ``scripts/diagnostics/measure_laser_rep_rate.py`` — a TimeTagger.Countrate helper that prints the measured laser repetition rate for plugging into ``laser_rep_rate_mhz``
- Added stitched-overview cell target detection for tiling workflows. The GUI "Detect cells" action is passive and only overlays markers; automated/API cell targeting explicitly moves through detected targets and can invoke a per-cell workflow callback.

**Bug Fixes**

- FLIM lifetimes from the three fit methods (moment, phasor, exp1) no longer diverge when the IRF peak is offset from t=0. The worker now auto-detects the IRF peak bin from the aggregated decay each frame and compensates each fitter: moment subtracts ``t_peak`` from the mean, exp1 restricts the fit to bins ≥ peak with a shifted time axis, phasor rotates the measured (g, s) by −ω·t_peak. On known dyes the three methods now converge toward the true lifetime.
- Phasor fit previously assumed the laser repetition period equalled ``n_bins * binwidth_ps``, which silently scaled τ by an arbitrary factor whenever the histogram window did not happen to span exactly one laser period
- The tiling GUI cell-detection path no longer iterates the stage through every detected target. Stage iteration is reserved for explicit automated/API cell targeting calls or manual click-to-navigate.

v0.1.0 (2026-05-17)
===================

First tagged release of ImSwitch2 — the rewrite/refresh of the
original ImSwitch project. Restarts from 0.x to signal that the API
is still evolving and some roadmap milestones (DAQ safety layer,
manager-level refactors) remain in progress.

**New Features**

- Added FLIM lifetime histogram widget for Swabian Time Tagger photon-counting detectors with real-time histogram display and accumulation support
- Introduced widget state persistence framework: save and load complete microscope configurations (detector settings, laser power, scan parameters) via File menu or Ctrl+Shift+S/L shortcuts
- Added multi-position tiling controller with spiral scan patterns, real-time stitched overview, and click-to-navigate canvas
- Configurable widget layout: define custom dock arrangements and tab groups directly in setup JSON files via the ``widgetLayout`` field
- Enhanced configuration editor with visual widget picker dialog: categorized checkboxes replace text-input-only widget selection
- Manual contrast range controls in napari viewer: type custom min/max values for detectors with negative voltages or unusual ranges
- Line profile analysis now normalizes by ROI dimensions for rectangle selections
- Scale bar in live-view napari viewer now displays physical units (µm)

**Bug Fixes**

- Fixed IndexError when napari viewer switches to 3D mode (e.g., after loading an APD 3D scan): 2D live-view layers now auto-pad to match viewer dimensionality
- Resolved layer corruption when switching between detectors: napari layers are now properly recreated when image dimensionality changes
- Contrast limits now apply correctly to integer-count detectors (APD, PMT): expanded range before setting limits prevents [0,1] clamp
- Fixed detector advanced properties tab showing wrong camera model name after switching detectors
- Eliminated APD manager "tuple does not support item assignment" warning during scan acquisition
- Improved TIS camera error messages with explicit guards and descriptive IndexError/RuntimeError text
- Swabian Time Tagger live preview now updates progressively during scans instead of only after completion
- Fixed Hamamatsu detector subarray property synchronization after advanced property changes
- Configuration editor now correctly parses single-quoted COM port strings (e.g., 'COM4') via ast.literal_eval fallback
- Fixed macOS main window clipping behind system Dock: now uses availableGeometry instead of showMaximized
- napari overlay line width (crosshair, grid) remains constant in screen pixels regardless of zoom level
- Autofocus now uses absolute positioning to prevent drift and improved gradient-variance focus metric for better SNR
- Fixed scan controller state persistence reliability when widget APIs vary across napari versions

**Performance**

- FLIM pipeline optimization: precompute scan-invariant trigonometric tables once per scan instead of per-frame, yielding 30-50% speedup for phasor and exponential fitting
- Added thread-safe locking for FLIM processing to prevent race conditions in concurrent frame emission

**Developer / Internal**

- Removed lantz dependency: replaced with direct pyvisa wrapper and vendored Cobolt laser drivers implementing ASCII protocol natively
- Lifted pyvisa<1.12 version pin, unblocking Python ecosystem upgrades and resolving setuptools<80 compatibility issues
- Refreshed architecture documentation with current manager inventory, widget list, and subsystem descriptions
- Completed hardware manager audit: applied 100+ instant fixes (mock fallbacks, lazy imports, validation checks)
- Improved cross-thread signaling robustness in scan and tiling controllers (Qt signal emission replaces QMetaObject.invokeMethod)

2.0.0
=====

Highlights:

- Added support for Pulse Streamer and other devices, and improve low lever managers: https://github.com/kasasxav/ImSwitch/pull/1
- Server-client support with fastAPI and Pyro5: https://github.com/kasasxav/ImSwitch/tree/master/imswitch/imcontrol/controller/server
- Added features for open microscopy hardware (OpenUC2, SQUID, ESP32) more on beniroquai's fork
- Support for event triggered sted (https://www.nature.com/articles/s41592-022-01588-y)
- Improvements in scanning curve design
- Implementation of file watcher (see also https://github.com/kasasxav/napari-file-watcher/)
- Fix bugs (most reported in Issues).

A list of all code changes is available on GitHub: https://github.com/kasasxav/ImSwitch/compare/v1.2.1...v2.0.0

1.2.1
=====

Highlights:

- Snaps can now be saved to the image viewer (#64)
- Snaps can now be saved as tiff files (#75)
- Resolved the issue of not being able to run scans with only one positioner or only one laser
- Fixed the step up/down buttons not working properly for multi-axis positioners
- Fixed the api.imcontrol.setDetectorToRecord method not working

A list of all code changes is available on GitHub: https://github.com/kasasxav/ImSwitch/compare/v1.2.0...v1.2.1


1.2.0
=====

Highlights:

- Saving multi-detector and timelapse recordings in a single file is now supported (#53)
- Selecting specific detectors to record is now supported (#52)
- It is now possible to edit values/on-off-state of non-involved lasers during scanning (#51)
- The image reconstruction module now allows reconstructing all loaded data files (e.g. multi-file timelapses) into a single reconstruction (#34)
- The documentation has been improved (#61, #63)
- Fixed the SLM widget causing crashes on macOS (#57)
- Fixed the module picker being empty in standalone Windows bundles (#55)

A list of all code changes is available on GitHub: https://github.com/kasasxav/ImSwitch/compare/v1.1.0...v1.2.0


1.1.0
=====

Highlights:

- ImSwitch is now available to install from PyPI, and standalone Windows bundles are also available to download from the releases page on GitHub. (#38)
- User configuration files are now saved to an appropriate user directory. On Windows, this is the documents directory, and on other operating systems it's the user's home directory. (#40)
- Added a Tools menu item for setting active modules. (#27)
- Added an image shifting tool to the hardware control module. (#30)
- Added support for presets to laser widget. (#25)
- The laser widget is now a vertical list instead of a horizontal one. (#24)
- Resolved the issue of timelapse recordings sometimes containing too few frames. (#33)

A list of all code changes is available on GitHub: https://github.com/kasasxav/ImSwitch/compare/v1.0.0...v1.1.0


1.0.0
=====

Initial release.
