***********************
Detectors — reference
***********************

This page documents every ``DetectorManager`` implementation that ships
with ImSwitch2.  For each manager you get the setup-file JSON it expects,
field-by-field, plus any required low-level managers and vendor
libraries.  Detectors that come from plugin packages — the Zurich
Instruments lock-in (``zhinst.lockin-demod``), the Thorlabs TSI camera
(``thorlabs.tsi-camera``) and The Imaging Source IC4 camera
(``tis.camera-ic4``) — are described in :doc:`plugins`.

For the manager-writing-side perspective see
:doc:`/adding-device-support`; to wrap a driver from another project see
:doc:`/how-to/port-from-third-party`.

**Vendor libraries.**  The ``hardware`` extra (``pip install -e
".[hardware]"``, see :doc:`../installation`) installs ``nidaqmx``,
``pylablib``, ``pyvisa`` / ``pyvisa-py`` and ``microscope``.  No camera SDK
is in it: ``pyvcam`` (Photometrics), the Swabian ``TimeTagger`` package,
``thorlabs_tsi_sdk`` and the Hamamatsu DCAM and TIS drivers come from the
vendors' installers (see *Vendor SDKs (not installed by pip)* in
:doc:`../installation`).  What a manager does when its library is missing
is given in its **Vendor library** section.


How detectors are configured
============================

Detectors live under the top-level ``"detectors"`` dict in your setup
JSON.  Each entry uses the
``DetectorInfo`` shape (``imswitch.imcontrol.model.SetupInfo``), which
extends the generic ``DeviceInfo`` with two role flags.  Across every
manager in this category the consumed ``DetectorInfo`` fields are:

* ``managerName`` — selects which class below to instantiate.
* ``managerProperties`` — the per-manager kwargs documented below.
* ``forAcquisition`` (default ``false``) — read by the base
  ``DetectorManager`` and used by controllers (Image, Recording,
  Settings, View, Tiling, BFTimelapse and the event-triggered EtMonalisa,
  EtSTED and EtSnouty) to decide whether this detector participates in
  normal acquisition / recording.
* ``forFocusLock`` (default ``false``) — read by the base
  ``DetectorManager``; flags the detector as the focus-lock camera.

At least one of ``forAcquisition`` / ``forFocusLock`` MUST be ``true``,
otherwise the base class raises ``ValueError`` at construction time.

The ``analogChannel`` and ``digitalLine`` fields inherited from
``DeviceInfo`` exist in the dataclass but no detector manager reads
them — leave them out (or ``null``).

.. code-block:: json

    "detectors": {
        "<your_detector_name>": {
            "managerName": "<one of the classes below>",
            "managerProperties": { "...": "..." },
            "forAcquisition": true,
            "forFocusLock": false
        }
    }


Camera pixel size
-----------------

Every camera manager accepts ``cameraPixelSizeUm`` in its
``managerProperties``.  This is the *optically effective* pixel size at
the sample plane — the physical sensor pitch divided by the total
magnification — in micrometers, **not** the sensor pitch itself.  It is
exposed at runtime as the ``Camera pixel size`` detector parameter and is
what calibrates recorded files (OME ``PhysicalSize``, Fiji
``element_size_um``), the napari layer scale, scale bars, tiling and
stitching.  Scan-driven detectors (APD, PMT, TimeTagger) ignore it and
derive their pixel size from the scan step instead.

.. code-block:: json

    "managerProperties": { "cameraPixelSizeUm": 0.082 }

Omitting the key falls back to 0.15 µm, and a warning at startup says
that this placeholder will go into recordings.  A misspelled key
(``camerapixelsizeum``) or an unparseable value (``"0,082"`` — a decimal
comma) also falls back to 0.15 µm, with a warning naming the mistake.  A
read-only ``Camera pixel size source`` parameter next to the pixel size
records where the value came from: ``setup file``, ``assumed default``, or
``user`` once the value has been edited at runtime.

**The setup file wins over saved widget state.**  When
``cameraPixelSizeUm`` is set, the parameter is treated as instrument
calibration owned by the config: it is neither written into
``imcontrol_widget_states`` nor restored from it, so editing the setup
file takes effect on the next start.  Without the key the value is an
editable runtime setting like any other and does round-trip through
state persistence.


APDManager
==========

Avalanche photodiode wired to a counter input on a National
Instruments DAQ card.  Image is built up sample-by-sample during a
scan driven by ``NidaqManager``.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "APD": {
            "managerName": "APDManager",
            "managerProperties": {
                "terminal": "PFI0",
                "ctrInputLine": 0,
                "deviceName": "Dev1"
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 20 12 18 50
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``terminal``
     - str
     - **required**
     - Physical input terminal on the NI DAQ that the APD pulse train is wired to (e.g. ``"PFI0"``).
   * - ``ctrInputLine``
     - str or int
     - **required**
     - Counter that the physical terminal is connected to.  If an int ``N`` is given it is expanded to ``"<deviceName>/ctr<N>"``; otherwise the string is used verbatim.
   * - ``deviceName``
     - str
     - ``"Dev1"``
     - NI-DAQ device prefix used when ``ctrInputLine`` is given as an int.
   * - ``liveUpdateIntervalMs``
     - float
     - ``50.0``
     - Minimum wall-clock gap between live-preview redraws during a scan (50 ms = 20 Hz).  The image is filled in line by line, but pushing every line to the viewer floods the GUI on fast scans; this bounds the redraw rate independently of line rate and image size.  Raise it if the GUI still lags on a very fast rig, lower it for a more fluid preview on slow scans.  Does not affect acquired or recorded data.
   * - ``simulation_mode``
     - bool
     - ``false``
     - Run the mock counter instead of the NI-DAQ counter task, even when a DAQ is present.
   * - ``mockPhotonCountMean``
     - float
     - ``800.0``
     - Mean photon count per sample generated by the mock counter.  Also read as ``mock_photon_count_mean``; the camelCase spelling wins when both are given.
   * - ``mockPhotonCountMax``
     - int
     - ``5000``
     - Upper clip of the mock photon count per sample (at least 1).  Also read as ``mock_photon_count_max``.
   * - ``mockRandomSeed``
     - int or null
     - ``None``
     - Seed of the mock counter's random generator, for reproducible mock scans.  Also read as ``mock_random_seed``.

**Low-level dependencies**

* ``nidaqManager`` — required.  The manager connects to its
  ``sigScanBuilt`` and ``sigScanStarted`` signals to drive image
  acquisition during scans.

**Line-step scans**

When the Advanced scanning widget runs a scan with more than one line step,
each physical line is scanned once per step and the manager keeps the steps
as a separate axis: the frame handed to the recording is
``(1, S, Ny, Nx)`` — saved as ``(T, C, Y, X)``, one channel per line step.
``PMTManager`` records line steps the same way; only its live view sums
them.

**Vendor library**

None directly imported by this module.  All NI-DAQ traffic flows
through ``nidaqManager``.  ``matplotlib.pyplot`` is imported at module
top for the debug-plot path.

**Source**

`APDManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/APDManager.py>`_


AVManager
=========

The no-hardware camera. It serves synthetic frames from ``MockCameraTIS``:
no video driver is bundled (the Allied Vision interface this manager once
wrapped was removed upstream in 2022), so every ``cameraListIndex`` loads the
mock. The shipped no-hardware setups use it, and its model name says *mock*
so that a recording made from it cannot be mistaken for a real camera's.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "AVCam": {
            "managerName": "AVManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "avcam": {
                    "exposure": 10000,
                    "gain": 1
                }
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 64
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``cameraListIndex``
     - int or str
     - Accepted for compatibility; ``"mock"`` states what every value does.
   * - ``avcam``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup; the mock accepts and ignores them.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

None. ``imswitch.imcontrol.model.interfaces.tiscamera_mock.MockCameraTIS``
is the only camera this manager can construct.

**Source**

`AVManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/AVManager.py>`_


HamamatsuManager
================

Hamamatsu sCMOS cameras driven through the DCAM API.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "OrcaFlash": {
            "managerName": "HamamatsuManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "hamamatsu": {
                    "exposure_time": 0.01,
                    "readout_speed": 2
                }
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 64
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``cameraListIndex``
     - int or str
     - Index of the camera in the DCAM-enumerated list (0-based).  Set to an invalid value (e.g. ``"mock"``) to force the mock fallback.
   * - ``hamamatsu``
     - dict
     - Dictionary of DCAM API property name → value pairs applied via ``setPropertyValue`` at startup.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.hamamatsu.HamamatsuCameraMR`` is
lazy-imported inside ``_getCameraObj``.  On failure the manager
substitutes ``MockHamamatsu`` from
``imswitch.imcontrol.model.interfaces.hamamatsu_mock``.

**Source**

`HamamatsuManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py>`_


PMTManager
==========

Photomultiplier tube on an analog input of a National Instruments DAQ
card.  Image is built during a scan driven by ``NidaqManager``.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "PMT": {
            "managerName": "PMTManager",
            "managerProperties": {
                "analogInputLine": 0,
                "deviceName": "Dev1",
                "offset_v": 0.0
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 18 46
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``analogInputLine``
     - str or int
     - ``None``
     - NI-DAQ analog input line the PMT is wired to.  If an int ``N`` is given it is expanded to ``"<deviceName>/ai<N>"``; otherwise the string is used verbatim.
   * - ``deviceName``
     - str
     - ``"Dev1"``
     - NI-DAQ device prefix used when ``analogInputLine`` is given as an int.
   * - ``aiVoltageMin``
     - float
     - ``-5.0``
     - Lower end of the analog-input range handed to the driver, in volts. Declare the preamp's real swing: a small signal on a wide range wastes ADC resolution. The card coerces the pair up to the nearest range it has.
   * - ``aiVoltageMax``
     - float
     - ``5.0``
     - Upper end of the analog-input range, in volts. A signal above it is clipped flat by the card with no error.
   * - ``offset_v``
     - float
     - ``0.0``
     - Voltage offset subtracted from each sample before being treated as signal.
   * - ``liveUpdateIntervalMs``
     - float
     - ``50.0``
     - Minimum wall-clock gap between live-preview redraws during a scan (50 ms = 20 Hz).  Same meaning as for ``APDManager``; bounds GUI redraws only, never acquired data.
   * - ``simulation_mode``
     - bool
     - ``false``
     - Run the mock voltage source instead of the NI-DAQ analog-input task, even when a DAQ is present.
   * - ``mockVoltageMin``
     - float
     - ``-5.0``
     - Lower bound of the mock voltage signal.  Also read as ``mock_voltage_min``; swapped with the maximum if given the wrong way round.
   * - ``mockVoltageMax``
     - float
     - ``5.0``
     - Upper bound of the mock voltage signal.  Also read as ``mock_voltage_max``.
   * - ``mockVoltageMean``
     - float
     - ``0.0``
     - Mean of the mock voltage signal.  Also read as ``mock_voltage_mean``.
   * - ``mockVoltageNoiseStd``
     - float
     - ``0.5``
     - Standard deviation of the mock signal's noise (clamped at 0).  Also read as ``mock_voltage_noise_std``.
   * - ``mockRandomSeed``
     - int or null
     - ``None``
     - Seed of the mock source's random generator, for reproducible mock scans.  Also read as ``mock_random_seed``.

**Low-level dependencies**

* ``nidaqManager`` — required.  The manager connects to its
  ``sigScanBuilt`` and ``sigScanStarted`` signals to drive image
  acquisition during scans.

**Line-step scans**

As with ``APDManager``, the line steps stay a separate axis in what the
manager hands to a recording: the raw frame is the unsummed
``(1, S, Ny, Nx)`` volume, published once the scan is complete and saved
with one channel per line step.  Only the live view differs: it shows the
steps **summed** into one 2D image.

**Vendor library**

None directly imported by this module.  All NI-DAQ traffic flows
through ``nidaqManager``.  ``matplotlib.pyplot`` is imported at module
top for the debug-plot path.

**Source**

`PMTManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/PMTManager.py>`_


PhotometricsManager
===================

Photometrics cameras driven via the ``pyvcam`` (PVCAM) SDK.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "PhotometricsCam": {
            "managerName": "PhotometricsManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "Photometrics": {
                    "Set exposure time": 10
                }
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 64
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``cameraListIndex``
     - int
     - Index of the camera in the PVCAM enumeration (read but not used to address the device — the manager opens the first detected camera).
       The string ``"mock"`` selects ``MockPhotometrics`` without trying the SDK.
   * - ``Photometrics``
     - dict (optional)
     - Dictionary of detector-parameter names (e.g. ``"Set exposure time"``) → values applied via ``setParameter`` after the camera is opened.  If the key is absent no defaults are pushed.

``cameraListIndex`` is **required**; ``Photometrics`` is optional and
the code checks ``if 'Photometrics' in detectorInfo.managerProperties``
before reading it.

**Low-level dependencies**

None.

**Vendor library**

``pyvcam.pvc`` and ``pyvcam.camera.Camera`` are lazy-imported inside
``_getCameraObj``.  On failure the manager logs a warning and substitutes
``MockPhotometrics`` from
``imswitch.imcontrol.model.interfaces.photometrics_mock``;
``"cameraListIndex": "mock"`` selects that mock directly.

**Source**

`PhotometricsManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py>`_


PiCamManager
============

Raspberry-Pi camera reached over a network socket (host/port).

**Setup JSON**

.. code-block:: json

    "detectors": {
        "PiCam": {
            "managerName": "PiCamManager",
            "managerProperties": {
                "cameraHost": "192.168.1.50",
                "cameraPort": 8000,
                "picam": {
                    "exposure": 10000,
                    "gain": 1
                }
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 64
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``cameraHost``
     - str
     - Hostname / IP address of the Pi-camera bridge server.
   * - ``cameraPort``
     - int
     - TCP port the bridge is listening on.
   * - ``picam``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup.

All three fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.picamera.CameraPiCam`` is
lazy-imported inside ``_getPiCamObj``.  On failure the manager
substitutes ``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`PiCamManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/PiCamManager.py>`_


.. _swabian-detector:

SwabianTimeTaggerManager
========================

Swabian Instruments TimeTagger for FLIM (fluorescence-lifetime imaging).
Fits a lifetime per pixel from TCSPC histograms during an NI-DAQ-driven
scan.

The card itself is described once, in the setup's top-level ``timeTagger``
block (see :doc:`../setupinfo-reference`): which input carries the photons,
the laser sync and the scan's line clock, with their trigger levels, edge
signs, dead times and delays. The block is loaded into one shared
``TimeTaggerManager`` that this detector, any second time-resolved detector
and the scripting facade all use. The detector only names the *roles* it
reads.

**Setup JSON**

.. code-block:: json

    "timeTagger": {
        "photonsChannel": -1,
        "photonsTriggerV": -0.25,
        "laserSyncChannel": 2,
        "laserSyncTriggerV": 0.5,
        "lineClockChannel": 3,
        "lineClockTriggerV": 0.5
    },
    "detectors": {
        "FLIM": {
            "managerName": "SwabianTimeTaggerManager",
            "managerProperties": {
                "click_role": "photons",
                "start_role": "laser_sync",
                "line_role": "line_clock",
                "binwidth_ps": 32,
                "t0_ps": 0,
                "min_counts_per_pixel": 20,
                "fit_method": "moment",
                "laser_rep_rate_mhz": 80.0,
                "enabled": true
            },
            "forAcquisition": true
        }
    }

A setup **without** a ``timeTagger`` block still works: the detector then
builds a private card manager from the legacy ``click_channel`` /
``start_channel`` / ``line_channel`` and ``*_trigger`` properties listed at
the end of the table, and logs, once at startup, the equivalent block to
move them into. Put the card in the block as soon as a script or a second
consumer needs it.

**managerProperties**

.. list-table::
   :widths: 24 14 18 44
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``click_role``
     - str
     - ``"photons"``
     - Role on the ``timeTagger`` block whose input receives the photon clicks. The resolved channel shows as the read-only ``click_channel`` parameter.
   * - ``start_role``
     - str
     - ``"laser_sync"``
     - Role whose input receives the TCSPC start (laser sync).
   * - ``line_role``
     - str
     - ``"line_clock"``
     - Role whose input receives the scan's line clock; the pixel markers are generated from its edges, starting ``pixelPatternOffsetPs`` (block field, default 10 ns) plus a positive ``lineClockDelayPs`` after each edge.
   * - ``frame_role``
     - str
     - ``"auto"``
     - Role whose input receives the scan's frame-start clock: ``"auto"`` uses ``frame_clock`` when the block configures it, ``"none"`` never. With it the card re-syncs its pixel index on every frame edge, so a lost line marker costs one frame, not the rest of the scan. The resolved channel shows as the read-only ``frame_channel`` parameter (``0`` when none).
   * - ``live_fit_period_s``
     - float
     - ``1.0``
     - How often, during a scan, a preview with per-pixel lifetimes is fitted and emitted; ``0`` emits intensity-only previews (``preview: "intensity"`` in their metadata) and fits the final frame only.
   * - ``n_bins``
     - int
     - one laser period
     - Number of TCSPC histogram bins per pixel. Omitted, it spans one period of ``laser_rep_rate_mhz`` at ``binwidth_ps`` (391 bins at 80 MHz / 32 ps). A declared window shorter than 80 % of the period is warned about at startup: the ``moment`` and ``phasor`` fits read a truncated decay as a short lifetime, plausibly and without any other sign.
   * - ``binwidth_ps``
     - int
     - ``32``
     - Histogram bin width in picoseconds.
   * - ``t0_ps``
     - int
     - ``0``
     - The IRF peak's position, in picoseconds, measured with the card's configured conditioning (tutorial 06): an absolute number. Forward mode applies it as ``-t0_ps`` on top of the photon input's configured delay for the scan and restores the input afterwards; reverse mode applies it as a circular roll.
   * - ``min_counts_per_pixel``
     - int
     - ``20``
     - Minimum photon count for a pixel's fit to be considered valid; below this the lifetime is treated as NaN.
   * - ``fit_method``
     - str
     - ``"moment"``
     - Lifetime fit method; one of ``"moment"``, ``"phasor"``, ``"exp1"``.  All three apply automatic IRF-peak compensation (see "Lifetime fitting" below); ``exp1`` is the most accurate for clean mono-exponential decays.
   * - ``laser_rep_rate_mhz``
     - float
     - ``80.0``
     - Laser repetition rate in MHz.  Used by the phasor fit to set ω = 2π·f_rep.  The Swabian ``Flim`` API does not expose the rate, so it must be supplied here; measure it once with ``scripts/diagnostics/measure_laser_rep_rate.py`` if unsure.
   * - ``background_rate_hz``
     - float
     - ``0.0``
     - Dark counts plus afterpulsing of the photon detector, in Hz, measured with the laser blocked. Flat over the laser period, it is subtracted from every pixel's histogram before fitting or gating (it pulls the moment towards half the period and the phasor towards the origin). ``0`` = no subtraction. Also a runtime parameter.
   * - ``enabled``
     - bool
     - ``true``
     - Whether the detector takes part in scans; also a runtime parameter (``'True'`` / ``'False'``). Off, the manager is constructed but ``initiateScan`` is a no-op, so another detector images while the card is not held and a calibration can run during the scan (tutorials 07 to 09).
   * - ``click_channel``
     - int
     - legacy, **required** without a ``timeTagger`` block
     - The photon input, read only when the setup has no ``timeTagger`` block (a negative number selects the falling edge). With the block it is ignored, with a log line naming it.
   * - ``start_channel``
     - int
     - legacy, **required** without a ``timeTagger`` block
     - The laser-sync input, read only without a ``timeTagger`` block.
   * - ``line_channel``
     - int
     - legacy, **required** without a ``timeTagger`` block
     - The line-clock input, read only without a ``timeTagger`` block.
   * - ``click_trigger``
     - float
     - legacy, ``trigger_levels[str(click_channel)]`` or ``0.5``
     - Trigger threshold in volts for the photon input, read only without a ``timeTagger`` block.
   * - ``start_trigger``
     - float
     - legacy, ``trigger_levels[str(start_channel)]`` or ``0.5``
     - Trigger threshold in volts for the laser-sync input, read only without a ``timeTagger`` block.
   * - ``line_trigger``
     - float
     - legacy, ``trigger_levels[str(line_channel)]`` or ``0.5``
     - Trigger threshold in volts for the line-clock input, read only without a ``timeTagger`` block.
   * - ``trigger_levels``
     - dict
     - legacy, ``{}``
     - Dict mapping channel-number-as-string to trigger threshold in volts, seeding the three ``*_trigger`` defaults. Read only without a ``timeTagger`` block.

The three ``*_trigger`` detector *parameters* exist with either
configuration: they show the card's current trigger levels for the chosen
roles and write a new value through to the card. A write is refused, with a
log line, while a scan holds the card, and the parameter then shows the
card's real value again.

**TCSPC direction**

With ``filterSyncByPhotons`` off in the ``timeTagger`` block (the default)
the histogram runs *forward*: the laser sync starts it and the photon stops
it, and ``t0_ps`` is applied as a delay on the photon channel. With the
card's conditional filter on, only the first sync after each photon reaches
the PC, so the histogram must run in *reverse*: the photon starts it and
the sync stops it. The detector swaps the ``Flim`` channels, mirrors the
time axis on the laser period so every fit, gate and plot still reads
forward time, applies ``t0_ps`` as a circular roll (a delay on the photon
channel would drop the earliest photons here), and refuses to prepare a
scan whose window is shorter than one laser period, because in reverse
mode a short window cuts the *peak* off, not the tail. Reverse mode is
pending an acceptance test on a real card (the Lifetime 2.0 plan, §3.2):
the vendor documents that filtered and compensated timestamps can be
reordered on some models, which the mock does not reproduce.

**Live products and frame validity**

Every frame the worker publishes (a preview each second, then the final
one) is also emitted as a ``LiveProducts`` on the detector's
``sigTimeResolvedProducts`` signal: intensity, lifetime, the aggregated
decay in forward time, the gate images of the open product session, the
peak position, the background per bin, the worst per-pixel pile-up
fraction, and metadata. It never carries the per-pixel cube. The pile-up
fraction (photons per excitation pulse, from the *configured* rep rate) is
warned about above 5 %. USB overflows are counted by the card manager
against a baseline taken when the scan was prepared; a frame read after
the count moved carries ``overflows`` and ``frame_valid: false`` in its
metadata, with an error in the log, and its lifetimes are not to be
trusted.

**The final frame.** The card closes a frame once it has counted the last
pixel end, and the worker reads it through ``getReadyFrameEx`` when the
card's frame count has moved past the count taken at arming -- whether
that happens before or after the scan reports done. A frame the card has
not closed within a grace period after scan-done is read as it stands,
with ``frame_closed_by_card: false`` in the metadata and a warning naming
the likely cause (a missing marker, or the frame edge arriving after
pixel 0); tutorial 08 checks this. Scans with more than one linestep are
refused until the TTL designer emits one line edge per linestep (ROADMAP
M9): the clock carries only ``Ny`` edges today, so the markers would stop
after the first step.

**Lifetime fitting**

The worker fits a lifetime per pixel using the selected ``fit_method``.
Before any fit runs, the aggregated TCSPC decay over valid pixels
(``intensity ≥ min_counts_per_pixel``) is summed and the IRF peak bin is
located by ``argmax``.  Each fit then compensates for the resulting
``t_peak`` offset:

* ``moment`` — reports ``mean − t_peak``.  Simple and fast, but biased
  low when τ approaches the histogram window (tail truncation).
* ``exp1`` — weighted log-linear fit restricted to bins ≥ peak with the
  time axis shifted so the peak is at ``t=0``.  Removes the IRF rising
  edge from the fit; most accurate of the three for clean
  single-exponential decays.
* ``phasor`` — computes the first-harmonic phasor ``(g, s)`` at
  ω = 2π · ``laser_rep_rate_mhz``, then rotates by ``−ω·t_peak`` to
  undo the time-shift's phase contribution.  Close to the true τ when
  τ ≪ T_rep; residual underestimate grows as τ approaches T_rep.

In addition to the per-pixel lifetime image, the worker also emits the
aggregated decay and a global τ fit (using the same method) for the
Lifetime widget's decay view (:doc:`../gui`).

**Time-resolved workflow products**

The manager implements the generic time-resolved detector contract used
by the headless workflows.  Normal LiveView, ``getLatestFrame()``, and
recording behavior stay unchanged: the detector stream still exposes a
2D lifetime image.  Advanced products are opt-in through
``facade.time_resolved``:

* binned per-pixel photon-arrival cube, ``cube_counts`` with axes
  ``("y", "x", "tcspc_bin")``;
* software gate images computed from configurable nanosecond windows;
* intensity, lifetime image, aggregated decay, and global τ metadata.

Scripts typically build the facade with:

.. code-block:: python

   facade = api.imcontrol.buildWorkflowFacade(
       time_resolved_detector_name="FLIM",
   )

and then run one of ``BinnedPhotonArrivalWorkflow``,
``GatedSTEDWorkflow``, or ``TauSTEDWorkflow``.  See
:doc:`../scripting-time-resolved-workflows` for examples and the HDF5
schema.

The current Swabian product side-channel supports 2D ``x/y`` scans.  If
explicit product capture is enabled for a scan with active outer axes
such as ``z`` or time, the manager raises a clear error until
multidimensional output support is implemented.

**The Lifetime widget**

``Lifetime`` in ``availableWidgets`` (``FLIMHist`` in older setup files)
shows this detector's products live: the aggregated decay with the IRF
peak and background, the histogram of the per-pixel lifetimes, the phasor,
the lifetime or intensity image or an intensity-weighted overlay as a
viewer layer, the pile-up map, and the shared card's signals; its Run and
Live buttons run the Scan widget's scan through the same workflow the
scripts use. See :doc:`../gui`.

**Low-level dependencies**

* ``nidaqManager`` — required.  The manager connects to its
  ``sigScanBuilt``, ``sigScanStarted`` and ``sigScanDone`` signals to
  align FLIM acquisition with the scan timeline.
* ``timeTaggerManager`` — the shared card, built from the setup's
  ``timeTagger`` block; ``None`` without the block, in which case the
  detector builds a private one (see above). From scan preparation until
  the final frame has landed the detector *holds* the card: trigger-level,
  delay and dead-time changes from any consumer are refused meanwhile.

**Vendor library and mock**

The Swabian ``TimeTagger`` Python package comes from the vendor installer
and is imported by ``TimeTaggerManager``, not by this detector. Without it,
or with no card on the USB, the card manager logs an error at startup and
the application starts; with ``enabled: true``, preparing a scan then
raises ``RuntimeError``, the failure is reported to the NI-DAQ manager and
the whole scan is rolled back before it starts. To keep the detector in a
setup that has no FLIM hardware, set ``"enabled": false`` — or, on a
simulated rig (``nidaq.simulation`` true), set ``"simulation": true`` in
the ``timeTagger`` block to use the in-process mock card
(``imswitch.imcontrol.model.interfaces.timetagger_mock``). The mock card
images a synthetic sample (``timeTagger.mockSample``) through a Poisson
TCSPC model with an IRF, dark counts and afterpulsing, takes its line and
frame edges from the scan designer's own TTL waveforms, and reproduces
trigger levels, dead time, the card's tag budget (overflows) and the
conditional filter; ``timeTagger.mockFaults`` breaks the signal on purpose
for the debugging tutorials. ``useMockOnFailure`` falls back to the mock when
the card cannot be opened, but only while the NI-DAQ is simulated too: a
rig never images a missing card as zeros.

**Source**

`SwabianTimeTaggerManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py>`_


ThorCamTSIManager
=================

Thorlabs Scientific Cameras (TSI SDK) — Zelux, Kiralux, Quantalux.

The ``imswitch-device-thorlabs`` example plugin ships this manager too, as
``thorlabs.tsi-camera`` with the alias ``ThorCamTSIManager``.  When that
plugin is installed, a setup naming ``ThorCamTSIManager`` loads the
plugin's class instead of this one, and a warning in the log says so; see
:doc:`plugins`.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "Zelux": {
            "managerName": "ThorCamTSIManager",
            "managerProperties": {
                "cameraSerial": "12345",
                "dllLocation": "dlls/64_lib",
                "defaults": {
                    "exposure_us": 50000,
                    "gain": 0,
                    "operation_mode": "Software",
                    "trigger_polarity": "Active High",
                    "frame_rate": 30
                }
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 12 18 48
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``cameraSerial``
     - str or null
     - ``None``
     - Camera serial number.  If ``null`` the SDK opens the first available camera.  Any value starting with ``"MOCK_"`` forces the mock fallback for headless testing.
   * - ``dllLocation``
     - str
     - ``"dlls/64_lib"``
     - Path (relative to the working directory on Windows) to the Thorlabs TSI DLLs.
   * - ``frameBufferDepth``
     - int
     - ``4``
     - Frames the SDK ring buffer holds between the camera and the acquisition loop. What the depth buys is tolerance to latency spikes (a GC pause, an HDF5 resize, a writer block) of up to ``depth / framerate`` seconds; the camera is re-armed at this depth on every trigger-mode change.
   * - ``flushFrameLimit``
     - int
     - ``256``
     - Most frames drained from the SDK's queue in one flush before acquisition restarts; raise it if stale frames survive a restart on a fast camera.
   * - ``defaults``
     - dict
     - ``{}``
     - Optional sub-dict of default parameter values; see below.

The ``defaults`` sub-dict supports the following keys, each looked up
with ``.get(...)`` against the literal default shown:

.. list-table::
   :widths: 26 14 18 42
   :header-rows: 1

   * - Key
     - Type
     - Default
     - Meaning
   * - ``exposure_us``
     - int
     - ``50000``
     - Initial exposure time in microseconds.
   * - ``gain``
     - int
     - ``0``
     - Initial camera gain (dB).
   * - ``operation_mode``
     - str
     - ``"Software"``
     - One of ``"Software"``, ``"Hardware"``, ``"Bulb"``.
   * - ``trigger_polarity``
     - str
     - ``"Active High"``
     - One of ``"Active High"``, ``"Active Low"``.
   * - ``frame_rate``
     - int
     - ``30``
     - Initial frame rate in Hz when frame-rate control is enabled.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.thorcamera_tsi.ThorTSICamera``
is lazy-imported inside ``_initCamera``.  On ``ImportError``,
``RuntimeError``, or ``ValueError`` (or if ``cameraSerial`` starts with
``"MOCK_"``) the manager substitutes ``MockThorTSICamera`` from the
same interface module.

**Source**

`ThorCamTSIManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py>`_


TISManager
==========

The Imaging Source (TIS) cameras through IC Imaging Control 3.  The
bundled ``pyicic`` wrapper loads the TISGrabber C DLL
(``tisgrabber_x64.dll``) with ``ctypes.windll``, so the real camera works
on Windows only; elsewhere the manager falls back to the mock.  Installing
the driver and the DLL is described in :doc:`../TISCamera`.

For IC Imaging Control 4 (Linux and Windows) there is a separate plugin
manager, ``tis.camera-ic4``; see :doc:`plugins`.  It has a different id, so
setups naming ``TISManager`` keep using this manager.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "TISCam": {
            "managerName": "TISManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "tis": {
                    "image_width": 1280,
                    "image_height": 1024
                }
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 22 14 64
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``cameraListIndex``
     - int or str
     - Index of the camera in the enumerated TIS camera list (0-based).  Set to an invalid value (e.g. ``"mock"``) to force the mock fallback.
   * - ``tis``
     - dict
     - Camera properties. ``image_width`` and ``image_height`` (pixels) set the full-chip size the manager reports. ``exposure``, ``gain`` and ``brightness`` are accepted but not applied at startup: the camera keeps its own settings, the detector parameters show what it reports, and edits there (exposure in ms, gain/brightness in device units) are written to the camera.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.tiscamera.CameraTIS`` (built on
the bundled ``pyicic`` wrapper) is lazy-imported inside ``_getTISObj``.
On failure the manager substitutes
``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`TISManager.py <https://github.com/Imswitch2/ImSwitch2/blob/main/imswitch/imcontrol/model/managers/detectors/TISManager.py>`_
