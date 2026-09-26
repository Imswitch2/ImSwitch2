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
:class:`ViewSetupInfo`
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

Every section is optional and depends on what hardware you have.  A
microscope setup lists its panels in ``availableWidgets`` (a list, ``true``
for all widgets, or ``false`` for none); left out, it defaults to an empty
list and ImControl loads no panels, which is what the processing-only
ImProcess presets rely on.


Top-level sections
==================

detectors
---------

Map of detector (camera) names to :class:`DetectorInfo` objects.

Each detector takes:

* ``managerName`` (required): Manager class (e.g., ``"HamamatsuManager"``, ``"AVManager"``)
* ``managerProperties``: Manager-specific parameters (see :doc:`devices/detectors`); empty when omitted
* ``analogChannel`` / ``digitalLine``: NI-DAQ channels if applicable (default ``null``)
* ``forAcquisition``: ``true`` if used for live imaging (default ``false``)
* ``forFocusLock``: ``true`` if used for focus lock (default ``false``)

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

**See also**: :class:`DetectorInfo`,
:doc:`devices/detectors`


lasers
------

Map of laser names to :class:`LaserInfo` objects.

Each laser requires (these have no default):

* ``managerName``: Manager class (e.g., ``"Cobolt0601LaserManager"``)
* ``wavelength``: Wavelength in nanometers (e.g., ``405``)
* ``valueRangeMin`` / ``valueRangeMax``: Power/intensity range

Optional:

* ``managerProperties``: Manager-specific parameters (see :doc:`devices/lasers`); empty when omitted
* ``analogChannel`` / ``digitalLine``: NI-DAQ channels if applicable (default ``null``)
* ``valueRangeStep`` (default ``1.0``): step of the power control in the
  **Laser** panel
* ``freqRangeMin`` / ``freqRangeMax`` / ``freqRangeInit`` (default ``0``):
  frequency-modulation range and start value; read only by managers whose
  laser supports modulation
* ``powerDevice``: name of another laser entry that sets this one's emission
  power, when gate and power are separate hardware (see below)

**Split gate and power devices**

Some beam paths spread one physical laser line across two entries: one owns
the digital line a scan gates (often a bare placeholder with no power of its
own), while an attenuator such as an AOTF channel sets the power over a serial
link.  Naming the partner in ``powerDevice`` lets a scan switch the power
device on for the duration of the scan, instead of the laser emitting only
when the user happens to have enabled it by hand:

.. code-block:: json

   "lasers": {
       "561": {
           "analogChannel": null,
           "digitalLine": "Dev1/port0/line1",
           "managerName": "NidaqLaserManager",
           "managerProperties": {},
           "valueRangeMin": 0,
           "valueRangeMax": 1,
           "wavelength": 561,
           "powerDevice": "561AOTF"
       },
       "561AOTF": {
           "analogChannel": null,
           "digitalLine": null,
           "managerName": "AAAOTFLaserManager",
           "managerProperties": {"rs232device": "aaaotf", "channel": 1},
           "valueRangeMin": 0,
           "valueRangeMax": 1023,
           "wavelength": 561
       }
   }

(The gate/AOTF pairing above is the layout shipped in
``example_sted.json``; ``powerDevice`` is what links the two.)

Leave it ``null`` (the default) when one entry owns both gate and power — an
AOM with its own analog channel and digital line, for instance.

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

**See also**: :class:`LaserInfo`,
:doc:`devices/lasers`


positioners
-----------

Map of positioner (stage) names to :class:`PositionerInfo` objects.

Each positioner requires:

* ``managerName``: Manager class (e.g., ``"PiezoconceptZManager"``)
* ``axes``: List of axis names (e.g., ``["X", "Y"]`` or ``["Z"]``)
* ``forPositioning`` / ``forScanning`` (default ``false``): ``true`` if used
  for manual positioning / for scanning.  At least one of the two must be
  ``true``, or ImControl fails to start.

Optional:

* ``managerProperties``: Manager-specific parameters (see :doc:`devices/positioners`); empty when omitted
* ``analogChannel`` / ``digitalLine``: NI-DAQ channels if applicable (default ``null``)
* ``resetOnClose`` (default ``false``): set it to ``true`` to have this
  positioner moved to ``0`` on every axis when ImSwitch2 closes with the
  **Positioner** panel loaded.  Otherwise a stage stays where it is on exit.
* ``isPositiveDirection`` (default ``true``): the positioner's direction
  sign.  Scans record it in their metadata so the data can be oriented.
* ``joystick`` (default ``false``): the positioner has a joystick; the
  **Positioner** panel shows a joystick checkbox for it.
* ``liveUpdate`` (default ``false``): the **Positioner** panel polls the
  hardware position continuously.
* ``hide`` (default ``false``): leave this positioner out of the
  **Positioner** panel.
* ``shortcutModifier`` (optional): default keyboard-jog group for this
  positioner — ``"ctrl"`` (``Ctrl+`` arrows / ``Ctrl+Y`` / ``Ctrl+A``) or
  ``"ctrl-shift"`` (``Ctrl+Shift+`` equivalents). Omit to claim the ``Ctrl+`` set
  on a first-come basis per axis. This only sets the *defaults*; individual jog
  actions (``positioner.<name>.<axis>.plus`` / ``.minus``) can be rebound to any
  key via the ``shortcuts`` section below.
* ``physicalActuator`` (optional str): identity of the physical device this
  positioner drives. Two positioners with the same non-null value are declared
  to be *one piece of hardware reached two ways* — the STED example addresses a
  single piezo as the analog ``ND-PiezoZ`` and as the serial ``PiezoZ``. The
  focus lock uses this to decide whether a scan conflicts with it; see
  :ref:`focuslock-scan-arbitration`. The identifier is arbitrary, only equality
  between positioners matters, and giving two same-axis positioners *different*
  ids is how you declare that they really are independent stages.

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

**See also**: :class:`PositionerInfo`,
:doc:`devices/positioners`


rotators
--------

Optional map of rotator mount names to :class:`DeviceInfo` objects, for
motorized rotation mounts (Standa, Thorlabs Kinesis and Thorlabs Elliptec
managers).  Required by the ``Rotator`` and ``RotationScan`` panels.

**Example** (a Standa mount, the ``HWP`` entry of ``example_sted.json``):

.. code-block:: json

   "rotators": {
       "HWP": {
           "managerName": "StandaRotatorManager",
           "managerProperties": {
               "motorListIndex": 0,
               "ximcLibLocation": "T:\\RedSTED\\Standa\\Standa\\ximc-2.13.6\\ximc",
               "stepsPerTurn": 200,
               "microstepsPerStep": 255
           }
       }
   }

All four ``StandaRotatorManager`` properties are required.

**See also**: :doc:`devices/rotators`


shortcuts
---------

Optional map overriding keyboard-shortcut bindings by **action ID**. Every
shortcut-able action has a stable ID (e.g. ``recording.toggleRecord``,
``view.toggleLiveView``, ``settings.nextDetector``, ``app.saveWidgetStates``,
``positioner.<name>.<axis>.plus``). Without this section, code defaults apply
(see :doc:`working-in-imswitch2`).

Each entry maps an action ID to:

* a **string** — a single key sequence (e.g. ``"Ctrl+Shift+R"``),
* a **list of strings** — multiple sequences that all trigger the action, or
* ``null`` — explicitly **disable** the action's binding.

Effective binding order is ``code defaults < setup config shortcuts``. Unknown
action IDs and invalid key sequences are logged and ignored; if two actions
resolve to the same key, the conflict is reported and the lower-priority one is
disabled (Qt's native "ambiguous shortcut" behaviour is never triggered). Bindings
can also be edited interactively via *Shortcuts → Configure Shortcuts…*, which
writes back to this section.

Mode-switch shortcuts are stored per-mode (in the Setup Modes data), **not** here.

**Example**:

.. code-block:: json

   "shortcuts": {
       "recording.toggleRecord": "Ctrl+Shift+R",
       "view.toggleLiveView": ["Ctrl+L", "F5"],
       "positioner.MotorizedStage.X.plus": "Alt+Right",
       "image.updateLevels": null
   }


.. _setupinfo-scan:

scan
----

Optional :class:`ScanInfo` object.
Required if you want to use the ``Scan`` widget.

Key fields:

* ``scanWidgetType``: Widget variant — one of ``"Base"``, ``"PointScan"``,
  ``"MoNaLISA"``, ``"Advanced"`` or ``"TriggerScope"``.  ``"TriggerScope"``
  runs scans on the TriggerScope board (see `triggerScope`_) from the
  ``TriggerScope*`` panels; there is no ``Scan`` panel for it.
* ``scanDesigner``: Scan trajectory class (e.g., ``"GalvoScanDesigner"``)
* ``scanDesignerParams``: Parameters for the scan designer.
  ``BetaScanDesigner`` reads ``return_time`` (seconds between lines) and
  ``move_time`` / ``settle_time`` (the fast-axis ramp and rest inside each
  pixel dwell, 2 ms each by default); a dwell not longer than
  ``move_time + settle_time`` is refused.
* ``TTLCycleDesigner``: TTL signal generator class (e.g., ``"PointScanTTLCycleDesigner"``, ``"AdvancedScanTTLCycleDesigner"``)
* ``TTLCycleDesignerParams``: Parameters merged into the TTL designer's
  input; the shipped setups leave it ``{}``
* ``sampleRate``: DAQ sample rate in Hz
* ``maxScanTimeMin``: Optional scan-duration guard in minutes; ``null``,
  ``0`` or omission disables it.  ``GalvoScanDesigner`` also refuses scans
  above 10 million spatial positions.
* ``lineClockLine``: NI-DAQ port line for line clock output (integer or ``"Dev1/port0/line{N}"`` string; ``null`` if not used)
* ``frameStartClockLine`` / ``frameEndClockLine``: Frame clock outputs (same format as ``lineClockLine``)

The widget type and the two designers have to match, because each panel
hands its designers a fixed set of parameters.  The shipped setups use:

* ``"Base"`` and ``"MoNaLISA"``: ``BetaScanDesigner`` + ``BetaTTLCycleDesigner``
* ``"PointScan"``: ``GalvoScanDesigner`` + ``PointScanTTLCycleDesigner``
  (``example_sted.json``)
* ``"Advanced"``: ``GalvoScanDesigner`` + ``AdvancedScanTTLCycleDesigner``
  (``galvo_apd_mock_scan_setup.json``)

**Example** (Base scan, from ``example_no_hardware.json``):

.. code-block:: json

   "scan": {
       "scanWidgetType": "Base",
       "scanDesigner": "BetaScanDesigner",
       "scanDesignerParams": {"return_time": 0.01},
       "TTLCycleDesigner": "BetaTTLCycleDesigner",
       "TTLCycleDesignerParams": {},
       "sampleRate": 100000,
       "maxScanTimeMin": 1,
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
valid NI-DAQ analog output channels. Positioners that are *swept smoothly*
by ``GalvoScanDesigner`` must also define realistic ``vel_max`` (µm/µs) and
``acc_max`` (µm/µs²) values in ``managerProperties``; optional
``jerk_max`` is expressed in µm/µs³. Missing velocity or acceleration
limits stop signal generation with a configuration error rather than producing
an unsafe or degenerate trajectory.

Whether a positioner is swept smoothly or *stepped* is the boolean
``managerProperties.smoothScan``. Smooth (the default for real devices) means
the galvo-like profile on the fast axis: a continuous constant-velocity sweep
with spline turnarounds. Stepped means the device is held at each position
for the dwell time — the right profile for a piezo or stage, which cannot
follow a galvo flyback; stepped devices need no ``vel_max``/``acc_max``. Set
``"smoothScan": false`` on piezo/stage axes (``example_sted.json`` does so
for ``ND-PiezoZ``), which also makes single-axis scans over that device —
e.g. a Z-only axial profile — run as a step-and-dwell staircase. When the
key is absent, devices with ``mock`` in their name are stepped and everything
else is assumed a sweepable galvo.

A scanned positioner's ``minVolt`` / ``maxVolt`` (in ``managerProperties``)
bound its waveform: a scan whose signal would leave that range is refused
before it starts, including TriggerScope firmware scans.  A positioner with
``"returnToCenterAfterScan": true`` is parked at its scan centre when the scan
ends, whether it completed, failed or was aborted, on the axis named by
``returnToCenterAfterScanAxis`` (default: its first axis).

The TTL rows of the scan panel are built from every laser and detector that
has a ``digitalLine``; a device without one gets no TTL row.

**See also**: :class:`ScanInfo`,
:ref:`Signal designers <signal-designers>`, :doc:`advanced-scanning`


nidaq
-----

Optional :class:`NidaqInfo` object.
Controls NI-DAQ card behavior.

Key fields:

* ``timerCounterChannel``: Counter that generates the 1 MHz pulse train the point detectors (APD, PMT) sample on (e.g. ``"Dev1/ctr2"``; an integer ``N`` means ``"Dev1/ctr{N}"``). Choose a counter no detector's ``ctrInputLine`` uses. Required on any rig with a point detector: without it the detectors have no sample clock and a scan is refused at start with a message naming this setting.
* ``startTrigger``: Enable start triggering for synchronization (``true`` / ``false``)
* ``simulation``: Allow NI-DAQ commands without physical hardware (``true`` / ``false``)

**Example** (simulation mode, no physical DAQ):

.. code-block:: json

   "nidaq": {
       "timerCounterChannel": null,
       "startTrigger": false,
       "simulation": true
   }

**See also**: :class:`NidaqInfo`


rs232devices
------------

Optional map of RS232 connection names to :class:`RS232Info` objects.

Some detector/laser/positioner managers require a corresponding RS232
connection to be referenced in their ``managerProperties``.

``RS232Manager`` reads eight properties, and all of them are needed:
``port``, ``baudrate``, ``bytesize``, ``parity``, ``stopbits``,
``encoding``, ``send_termination`` and ``recv_termination``.  ``port`` takes
a plain name (``"COM3"``, ``"/dev/ttyUSB0"``) or a VISA resource
(``"ASRL5::INSTR"``).  If any property is missing or invalid, or the port
cannot be opened, the connection is replaced by a mock port and only a
warning is logged; every query then returns nothing.  Write the terminations
as real line endings (``"\n"`` in JSON); an escaped backslash
(``"\\n"``) is decoded, with a warning asking you to fix the file.

**Example** (the ``pczpiezo`` entry of ``example_sted.json``):

.. code-block:: json

   "rs232devices": {
       "pczpiezo": {
           "managerName": "RS232Manager",
           "managerProperties": {
               "port": "ASRL5::INSTR",
               "encoding": "ascii",
               "recv_termination": "\n",
               "send_termination": "\n",
               "baudrate": 115200,
               "bytesize": 8,
               "parity": "none",
               "stopbits": 1,
               "rtscts": "false",
               "dsrdtr": "false",
               "xonxoff": "false"
           }
       }
   }

``rtscts``, ``dsrdtr`` and ``xonxoff`` appear in the shipped setups but are
not read by ``RS232Manager``.


availableWidgets
----------------

List of widget names to load, ``true`` for all, or ``false`` for none.
Defaults to an empty list, which loads no panels.

Available widget names (case-sensitive):

* Core: ``Settings``, ``View``, ``Recording``, ``Image``
* Viewer tools: ``ViewerTools``, ``LineProfile``
* Hardware control: ``Laser``, ``Positioner``, ``Rotator``, ``RotationScan``,
  ``FlipMirror``, ``BSC203``, ``SLMs``
* Stand integration (need `microscopeStand`_): ``LeicaStand``, ``MotCorr``
  (Leica motorized correction collar)
* Scanning and acquisition: ``Scan``, ``Tiling``, ``WellPlate``,
  ``LightSheetMulticolor``, and the TriggerScope panels
  ``TriggerScopeRaster``, ``TriggerScopePLSR``, ``TriggerScopeGalvoDetection``,
  ``TriggerScopePLSRMulticolor``, ``TriggerScopeScan``, ``TriggerScopeLSXYR``
* Focus: ``FocusLock``, ``Autofocus``
* Event-triggered and smart microscopy: ``EtSTED``, ``EtMonalisa``,
  ``EtSnouty``, ``SetupModes``, ``SetupStatus``
* Analysis tools: ``BeadRec``, ``AlignAverage``, ``AlignXY``,
  ``AlignmentLine``, ``ULenses``, ``FFT``, ``FLIMHist``
* Scripting: ``Console``, ``Watcher``

``SLM`` and ``BFTimelapse`` have no default dock position: they appear only
when `widgetLayout`_ places them.  Without a ``widgetLayout`` entry they are
skipped without a message, as is any name not in the list above.

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

Optional :class:`WidgetLayoutInfo` object.

Overrides the default dock layout.  Each inner list is a *tab group*
(widgets share one dock row and appear as tabs).  Successive inner lists
are stacked vertically.  A panel still has to be named in
``availableWidgets`` to load; enabled panels this layout does not place keep
their default position.

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

**See also**: :class:`WidgetLayoutInfo`


Additional sections
===================

The following sections support specialized hardware or workflows.  Each is
documented below with full field lists and examples:

* `focusLock`_
* `autofocus`_
* `tiling configuration`_
* `etSTED`_
* `microscopeStand`_
* `slms`_ (modern multi-SLM support)
* `flipMirrors`_
* `triggerScope`_
* `teensyPulse`_
* `pyroServerInfo`_
* `smartMicroscopyModes`_ (and its two companion sections)
* `processing`_ (ImProcess)
* `rois`_
* `laserPresets`_

**Legacy / deprecated sections**:

* `slm (singular)`_ — superseded by ``slms``; only used by old ``SLMController``
* `pulseStreamer`_ — dormant; ``PulseStreamerManager`` no longer constructed at runtime


focusLock
---------

Optional :class:`FocusLockInfo` object.
**Required if you use the FocusLock widget** or want focus-lock functionality.

Key fields:

* ``camera`` (str): Detector name (must match a detector with ``forFocusLock: true``)
* ``positioner`` (str): Positioner name (typically a Z-axis piezo)
* ``positionerAxis`` (str or int, optional): Axis of that positioner the lock
  moves; defaults to ``"Z"`` when the positioner has one, otherwise ``0``
* ``updateFreq`` (int): Focus-estimate update rate in **hertz** (must be positive; rates above 1000 are floored to a 1 ms timer)
* ``frameCropx`` / ``frameCropy`` (int): Starting X/Y position of camera frame crop in pixels
* ``frameCropw`` / ``frameCroph`` (int): Width/height of camera frame crop in pixels
* ``swapImageAxes`` (bool): Swap camera image axes when grabbing frame
* ``piKp`` (float): Default kp (proportional gain) of feedback loop
* ``piKi`` (float): Default ki (integral gain) of feedback loop
* ``reacquireTimeoutS`` (float, default ``1.0``): Settle allowance for the actuator
  after a scan released it. The reacquisition deadline is this **plus** the sample
  window, ``reacquireSamples / updateFreq``, so a slow focus camera cannot make the
  deadline structurally impossible
* ``reacquireTolerancePx`` (float, default ``0.5``): How close the signal must
  return to its pre-scan setpoint before the lock re-engages, in camera pixels
* ``reacquireSamples`` (int, default ``5``): Consecutive estimates averaged
  before deciding the signal has settled; must be at least ``2``

A non-positive ``updateFreq``, a negative ``reacquireTimeoutS`` or a
``reacquireSamples`` below ``2`` stops the setup file from loading.

.. _focuslock-scan-arbitration:

Scan arbitration
^^^^^^^^^^^^^^^^

The focus lock and a hardware Z scan can drive the *same physical actuator*.
The STED example reaches one piezo twice: as the analog scanner ``ND-PiezoZ``
and as the serial positioner ``PiezoZ``. An actively correcting lock will
oppose the intentional Z waveform.

ImSwitch2 therefore suspends focus actuation for the duration of any scan that
can reach the lock's axis, and does not resume the instant the scan ends: it
first waits for the signal to settle *and* to return within
``reacquireTolerancePx`` of the setpoint it was holding. On timeout the lock is
left off and a warning is logged, rather than correcting against a signal that
never came back. The **Pause during scans** checkbox opts out of this.

Whether a scan conflicts is decided automatically:

#. the lock's own positioner is among the scan's positioners, or
#. both positioners declare the same ``physicalActuator``, or
#. a scanned positioner carries the lock's axis.

Rule 3 covers the STED case with no configuration at all. Where two Z
positioners really are separate devices, give them **different**
``physicalActuator`` ids to say so — that suppresses rule 3 and keeps the lock
running through those scans.

Triggered tiling holds each tile until the lock is holding again before firing
the next scan. Without that gate the loop would cancel its own reacquisition:
at ``updateFreq: 10`` a five-sample window needs ~0.5 s, against a default
0.15 s tile settle.

.. note::

   Scan controllers that do not populate ``target_device`` publish no actuator
   list, so the lock treats their scans as conflicting and suspends around each
   one. Among the TriggerScope family only the raster controller publishes;
   pLS-RESOLFT, LSXYR, galvo-detection and multicolor scans therefore always
   suspend the lock. This is safe but pessimistic.

.. warning::

   ``reacquireTolerancePx`` ships with a placeholder default that has not been
   measured on hardware. Its meaningful value depends on your rig's pixel-to-µm
   calibration: too loose re-engages against a defocused sample, too tight
   times out on every tile. Calibrate it before relying on 3D tiling.

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

**See also**: :class:`FocusLockInfo`


autofocus
---------

Optional :class:`AutofocusInfo` object.
**Required if you use the Autofocus widget** or want autofocus functionality.

Key fields:

* ``camera`` (str): Detector name
* ``positioner`` (str): Positioner name (typically Z-axis)
* ``updateFreq`` (int): Update rate of the autofocus plot, in hertz
* ``settleTimeMs`` (float, default ``150``): Wait after each Z move before the focus metric's frame is taken. The frame is taken through the same fresh-frame handshake as tiling, so a camera slower than this still yields a frame that started exposing after the move
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

**See also**: :class:`AutofocusInfo`


tiling configuration
--------------------

Optional :class:`TilingInfo` object.
**Required if you use the Tiling widget** for spiral tiling scans.

See :doc:`tiling` for what these do in practice.

Devices:

* ``xyPositioner`` (str): Name of the XY positioner (must match a positioner in the setup)
* ``zPositioner`` (str, optional): Reserved compatibility field. The built-in
  Tiling controller and ``TilingWorkflow`` do **not** perform per-tile
  autofocus from this value. Leave it empty unless a site-specific extension
  consumes it; use the scripted Z-stack/multi-well workflows when autofocus is
  required between positions.
* ``camera`` (str, optional): Detector used to build and align the mosaic (empty
  string = first ``forAcquisition`` detector). This is the default, not a fixed
  choice: on a rig with more than one acquisition detector the widget shows an
  **Align on** combo that overrides it for the session. The override is not
  persisted, so this field stays the value every session starts from. Switching
  it discards the current overview because the mosaic geometry comes from that
  detector's pixel size.

Geometry:

* ``defaultTileStepUm`` (float): Default stage step between tile centres in micrometres. Must be smaller than the field of view or the tiles cannot overlap.
* ``defaultTilesX`` (int, default ``3``) and ``defaultTilesY`` (int, default ``3``): Opening grid size. Equal values start the widget with its ``Square`` lock engaged; unequal ones start it unlocked, so a rig that normally surveys a rectangle opens ready for one.
* ``defaultPattern`` (str, default ``"spiral"``): ``"spiral"`` grows outward from the current position and can be stopped early with a filled, centred mosaic; ``"serpentine"`` rasters from the current position in +X and +Y with no move longer than one tile step. Anything else is refused rather than defaulted.
* ``settleTimeMs`` (float, default ``150``): Wait after each stage move before the tile is acquired. The first thing to increase when a mosaic does not line up.
* ``flipTileAxisX`` / ``flipTileAxisY`` (bool, default ``false``): Mirror the mosaic left/right or up/down.
* ``swapTileAxes`` (bool, default ``false``): Exchange the mosaic axes, for a camera mounted at 90° to the stage. Applied **before** the flips. These three enumerate all eight ways a camera can sit relative to the stage.

Acquisition:

* ``mode`` (str, default ``"free-running"``): ``"free-running"`` grabs frames from a continuously running camera; ``"triggered"`` runs one scan per tile, which is required for scan-driven detectors (APD/PMT) and used for cameras clocked by the scan trigger.
* ``scanSource`` (str, optional): Which scan controller triggered mode drives. Only meaningful on rigs with more than one.
* ``scanTimeoutS`` (float, default ``300``): How long to wait for one tile's scan before giving up.

Triggered mode refuses a scan source that drives the same XY positioner used
by tiling. Use a galvo/beam scan for each tile or a different positioner for the
between-tile motion.

Alignment:

* ``registerTiles`` (bool, default ``false``): Measure where each tile really belongs instead of trusting the commanded stage position. Each tile is correlated against every already-placed neighbour it overlaps, and the whole layout is solved again once the run ends.
* ``registrationMaxShiftFraction`` (float, default ``0.5``): Reject corrections larger than this fraction of the tile step, guarding against false matches on repeating structure.
* ``detectorTransforms`` (object, default ``{}``): How each additionally-saved detector's pixels relate to the one tiling aligns on, as ``{"Camera": "identity"}``. A tiling run saves whatever the Recording widget is set to capture, at every tile position — but sharing a stage position establishes only the *tile grid*, not pixel-level agreement between detectors: sensor origin, ROI, orientation, rotation and optical-path offsets all differ independently, so this is declared rather than inferred, and ``"identity"`` is a statement about the rig that whoever writes it owns. Only ``"identity"`` is accepted so far. This is optional: a detector with no entry is still saved, with its transform recorded as ``unknown`` so a reader knows nobody stated one, rather than being dropped — the relationship can be established offline, but a tile never acquired cannot. The detector tiling aligns on needs no entry — it is the reference.

Saving:

* ``saveTiles`` (bool, default ``false``): Write each tile as it is acquired, plus the mosaic and the sidecar files.
* ``saveFormat`` (str, default ``"TIFF"``): Container for the saved tiles.
* ``measurementsRoot`` (str, optional): Where run folders are written. Empty takes the Recording widget's output folder, which is normally what you want.

**Example**:

.. code-block:: json

   "tiling": {
       "xyPositioner": "StageXY",
       "zPositioner": "",
       "camera": "Camera",
       "defaultTileStepUm": 100.0,
       "settleTimeMs": 150.0,
       "registerTiles": true,
       "saveTiles": true,
       "swapTileAxes": false
   }

**Required devices**: One XY positioner. A camera is optional only when another
acquisition detector can supply the tiles. ``zPositioner`` is not used by the
built-in tiling workflow.

**See also**: :doc:`tiling`, :class:`TilingInfo`


etSTED
------

Optional :class:`EtSTEDInfo` object for the EtSTED widget (event-triggered
STED, see :doc:`gui`): it flips or swaps the coordinates of a detected event
before they are transformed into scan coordinates.  Without it, no flip or
swap is applied; the fast detector and laser are chosen in the widget.

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

**See also**: :class:`EtSTEDInfo`


microscopeStand
---------------

Optional :class:`MicroscopeStandInfo` object.
**Required if you use the MotCorr, LeicaStand, EtMonalisa or BFTimelapse
widgets**, which all talk to the stand, or other microscope stand
integration.  See :doc:`devices/stands`.

Key fields:

* ``managerName`` (str): Manager class (e.g., ``"LeicaDMIStandManager"``)
* ``rs232device`` (str): Name of the RS232 device to use (must match an entry in ``rs232devices``)
* ``managerProperties`` (dict, optional): Manager-specific settings (e.g., available cube slot names)

**Example**:

.. code-block:: json

   "microscopeStand": {
       "managerName": "LeicaDMIStandManager",
       "rs232device": "LeicaStand",
       "managerProperties": {
           "availableCubes": {
               "1": "BF",
               "2": "GFP",
               "3": "RFP"
           }
       }
   }

**Required devices**: One RS232 connection.

Use ``LeicaDMIZPositionerManager`` in ``positioners`` when the Leica DMI Z
focus drive should also be exposed as a positioner.

**See also**: :class:`MicroscopeStandInfo`


slms
----

Map of SLM names to :class:`SLMsInfo` objects.
**This is the modern plural form**; use this for new setups.  Required if you use
the ``SLMs`` widget.

Key fields (per SLM):

* ``managerName`` (str): Manager class (e.g., ``"HamamatsuSLMdviManager"``)
* ``managerProperties`` (dict): Manager-specific settings
* ``monitorIdx`` (int): Monitor index in system list (starts at 0)
* ``serial_number`` (str): Unique serial number of the SLM head
* ``width`` / ``height`` (int): SLM dimensions in pixels
* ``wavelength`` (int): Wavelength of the laser line used with the SLM, in nm
* ``pixelSize`` (float): Pixel pitch in millimeters (``0.02`` for a 20 µm pitch)
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
           "pixelSize": 0.02,
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

**See also**: :class:`SLMsInfo`


flipMirrors
-----------

Optional map of motorized flip mirror names to :class:`FlipMirrorInfo`
objects.  Required by the ``FlipMirror`` panel.

Key fields (per mirror):

* ``managerName`` (str): ``"ThorlabsMFFManager"`` for Thorlabs MFF101/MFF102
  mounts, ``"ThorlabsMFF_mock"`` for a simulated one
* ``serial_number`` (str): Serial number the manager uses to find the device
* ``invert`` (bool, default ``false``): Swap logical states 0 and 1
* ``initial_state`` (int, optional): State to move to at startup; omit to
  leave the mirror where it is
* ``state_names`` (dict, optional): Display names for states ``"0"`` and ``"1"``
* ``managerProperties`` (dict, optional): Manager-specific settings

**Example** (from ``example_snouty_smart_modes.json``):

.. code-block:: json

   "flipMirrors": {
       "Illumination": {
           "managerName": "ThorlabsMFF_mock",
           "initial_state": 0,
           "state_names": {
               "0": "Widefield",
               "1": "Light sheet"
           }
       }
   }


triggerScope
------------

Optional :class:`TriggerScopeInfo` object.  Required to use TriggerScope
hardware and ``"scanWidgetType": "TriggerScope"``.

* ``rs232device`` (str): Name of the ``rs232devices`` entry that connects to
  the board

The board drives every laser, detector and positioner whose
``analogChannel`` is ``"Triggerscope/DAC<n>"`` or whose ``digitalLine`` is
``"Triggerscope/TTL<n>"``.  Each analog device's ``minVolt`` / ``maxVolt``
in ``managerProperties`` (default ±10 V) bounds the voltages it is sent.

.. code-block:: json

   "triggerScope": {
       "rs232device": "triggerscope"
   }


teensyPulse
-----------

Optional :class:`TeensyPulseInfo` object.
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

**See also**: :class:`TeensyPulseInfo`,
:doc:`how-to/wire-teensy`


pyroServerInfo
--------------

Optional :class:`PyroServerInfo` object.
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

**See also**: :class:`PyroServerInfo`


smartMicroscopyModes
--------------------

Three optional maps that let an event-triggered workflow (for example
``EtSnouty``) switch the microscope between setup modes (see
:doc:`working-in-imswitch2`) as it runs:

* ``smartMicroscopyModes``: ``workflowName → {role: setupModeName}``.  The
  roles are ``scouting``, ``event``, ``resume``, ``idle`` and ``validation``;
  each names an existing setup mode.
* ``smartMicroscopyModePolicies``: ``workflowName → policy``, one of
  ``allow``, ``warnOnly`` or ``blockOnHazard``.  A missing or unknown policy
  means ``blockOnHazard``, which refuses to arm when a pre-flight check finds a
  hazard or a missing role mode.
* ``smartMicroscopyModeSwitchingEnabled``: ``workflowName → bool``.  ``false``
  or absent keeps the workflow's own, older mode switching.

**Example** (from ``example_snouty_smart_modes.json``):

.. code-block:: json

   "smartMicroscopyModes": {
       "EtSnouty": {
           "scouting": "Snouty widefield scouting",
           "event": "Snouty light-sheet event scan",
           "resume": "Snouty widefield scouting",
           "idle": "Snouty safe idle"
       }
   },
   "smartMicroscopyModePolicies": {"EtSnouty": "blockOnHazard"},
   "smartMicroscopyModeSwitchingEnabled": {"EtSnouty": true}


processing
----------

Optional object read by ImProcess, not by ImControl: which ImProcess panels
open at startup (``graphPanel``, ``roiManagerPanel`` and so on) and which
``reconstructors`` and ``processors`` it offers.  The processing-only presets
(``*_processor.json``, ``general_image_processing.json``) consist of little
else.  See :ref:`improcess-processing-presets`.


rois
----

Optional map of ROI preset names to :class:`ROIInfo` objects.
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

**See also**: :class:`ROIInfo`


laserPresets
------------

Optional laser power preset system.

* ``laserPresets`` (dict): Map of preset names to ``{laserName: LaserPresetInfo}`` nested dicts

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
   }

**See also**: :class:`LaserPresetInfo`


slm (singular)
--------------

.. deprecated:: ImSwitch2
   Use ``slms`` (plural) for new setups.  The ``slm`` field is only read by
   the legacy ``SLMController`` and is superseded by the multi-SLM ``slms``
   map.

Optional :class:`SLMInfo` object.  Prefer
``slms`` for all new configurations.

**See also**: :class:`SLMInfo`, `slms`_


pulseStreamer
-------------

.. deprecated:: ImSwitch2
   **Legacy / not constructed at runtime.**  ``MasterController`` no longer
   constructs ``PulseStreamerManager`` (its instantiation is commented out
   in ``imswitch/imcontrol/controller/MasterController.py``).  The active
   pulse-generator path is ``teensyPulse`` → ``TeensyPulseManager``.  This
   field remains for backward compatibility with old configs but has no
   runtime effect.

Optional :class:`PulseStreamerInfo` object.

Key fields:

* ``ipAddress`` (str or null): IP address of PulseStreamer hardware

**See also**: :class:`PulseStreamerInfo`,
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
     - ``xyPositioner``, optional ``zPositioner``, ``camera`` and
       ``detectorTransforms`` entries
     - Widget fails if XY positioner is missing; ``zPositioner`` is validated
       when supplied but is not consumed by built-in tiling
   * - ``scan``
     - Positioners with ``forScanning: true``; lasers and detectors with ``digitalLine`` set (for TTL targets)
     - None by name: every laser and detector with a ``digitalLine`` becomes a TTL row; ``lineClockLine`` references NI-DAQ port
     - Widget fails if no forScanning positioners; a device without ``digitalLine`` gets no TTL row
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
* ``example_monalisa.json`` — MoNaLISA (parallelized RESOLFT) microscope:
  two Hamamatsu cameras, stage scanning with ``BetaScanDesigner``
* ``example_sted.json`` — STED microscope with galvo scanning
* ``example_coolLED.json`` — CoolLED illumination system
* ``example_kiralux_teensy.json`` — Thorlabs Kiralux camera + Teensy pulse generator
* ``example_snouty_smart_modes.json`` — mock flip-mirror beam path with setup
  modes and `smartMicroscopyModes`_; no hardware

Hardware-free scan setups (simulated NI-DAQ, see :doc:`mock-infrastructure`):

* ``mock_scan_setup.json`` — mock camera, MoNaLISA scan, for the
  record-and-reconstruct loop
* ``galvo_apd_mock_scan_setup.json`` — APD with an Advanced galvo scan
* ``hamamatsu_mock_scan_setup.json`` — mock Hamamatsu camera triggered by a
  Base scan
* ``mixed_hamamatsu_apd_mock_scan_setup.json`` — mock Hamamatsu camera and
  APD in one Base scan

ImProcess-only presets, with no hardware: ``fiji_processor.json``,
``general_image_processing.json``, ``monalisa_processor.json``,
``snouty_processor.json``, ``widefieldstarss_processor.json`` (see
:ref:`improcess-processing-presets`).

Or use the **visual config editor** (Config Studio), which shows typed fields
for every manager's ``managerProperties``.  In a running ImSwitch2 it is
**Tools → Edit hardware configuration…**.  Without starting the microscope,
run it from a source checkout with:

.. code-block:: bash

   python utility_scripts/imswitch_config_editor.py

or, from an installed copy:

.. code-block:: bash

   python -c "from imswitch.imcontrol.view.configeditor import main; main()"

To check a setup file without starting ImSwitch2 (unknown managers,
deprecated sections):

.. code-block:: bash

   python -m imswitch.imcontrol.model.plugins validate-setup my_setup.json


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
