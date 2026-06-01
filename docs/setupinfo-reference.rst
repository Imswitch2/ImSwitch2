*****************************
SetupInfo Configuration Guide
*****************************

This page is a **quick reference** for the top-level sections and common
fields in ImSwitch2 hardware configuration JSON files.  For comprehensive
per-manager documentation (``managerName`` + ``managerProperties``), see
the device-specific pages: :doc:`devices/detectors`, :doc:`devices/lasers`,
:doc:`devices/positioners`, :doc:`devices/rotators`.

For a gentle introduction to the config system, see
:doc:`imcontrol-setups`.


Configuration file location
===========================

Setup files live in the ``imcontrol_setups`` directory inside your user
config directory:

* **Windows**: ``%USERPROFILE%\Documents\ImSwitchConfig\imcontrol_setups\``
* **macOS / Linux**: ``~/ImSwitchConfig/imcontrol_setups/``

The directory is created automatically on first launch and pre-populated
with example configurations.


Configuration structure overview
================================

Hardware configs are JSON files deserialized into a
:class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.ViewSetupInfo`
Python object.  Each device category (detectors, lasers, positioners, etc.)
is a JSON object mapping **unique device names** to device-info objects.

Example skeleton:

.. code-block:: json

   {
       "detectors": {
           "Camera": { /* DetectorInfo fields */ }
       },
       "lasers": {
           "Laser405": { /* LaserInfo fields */ },
           "Laser488": { /* LaserInfo fields */ }
       },
       "positioners": {
           "Stage": { /* PositionerInfo fields */ }
       },
       "scan": { /* ScanInfo fields */ },
       "focusLock": { /* FocusLockInfo fields */ },
       "tiling": { /* TilingInfo fields */ },
       "etSTED": { /* EtSTEDInfo fields */ },
       "nidaq": { /* NidaqInfo fields */ },
       "availableWidgets": [
           "Settings", "View", "Recording", "Image", "Laser"
       ]
   }

Every config **must** include an ``availableWidgets`` field (can be a list,
``true`` for all widgets, or ``false`` for none).  All other sections are
optional and depend on what hardware you have.


Top-level sections
==================

detectors
---------

Map of detector (camera) names to :class:`~imswitch.imcontrol.model.SetupInfo.DetectorInfo` objects.

Each detector requires:

* ``managerName``: Manager class (e.g., ``"HamamatsuManager"``, ``"AVManager"``)
* ``managerProperties``: Manager-specific parameters (see :doc:`devices/detectors`)
* ``analogChannel`` / ``digitalLine``: NI-DAQ channels if applicable (``null`` otherwise)
* ``forAcquisition``: ``true`` if used for live imaging
* ``forFocusLock``: ``true`` if used for focus lock

**Example** (mock camera):

.. code-block:: json

   "detectors": {
       "Camera": {
           "analogChannel": null,
           "digitalLine": null,
           "managerName": "AVManager",
           "managerProperties": {
               "cameraListIndex": "mock",
               "avcam": {
                   "exposure": 100,
                   "gain": 1
               }
           },
           "forAcquisition": true,
           "forFocusLock": false
       }
   }

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.DetectorInfo`,
:doc:`devices/detectors`


lasers
------

Map of laser names to :class:`~imswitch.imcontrol.model.SetupInfo.LaserInfo` objects.

Each laser requires:

* ``managerName``: Manager class (e.g., ``"Cobolt0601LaserManager"``)
* ``managerProperties``: Manager-specific parameters (see :doc:`devices/lasers`)
* ``wavelength``: Wavelength in nanometers (e.g., ``405``)
* ``valueRangeMin`` / ``valueRangeMax``: Power/intensity range
* ``analogChannel`` / ``digitalLine``: NI-DAQ channels if applicable

**Example** (Cobolt laser on COM11):

.. code-block:: json

   "lasers": {
       "Cobolt405nm": {
           "analogChannel": null,
           "digitalLine": 3,
           "managerName": "Cobolt0601LaserManager",
           "managerProperties": {
               "digitalPorts": ["COM11"]
           },
           "valueRangeMin": 0,
           "valueRangeMax": 200,
           "wavelength": 405
       }
   }

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.LaserInfo`,
:doc:`devices/lasers`


positioners
-----------

Map of positioner (stage) names to :class:`~imswitch.imcontrol.model.SetupInfo.PositionerInfo` objects.

Each positioner requires:

* ``managerName``: Manager class (e.g., ``"PiezoconceptZManager"``)
* ``managerProperties``: Manager-specific parameters (see :doc:`devices/positioners`)
* ``axes``: List of axis names (e.g., ``["X", "Y"]`` or ``["Z"]``)
* ``forPositioning``: ``true`` if used for manual positioning
* ``forScanning``: ``true`` if used for scanning

**Example** (mock XY stage):

.. code-block:: json

   "positioners": {
       "Mock XY Stage": {
           "analogChannel": null,
           "digitalLine": null,
           "managerName": "MHXYStageManager",
           "managerProperties": {
               "rs232device": ""
           },
           "axes": ["X", "Y"],
           "forPositioning": true,
           "forScanning": false
       }
   }

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.PositionerInfo`,
:doc:`devices/positioners`


rotators
--------

Optional map of rotator mount names to ``DeviceInfo`` objects.  Required for
Standa motorized rotator mounts.

**Example**:

.. code-block:: json

   "rotators": {
       "Rotator1": {
           "analogChannel": null,
           "digitalLine": null,
           "managerName": "StandaRotatorManager",
           "managerProperties": {
               "serialPort": "/dev/ttyUSB0"
           }
       }
   }

**See also**: :doc:`devices/rotators`


scan
----

Optional :class:`~imswitch.imcontrol.model.SetupInfo.ScanInfo` object.
Required if you want to use the ``Scan`` widget.

Key fields:

* ``scanWidgetType``: Widget variant — one of ``"Base"``, ``"PointScan"``, ``"MoNaLISA"``, or ``"Advanced"``
* ``scanDesigner``: Scan trajectory class (e.g., ``"GalvoScanDesigner"``)
* ``scanDesignerParams``: Parameters for the scan designer
* ``TTLCycleDesigner``: TTL signal generator class (e.g., ``"PointScanTTLCycleDesigner"``, ``"AdvancedScanTTLCycleDesigner"``)
* ``TTLCycleDesignerParams``: Parameters for TTL cycle (e.g., ``{"ttlDeviceList": ["Laser405"]}``}
* ``sampleRate``: DAQ sample rate in Hz
* ``lineClockLine``: NI-DAQ port line for line clock output (integer or ``"Dev1/port0/line{N}"`` string; ``null`` if not used)
* ``frameStartClockLine`` / ``frameEndClockLine``: Frame clock outputs (same format as ``lineClockLine``)

**Example** (minimal Base scan):

.. code-block:: json

   "scan": {
       "scanWidgetType": "Base",
       "scanDesigner": "GalvoScanDesigner",
       "scanDesignerParams": {},
       "TTLCycleDesigner": "PointScanTTLCycleDesigner",
       "TTLCycleDesignerParams": {
           "ttlDeviceList": ["Laser405"]
       },
       "sampleRate": 100000,
       "maxScanTimeMin": null,
       "lineClockLine": null,
       "frameStartClockLine": null,
       "frameEndClockLine": null
   }

**Example** (Advanced scan with line clock):

.. code-block:: json

   "scan": {
       "scanWidgetType": "Advanced",
       "scanDesigner": "GalvoScanDesigner",
       "scanDesignerParams": {},
       "TTLCycleDesigner": "AdvancedScanTTLCycleDesigner",
       "TTLCycleDesignerParams": {},
       "sampleRate": 100000,
       "lineClockLine": "Dev1/port0/line6",
       "frameStartClockLine": null,
       "frameEndClockLine": null
   }

**Required devices**: At least one positioner with ``forScanning: true``.  For
galvo-based scans, the positioners' ``analogChannel`` fields must reference
valid NI-DAQ analog output channels.  Lasers referenced in
``TTLCycleDesignerParams.ttlDeviceList`` must have ``digitalLine`` set.

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.ScanInfo`,
:ref:`Signal designers <Signal designers>`


nidaq
-----

Optional :class:`~imswitch.imcontrol.model.SetupInfo.NidaqInfo` object.
Controls NI-DAQ card behavior.

Key fields:

* ``timerCounterChannel``: Counter channel for timing (e.g., ``0`` → ``"Dev1/ctr0"``)
* ``startTrigger``: Enable start triggering for synchronization (``true`` / ``false``)
* ``simulation``: Allow NI-DAQ commands without physical hardware (``true`` / ``false``)

**Example** (simulation mode, no physical DAQ):

.. code-block:: json

   "nidaq": {
       "timerCounterChannel": null,
       "startTrigger": false,
       "simulation": true
   }

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.NidaqInfo`


rs232devices
------------

Optional map of RS232 connection names to :class:`~imswitch.imcontrol.model.SetupInfo.RS232Info` objects.

Some detector/laser/positioner managers require a corresponding RS232
connection to be referenced in their ``managerProperties``.

**Example**:

.. code-block:: json

   "rs232devices": {
       "StageConnection": {
           "managerName": "RS232Manager",
           "managerProperties": {
               "port": "COM3",
               "baudrate": 9600
           }
       }
   }


availableWidgets
----------------

**Required field.**  List of widget names to load, ``true`` for all, or
``false`` for none.

Available widget names (case-sensitive):

* Core: ``Settings``, ``View``, ``Recording``, ``Image``
* Hardware control: ``Laser``, ``Positioner``, ``Rotator``, ``RotationScan``
* Advanced: ``Scan``, ``BeadRec``, ``FocusLock``, ``Autofocus``, ``SLM``, ``Tiling``
* Event-triggered: ``EtSTED``
* Analysis tools: ``AlignAverage``, ``AlignXY``, ``AlignmentLine``, ``uLenses``, ``FFT``, ``FLIMHist``, ``BFTimelapse``
* Scripting: ``Console``, ``ViewerTools``, ``LineProfile``
* Stand integration: ``MotCorr`` (Leica motorized correction collar)

**Example**:

.. code-block:: json

   "availableWidgets": [
       "Settings",
       "View",
       "Recording",
       "Image",
       "Laser",
       "Positioner"
   ]

**See also**: :attr:`ViewSetupInfo.availableWidgets <imswitch.imcontrol.view.guitools.ViewSetupInfo.ViewSetupInfo.availableWidgets>`


widgetLayout
------------

Optional :class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.WidgetLayoutInfo` object.

Overrides the default dock layout.  Each inner list is a *tab group*
(widgets share one dock row and appear as tabs).  Successive inner lists
are stacked vertically.

**Example**:

.. code-block:: json

   "widgetLayout": {
       "right": [
           ["Positioner", "Laser"],
           ["Scan"],
           ["Tiling", "BeadRec"]
       ],
       "left": [
           ["Settings"],
           ["View"],
           ["Recording"]
       ]
   }

**See also**: :class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.WidgetLayoutInfo`


Additional sections
===================

The following sections support specialized hardware or workflows.  Each is
documented below with full field lists and examples:

* `focusLock`_
* `autofocus`_
* `tiling`_
* `etSTED`_
* `microscopeStand`_
* `slms`_ (modern multi-SLM support)
* `teensyPulse`_
* `pyroServerInfo`_
* `rois`_
* `laserPresets and defaultLaserPresetForScan`_

**Legacy / deprecated sections**:

* `slm (singular)`_ — superseded by ``slms``; only used by old ``SLMController``
* `pulseStreamer`_ — dormant; ``PulseStreamerManager`` no longer constructed at runtime


focusLock
---------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.FocusLockInfo` object.
**Required if you use the FocusLock widget** or want focus-lock functionality.

Key fields:

* ``camera`` (str): Detector name (must match a detector with ``forFocusLock: true``)
* ``positioner`` (str): Positioner name (typically a Z-axis piezo)
* ``updateFreq`` (int): Update frequency in milliseconds
* ``frameCropx`` / ``frameCropy`` (int): Starting X/Y position of camera frame crop in pixels
* ``frameCropw`` / ``frameCroph`` (int): Width/height of camera frame crop in pixels
* ``swapImageAxes`` (bool): Swap camera image axes when grabbing frame
* ``piKp`` (float): Default kp (proportional gain) of feedback loop
* ``piKi`` (float): Default ki (integral gain) of feedback loop

**Example**:

.. code-block:: json

   "focusLock": {
       "camera": "FocusLockCamera",
       "positioner": "PiezoZ",
       "updateFreq": 10,
       "frameCropx": 544,
       "frameCropy": 462,
       "frameCropw": 600,
       "frameCroph": 210,
       "swapImageAxes": true,
       "piKp": 25.0,
       "piKi": 0.1
   }

**Required devices**: One detector with ``forFocusLock: true``, one positioner
(typically Z-axis).

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.FocusLockInfo`


autofocus
---------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.AutofocusInfo` object.
**Required if you use the Autofocus widget** or want autofocus functionality.

Key fields:

* ``camera`` (str): Detector name
* ``positioner`` (str): Positioner name (typically Z-axis)
* ``updateFreq`` (int): Update frequency in milliseconds
* ``frameCropx`` / ``frameCropy`` (int): Starting X/Y position of frame crop in pixels
* ``frameCropw`` / ``frameCroph`` (int): Width/height of frame crop in pixels

**Example**:

.. code-block:: json

   "autofocus": {
       "camera": "Camera",
       "positioner": "PiezoZ",
       "updateFreq": 100,
       "frameCropx": 0,
       "frameCropy": 0,
       "frameCropw": 512,
       "frameCroph": 512
   }

**Required devices**: One detector, one positioner (typically Z-axis).

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.AutofocusInfo`


tiling
------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.TilingInfo` object.
**Required if you use the Tiling widget** for spiral tiling scans.

Key fields:

* ``xyPositioner`` (str): Name of the XY positioner (must match a positioner in the setup)
* ``zPositioner`` (str, optional): Name of the Z positioner for per-tile autofocus (empty string = disabled)
* ``camera`` (str, optional): Detector to use for tile acquisition (empty string = first ``forAcquisition`` detector)
* ``defaultTileStepUm`` (float): Default stage step between tile centres in micrometers

**Example**:

.. code-block:: json

   "tiling": {
       "xyPositioner": "StageXY",
       "zPositioner": "",
       "camera": "Camera",
       "defaultTileStepUm": 100.0
   }

**Required devices**: One XY positioner.  Optional Z positioner and camera.

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.TilingInfo`


etSTED
------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.EtSTEDInfo` object.
**Required if you use the EtSTED widget** for event-triggered STED microscopy.

Key fields:

* ``swapXY`` (bool): Swap X and Y axes before transforming coordinates of a detected event (default ``false``)
* ``invertX`` (bool): Invert X value before transforming coordinates (default ``false``)
* ``invertY`` (bool): Invert Y value before transforming coordinates (default ``false``)

**Example**:

.. code-block:: json

   "etSTED": {
       "swapXY": true,
       "invertX": true,
       "invertY": true
   }

**Required devices**: Typically paired with a scanning setup (see ``scan``
section) and a STED-capable laser.

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.EtSTEDInfo`


microscopeStand
---------------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.MicroscopeStandInfo` object.
**Required if you use the MotCorr widget** for Leica motorized correction collar
or other microscope stand integration.

Key fields:

* ``managerName`` (str): Manager class (e.g., ``"LeicaDMIManager"``)
* ``rs232device`` (str): Name of the RS232 device to use (must match an entry in ``rs232devices``)
* ``managerProperties`` (dict, optional): Manager-specific settings (e.g., available cube positions)

**Example**:

.. code-block:: json

   "microscopeStand": {
       "managerName": "LeicaDMIManager",
       "rs232device": "LeicaStand",
       "managerProperties": {
           "availableCubes": ["BF", "GFP", "RFP"]
       }
   }

**Required devices**: One RS232 connection.

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.MicroscopeStandInfo`


slms
----

Map of SLM names to :class:`~imswitch.imcontrol.model.SetupInfo.SLMsInfo` objects.
**This is the modern plural form**; use this for new setups.  Required if you use
the ``SLMs`` widget.

Key fields (per SLM):

* ``managerName`` (str): Manager class (e.g., ``"HamamatsuSLMdviManager"``)
* ``managerProperties`` (dict): Manager-specific settings
* ``monitorIdx`` (int): Monitor index in system list (starts at 0)
* ``serial_number`` (str): Unique serial number of the SLM head
* ``width`` / ``height`` (int): SLM dimensions in pixels
* ``wavelength`` (int): Wavelength of the laser line used with the SLM, in nm
* ``pixelSize`` (float): Pixel pitch in millimeters
* ``nSections`` (int, optional): Number of sections the SLM is divided into (e.g., 2 for double-pass)
* ``widgetOptions`` (dict, optional): Widget options (e.g., patterns to display)
* ``correctionPatternsDir`` (str): Directory of .bmp images for flatness correction
* ``wavelengthTableFile`` (str): JSON file with wavelength correction table

**Example** (Hamamatsu SLM):

.. code-block:: json

   "slms": {
       "SLM STED": {
           "managerName": "HamamatsuSLMdviManager",
           "managerProperties": {
               "mockermode": false,
               "startConfig": "C:\\\\Users\\\\user\\\\Documents\\\\ImSwitchConfig\\\\imcontrol_slm\\\\configs\\\\LSH0701153/2D_STED_Default.h5"
           },
           "monitorIdx": 2,
           "serial_number": "LSH0701153",
           "width": 792,
           "height": 600,
           "wavelength": 775,
           "pixelSize": 20,
           "nSections": 2,
           "widgetOptions": {
               "patterns": ["vortex", "top_hat", "half_moon_x", "half_moon_y", "linear_phase"],
               "cgh": false,
               "aberrations": true
           },
           "correctionPatternsDir": "C:\\\\Users\\\\user\\\\Documents\\\\ImSwitchConfig\\\\imcontrol_slm\\\\slm_defcorrpattern\\\\",
           "wavelengthTableFile": "none"
       }
   }

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.SLMsInfo`


teensyPulse
-----------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.TeensyPulseInfo` object.
**Required if you want to use a Teensy/Arduino pulse generator** (ImSwitch v4
firmware with v3 fallback).  See :doc:`how-to/wire-teensy` for wiring guide and
:doc:`how-to/add-pulse-generator-backend` for backend details.

Key fields:

* ``port`` (str or null): Serial port (e.g., ``"COM7"`` or ``"/dev/ttyACM0"``; ``null`` = pulse generator disabled)
* ``baud`` (int): Baud rate (default 115200)
* ``pinMap`` (dict, optional): Name-to-channel mapping for convenience (e.g., ``{"laser488": 3}``)
* ``useMockOnFailure`` (bool): Fall back to in-process mock if port can't be opened (default ``true``)
* ``mockNChannels`` / ``mockMinPulseUs`` / ``mockMaxSteps`` (int): Mock capabilities when real device unavailable

**Example**:

.. code-block:: json

   "teensyPulse": {
       "port": "COM7",
       "baud": 115200,
       "pinMap": {
           "laser405": 1,
           "laser488": 2,
           "camera_trigger": 3
       },
       "useMockOnFailure": true
   }

**Required devices**: None (standalone serial device).

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.TeensyPulseInfo`,
:doc:`how-to/wire-teensy`


pyroServerInfo
--------------

Optional :class:`~imswitch.imcontrol.model.SetupInfo.PyroServerInfo` object.
Controls the Pyro remote control server for scripting access.

Key fields:

* ``name`` (str): Server name (default ``"ImSwitchServer"``)
* ``host`` (str): Host address (default ``"127.0.0.1"``)
* ``port`` (int): Port number (default ``54333``)
* ``active`` (bool): Enable server at startup (default ``false``)

**Example**:

.. code-block:: json

   "pyroServerInfo": {
       "name": "ImSwitchServer",
       "host": "127.0.0.1",
       "port": 54333,
       "active": false
   }

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.PyroServerInfo`


rois
----

Optional map of ROI preset names to :class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.ROIInfo` objects.
Additional detector ROI presets available in the detector settings widget.

Key fields (per ROI):

* ``x`` (int): Starting X position in pixels
* ``y`` (int): Starting Y position in pixels
* ``w`` (int): Width in pixels
* ``h`` (int): Height in pixels

**Example**:

.. code-block:: json

   "rois": {
       "Full chip": {
           "x": 0,
           "y": 0,
           "w": 2448,
           "h": 2048
       },
       "Center 200x200": {
           "x": 1124,
           "y": 924,
           "w": 200,
           "h": 200
       }
   }

**See also**: :class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.ROIInfo`


laserPresets and defaultLaserPresetForScan
------------------------------------------

Optional laser power/modulation preset system.

* ``laserPresets`` (dict): Map of preset names to ``{laserName: LaserPresetInfo}`` nested dicts
* ``defaultLaserPresetForScan`` (str or null): Name of preset to auto-load when scanning starts

**LaserPresetInfo fields** (per laser, per preset):

* ``value`` (float): Laser power or intensity value

**Example**:

.. code-block:: json

   "laserPresets": {
       "HighPower": {
           "Laser405": {
               "value": 200.0
           },
           "Laser488": {
               "value": 150.0
           }
       },
       "LowPower": {
           "Laser405": {
               "value": 50.0
           },
           "Laser488": {
               "value": 30.0
           }
       }
   },
   "defaultLaserPresetForScan": "HighPower"

**See also**: :class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.LaserPresetInfo`


slm (singular)
--------------

.. deprecated:: ImSwitch2
   Use ``slms`` (plural) for new setups.  The ``slm`` field is only read by
   the legacy ``SLMController`` and is superseded by the multi-SLM ``slms``
   map.

Optional :class:`~imswitch.imcontrol.model.SetupInfo.SLMInfo` object.  Prefer
``slms`` for all new configurations.

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.SLMInfo`, `slms`_


pulseStreamer
-------------

.. deprecated:: ImSwitch2
   **Legacy / not constructed at runtime.**  ``MasterController`` no longer
   constructs ``PulseStreamerManager`` (instantiation is commented out at
   ``imswitch/imcontrol/controller/MasterController.py:23``).  The active
   pulse-generator path is ``teensyPulse`` → ``TeensyPulseManager``.  This
   field remains for backward compatibility with old configs but has no
   runtime effect.

Optional :class:`~imswitch.imcontrol.model.SetupInfo.PulseStreamerInfo` object.

Key fields:

* ``ipAddress`` (str or null): IP address of PulseStreamer hardware

**See also**: :class:`~imswitch.imcontrol.model.SetupInfo.PulseStreamerInfo`,
`teensyPulse`_


Cross-references between sections and devices
=============================================

This table explains which sections depend on which device flags or device
references, and what fails silently if references are missing or incorrect.

.. list-table::
   :header-rows: 1
   :widths: 20 30 30 20

   * - Section
     - Device flags / managers required
     - Device fields referenced by name
     - Failure mode if missing/wrong
   * - ``focusLock``
     - One detector with ``forFocusLock: true``
     - ``camera``, ``positioner``
     - Widget fails to initialize
   * - ``autofocus``
     - One detector, one positioner
     - ``camera``, ``positioner``
     - Widget fails to initialize
   * - ``tiling``
     - One XY positioner
     - ``xyPositioner``, optional ``zPositioner``, ``camera``
     - Widget fails if XY positioner missing; silently skips optional fields
   * - ``scan``
     - Positioners with ``forScanning: true``; lasers with ``digitalLine`` set (for TTL targets)
     - Lasers in ``TTLCycleDesignerParams.ttlDeviceList``; ``lineClockLine`` references NI-DAQ port
     - Widget fails if no forScanning positioners; TTL signals silently omitted if laser references invalid
   * - ``etSTED``
     - Paired with ``scan`` + STED laser
     - None (coordinate transform only)
     - No immediate failure; incorrect transforms applied
   * - ``microscopeStand``
     - One RS232 connection
     - ``rs232device``
     - Widget fails if RS232 device missing
   * - ``teensyPulse``
     - None (standalone serial device)
     - None
     - Falls back to mock if ``useMockOnFailure: true``; hard failure otherwise
   * - ``slms``
     - None (manager constructs per device)
     - None (each SLM is independent)
     - Widget fails if manager instantiation fails
   * - ``nidaq``
     - NI-DAQ hardware (or ``simulation: true``)
     - ``timerCounterChannel`` references counter channel
     - Silent failure if simulation disabled + no hardware
   * - ``laserPresets``
     - Lasers in setup
     - Laser names in preset map
     - Preset silently skips unknown lasers


Complete examples
=================

See the bundled example configurations in
``~/ImSwitchConfig/imcontrol_setups/`` (created on first launch):

* ``example_no_hardware.json`` — mock devices, no physical hardware
* ``example_mock.json`` — minimal mock camera + positioner
* ``example_monalisa.json`` — full MoNaLISA point-scanning microscope
* ``example_sted.json`` — STED microscope with galvo scanning
* ``example_coolLED.json`` — CoolLED illumination system
* ``example_kiralux_teensy.json`` — Thorlabs Kiralux camera + Teensy pulse generator

Or use the **visual config editor**:

.. code-block:: bash

   python utility_scripts/imswitch_config_editor.py

It includes built-in templates for every supported manager.


API reference
=============

Full class hierarchy:

.. autoclassconheader:: imswitch.imcontrol.view.guitools.ViewSetupInfo.ViewSetupInfo
   :members:
   :inherited-members:
   :member-order: bysource

.. autoclassconheader:: imswitch.imcontrol.model.SetupInfo.SetupInfo
   :members:
   :inherited-members:
   :member-order: bysource
