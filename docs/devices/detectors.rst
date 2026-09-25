***********************
Detectors — reference
***********************

This page documents every ``DetectorManager`` implementation in
ImSwitch.  For each manager you get the setup-file JSON it expects,
field-by-field, plus any required low-level managers and vendor
libraries.

For the manager-writing-side perspective see
:doc:`/adding-device-support`; for end-to-end recipes see the
:doc:`how-to guides </how-to/wire-teensy>`.


How detectors are configured
============================

Detectors live under the top-level ``"detectors"`` dict in your setup
JSON.  Each entry uses the
:class:`~imswitch.imcontrol.model.SetupInfo.DetectorInfo` shape, which
extends the generic ``DeviceInfo`` with two role flags.  Across every
manager in this category the consumed ``DetectorInfo`` fields are:

* ``managerName`` — selects which class below to instantiate.
* ``managerProperties`` — the per-manager kwargs documented below.
* ``forAcquisition`` (default ``false``) — read by the base
  ``DetectorManager`` and used by controllers (Image, Recording,
  Settings, Tiling, EtMonalisa, EtSTED, BFTimelapse) to decide whether
  this detector participates in normal acquisition / recording.
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

Omitting the key falls back to 0.15 µm.  A misspelled key
(``camerapixelsizeum``) or an unparseable value (``"0,082"`` — a decimal
comma) also falls back to 0.15 µm, but logs a warning naming the mistake.

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
``PMTManager`` instead sums the line steps into a single 2D image before
publishing.

**Vendor library**

None directly imported by this module.  All NI-DAQ traffic flows
through ``nidaqManager``.  ``matplotlib.pyplot`` is imported at module
top for the debug-plot path.

**Source**

`APDManager.py <../../imswitch/imcontrol/model/managers/detectors/APDManager.py>`_


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

`AVManager.py <../../imswitch/imcontrol/model/managers/detectors/AVManager.py>`_


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

`HamamatsuManager.py <../../imswitch/imcontrol/model/managers/detectors/HamamatsuManager.py>`_


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

Unlike ``APDManager``, which keeps the line steps as separate channels, this
manager **sums** them into one 2D image before publishing, so a multi-line-step
scan yields a single ``(1, Ny, Nx)`` frame.

**Vendor library**

None directly imported by this module.  All NI-DAQ traffic flows
through ``nidaqManager``.  ``matplotlib.pyplot`` is imported at module
top for the debug-plot path.

**Source**

`PMTManager.py <../../imswitch/imcontrol/model/managers/detectors/PMTManager.py>`_


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
``_getCameraObj``.  On failure the manager substitutes ``MockHamamatsu``
from ``imswitch.imcontrol.model.interfaces.hamamatsu_mock``.

**Source**

`PhotometricsManager.py <../../imswitch/imcontrol/model/managers/detectors/PhotometricsManager.py>`_


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

`PiCamManager.py <../../imswitch/imcontrol/model/managers/detectors/PiCamManager.py>`_


SwabianTimeTaggerManager
========================

Swabian Instruments TimeTagger for FLIM (fluorescence-lifetime imaging).
Fits a lifetime per pixel from TCSPC histograms during an NI-DAQ-driven
scan.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "FLIM": {
            "managerName": "SwabianTimeTaggerManager",
            "managerProperties": {
                "click_channel": 1,
                "start_channel": 2,
                "line_channel": 3,
                "binwidth_ps": 32,
                "t0_ps": 0,
                "min_counts_per_pixel": 20,
                "fit_method": "moment",
                "laser_rep_rate_mhz": 80.0,
                "click_trigger": 0.5,
                "start_trigger": 0.5,
                "line_trigger": 0.5,
                "trigger_levels": { "1": 0.5, "2": 0.5, "3": 0.5 },
                "enabled": true
            },
            "forAcquisition": true
        }
    }

**managerProperties**

.. list-table::
   :widths: 24 14 18 44
   :header-rows: 1

   * - Field
     - Type
     - Default
     - Meaning
   * - ``click_channel``
     - int
     - **required**
     - TimeTagger input channel receiving photon clicks.
   * - ``start_channel``
     - int
     - **required**
     - TimeTagger input channel receiving the TCSPC start (sync) signal.
   * - ``line_channel``
     - int
     - **required**
     - TimeTagger input channel receiving the per-line marker.
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
     - Histogram zero-time offset in picoseconds.
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
   * - ``click_trigger``
     - float
     - ``trigger_levels[str(click_channel)]`` or ``0.5``
     - Trigger threshold in volts for the photon-click channel.
   * - ``start_trigger``
     - float
     - ``trigger_levels[str(start_channel)]`` or ``0.5``
     - Trigger threshold in volts for the TCSPC start/sync channel.
   * - ``line_trigger``
     - float
     - ``trigger_levels[str(line_channel)]`` or ``0.5``
     - Trigger threshold in volts for the per-line marker channel.
   * - ``trigger_levels``
     - dict
     - ``{}``
     - Backward-compatible dict mapping channel-number-as-string to trigger threshold in volts.  Used only to seed the per-role ``click_trigger`` / ``start_trigger`` / ``line_trigger`` defaults.  Direct per-role fields take precedence.
   * - ``enabled``
     - bool
     - ``true``
     - If ``false`` the manager is constructed but ``initiateScan`` is a no-op.

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
``FLIMHistWidget`` decay view.

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

**FLIM histogram widget modes**

``FLIMHistWidget`` has two display modes selectable from the toolbar:

* ``Lifetime dist.`` — histogram of per-pixel fitted lifetimes (ns).
  The red marker is the mean of the displayed distribution.  Switching
  fit methods changes both the bars (because each pixel's τ is
  recomputed) and the marker.
* ``Decay`` — aggregated TCSPC photon-arrival histogram across all
  valid pixels.  The bars are independent of fit method (raw photon
  counts vs. arrival time); only the red global-τ marker moves when
  the method changes.  Useful for sanity-checking the fit itself.

**Low-level dependencies**

* ``nidaqManager`` — required.  The manager connects to its
  ``sigScanBuilt``, ``sigScanStarted`` and ``sigScanDone`` signals to
  align FLIM acquisition with the scan timeline.

**Vendor library**

The Swabian ``TimeTagger`` Python package is imported at module top
inside a ``try/except ImportError`` (``TimeTagger.Flim`` and
``TimeTagger.createTimeTagger`` are pulled in the same block).  If the
import fails ``_TIMETAGGER_AVAILABLE`` is set to ``False`` and the
manager logs an error and refuses to drive scans.  If the import
succeeds but ``createTimeTagger()`` raises at runtime, ``_isMock`` is
set to ``True`` and the manager runs in a degraded "no FLIM data"
mode.  There is no separate mock-camera class — this manager has no
vendor wrapper layer.

**Source**

`SwabianTimeTaggerManager.py <../../imswitch/imcontrol/model/managers/detectors/SwabianTimeTaggerManager.py>`_


ThorCamTSIManager
=================

Thorlabs Scientific Cameras (TSI SDK) — Zelux, Kiralux, Quantalux.

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

`ThorCamTSIManager.py <../../imswitch/imcontrol/model/managers/detectors/ThorCamTSIManager.py>`_


TISManager
==========

The Imaging Source (TIS) cameras driven via the bundled TIS interface
wrapper.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "TISCam": {
            "managerName": "TISManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "tis": {
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
     - Index of the camera in the enumerated TIS camera list (0-based).  Set to an invalid value (e.g. ``"mock"``) to force the mock fallback.
   * - ``tis``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.tiscamera.CameraTIS`` is
lazy-imported inside ``_getTISObj``.  On failure the manager substitutes
``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`TISManager.py <../../imswitch/imcontrol/model/managers/detectors/TISManager.py>`_
