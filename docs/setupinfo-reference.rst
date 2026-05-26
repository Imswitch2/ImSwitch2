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

* ``scanWidgetType``: Widget variant (``"Base"``, ``"PointScan"``, ``"MoNaLISA"``, etc.)
* ``scanDesigner``: Scan trajectory class (e.g., ``"GalvoScanDesigner"``)
* ``scanDesignerParams``: Parameters for the scan designer
* ``TTLCycleDesigner``: TTL signal generator class (e.g., ``"PointScanTTLCycleDesigner"``)
* ``TTLCycleDesignerParams``: Parameters for TTL cycle (e.g., ``{"ttlDeviceList": ["Laser405"]}``}
* ``sampleRate``: DAQ sample rate in Hz

**Example** (minimal):

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

The following sections are less commonly used but available for specific
hardware or workflows:

* ``slm`` / ``slms``: Spatial light modulator settings (:class:`~imswitch.imcontrol.model.SetupInfo.SLMInfo`, :class:`~imswitch.imcontrol.model.SetupInfo.SLMsInfo`)
* ``focusLock``: Focus lock configuration (:class:`~imswitch.imcontrol.model.SetupInfo.FocusLockInfo`)
* ``autofocus``: Autofocus configuration (:class:`~imswitch.imcontrol.model.SetupInfo.AutofocusInfo`)
* ``tiling``: Tiling scan settings (:class:`~imswitch.imcontrol.model.SetupInfo.TilingInfo`)
* ``microscopeStand``: Leica microscope stand integration (:class:`~imswitch.imcontrol.model.SetupInfo.MicroscopeStandInfo`)
* ``etSTED``: Event-triggered STED settings (:class:`~imswitch.imcontrol.model.SetupInfo.EtSTEDInfo`)
* ``pulseStreamer``: Swabian PulseStreamer settings (:class:`~imswitch.imcontrol.model.SetupInfo.PulseStreamerInfo`)
* ``teensyPulse``: Teensy/Arduino pulse generator settings (:class:`~imswitch.imcontrol.model.SetupInfo.TeensyPulseInfo`; see :doc:`how-to/wire-teensy` and :doc:`how-to/add-pulse-generator-backend`)
* ``pyroServerInfo``: Pyro remote control server settings
* ``rois``: Additional detector ROI presets (:class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.ROIInfo`)
* ``laserPresets``: Laser power/modulation presets (:class:`~imswitch.imcontrol.view.guitools.ViewSetupInfo.LaserPresetInfo`)
* ``defaultLaserPresetForScan``: Auto-load laser preset when scanning


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
