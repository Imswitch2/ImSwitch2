*****************
State persistence
*****************

How ImSwitch2 remembers widget settings between sessions, exports and imports
them, and applies named setup modes.  One contract serves all three:
``StatefulComponentMixin``.  This page is for developers adding or changing a
component; for the user's view see :doc:`working-in-imswitch2`.  The overview
is in :ref:`architecture's Widget state persistence section
<widget-state-persistence>`.

.. contents::
   :local:
   :depth: 2


The contract
============

A controller that wants its settings remembered derives from
``StatefulComponentMixin`` (``imcontrol/controller/basecontrollers.py``) and
implements four methods.  The state it returns must be JSON-serialisable:
``WidgetStatePersistence.snapshotComponent`` checks that and refuses
(logs an error, returns ``None``) when it is not.

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Method
     - Contract
   * - ``getComponentState()``
     - A JSON-serialisable ``dict`` of the component's settings.  May raise
       for an unrecoverable failure; the registry logs it and skips the
       component.
   * - ``applyComponentState(state, *, applyMode)``
     - Restore a snapshot and return a list of warning strings.  Recoverable
       problems (a device missing from this setup, an unknown key, a clamped
       value) become warnings, not exceptions.  The component enforces the
       apply-mode policy below.
   * - ``describeComponentState(state)``
     - A list of human-readable lines for the setup-mode inspector and the
       update preview.  The setup-mode diff is a set difference of these
       lines.
   * - ``getComponentStateHazards(state, *, applyMode, context=None)``
     - A list of hazard records, empty when there are none.  Each record is a
       dict with ``kind``, ``severity``, ``message`` and ``details`` (the laser
       controller reports ``kind: "high_laser_power"``, ``severity:
       "warning"``, and ``details`` holding the laser name, value, units and
       threshold).  The component finds the hazard; the caller owns the policy
       (threshold, confirmation dialog, suppression).

Class attributes describe the component:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - Attribute
     - Meaning
   * - ``componentName``
     - The canonical name, e.g. ``'Laser'``.
   * - ``legacyStateNames``
     - Older names the component was saved under (see `Legacy-name aliases`_).
   * - ``stateSchemaVersion``
     - Integer, default 1, written into the saved file's metadata.
   * - ``setupModeDisplayName``, ``setupModeCategory``
     - Presentation of the component in the setup-mode dialogs.
   * - ``setupModeApplyPriority``
     - Order in which a setup mode applies its components; lower first, ties
       keep the mode file's order.  Use a band of ``SetupModeApplyPriority``:
       ``DETECTOR_SETTINGS`` 100, ``SCAN`` 200, ``MULTI_SPATIAL_LIGHT_MODULATOR``
       300, ``SPATIAL_LIGHT_MODULATOR`` 310, ``MICROSCOPE_STAND`` 400,
       ``BEAM_PATH`` 410, ``EXCITATION`` 500, ``DEFAULT`` 1000.
   * - ``setupModeHardwareCritical``
     - ``True`` when applying the component changes safety-relevant hardware.
       Smart-microscopy mode switching reads this flag instead of a list of
       component names (``isSetupModeHardwareCritical``).

``ComponentStateApplyMode`` is defined in
``imcommon/model/state_contracts.py`` (``imcontrol.model.state_contracts`` and
``basecontrollers`` re-export it).  Its values serialise as
``"startup_restore"`` and ``"setup_mode_apply"``.  The same module defines
``RestoreWarning``, a ``str`` subclass carrying a ``critical`` flag: a plain
``str`` counts as critical, and ``RestoreWarning(text, critical=False)`` marks
a routine warning ("that detector is not in this setup") that is logged
instead of shown in a dialog.


Registering a component
=======================

A controller registers itself in its constructor:

.. code-block:: python

    getWidgetStatePersistence().register('Laser', self)

``getWidgetStatePersistence()`` (``imcommon/model/WidgetStatePersistence.py``,
re-exported by ``imcontrol.model``) returns the process-wide registry.
``register`` raises ``ValueError`` unless the object has callable
``getComponentState`` and ``applyComponentState``.  The name is resolved to its
canonical name through the alias table, and a second registration under the
same canonical name replaces the first (this is how whichever scan controller
the setup uses ends up as ``Scan``).  Dock layouts are registered by small
adapter objects rather than controllers: ``ImConMainController`` registers
``GuiLayout`` and ImProcess registers ``ImProcessGuiLayout`` and
``ImProcessROIManager``.

Setup modes offer every component of the module that derives from
``StatefulComponentMixin`` (``SetupModeController._getModeAwareControllers``),
except ``GuiLayout``; each mode lists in ``includedComponents`` the subset it
snapshots and applies.  Components not in that list are left untouched.


Apply modes and hardware-active exclusions
==========================================

``applyMode`` tells the component why it is being restored.

``STARTUP_RESTORE``
    Passive restore at launch or when importing a file.  The component sets
    values and settings but must not emit light, move a stage, start
    acquisition, start a scan or switch a beam path.

``SETUP_MODE_APPLY``
    The operator explicitly applied a named setup mode, so the component may
    activate hardware that the mode saved.  Before applying, the caller asks
    for hazards (``SetupModeController.getModeHazards``) and can show a
    confirmation.

The registry does not enforce this; each component does.  What the shipped
components do differently between the two modes:

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * - Component
     - ``STARTUP_RESTORE``
     - ``SETUP_MODE_APPLY``
   * - ``Laser``
     - Restores power values and modulation settings and the selected preset
       label; does not enable lasers and warns "Laser enable states not
       restored in startup mode."
     - Also enables or disables lasers per the saved state (a non-binary laser
       whose saved power is not above zero is left off).
   * - ``FlipMirror``
     - Moves nothing and sets no links; warns when a mirror's current
       position differs from the saved one.
     - Removes the existing links, moves the mirrors, restores links.
   * - ``LeicaStand``
     - Does not switch; warns when the saved mode differs from the current one.
     - Switches to the saved FLUO or CS mode.
   * - ``SLMs``
     - Selects the saved configuration in the widget but does not push it;
       warns that it was not loaded.
     - Loads the configuration, which pushes the pattern to the SLM.

Other components behave the same in both modes and never activate hardware:
``Settings`` (ROI, binning, frame mode, editable detector parameters; never
starts acquisition), ``Positioner`` (step sizes and widget options; never
moves a stage), ``Recording`` (formats and modes; never starts a recording and
never restores the output folder), ``Rotator``, ``BeadRec``, ``Image`` (layer visibility), the
``Scan`` controller and the TriggerScope panels (parameters, never a run) and
``GuiLayout`` (dock and splitter layout).


What is saved
=============

Only what the component returns from ``getComponentState``.  The rules the
shipped components follow, and that new ones should:

* **Settings, not hardware readings.**  ``Settings`` saves ROI, binning, frame
  mode and the *editable* detector parameters of detectors with
  ``forAcquisition`` set.  Read-only parameters such as the real exposure time
  are camera readings and are refreshed from hardware on restore.  Values come
  from the detector, not from widget fields that were typed but never applied.
* **Setup-file-owned parameters are not restored.**  A detector manager
  declares ``configOwnedParameters`` (the camera pixel size when
  ``cameraPixelSizeUm`` is set in the setup file).  ``Settings`` neither saves
  nor restores them, so editing the setup file takes effect without deleting
  the state file; a saved value from an older build is ignored and logged.
* **No positions or acquisition state.**  ``Positioner`` saves step sizes and
  widget options, not positions or speeds; nothing saves a running
  acquisition.


Files and flow
==============

All paths are below the user's ``ImSwitchConfig`` folder.

.. list-table::
   :header-rows: 1
   :widths: 28 36 36

   * - Artifact
     - Path
     - Content
   * - Startup state
     - ``imcontrol_widget_states/<key>/default.json``
     - ``{"_metadata": {"controller_name", "canonical_name",
       "schema_version"}, "state": {...}}``, one file per registered
       component; ``<key>`` is the name it registered under.
   * - Export bundle
     - a path chosen in the menu
     - One JSON file mapping each registration key to the same
       ``_metadata`` / ``state`` pair.
   * - Setup mode
     - ``imcontrol_setup_modes/<name>.json``
     - ``schemaVersion``, ``name``, ``description``, ``shortcut``,
       ``createdAt``, ``updatedAt``, ``includedComponents`` and ``state``
       (component name to state).
   * - Setup-mode warning settings
     - ``imcontrol_setup_mode_settings.json``
     - The laser power threshold and which warnings are suppressed.

``ImConMainController`` loads the ``default`` state (``STARTUP_RESTORE``) once
all controllers exist and the ``GuiLayout`` adapter is registered.  Warnings
that are critical are shown in one dialog; the others are only logged.  On
close it asks whether to save the current state as the default for the next
start, and answers *No* when nobody replies for a while, so an unattended
shutdown does not become tomorrow's defaults.  **Save Widget States** and
**Load Widget States** export and import the bundle (imports apply with
``STARTUP_RESTORE`` and skip keys that are not registered).

A saved file whose ``schema_version`` differs from the component's
``stateSchemaVersion`` is still applied, with a logged warning, so
``applyComponentState`` must tolerate missing and renamed keys.  Bump
``stateSchemaVersion`` only when the payload changes shape.


Legacy-name aliases
===================

Older builds saved components under their controller class names, and those
files and bundles must still load.  ``WidgetStatePersistence.CANONICAL_ALIASES``
maps each canonical name to its older keys, for example ``Laser`` to
``LaserController`` and ``Scan`` to ``ScanController``,
``ScanControllerAdvanced`` and the class names of retired scan controllers.
``register``, ``loadWidgetState``, ``load_from_file`` and the other entry
points resolve any of these to the canonical name.  Reading tries the path of
the name asked for first, then the canonical name's path.  Keys that are not in
the table (``GuiLayout``, the ImProcess keys) resolve to themselves.  Do not
remove an alias without a migration for the files that use it.


Adding a component
==================

#. Derive from ``StatefulComponentMixin`` (before ``ImConWidgetController``)
   and set ``componentName``; add ``legacyStateNames``, the setup-mode
   attributes and ``setupModeHardwareCritical`` as needed.
#. Implement the four methods; return warnings instead of raising.
#. In ``applyComponentState``, branch on ``applyMode`` for anything that emits,
   moves or starts, and return a warning for what was skipped.
#. Register the instance in the constructor.  A new canonical name that has
   older keys also needs an entry in ``CANONICAL_ALIASES``.
#. Test both modes: that a ``STARTUP_RESTORE`` of a state that saved something
   active (an enabled laser, a mirror position) leaves the hardware untouched,
   and that the state passes ``json.dumps``.
