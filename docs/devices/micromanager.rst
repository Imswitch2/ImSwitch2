******************************
Micro-Manager devices (bridge)
******************************

The ``imswitch-device-micromanager`` plugin lets a setup file use any device that a
`Micro-Manager <https://micro-manager.org>`_ device adapter can drive. It is built on
`pymmcore-plus <https://github.com/pymmcore-plus/pymmcore-plus>`_, which wraps MMCore, the C++ core of
Micro-Manager. A camera, stage, shutter or two-position switch that Micro-Manager can drive becomes an
ordinary ImSwitch2 detector, positioner, laser or flip mirror, and a converter turns an existing
Micro-Manager hardware configuration (``*.cfg``) into a setup-file skeleton.

The plugin is a normal device plugin (see :doc:`plugins`); it needs no change to the ImSwitch2 core.
This page covers installing it, the setup-file shape of each manager, the converter, the limits and
how it is tested. The design record is ``docs/design/plans/micromanager-bridge.md`` in the repository.

.. contents::
   :local:
   :depth: 2


Install
=======

The plugin needs **Python 3.11 or newer**, because ``pymmcore`` does not support 3.10. ImSwitch2
itself still supports 3.10, and the Windows and macOS installers currently bundle 3.10, so the plugin
cannot be loaded from an installer build yet: install ImSwitch2 from source (see
:doc:`../installation`) into a Python 3.11 or newer environment, then the plugin from a source
checkout (it is not published on PyPI yet):

.. code-block:: bash

   pip install -e ".[test]"                                     # ImSwitch2 itself, from the repository root
   pip install -e "examples/plugins/imswitch-device-micromanager[test]"

The ``test`` extra adds ``mm-test-adapters`` (below), ``pytest`` and ``jsonschema``; leave it out for
a rig that uses real adapters only. ``pymmcore-plus`` pulls in ``ome-writers`` and ``tensorstore``, so
the install is larger than that of the other plugins.

Which device adapters exist depends on the platform. Micro-Manager's own nightly builds exist for
Windows x64 and Intel macOS only:

.. list-table::
   :widths: 24 38 38
   :header-rows: 1

   * - Platform
     - Real hardware adapters
     - Hardware-free demo adapters
   * - Windows x64
     - ``mmcore install`` (downloads the nightly that matches the device interface version of
       ``pymmcore``)
     - ``pip install mm-test-adapters``
   * - macOS Intel
     - ``mmcore install``
     - ``pip install mm-test-adapters``
   * - macOS Apple Silicon
     - none: Micro-Manager publishes no arm64 adapters
     - ``pip install mm-test-adapters``
   * - Linux x64
     - none: there are no Micro-Manager nightlies for Linux
     - ``pip install mm-test-adapters``

``mm-test-adapters`` ships the real MMCore ``DemoCamera``, ``Utilities`` and ``SequenceTester``
adapters and ``MMConfig_demo.cfg``. Development, CI and the bundled setup template use them, so the
bridge runs through the real MMCore without any hardware. The hub looks for adapters in the
``adapterPaths`` setup property, then the ``MICROMANAGER_PATH`` environment variable, then wherever
``pymmcore-plus`` finds a Micro-Manager install (which falls back to the demo adapters).


How a setup uses it
===================

Every bridged device names the **hub** in ``managerProperties.rs232device``. The hub is a
``micromanager.core`` entry in ``rs232devices``: it owns the one MMCore of the setup, its frame buffer
and the frame drain thread, so a setup that needs several Micro-Manager devices still has a single
core. The setup sections that hold the bridged devices are the usual ones:

.. list-table::
   :widths: 30 20 50
   :header-rows: 1

   * - ``managerName``
     - Setup section
     - Presents Micro-Manager's
   * - ``micromanager.core``
     - ``rs232devices``
     - the hub (no device)
   * - ``micromanager.camera``
     - ``detectors``
     - Camera device (mono only)
   * - ``micromanager.stage``
     - ``positioners``
     - Stage (one axis) or XYStage (axes ``X`` and ``Y``)
   * - ``micromanager.light``
     - ``lasers``
     - Shutter, a State device position or a two-valued property, with an optional power property
   * - ``micromanager.flip-mirror``
     - ``flipMirrors``
     - State device with exactly two positions, or a Shutter used as a switch

The ids live in the ``micromanager.`` namespace and claim no in-tree manager name, so installing the
plugin never re-routes an existing setup.


The hub: ``micromanager.core``
------------------------------

.. code-block:: json

   "rs232devices": {
       "MMCore": {
           "managerName": "micromanager.core",
           "managerProperties": {
               "configFile": "~/MMConfig_scope.cfg",
               "adapterPaths": [],
               "circularBufferMB": null,
               "logDir": null,
               "adoptExistingCore": false
           }
       }
   }

.. list-table::
   :widths: 24 14 62
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``configFile``
     - str
     - Optional. A Micro-Manager ``.cfg`` loaded when the hub starts (*system-config mode*, below).
       ``~`` is expanded; a relative name is looked up in the working directory, then in the first
       adapter folder. Without it, devices load themselves (*inline mode*).
   * - ``adapterPaths``
     - list of str
     - Optional, default ``[]``. Folders searched for adapter libraries; empty uses
       ``MICROMANAGER_PATH``, then ``pymmcore-plus``' own search. A folder that does not exist is an
       error.
   * - ``circularBufferMB``
     - int or null
     - Optional, default ``null`` (MMCore's own default, 250 MB). Size of MMCore's circular image
       buffer.
   * - ``logDir``
     - str or null
     - Optional, default ``null``. Folder that receives MMCore's own log file.
   * - ``adoptExistingCore``
     - bool
     - Optional, default ``false``. Reuse the process-wide ``CMMCorePlus`` another package (for
       example napari-micromanager) created instead of constructing a private one. The signal backend
       of such a core is not under ImSwitch2's control.

**What the hub does.** It constructs its own ``CMMCorePlus`` with the ``psygnal`` signal backend (under
a Qt application, pymmcore-plus would otherwise pick the Qt signaler for every MMCore callback), logs
the adapter folder it chose and the device interface version, loads ``configFile``, and turns MMCore's
auto-shutter off so MMCore never opens and closes a shutter behind the bridge's back. On finalize it
unloads every device; ImSwitch2 finalizes the bridged devices first and the hub last.

**It never aborts the app.** A missing Micro-Manager install, missing adapters or a bad ``.cfg`` becomes
an ERROR status carrying MMCore's message in the Hardware status window, and every bridged device is
then built in the same ERROR state instead of crashing the startup. **Reconnect** in Hardware status
reloads the ``.cfg`` and re-initialises every device on the hub.

**Vendor library.** ``pymmcore-plus`` (``pip`` installs it with the plugin), plus the adapters above.
**Low-level dependencies.** None.


Two ways to bring a device in
-----------------------------

*System-config mode* (recommended: what a Micro-Manager user keeps): the hub loads the ``.cfg`` and a
device entry names a label from that file.

.. code-block:: json

   "detectors": {
       "Camera": {
           "managerName": "micromanager.camera",
           "managerProperties": {"rs232device": "MMCore", "label": "Camera"},
           "forAcquisition": true
       }
   }

*Inline mode* (one device, no ``.cfg``): the entry carries ``adapter`` and ``device`` (and optionally
``parent``, the label of the hub device the adapter needs, and ``preInitProperties``). The manager
loads the device under ``label``, applies the pre-init properties, initialises it and unloads it again
when it finalizes.

.. code-block:: json

   "detectors": {
       "Camera2": {
           "managerName": "micromanager.camera",
           "managerProperties": {
               "rs232device": "MMCore", "label": "Camera2",
               "adapter": "DemoCamera", "device": "DCam",
               "preInitProperties": {"MaximumExposureMs": 5000}
           },
           "forAcquisition": true
       }
   }

The modes can be mixed in one setup (a ``.cfg`` plus one extra inline device). ``adapter`` and ``device``
must be given together; an inline label that the ``.cfg`` already loaded is refused (loading it a second
time would replace the configured one). A label the core does not have is refused with the list of
loaded devices of the right type and a "did you mean" suggestion, so a typo is a one-line fix. The
inline fields are the same for every manager below and are not repeated in the field tables.


Camera: ``micromanager.camera``
-------------------------------

A Micro-Manager **mono** Camera device as an ImSwitch2 detector.

.. list-table::
   :widths: 24 14 62
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``
     - str
     - **Required.** Name of the hub entry in ``rs232devices``.
   * - ``label``
     - str
     - **Required.** Micro-Manager device label.
   * - ``adapter``, ``device``, ``parent``, ``preInitProperties``
     - see above
     - Inline mode.
   * - ``cameraPixelSizeUm``
     - float
     - Optional. Sample-plane pixel size of the **unbinned** camera in micrometres. Wins over
       Micro-Manager's pixel-size configuration.

**Pixel size.** The setup value wins. Without it the manager takes Micro-Manager's current pixel-size
configuration (``getPixelSizeUm()``, divided by the binning, because MMCore includes the binning),
marks the provenance as "Micro-Manager pixel-size config" and does not let a saved widget state
override it. Micro-Manager reports 0.0 when no pixel-size configuration matches (for example an
objective position without one); that means "unknown", not a size. If neither the setup nor
Micro-Manager has a value, ImSwitch2's 0.15 um placeholder applies and is flagged "assumed default".

**What it presents.**

- *Exposure* (milliseconds, Micro-Manager's unit) is a first-class parameter.
- *Every other Micro-Manager property* of the camera becomes a parameter in the group "Camera
  properties": a property with allowed values becomes a choice list, a number without allowed values a
  number, a free-text property a read-only entry, and a read-only property stays read-only. Pre-init
  properties are skipped (they belong in ``preInitProperties``). Micro-Manager properties carry no
  units, so none are shown. A value is always read back, so the settings show what the camera took.
- *Binning*: the allowed values of the ``Binning`` property are the supported binnings.
- *Frame size and ROI*: ``fullShape`` is the full frame at the current binning. ``crop`` stores the ROI
  the adapter applied (adapters snap), in Micro-Manager's binned pixels. A change that may alter the
  frame (any property, binning, ROI) stops the stream, applies the change, re-reads the frame size and
  pixel type and restarts the stream. ``dtype`` follows the camera's ``PixelType`` before the first
  frame (8-bit cameras are ``uint8``).

**Mono only.** Colour cameras (``32bitRGB``, ``64bitRGB``) are refused at start, and a ``PixelType``
change that would select one is undone: ImSwitch2's chunk contract is ``(frames, height, width)``,
which the Zarr writer and napari layers assume.

**Frames.** The hub drains MMCore's buffer into one bounded queue per camera; the camera manager only
ever reads its own queue. A recording sees every frame once and in order, and live view keeps running
while a recording reads. A frame lost to a buffer overflow, or a sequence that ends by itself, makes the
next chunk read and the ``stopAcquisition`` raise, so a recording that lost frames fails instead of
finishing short.


Stage: ``micromanager.stage``
-----------------------------

A Micro-Manager **Stage** (one axis) or **XYStage** (axes ``X`` and ``Y``) as a positioner.

.. code-block:: json

   "positioners": {
       "Z": {
           "managerName": "micromanager.stage",
           "managerProperties": {"rs232device": "MMCore", "label": "Z"},
           "axes": ["Z"],
           "forPositioning": true
       },
       "XY": {
           "managerName": "micromanager.stage",
           "managerProperties": {"rs232device": "MMCore", "label": "XY", "homeable": true},
           "axes": ["X", "Y"],
           "forPositioning": true
       }
   }

.. list-table::
   :widths: 24 14 62
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``, ``label``
     - str
     - **Required**, as for the camera.
   * - ``adapter``, ``device``, ``parent``, ``preInitProperties``
     - see above
     - Inline mode.
   * - ``waitForDevice``
     - bool
     - Optional, default ``true``. A move blocks until the device reports it finished, then the position
       is read back. With ``false`` the command returns at once and the position follows the device's
       own position-changed events (not every adapter sends them; ``updatePosition`` reads it).
   * - ``homeable``
     - bool
     - Optional, default ``false``. The setup declares that the adapter supports a home command and that
       the stage may be homed.

**Axes.** The axes come from the positioner entry's ``axes``: a Stage has exactly one axis of any name
(``["Z"]``, ``["Focus"]``), an XYStage exactly ``["X", "Y"]``; anything else, or a label that is not a
stage, refuses to start and says what Micro-Manager has. Positions are micrometres in both systems;
nothing is scaled. ``forScanning`` must stay ``false``: Micro-Manager stages have no analog scan line,
and ImSwitch2's waveform scanning stays native.

**Position.** The tracked position is what the hardware says, never a zero: it is read from the device
at startup and after every blocking move. A stage whose hub is down at startup starts at position
``nan`` ("unknown") and refuses every command until the hub is reconnected.

**Homing.** Homing moves hardware and Micro-Manager has no query for whether an adapter supports it, so
it is never tried on its own. With ``homeable: true`` the positioner starts *unreferenced*, and the
Reference action homes the whole device, waits for it, re-reads the position and marks it referenced. An
adapter without a home command (the demo ``Z``) reports Micro-Manager's own "Unsupported device command"
on that action, never at startup. Without ``homeable`` the Reference action does nothing.


Light: ``micromanager.light``
-----------------------------

A Micro-Manager light source as an ImSwitch2 **laser**. ``label`` is the device that switches emission,
and ``enable`` says how:

.. list-table::
   :widths: 24 76
   :header-rows: 1

   * - ``enable``
     - Meaning
   * - ``{"kind": "shutter"}``
     - ``label`` is a Shutter device (``setShutterOpen``).
   * - ``{"kind": "state", "on": ..., "off": ...}``
     - ``label`` is a State device; ``on`` and ``off`` are state labels (strings) or state indices
       (integers) and must differ. A label the device does not have is refused at startup with the
       device's labels.
   * - ``{"kind": "property", "name": ..., "on": ..., "off": ...}``
     - A property of ``label`` takes one value for emission and another for dark. When the property
       lists allowed values both must be among them (numbers match by value, so ``1`` is ``"1"``); a
       read-only property is refused.

The device type must fit the kind (a Shutter for ``shutter``, a State device for ``state``;
``property`` takes any device); a mismatch is refused naming the type found.

.. code-block:: json

   "lasers": {
       "LED": {
           "managerName": "micromanager.light",
           "managerProperties": {
               "rs232device": "MMCore", "label": "LED Shutter", "enable": {"kind": "shutter"}
           },
           "wavelength": 470,
           "valueRangeMin": null,
           "valueRangeMax": null
       }
   }

**Power.** Without a ``power`` block the light is *binary*: an on/off switch and no power field. With
one, a numeric (float or integer) property sets the output:

.. code-block:: json

   "managerProperties": {
       "rs232device": "MMCore", "label": "LED Shutter", "enable": {"kind": "shutter"},
       "power": {"label": "DA", "property": "Voltage", "units": "V", "decimals": 2}
   }

.. list-table::
   :widths: 24 14 62
   :header-rows: 1

   * - ``power`` field
     - Type
     - Meaning
   * - ``property``
     - str
     - **Required.** The numeric property; must exist and must not be read-only.
   * - ``units``
     - str
     - **Required, no default.** Micro-Manager properties carry no unit and the bridge does not guess one.
   * - ``decimals``
     - int >= 0
     - **Required, no default.** The value is written with exactly this many decimals (0 for an integer
       property).
   * - ``label``
     - str
     - Optional, default the same device as ``label``. A power device on another label must be loaded by
       the hub's ``.cfg``.

With ``power``, the laser entry's ``valueRangeMin`` and ``valueRangeMax`` must be numbers: they are the
safe range of the property. They must lie inside the limits Micro-Manager states for it (both ranges are
in the refusal), and a value outside them is refused even where Micro-Manager would take it. Every
command is read back: a device that does not show the new state is a failed command (``setEnabled``
logs it and returns ``False``; ``applyEnabled`` raises ``RawDriveError``).

**What it does not do.** Power and emission are separate switches in Micro-Manager, so writing power does
not switch emission. There is no TTL path, so a scan cannot gate the light, and there is no modulation.
After a hub reconnect and when it finalizes, the light is switched off and must read back off before it
counts as connected. ``wavelength`` is ImSwitch2's own field (it colours the panel) and is required there.


Flip mirror: ``micromanager.flip-mirror``
-----------------------------------------

A **two-position** Micro-Manager device as an ImSwitch2 flip mirror: a State device with exactly two
positions (a flip mount, a two-way light path), or a Shutter presented as a switch (closed is state 0,
open is state 1).

.. code-block:: json

   "flipMirrors": {
       "WhiteLight": {
           "managerName": "micromanager.flip-mirror",
           "managerProperties": {"rs232device": "MMCore", "label": "White Light Shutter"},
           "state_names": {"0": "Closed", "1": "Open"}
       }
   }

.. list-table::
   :widths: 24 14 62
   :header-rows: 1

   * - Field
     - Type
     - Meaning
   * - ``rs232device``, ``label``
     - str
     - **Required**, as for the camera.
   * - ``adapter``, ``device``, ``parent``, ``preInitProperties``
     - see above
     - Inline mode (for example ``DemoCamera`` / ``DStateDevice`` with the pre-init property
       ``Number of positions`` set to 2).
   * - ``states``
     - list of 2
     - Optional, State devices only. The two Micro-Manager positions (state labels or indices) used as
       logical states 0 and 1; default: position 0 is state 0 and position 1 is state 1. It orders the
       two positions; it never turns a wheel into a switch. Refused for a Shutter (use ``invert``).

``invert``, ``initial_state`` and ``state_names`` are fields of the flip-mirror entry itself (as for the
Thorlabs MFF), not of ``managerProperties``:

- ``invert`` (default ``false``) swaps logical states 0 and 1 against the device.
- ``initial_state`` (``0``, ``1`` or ``null``): the state to move to at startup; ``null`` leaves the mirror
  where it is. A move that fails at startup is a connection error of this manager. A reconnect never
  replays it: it only observes where the mirror is.
- ``state_names`` names the two buttons (``"0"`` and ``"1"``). A state it does not name is called what
  Micro-Manager calls the position: its state label, or ``Closed`` / ``Open`` for a Shutter. The names
  describe the physical position, so they swap along with ``invert``. The widget reads them once, when
  the panel is built; a mirror that was not connected then names the states ``0`` and ``1``.

**Two positions only.** A State device with more or fewer than two positions (a filter wheel, an
objective turret, a multi-way light path) is **refused at startup**, with its position count in the
message; ``states`` does not change that. ImSwitch2 has no selector device kind yet, and squeezing ten
positions into two buttons would misrepresent the hardware (see *Known limits*).

**Moves** are waited for (without holding the hub lock, so a slow mount does not stall the camera) and
read back: a device that does not show the new position is a failed move and raises. The reason of the
last failure is kept (``get_last_error``) until the next command succeeds.


Device types the bridge does not map
====================================

Not every Micro-Manager device type becomes an ImSwitch2 device. A device of one of these types in a
``.cfg`` is loaded by the hub (other devices may need it) but gets no setup entry, and the converter
lists it in its report:

.. list-table::
   :widths: 24 76
   :header-rows: 1

   * - Micro-Manager type
     - Why it is not mapped
   * - State device with more than two positions
     - Filter wheels, objective turrets and multi-way light paths need a selector device kind that
       ImSwitch2 does not have yet; deferred (see *Known limits*).
   * - Hub, Serial, Core
     - Micro-Manager's own plumbing: peripherals name the hub as their parent and the ``.cfg`` opens
       serial ports; there is nothing to control.
   * - SignalIO, Galvo
     - They duplicate ImSwitch2's own waveform scanning (NI-DAQ, TriggerScope), which stays native.
   * - AutoFocus
     - No ImSwitch2 contract for it yet; waits for a concrete user.
   * - SLM
     - Waits for a concrete user.
   * - Magnifier, ImageProcessor, pumps, Generic
     - No ImSwitch2 counterpart (ImSwitch2 processes frames in ImProcess).

A device whose type cannot be determined (the adapter is not installed on the converting machine) is
*unresolved* in the converter, not guessed.


Converting a Micro-Manager configuration
========================================

.. code-block:: bash

   python -m imswitch_device_micromanager convert MMConfig_scope.cfg -o scope_setup.json

The command reads the ``.cfg`` with the grammar MMCore itself parses (``Device``, ``Parent``,
``Property``, ``Label``, ``ConfigGroup``, ``ConfigPixelSize``, ``PixelSize_um``, ``Delay``,
``FocusDirection`` ...), writes ``scope_setup.json`` and a sidecar ``scope_setup.conversion-report.txt``,
and prints the report. It needs no ImSwitch2 import, starts no application and **loads or initialises no
hardware**.

.. list-table::
   :widths: 30 70
   :header-rows: 1

   * - Option
     - Meaning
   * - ``-o``, ``--output``
     - The setup file to write (required). The report goes next to it as ``<name>.conversion-report.txt``.
   * - ``--type LABEL=TYPE``
     - Give or override a device type by hand: ``camera``, ``stage``, ``xystage``, ``shutter`` or ``state``.
       Repeatable. A label the ``.cfg`` does not declare is an error.
   * - ``--no-classify``
     - Do not ask the adapter libraries: every device not given by ``--type`` is unresolved.
   * - ``--adapter-path DIR``
     - Adapter folder to classify with, also written to the hub as ``adapterPaths``. Repeatable. Default:
       ``MICROMANAGER_PATH``, then ``pymmcore-plus``' own search.
   * - ``--hub-name NAME``
     - Key of the hub in ``rs232devices`` (default ``MMCore``).

**Exit code.** 0 when the setup was written, even with TODOs. 2 when it was not: an unreadable or
malformed ``.cfg`` (the message names the file and line), a bad ``--type``, or an output that cannot be
written.

How devices are classified
--------------------------

A ``Device,`` line carries a label, an adapter library and a device name, but not the device *type*. The
converter asks a bare MMCore that knows only where the adapters are (``getAvailableDevices(library)``
next to ``getAvailableDeviceTypes(library)``), so nothing is loaded or initialised. A library the
machine does not have, or a device name the library does not list, leaves the device *unresolved*; the
report says which and why, and ``--type`` can supply the type.

What lands in the setup file
----------------------------

.. list-table::
   :widths: 30 20 50
   :header-rows: 1

   * - Micro-Manager device
     - Setup section
     - Entry
   * - (always)
     - ``rs232devices``
     - the hub, ``configFile`` as given on the command line, ``adapterPaths``
   * - Camera
     - ``detectors``
     - ``micromanager.camera``, ``forAcquisition`` true
   * - Stage
     - ``positioners``
     - ``micromanager.stage``, axes ``["Z"]`` when the ``.cfg`` makes it the focus device
       (``Property,Core,Focus``), else ``["TODO"]``
   * - XYStage
     - ``positioners``
     - ``micromanager.stage``, axes ``["X", "Y"]``
   * - Shutter
     - ``lasers``
     - ``micromanager.light``, ``enable`` ``{"kind": "shutter"}``, ``valueRangeMin`` / ``valueRangeMax``
       ``null`` (a binary light)
   * - State device with two positions
     - ``flipMirrors``
     - ``micromanager.flip-mirror``

The position count of a State device comes from the ``.cfg`` alone: the pre-init property
``Number of positions`` when there is one, else the ``Label`` lines. If the ``.cfg`` says neither, the
device is listed with "position count unknown" and you add the ``flipMirrors`` entry yourself. A wheel
that is written as a flip mirror by mistake fails loudly at startup with its real count.
``availableWidgets`` holds Settings, View, Recording and Image, plus Positioner, Laser and FlipMirror
when the setup has entries for them. Entries are keyed by the Micro-Manager label.

What lands in the report
------------------------

Every device that was **not** written gets one line with its label, adapter library and device name, its
type and the reason (multi-position State devices with their position count, Hub, AutoFocus, unresolved
devices ...), and every value the converter could not derive is a ``TODO`` line. A value it cannot derive
is never invented, and the JSON never carries comments (``validate-setup`` reads it with ``json.load``):

- ``cameraPixelSizeUm`` is **omitted**. When the ``.cfg`` defines pixel-size configurations the report
  says that Micro-Manager supplies the value; otherwise it is a TODO.
- The axis name of a Stage that is not the focus device is the string ``"TODO"``; it loads, and no scan
  or panel mistakes it for an axis. Choose a name.
- A light's ``wavelength`` is **omitted** (the loader reads a missing one as ``None``). ``validate-setup``
  shows one warning per light until you set it, which is the same TODO.

An excerpt of the report for ``MMConfig_demo.cfg`` (the demo configuration of the test adapters; 13
devices):

.. code-block:: text

   Written to the setup file: hub rs232devices['MMCore'] + 5 device(s)
     detectors['Camera']  DemoCamera/DCam  Camera  -> micromanager.camera
     positioners['Z']  DemoCamera/DStage  Stage  -> micromanager.stage, axes ['Z']
     positioners['XY']  DemoCamera/DXYStage  XYStage  -> micromanager.stage, axes ['X', 'Y']
     lasers['White Light Shutter']  DemoCamera/DShutter  Shutter  -> micromanager.light, binary light
     lasers['LED Shutter']  Utilities/State Device Shutter  Shutter  -> micromanager.light, binary light

   To do before the first start (2)
     TODO lasers['White Light Shutter']: wavelength is not written ...
     TODO lasers['LED Shutter']: wavelength is not written ...

   Not written (8)
     'DHub'  DemoCamera/DHub  Hub  not written: ...
     'Dichroic'  DemoCamera/DWheel  State, 10 positions (10 Label line(s))  not written: multi-position State device ...
     ... Emission, Excitation, Objective (6), Path (3), LED (10) ...
     'Autofocus'  DemoCamera/DAutoFocus  AutoFocus  not written: no ImSwitch2 contract for it yet ...

Check the result with ``python -m imswitch.imcontrol.model.plugins validate-setup scope_setup.json``: it
passes, with the wavelength warnings above. The converter writes ``configFile`` exactly as you gave it on
the command line; a relative path is resolved against the folder ImSwitch2 is started from (the report
points that out), so pass an absolute path for a setup you start elsewhere.


The demo setup template
=======================

``setup_templates/demo_scope.json`` in the plugin is a complete, bootable setup for the Micro-Manager
demo devices (``MMConfig_demo.cfg``). It needs ``mm-test-adapters`` and Python 3.11 or newer. It defines the
hub, a ``Camera`` (a ``DemoCamera``, 512 x 512, 16 bit), a ``Z`` stage, a homeable ``XY`` stage, an
``LED`` light on the demo ``LED Shutter`` and a ``WhiteLight`` flip mirror on the demo ``White Light
Shutter``.

1. Copy the template into ImSwitch2's setup folder (``~/ImSwitchConfig/imcontrol_setups``, or
   ``Documents\ImSwitchConfig\imcontrol_setups`` on Windows).
2. Start ImSwitch2 and choose the setup: on the first start the setup chooser opens by itself, afterwards
   use **Hardware > Pick hardware setup...** (ImSwitch2 restarts).
3. You should see the Settings panel with the camera's Exposure and the "Camera properties" group; **Live
   view** showing the DemoCamera's test image; the Positioner panel with ``Z`` and ``XY`` (``XY`` starts
   unreferenced and its Reference action homes it; the demo ``Z`` has nothing to reference); the Laser
   panel with an on/off switch for ``LED``; the Flip Mirrors panel with ``WhiteLight`` and its buttons
   *Closed* and *Open* (the demo configuration leaves the shutter open); and the Recording panel, which
   records a chosen number of frames to HDF5, TIFF or Zarr.

The template carries one key besides the device sections, ``availableWidgets``: ImControl builds no
panel that is not listed.


Known limits
============

- **Python 3.11 or newer**, and so no installer build yet (see *Install*).
- **One buffer: equal frame sizes.** MMCore has a single circular buffer for all cameras, clears it when
  any sequence starts or a ROI is set, and does not check that two cameras deliver equally sized frames.
  The hub therefore owns the buffer and lets a second camera start streaming only with the frame shape
  and pixel type of the one already streaming; a different size is refused with an explanation. Micro-Manager's work on
  proper multi-camera buffers (pull request 936) is still open.
- **Filter wheels, turrets and light paths are deferred.** A State device with more than two positions has
  no ImSwitch2 counterpart yet: ImSwitch2 has no selector device kind. The flip mirror refuses it, and the
  converter reports it.
- **Colour cameras are deferred.** Cameras with colour frames are refused at start.
- **Lights are not scan-gated.** There is no TTL path from a bridged light, and no modulation; stages have
  no analog scan line. Scanning stays with ImSwitch2's native waveform generation.
- **Windows adapter DLLs.** pymmcore-plus adds the adapter folder to ``PATH`` but not to the DLL search
  directories. A vendor adapter whose SDK DLLs live in another folder may fail to load with
  ``adapterPaths`` alone. This has not been measured on a rig yet.
- **Hardware is not verified.** Everything on this page is verified against the real MMCore with the
  Micro-Manager demo adapters only; no rig has run the bridge yet.


How it is tested
================

The plugin's tests run against the **real MMCore** with the real ``DemoCamera`` and ``Utilities`` adapters
from ``mm-test-adapters``; there is no hand-written fake core. They cover the hub and its failure
isolation, both load modes, every manager (including the refusals above), transport reconnect with each
manager's re-initialisation hook, frame-loss detection with a deliberately slow consumer, the converter
against ``MMConfig_demo.cfg`` and its output through ``validate-setup``, and an end-to-end test that boots
the real ImControl application headlessly on the demo template (live view, a 30-frame recording read
back, both stages, the light, the flip mirror, the close order and a clean interpreter exit). From the
repository root:

.. code-block:: bash

   python -m pytest -p no:napari examples/plugins/imswitch-device-micromanager/tests

Use ``QT_QPA_PLATFORM=offscreen`` on a machine without a display, and do not run them with
``pytest-xdist``: the tests do not share an MMCore between processes. The discovery tests are skipped
until the plugin is installed into the environment.
