******************************
Micro-Manager devices (bridge)
******************************

The ``imswitch-device-micromanager`` plugin lets a setup file use any device that a
`Micro-Manager <https://micro-manager.org>`_ device adapter can drive. It is built on
`pymmcore-plus <https://github.com/pymmcore-plus/pymmcore-plus>`_, which wraps MMCore, the C++ core of
Micro-Manager. A camera, stage, shutter or multi-position switch (flip mount, filter wheel, objective
turret, light path) that Micro-Manager can drive becomes an
ordinary ImSwitch2 detector, positioner, laser or flip mirror, and a converter turns an existing
Micro-Manager hardware configuration (``*.cfg``) into a setup-file skeleton.

The plugin is a normal device plugin (see :doc:`plugins`); it needs no change to the ImSwitch2 core.
It is installed from a source checkout of ImSwitch2 into a Python 3.11 or newer environment, and no rig has
run it yet: everything here is verified against the real MMCore with Micro-Manager's demo adapters only.
This page covers a quick start, installing the plugin, the setup-file shape of each manager, the converter,
the limits, troubleshooting and how it is tested. The design record is
``docs/design/plans/micromanager-bridge.md`` in the repository.

.. contents::
   :local:
   :depth: 2


Quick start
===========

Install the plugin first (next section). Then there are three ways in, depending on what you have:

**You have a Micro-Manager configuration file (``*.cfg``).** In the config editor (**Hardware > Edit hardware
configuration...**) choose **Import Micro-Manager...**, pick the ``.cfg`` and read the report: it lists what
was written and what is still a ``TODO`` (a light's ``wavelength``, the axis name of a stage that is not the
focus device). Or run the converter from a terminal and open the result in the editor:

.. code-block:: bash

   python -m imswitch_device_micromanager convert /full/path/MMConfig_scope.cfg -o scope_setup.json
   python -m imswitch.imcontrol.model.plugins validate-setup scope_setup.json

Use an absolute path for the ``.cfg``: it is written into the hub as given, and a relative one is resolved
against the folder ImSwitch2 is started from. Fill in the ``TODO`` values, save the file in
``~/ImSwitchConfig/imcontrol_setups`` and pick it with **Hardware > Pick hardware setup...**.

**You have a device Micro-Manager supports, but no configuration yet.** Build the ``.cfg`` where Micro-Manager
expects it: in Micro-Manager's Hardware Configuration Wizard, add the device, give it its label, and confirm
there that it works (live view, the stage moves). Save the configuration, then import it as above. The
hardware stays Micro-Manager's business; ImSwitch2 only loads what the ``.cfg`` describes. For one device
you can skip the ``.cfg``: the setup entry names the adapter and the device itself (*inline mode*, below).

**You have no hardware.** Install the demo adapters (``pip install mm-test-adapters``) and copy the bundled
setup template ``demo_scope.json`` (see *The demo setup template*): a camera, two stages, a light and two
switches on Micro-Manager's own demo devices.


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
     - State device with two or more positions (flip mount, filter wheel, turret, light path), or a Shutter used as a switch

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
unloads every device; ImSwitch2 finalizes the bridged devices first and the hub last. The hub sets the
environment variable ``PYMM_SIGNALS_BACKEND=psygnal`` for the whole Python process, so it also affects any
other pymmcore-plus user in the same process (napari-micromanager, for example): cores built after the hub
connects get the psygnal signaler too.

**It never aborts the app.** A missing Micro-Manager install, missing adapters or a bad ``.cfg`` becomes
an ERROR status carrying MMCore's message in the Hardware status window, and every bridged device is
then built in the same ERROR state instead of crashing the startup. **Reconnect** in Hardware status
reloads the ``.cfg`` and re-initialises every device on the hub; cameras that were streaming resume in
any order (a camera that resumes while another already streams is checked by reads only, since measuring the
sensor would clear the shared buffer).

A camera built while the hub was down shows a read-only *Exposure* row and the *Camera pixel size* in the
Settings panel. The panel builds its tree once, at startup, so once the camera is connected the
exposure becomes editable and the pixel size and geometry update, but the camera's other Micro-Manager
properties get a row only after ImSwitch2 is restarted (a warning in the log lists them).

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

The **Camera pixel size** parameter is always the *unbinned* sample-plane size. What ImSwitch2 gives to
recordings, napari layers and tiling (``pixelSizeUm``, ``scale``) is the size of a *recorded* pixel, so it
is the parameter times the current binning: at binning 2 a recording of a camera with a 0.5 um pixel is
written with 1.0 um. (The other camera managers in the tree leave ``pixelSizeUm`` at the parameter whatever
the binning; the bridge follows the contract of the pixel size instead.)

While the pixel size is Micro-Manager's, it follows the **objective**: when a pixel-size configuration
changes (an objective or configuration group is set, or a device of a pixel-size configuration changes a
property) or when the settings are refreshed, the value is read again. If an objective position has no
pixel-size configuration the last value stays and a warning says so. A value from the setup file or one
the user typed is never overwritten; when Micro-Manager's calibration changes under it, one warning
says that the setup/user value stays in effect. A placeholder gives way to Micro-Manager's value as soon as
it has one.

**What it presents.**

- *Exposure* (milliseconds, Micro-Manager's unit) is a first-class parameter.
- *Every other Micro-Manager property* of the camera becomes a parameter in the group "Camera
  properties": a property with allowed values becomes a choice list, a number without allowed values a
  number, a free-text property a read-only entry, and a read-only property stays read-only. Pre-init
  properties are skipped (they belong in ``preInitProperties``). Micro-Manager properties carry no
  units, so none are shown. A value is always read back, so the settings show what the camera took.
- *Binning*: the allowed values of the ``Binning`` property are the supported binnings. The Settings panel
  lists the camera's current supported binnings and shows its current binning, also for a camera that was
  absent at start-up and adopted later (and the options of its list parameters).
- *Reconnect with several cameras.* After a hub reconnect no stream restarts until every camera has
  re-verified its frame format on the idle core; then all streams that were running restart together. A
  camera whose hardware now delivers another format reports it and stays stopped, the others resume. A
  restart the format guard refuses becomes that stream's fault (its next read raises it). If a camera never
  reports, starting or stopping any camera ends the wait; the next hub reconnect drops it.
- *Frame size and ROI*: ``fullShape`` is the full frame at the current binning. ``crop`` stores the ROI
  the adapter applied (adapters snap), in Micro-Manager's binned pixels. A change that may alter the
  frame (any property, binning, ROI) stops the stream, applies the change, re-reads the frame size and
  pixel type and restarts the stream. ``dtype`` follows the camera's ``PixelType`` before the first
  frame (8-bit cameras are ``uint8``).
- *Format changes next to a second camera.* While another camera streams, a change that would give this
  camera another frame size or pixel type than the running one is refused before anything is written
  (a ``PixelType`` write is known in advance; the message names the other camera and Micro-Manager pull
  request 936). A colour or otherwise unsupported ``PixelType`` is refused with the mono-only message
  whatever else streams. A property whose effect cannot be predicted is written only when the format can
  be read back afterwards, that is when this camera is MMCore's current one or no other camera streams;
  MMCore reports only its current camera and cannot switch it during a sequence, so for any other camera
  beside a running stream such a write is refused before anything is written (stop the other camera
  first). When it is allowed, the format is read back and the old value put back if the formats clash;
  the stream is restarted with the old format either way and never left stopped. The same holds for MMCore's *current* camera even when it is idle itself: the
  shared buffer is decoded with the current camera's format, so a stopped camera that is still current
  must keep the running stream's format. Binning is refused outright while another camera streams.

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
at startup and after every blocking move. A blocking move that takes longer than MMCore's device timeout
(``getTimeoutMs``, 5 s by default) raises in the UI call but does not stop the hardware; the tracked
position refreshes on the next read. A stage whose hub is down at startup starts at position
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
counts as connected. ``wavelength`` is ImSwitch2's own field (it colours the panel): the laser kind requires it, the
converter cannot know it and leaves it out, and ``validate-setup`` warns until you set it.


Flip mirror: ``micromanager.flip-mirror``
-----------------------------------------

A Micro-Manager device as an ImSwitch2 flip mirror. In ImSwitch2 a flip mirror is a **switch with N named
positions**: two for a mirror, more for a filter wheel, an objective turret or a multi-way light path, which
differ from a mirror only in the number of positions. The ``label`` is a State device with two or more
positions, or a Shutter presented as a two-state switch (closed is state 0, open is state 1). The Flip
Mirrors panel shows one row per device: a group of buttons for up to four positions, a drop-down list for
more (a ten-position wheel is one row, not ten buttons). A State device's state ``N`` is Micro-Manager's
state index ``N``.

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
     - Optional, State devices with exactly two positions only. The two Micro-Manager positions (state
       labels or indices) used as logical states 0 and 1; default: position 0 is state 0 and position 1
       is state 1. It only orders the two positions. A device with more positions exposes every position,
       so ``states`` is refused there; refused for a Shutter too (use ``invert``).

``invert``, ``initial_state`` and ``state_names`` are fields of the flip-mirror entry itself (as for the
Thorlabs MFF), not of ``managerProperties``:

- ``invert`` (default ``false``) swaps logical states 0 and 1 against the device. It is meaningful for
  two-state devices only and is **refused** (the setup does not load) for a device with more positions.
- ``initial_state`` (a state number ``0`` to ``N-1``, or ``null``): the state to move to at startup;
  ``null`` leaves the device where it is. A number the device does not have is refused at startup with
  the device's position count. A move that fails at startup is a connection error of this manager. A
  reconnect never replays it: it only observes where the device is.
- ``state_names`` names the positions, keyed ``"0"`` to ``"N-1"``. A state it does not name is called what
  Micro-Manager calls the position: its state label (a demo ``DWheel`` reads ``400DCLP``, ``Q505LP``,
  ...), or ``Closed`` / ``Open`` for a Shutter. A key beyond the device's positions is refused. The names
  describe the physical position, so for a two-state device they swap along with ``invert``. The widget
  reads them when the panel is built; a device that was not connected then names the states ``0``
  and ``1`` and the row is rebuilt with the device's own positions once it is connected.

A State device with fewer than two positions is refused at startup, with its position count in the message.
The number of positions is the device's current one: after a reconnect, or when a device that was absent at
startup is adopted, the Flip Mirrors panel rebuilds the row (a ten-position wheel replaces the two
placeholder buttons). ``invert``, ``initial_state`` and ``state_names`` are checked against the new count and a
clash is an error of the manager. Two flip mirrors can follow each other (the panel's
*Follow* column) only when they have the same number of positions; the master list of a row offers only
those, and the link is refused otherwise. A link that exists when one of its mirrors comes back with another
number of positions is removed (with a warning in the log), so the master cannot be commanded to a position its
follower does not have.

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
     - the hub, ``configFile`` as given on the command line, ``adapterPaths`` (only when ``--adapter-path`` was given)
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
   * - State device with two or more positions
     - ``flipMirrors``
     - ``micromanager.flip-mirror`` (filter wheels, turrets and light paths included)

The position count of a State device comes from the ``.cfg`` alone: the pre-init property
``Number of positions`` when there is one, else the ``Label`` lines. If the ``.cfg`` says neither, the
device is listed with "position count unknown" and you add the ``flipMirrors`` entry yourself if it has
two or more positions; a State device with fewer than two is listed as not written. The manager reads
the real count when it starts.
``availableWidgets`` holds Settings, View, Recording and Image, plus Positioner, Laser and FlipMirror
when the setup has entries for them. Entries are keyed by the Micro-Manager label.

What lands in the report
------------------------

Every device that was **not** written gets one line with its label, adapter library and device name, its
type and the reason (a State device with fewer than two positions or an unknown count, Hub, AutoFocus,
unresolved devices ...), and every value the converter could not derive is a ``TODO`` line. A value it cannot derive
is never invented, and the JSON never carries comments (``validate-setup`` reads it with ``json.load``):

- ``cameraPixelSizeUm`` is **omitted**. When the ``.cfg`` defines pixel-size configurations the report
  says that Micro-Manager supplies the value; otherwise it is a TODO.
- The axis name of a Stage that is not the focus device is the string ``"TODO"``; it loads, and no scan
  or panel mistakes it for an axis. Choose a name.
- A light's ``wavelength`` is **omitted** (the loader reads a missing one as ``None``). ``validate-setup``
  shows one warning per light until you set it, which is the same TODO.

The demo configuration of the test adapters, ``MMConfig_demo.cfg`` (13 devices), converts to the hub, the
Camera, the Z and XY stages, two lights (the two shutters) and six switches (the filter wheels Dichroic,
Emission and Excitation, the Objective turret, the light Path and the LED selector); the hub device
``DHub`` and the ``Autofocus`` are reported and not written. An excerpt of its report:

.. code-block:: text

   Written to the setup file: hub rs232devices['MMCore'] + 11 device(s)
     detectors['Camera']  DemoCamera/DCam  Camera  -> micromanager.camera
     flipMirrors['Dichroic']  DemoCamera/DWheel  State, 10 positions (10 Label line(s))  -> micromanager.flip-mirror
     ... Emission, Excitation, Objective (6), Path (3), LED (10) ...
     positioners['Z']  DemoCamera/DStage  Stage  -> micromanager.stage, axes ['Z']
     positioners['XY']  DemoCamera/DXYStage  XYStage  -> micromanager.stage, axes ['X', 'Y']
     lasers['White Light Shutter']  DemoCamera/DShutter  Shutter  -> micromanager.light, binary light
     lasers['LED Shutter']  Utilities/State Device Shutter  Shutter  -> micromanager.light, binary light

   To do before the first start (2)
     TODO lasers['White Light Shutter']: wavelength is not written ...
     TODO lasers['LED Shutter']: wavelength is not written ...

   Not written (2)
     'DHub'  DemoCamera/DHub  Hub  not written: ...
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
``LED`` light on the demo ``LED Shutter``, a ``WhiteLight`` flip mirror on the demo ``White Light
Shutter`` and a ``Dichroic`` flip mirror on the demo ten-position filter wheel of the same name.

1. Copy the template into ImSwitch2's setup folder (``~/ImSwitchConfig/imcontrol_setups``, or
   ``Documents\ImSwitchConfig\imcontrol_setups`` on Windows).
2. Start ImSwitch2 and choose the setup: on the first start the setup chooser opens by itself, afterwards
   use **Hardware > Pick hardware setup...** (ImSwitch2 restarts).
3. You should see the Settings panel with the camera's Exposure and the "Camera properties" group; **Live
   view** showing the DemoCamera's test image; the Positioner panel with ``Z`` and ``XY`` (``XY`` starts
   unreferenced and its Reference action homes it; the demo ``Z`` has nothing to reference); the Laser
   panel with an on/off switch for ``LED``; the Flip Mirrors panel with ``WhiteLight`` and its buttons
   *Closed* and *Open* (the demo configuration leaves the shutter open) and ``Dichroic`` as a drop-down of its
   ten positions (``400DCLP``, ``Q505LP``, ...); and the Recording panel, which
   records a chosen number of frames to HDF5, TIFF or Zarr.

The template carries one key besides the device sections, ``availableWidgets``: ImControl builds no
panel that is not listed.


In the config editor
====================

Hardware > Edit hardware configuration... (Config Studio) knows the five managers: they are in the device
palette by category, their forms come from the plugin's schemas, and the plugin's ``demo_scope.json`` is
offered under *Plugin Templates*. A template opens without complaints about its other devices (the camera of
the template names the hub that the same template declares).

- **Import Micro-Manager...** (toolbar, and under the setup list) asks for a ``.cfg``, runs the converter
  above on it and merges the result into the open config: the hub into ``rs232devices``, each device into its
  own section, and the matching entries into ``availableWidgets``. A name that is already taken is suffixed
  (``Camera_1``) and the new devices follow their renamed hub, so nothing is overwritten. A window then shows
  the conversion report: what was written, the ``TODO`` lines, and the devices that were *not* written. The
  hub's ``configFile`` is the path you chose. Where the adapters are not installed on the machine, device
  types cannot be read: only the hub is imported and the window says so. The entry is greyed out, with the
  package name in its tooltip, when ``imswitch-device-micromanager`` is not installed.
- **Label picker.** In the form of a bridge device, ``label`` is a drop-down of the device labels in the hub's
  ``.cfg`` (read from ``configFile`` of the ``rs232devices`` entry named in ``rs232device``, relative names
  resolved like MMCore does), narrowed to the types the manager accepts when the adapters can classify them.
  You can still type any text.
- **Hub picker.** ``rs232device`` lists the ``micromanager.core`` entries of the config. The labels are read
  when a device is selected and again when you pick another hub in the form; with no hub, no ``configFile`` or
  an unreadable file the label field is plain text with a hint saying why.

The editor shows the hub in its *RS232 devices* section: Micro-Manager's core takes the slot that ImSwitch2
uses for shared communication resources, and the name of the section is a label only.


Known limits
============

- **Python 3.11 or newer**, and so no installer build yet (see *Install*).
- **One buffer: equal frame sizes.** MMCore has a single circular buffer for all cameras, clears it when
  any sequence starts or a ROI is set, and does not check that two cameras deliver equally sized frames.
  The hub therefore owns the buffer and lets a second camera start streaming only with the frame shape
  and pixel type of the one already streaming; a different size is refused with an explanation. Micro-Manager's work on
  proper multi-camera buffers (pull request 936) is still open.
- **No second camera starts, and no ROI changes, while another camera records.** Starting a sequence or
  setting an ROI clears MMCore's one buffer, and a frame the recording camera produces at that moment
  would vanish without a trace. So while a camera is running and a recording is reading its frames, a
  start (live view included) or a ROI change of any other camera is refused with an explanation; stop the
  recording first, or start the cameras together before the recording begins. Live view of two cameras
  without a recording is not affected.
- **Colour cameras are deferred.** Cameras with colour frames are refused at start.
- **Lights are not scan-gated.** There is no TTL path from a bridged light, and no modulation; stages have
  no analog scan line. Scanning stays with ImSwitch2's native waveform generation.
- **Windows adapter DLLs.** pymmcore-plus adds the adapter folder to ``PATH`` but not to the DLL search
  directories. A vendor adapter whose SDK DLLs live in another folder may fail to load with
  ``adapterPaths`` alone. This has not been measured on a rig yet.
- **Hardware is not verified.** Everything on this page is verified against the real MMCore with the
  Micro-Manager demo adapters only; no rig has run the bridge yet.


Troubleshooting
===============

A hub or device that cannot start does not stop ImSwitch2: it shows as an error in **Hardware > Hardware
status** with Micro-Manager's own message, and the log (**View > Log**) has the same text. The hub also logs
at start which adapter folder it chose and the MMCore versions. These are the messages you are likely to meet.

**"no Micro-Manager device adapters found: set adapterPaths, set MICROMANAGER_PATH, run ``mmcore install`` ..."**
   The hub found no adapter folder. It looks in the hub's ``adapterPaths``, then the ``MICROMANAGER_PATH``
   environment variable, then wherever ``pymmcore-plus`` finds an install (``mmcore list`` shows what it
   knows). Run ``mmcore install`` (Windows and Intel macOS), point ``MICROMANAGER_PATH`` or ``adapterPaths``
   at the folder of an existing Micro-Manager install, or ``pip install mm-test-adapters`` for the demo
   devices. "adapter folder(s) do not exist" means one of those paths is wrong; "configuration file not
   found" names a ``configFile`` that is not there.

**A device fails to load with an MMCore message about the device interface version.**
   Every adapter is built for one device interface version, and the ``pymmcore`` in your environment speaks
   one version. They must be the same. ``mmcore --version`` prints the ``pymmcore`` side ("Device API
   version ..."); the same number is the fourth component of the ``pymmcore`` version (``12.5.1.75`` is
   version 75). ``mmcore install`` downloads the matching nightly. If you point ``adapterPaths`` at an
   existing Micro-Manager install, it has to be a build of that interface version, or you have to change
   the ``pymmcore`` version to match it. The exact wording of the message is MMCore's.

**"camera 'X' cannot start streaming or change its ROI while camera 'Y' is recording"**
   MMCore has one frame buffer for all cameras, and starting a camera or changing a ROI clears it, which
   would silently drop frames of the recording. Stop the recording of ``Y`` first, or start both cameras
   before the recording begins. A related message, "would deliver ... frames while camera 'Y' is streaming",
   means the two cameras would deliver different frame sizes or pixel types: give them the same ROI,
   binning and pixel type (see *Known limits*).

**"Unsupported device command" when you press Reference.**
   That is Micro-Manager's own reply: the adapter has no home command. Homing is only ever tried from the
   Reference action, and only for a stage with ``homeable: true``. Remove ``homeable`` (or set it to
   ``false``) for this stage.

**"Micro-Manager has no camera device 'X'. Did you mean 'Y'? Loaded camera devices: [...]"**
   The ``label`` of the entry is not a loaded device of that type (``stage``, ``light`` and ``flip mirror``
   entries say the same with their own device type). Labels are case sensitive; in system-config mode they
   come from the ``.cfg`` the hub loaded, so a label added to Micro-Manager later is only there after the
   ``.cfg`` is saved and the hub reconnects. The label picker of the config editor lists the labels of the
   ``.cfg``.

**"... delivers colour frames ..."**
   The camera is a colour camera (RGB pixel type) and the bridge carries mono cameras only. Give the camera
   an 8-bit or 16-bit mono pixel type, as a pre-init property or in the ``.cfg``.

**pip refuses to install the plugin on Python 3.10.**
   ``pymmcore`` needs Python 3.11 or newer, so the plugin does too, and the standalone installers bundle
   3.10. Install ImSwitch2 from source into a 3.11 or newer environment (see *Install*).

**``validate-setup`` warns "'wavelength' is a required property" after a conversion.**
   Not an error: Micro-Manager does not know the wavelength of a light, so the converter leaves it out
   instead of inventing one. Set ``wavelength`` (nanometres) in each ``lasers`` entry.

**On Windows, a vendor adapter fails to load although its library is in the adapter folder.**
   Vendor adapters load the vendor's SDK DLLs, and these have to be found too. ``pymmcore-plus`` adds the
   adapter folder to ``PATH``; if the SDK's DLLs are in another folder, adding that folder to ``PATH``
   before starting ImSwitch2 is the first thing to try. This has not been measured on a rig yet.


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
until the plugin is installed into the environment. The end-to-end module shares one app between its
tests and skips itself under ``pytest-xdist`` and ``pytest-randomly``.


Validating against a native manager
===================================

When the same camera can be driven by an ImSwitch2 manager and by a Micro-Manager adapter (a Hamamatsu
camera through ``HamamatsuManager`` and ``HamamatsuHam``, for example), the plugin ships a comparison kit:
a setup template that carries both entries (one of them under ``disabledDevices``, because the camera's driver
allows one open handle), and a headless tool that measures one detector the same way whichever driver runs
it and diffs two reports. The rig rows, what to write down and the property-name mapping are in
``docs/design/plans/micromanager-bridge-rig-test.md``. In short, once per variant and then the diff:

.. code-block:: bash

   python examples/plugins/imswitch-device-micromanager/tools/compare_cameras.py measure SETUP.json --detector NAME --out report.json
   python examples/plugins/imswitch-device-micromanager/tools/compare_cameras.py diff native.json bridge.json

``measure`` boots the real application on the setup (without moving ``~/ImSwitchConfig``), sets each
exposure through the Settings controller, counts live-view frames, records a fixed number of frames through
the real recording manager and reads them back; ``diff`` prints every quantity side by side with PASS or
CHECK. The template (``hamamatsu_native_vs_bridge.json``) needs a camera and is not offered by the config
editor's template list.
