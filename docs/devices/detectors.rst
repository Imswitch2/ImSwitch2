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

**Low-level dependencies**

* ``nidaqManager`` — required.  The manager connects to its
  ``sigScanBuilt`` and ``sigScanStarted`` signals to drive image
  acquisition during scans.

**Vendor library**

None directly imported by this module.  All NI-DAQ traffic flows
through ``nidaqManager``.  ``matplotlib.pyplot`` is imported at module
top for the debug-plot path.

**Source**

`APDManager.py <../../imswitch/imcontrol/model/managers/detectors/APDManager.py>`_


AVManager
=========

Allied Vision (Vimba) area-scan cameras.

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
     - Index of the camera in the enumerated AV camera list (0-based).  Set to an invalid value (e.g. ``"mock"``) to force the mock fallback.
   * - ``avcam``
     - dict
     - Dictionary of AV camera property name → value pairs applied via ``setPropertyValue`` at startup.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.avcamera.CameraAV`` is
lazy-imported inside ``_getAVObj``.  If construction fails the
manager substitutes ``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock`` for headless
operation.

**Source**

`AVManager.py <../../imswitch/imcontrol/model/managers/detectors/AVManager.py>`_


BaslerManager
=============

Basler (pylon SDK) industrial cameras.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "BaslerCam": {
            "managerName": "BaslerManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "basler": {
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
     - Index of the camera in the enumerated Basler camera list (0-based).  Set to an invalid value (e.g. ``"mock"``) to force the mock fallback.
   * - ``basler``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.baslercamera.CameraBasler`` is
lazy-imported inside ``_getBaslerObj``.  On failure the manager
substitutes ``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`BaslerManager.py <../../imswitch/imcontrol/model/managers/detectors/BaslerManager.py>`_


ESP32CamManager
===============

Network-attached ESP32-CAM module reached over HTTP.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "ESP32Cam": {
            "managerName": "ESP32CamManager",
            "managerProperties": {
                "cameraHost": "192.168.4.1",
                "cameraPort": 80,
                "esp32cam": {
                    "exposure": 100,
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
     - Hostname / IP address of the ESP32-CAM web server.
   * - ``cameraPort``
     - int
     - TCP port the camera is listening on.
   * - ``esp32cam``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup.

All three fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.esp32camera.CameraESP32Cam`` is
lazy-imported inside ``_getESP32CamObj``.  On failure the manager
substitutes ``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`ESP32CamManager.py <../../imswitch/imcontrol/model/managers/detectors/ESP32CamManager.py>`_


GXPIPYManager
=============

Daheng Imaging (GxIPY SDK) industrial cameras.

**Setup JSON**

.. code-block:: json

    "detectors": {
        "GXCam": {
            "managerName": "GXPIPYManager",
            "managerProperties": {
                "cameraListIndex": 0,
                "gxipycam": {
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
     - Index of the camera in the enumerated Daheng camera list (0-based).  Set to an invalid value (e.g. ``"mock"``) to force the mock fallback.
   * - ``gxipycam``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup.

Both fields are **required**; no defaults.

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.gxipycamera.CameraGXIPY`` is
lazy-imported inside ``_getGXObj``.  On failure the manager substitutes
``MockCameraTIS`` from ``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`GXPIPYManager.py <../../imswitch/imcontrol/model/managers/detectors/GXPIPYManager.py>`_


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


JetsonCamManager
================

NVIDIA Jetson on-board CSI camera (IMX219 / Pi-camera-style sensor).

**Setup JSON**

.. code-block:: json

    "detectors": {
        "JetsonCam": {
            "managerName": "JetsonCamManager",
            "managerProperties": {
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
   * - ``avcam``
     - dict
     - Dictionary of camera property name → value pairs applied via ``setPropertyValue`` at startup.

The single field is **required**; no defaults.  (The class docstring
also mentions ``cameraListIndex`` but the source never reads it — the
Jetson camera is opened without a list index.)

**Low-level dependencies**

None.

**Vendor library**

``imswitch.imcontrol.model.interfaces.jetsoncam.CameraJETSON`` is
lazy-imported inside ``_getJetsonObj``.  On failure the manager
substitutes ``MockCameraTIS`` from
``imswitch.imcontrol.model.interfaces.tiscamera_mock``.

**Source**

`JetsonCamManager.py <../../imswitch/imcontrol/model/managers/detectors/JetsonCamManager.py>`_


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
   * - ``offset_v``
     - float
     - ``0.0``
     - Voltage offset subtracted from each sample before being treated as signal.

**Low-level dependencies**

* ``nidaqManager`` — required.  The manager connects to its
  ``sigScanBuilt`` and ``sigScanStarted`` signals to drive image
  acquisition during scans.

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
                "n_bins": 64,
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
     - ``64``
     - Number of TCSPC histogram bins per pixel.
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
