*********
Changelog
*********

Unreleased
==========

**New Features**

- FLIM histogram widget gained a "Decay" mode that plots the aggregated TCSPC photon-arrival histogram with a red marker showing the global single-τ fit for the selected method; the original per-pixel lifetime distribution remains as "Lifetime dist." mode
- Added ``laser_rep_rate_mhz`` parameter to ``SwabianTimeTaggerManager`` so the phasor fit uses the true ω = 2π·f_rep instead of assuming the histogram window equals one laser period
- Added ``scripts/diagnostics/measure_laser_rep_rate.py`` — a TimeTagger.Countrate helper that prints the measured laser repetition rate for plugging into ``laser_rep_rate_mhz``
- Added opt-in time-resolved detector workflows for binned photon-arrival cubes, software gated-STED images, and tau-STED lifetime products. The first backend is ``SwabianTimeTaggerManager`` via a generic detector contract and ``api.imcontrol.buildWorkflowFacade(time_resolved_detector_name=...)``.
- Added stitched-overview cell target detection for tiling workflows. The GUI "Detect cells" action is passive and only overlays markers; automated/API cell targeting explicitly moves through detected targets and can invoke a per-cell workflow callback.
- Renamed the WFS polarisation-resolved acquisition workflow to ``WidefieldStarssWorkflow`` / ``WidefieldStarssParams``. The old ``RecordingWorkflow`` / ``RecordingParams`` names remain available as compatibility aliases.
- Added per-detector acquisition selection: the **Acquire with** control in the View widget chooses which detectors acquire. Free-running detectors are reseeded immediately; scan-driven changes take effect at the next scan iteration rather than disturbing one in flight. An active recording, workflow or event modality overrides deselection, and faulted detectors are always excluded. See :doc:`gui`.
- Point detectors (APD/PMT) now support multi-line-step scans driven by the Advanced scanning widget, where each line step gates a different set of lasers over the same physical line. APD frames keep the line step as its own axis and are saved as ``(T, C, Y, X)`` with correctly ordered OME axis metadata instead of collapsing to ``(T, Y, X)``. See :doc:`gui`.
- Detector acquisition is now lease-based, with a shared scan-execution coordinator owning the participant snapshot, the ``SCAN`` lease and an end-of-scan barrier. Two scans can no longer collide: a second entry point is refused with a clear "scan busy" error, including between repeat frames. The barrier waits for each point detector's final read before releasing hardware, so the last frame of a scan is no longer lost to teardown or to the next repeat frame. Developer reference: :doc:`scan-lifecycle`.
- Added ``LaserInfo.powerDevice`` so a laser entry that only owns a scan-gated TTL line can name the separate device that sets its emission power (an AOTF channel, for example); scans then switch that partner on for the scan's duration. See :doc:`setupinfo-reference`.
- Added the ``liveUpdateIntervalMs`` manager property for APD/PMT detectors to tune the live-preview redraw rate per rig (default 50 ms). See :doc:`devices/detectors`.
- Line-step scans emit ``[LineStepDiag]`` diagnostics from the scan controller, NI-DAQ manager and APD manager — expanded line count, sample budget, clock edges and read plan — for verifying line-step timing against the generated waveform on a real rig.
- BeadRec fitting in both ImControl and the ImProcess reconstructor now includes an isotropic 2D exponential-decay model and a one-dimensional sine model with fitted wavelength, phase and arbitrary image-plane angle.

**Bug Fixes**

- FLIM lifetimes from the three fit methods (moment, phasor, exp1) no longer diverge when the IRF peak is offset from t=0. The worker now auto-detects the IRF peak bin from the aggregated decay each frame and compensates each fitter: moment subtracts ``t_peak`` from the mean, exp1 restricts the fit to bins ≥ peak with a shifted time axis, phasor rotates the measured (g, s) by −ω·t_peak. On known dyes the three methods now converge toward the true lifetime.
- Phasor fit previously assumed the laser repetition period equalled ``n_bins * binwidth_ps``, which silently scaled τ by an arbitrary factor whenever the histogram window did not happen to span exactly one laser period
- The tiling GUI cell-detection path no longer iterates the stage through every detected target. Stage iteration is reserved for explicit automated/API cell targeting calls or manual click-to-navigate.
- Recording a scan with a point detector (APD/PMT/TimeTagger) no longer fails with a false ``Detector '<name>' stalled: no frames received for 10.0s`` whenever the scan itself ran longer than the watchdog timeout. These detectors integrate the whole scan and publish a single frame at the end, so the watchdog now starts counting at the scan's expected or observed completion instead of at recording start. Line-step scans, which multiply scan duration by the number of steps, were the common way to trip this.
- Enabling **Repeat** on an NI-DAQ scan no longer crashes the GUI. ``scanDone`` re-armed the next frame synchronously from the finished scan's task-completion slot, recreating NI-DAQ tasks, wait threads and per-detector scan threads while the previous ones were still shutting down (a ``QThread`` destroyed while running). The re-arm is now deferred to the next event-loop turn, and aborting or un-checking Repeat in that gap cancels cleanly. Only reproducible on real hardware — simulation mode never created the wait threads.
- Point-detector live preview is no longer choppy on slow scans and no longer floods the GUI on fast ones. Redraws were gated on image *shape* with a random draw per scan line; they are now limited deterministically by wall clock (default 20 Hz, see ``liveUpdateIntervalMs``).
- Detector settings restored at startup now reach the hardware. State is applied to the detector first and the widget refreshed from the resulting state, so ROI, binning and editable parameters land on the correct detector instead of being shown but never applied.
- A ``cameraPixelSizeUm`` set in the setup file is no longer overridden at startup by the value saved in ``imcontrol_widget_states``. The camera pixel size is instrument calibration, so when the setup file declares it the config wins: the value is neither snapshotted nor restored, and a stale snapshot from before the config was edited no longer silently reinstates the old calibration into every recording's metadata (OME ``PhysicalSize``, Fiji ``element_size_um``, ``Detector:<name>:Param:Camera pixel size``). Cameras without the key keep the previous behaviour, where the pixel size is an ordinary editable setting that persists. See :doc:`devices/detectors`.
- A failure while refreshing the detector settings display after a state restore is now reported as a warning instead of silently leaving the widget showing pre-restore values while the detector runs with the restored ones.
- The Hamamatsu settings tree now reflects the camera's actual trigger source. ``_updatePropertiesFromCamera`` compared ``getPropertyValue('trigger_source')`` — which returns ``(value, type)`` — against an integer, so every branch was dead and **Trigger source** was never refreshed from the camera; it kept showing whatever ImSwitch had last written. The read-back no longer routes back through the hardware write either, so changing the exposure no longer re-pokes the trigger configuration.
- The IC4 (Imaging Source) camera now reports the ROI, exposure and gain the sensor actually took rather than what was requested. Width/Height/Offset are rounded down to the sensor's increment (commonly 4 or 8 px) and exposure/gain are clamped to their legal ranges, so an arbitrary ROI from the GUI's selector left ``frameStart``/``shape`` — and with them the settings display, the viewer scale, the recording shape and the OME metadata — describing frames the camera was not producing. Matches the read-back ``HamamatsuManager.crop`` already did.
- Lasers no longer perform hardware writes from the scan-signal path, which could drive TTL lines incorrectly during a scan; resolving scan devices now only updates which laser controls are editable. Scan arming reapplies the intended amplitude per scan, disarms only the lasers that scan armed, and leaves power setpoints editable while a scan runs (only the on/off control locks).
- Scan-driven detectors (APD/PMT/TimeTagger) are now counted as producing one frame per scan when computing expected frame totals, preventing hung or truncated scan recordings.
- The TIS (Imaging Source) camera no longer crashes focus lock or chunk capture when the driver buffer is briefly empty. A missing frame is treated as a transient condition, and the last valid frame is preserved for polling callers instead of being duplicated into the stream.
- TriggerScope scans now use the shared scan-execution lifecycle across raster, RESOLFT, galvo and multicolor controllers. Laser membership is resolved before the firmware starts so selected laser controls lock and their gates are armed; board-wide start/done signals are handled only by the owning controller; scan-driven detector leases and their finish barrier are retained through completion. A stop still lets the autonomous iteration and its final detector read finish, while reliably preventing repeat or sequence continuation even when recording teardown arrives during completion publication.
- Scan recordings on setups with several scan widgets are now bound to the scan that actually runs. A scan-once recording is armed and then picks up the frame count, camera-TTL map and scan dimensions from whichever scan widget the operator starts, instead of from whichever controller happened to expose those accessors — previously a rig with both a TriggerScope raster and a TriggerScope RESOLFT widget always recorded with the raster's pixel grid and the scan failed. A recording that cannot be armed now stops the scan rather than letting it run unrecorded. All TriggerScope controllers report their geometry (``roSteps x cycleSteps x timeLapsePoints`` for the RESOLFT-family modes). Timelapse scans, which the recording starts itself, take their scanner from a new **Scan source** control in the Recording widget, shown only when the setup offers a choice and refused while no choice has been made rather than defaulting to one. Scan dimensions and step sizes are omitted rather than borrowed from another scanner when the chosen one does not report them. See :doc:`scan-lifecycle`.
- Live View, focus and other latest-frame readers now follow frames drained first by BeadRec or a recording. The shared frame broker latches the newest hardware frame regardless of which consumer performed the drain, preventing a TriggerScope raster reconstruction from leaving Live View frozen on its pre-scan image.

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
