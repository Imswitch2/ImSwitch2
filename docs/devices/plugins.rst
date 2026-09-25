**************
Device plugins
**************

ImSwitch2 can load device managers from **external plugin packages**. A plugin
is an ordinary Python package that advertises one or more device managers
through package metadata and a JSON manifest. ImSwitch discovers installed
plugins, and imports a plugin's manager code only when a configured device
actually selects it.

This lets new device support live and evolve **outside** the ImSwitch core
repository, while existing in-tree managers and setup files keep working
unchanged.

.. contents::
   :local:
   :depth: 2


How resolution works
====================

When a setup file declares a device, ImSwitch resolves its ``managerName`` to a
manager class in this order:

1. The **device plugin registry** — built-in managers plus contributions
   discovered from installed plugins. A match may be by the contribution ``id``
   (e.g. ``zhinst.lockin-demod``) or by a compatibility alias (e.g. a legacy
   class name).
2. The **legacy internal import path** inside
   ``imswitch.imcontrol.model.managers`` — so existing setup files that name an
   in-tree manager class continue to work.

If neither resolves a registry-backed device kind, ImSwitch raises an actionable
error listing the installed managers for that kind.

The seven device kinds loaded through ``MultiManager`` are
``detector``, ``laser``, ``positioner``, ``rotator``, ``rs232``,
``flip_mirror`` and ``slm``. (``stand`` and ``pulse_generator`` use bespoke
loaders and are not plugin-resolved yet.)


Using an installed plugin
=========================

Install the plugin into the same Python environment as ImSwitch, then reference
its manager ``id`` in your setup file exactly like a built-in manager:

.. code-block:: json

   {
     "detectors": {
       "ZI Lock-in": {
         "managerName": "zhinst.lockin-demod",
         "managerProperties": { "useMock": true },
         "forAcquisition": true
       }
     }
   }

Both namespaced plugin ids (``zhinst.lockin-demod``) and legacy class names
(``HamamatsuManager``) are valid ``managerName`` values.


Diagnostics CLI
===============

A small command-line tool lists installed contributions and validates a setup
file against them:

.. code-block:: bash

   # List every registered manager (built-ins + installed plugins)
   python -m imswitch.imcontrol.model.plugins list
   python -m imswitch.imcontrol.model.plugins list --kind detector

   # Show one contribution's details
   python -m imswitch.imcontrol.model.plugins inspect zhinst.lockin-demod

   # Check that every device in a setup file resolves, and (when a plugin
   # ships a schema) validate its managerProperties
   python -m imswitch.imcontrol.model.plugins validate-setup my_setup.json

``validate-setup`` never imports hardware: it resolves names through the
registry and verifies legacy modules exist via ``importlib.util.find_spec``.


Writing a plugin
================

Public API
----------

Import **only** from the stable public surface ``imswitch.pluginapi`` — never
from deep internal ``imswitch.imcontrol...`` paths:

.. code-block:: python

   from imswitch.pluginapi import (
       DetectorManager, DetectorNumberParameter, DetectorListParameter,
       DetectorAction, LaserManager, PositionerManager, RotatorManager,
       DeviceInfo, DetectorInfo, LaserInfo, PositionerInfo, RS232Info,
   )

A manager subclasses the matching base class and implements its abstract
methods. ImSwitch constructs managers as
``ManagerClass(deviceInfo, name, **lowLevelManagers)`` — the same contract as
in-tree managers (see :doc:`../adding-device-support`).

Manifest
--------

Ship a JSON manifest named ``imswitch.json`` in your package and advertise it
through the ``imswitch.manifest`` entry point. The manifest value is a
``<package>:<resource>`` pair, **not** a ``module:attr`` import target.

.. code-block:: toml

   [project.entry-points."imswitch.manifest"]
   my-plugin = "imswitch_my_plugin:imswitch.json"

   [tool.setuptools.package-data]
   imswitch_my_plugin = ["imswitch.json", "schemas/*.schema.json", "setup_templates/*.json"]

.. code-block:: json

   {
     "name": "imswitch-my-plugin",
     "display_name": "My Device Support",
     "schema_version": "0.1",
     "imswitch_min_version": "0.1",
     "license": "GPL-3.0-or-later",
     "contributions": {
       "device_managers": [
         {
           "id": "vendor.device",
           "kind": "detector",
           "display_name": "Vendor Camera",
           "python_name": "imswitch_my_plugin.detectors:VendorCameraManager",
           "mock_python_name": "imswitch_my_plugin.detectors:MockVendorCameraManager",
           "manager_name_aliases": ["VendorCameraManager"],
           "manager_properties_schema": "schemas/vendor_camera.schema.json",
           "setup_templates": ["setup_templates/vendor_camera.json"]
         }
       ]
     }
   }

Manifests are JSON (parsed with the standard library — no PyYAML). Do **not**
add a ``Framework :: ImSwitch`` trove classifier: PyPI rejects unregistered
``Framework ::`` values. Use the ``imswitch-plugin`` keyword for discoverability.

Required ``device_managers`` fields are ``id``, ``kind``, ``display_name`` and
``python_name``. ``mock_python_name``, ``manager_name_aliases``,
``manager_properties_schema``, ``setup_templates``, ``docs_url`` and
``supported_platforms`` are optional.

Generating the managerProperties schema
---------------------------------------

You do not have to write ``manager_properties_schema`` by hand. The same
extractor that produces the core managers' schemas
(``imswitch/imcontrol/model/configeditor/schemas/``) runs over an installed
plugin, from an ImSwitch checkout:

.. code-block:: bash

   python tools/extract_manager_schemas.py --package imswitch_my_plugin --report --properties
   python tools/extract_manager_schemas.py --package imswitch_my_plugin --write
   python tools/extract_manager_schemas.py --package imswitch_my_plugin --check   # in the plugin's CI

The package is located through the import system's finders and **never
imported** -- neither it nor, for a dotted name such as
``hardware_vendor.imswitch_plugin``, its parent packages run their
``__init__``, so vendor SDK imports there do not run. For every
``device_managers`` entry of ``imswitch.json`` the tool finds the class
``python_name`` names in the package's own source -- by module as well as by
name, so two ``CameraManager`` classes in two modules get two schemas, each
inheriting from the base class its own module imports; a contribution whose
``python_name`` points outside the package (a class the plugin takes from
its driver package) is reported as unresolved and gets no schema -- reads how
it uses
``managerProperties`` (``props["key"]``, ``props.get("key", default)``,
``"key" in props``, guards such as ``try/except KeyError``, helper methods
handed the dict, camelCase/snake_case alias pairs) and writes, into the
package:

* ``schemas/managers/<id>.json`` -- a JSON Schema (Draft 2020-12): a key read
  without a guard is ``required``; a default or an ``int()``/``float()`` call
  gives the editor a widget preference (``x-imswitch-kind``); a validation
  ``type`` is emitted only where the code proves one (``Path(...)``,
  ``.items()``, a nested subscript);
* ``schemas/fixtures/<id>.json`` -- a synthetic device the schema accepts;
* ``schemas/index.json`` -- the generator version, each manager's property
  counts and the coverage totals.

Point each contribution at its file and ship the directory as package data:

.. code-block:: json

   "manager_properties_schema": "schemas/managers/vendor.device.json"

.. code-block:: toml

   [tool.setuptools.package-data]
   imswitch_my_plugin = ["imswitch.json", "schemas/**/*.json", "setup_templates/*.json"]

ImSwitch then validates setup files against it (``validate-setup``) and the
config editor shows a typed field per property, without a hand-written
template. Where the code cannot prove a constraint you know (an accepted
set of values, a numeric range), put a hand-written
``schemas/overrides/<id>.json`` beside the generated files; it is merged
last and never overwritten. See
``imswitch/imcontrol/model/configeditor/schemas/overrides/README.md`` for
the format, and ``docs/design/plans/config-editor-schema-extraction.md``
for the extraction rules and what they can and cannot see (a dict handed
whole to a vendor driver is an open pass-through, a key computed at runtime
is reported as an unresolved read rather than guessed).

Mock selection
--------------

ImSwitch never substitutes a mock silently. A plugin may declare
``mock_python_name``; the mock is loaded only when mock selection is explicit
(a global mock/dev mode, or a per-device ``managerProperties.useMockOnFailure``
that the manager itself honors). For safety-critical devices, prefer an explicit
opt-in over silent fallback.


Examples and template
=====================

* **Plugin template** — a minimal, hardware-free demo detector and laser, with a
  manifest, schema, setup templates, tests and CI:
  ``imswitch-plugin-template`` (a standalone, copy-to-start repository).
* **First real plugin** — a Zurich Instruments lock-in detector, bundled in this
  repository under ``examples/plugins/imswitch-zhinst-devices/``. It exercises
  optional hardware extras (``zhinst-toolkit``), lazy hardware imports, a mock
  mode, a managerProperties schema and a setup template.
* **Extractions** — ``examples/plugins/imswitch-device-thorlabs/`` holds
  in-tree managers moved out of the core tree into a plugin, each keeping its
  legacy class name as an alias so existing setups resolve to it unchanged.
  It now bundles two device *kinds* from one vendor: the Thorlabs TSI camera
  (``ThorCamTSIManager``, detector) and the Kinesis MLS203 XY stage
  (``KinesisStageManager``, positioner) — showing that a single plugin can
  contribute managers across kinds. During the transition the in-tree copies
  stay; the install hints become active once they are removed.

These are verified to be discovered and loaded by the registry with no changes
to the ImSwitch core.


Extracting an in-tree manager into a plugin
===========================================

Existing in-tree managers can be moved into plugin packages gradually. New
device support should default to a plugin; established in-tree devices stay until
there is a published plugin and a migration path. Extract non-safety-critical
devices (cameras/detectors) before laser/DAQ/stage paths, which need extra
review.

Checklist for moving a manager out of the core tree:

#. **Package it.** Start from the plugin template; move the manager file and its
   ``managerProperties`` schema into the package. Import only from
   ``imswitch.pluginapi``; keep vendor SDK imports lazy.
#. **Keep the name stable.** Use the manager's existing setup ``managerName`` as
   the contribution ``id`` *or* as a ``manager_name_aliases`` entry, so existing
   setup files resolve to the plugin unchanged.
#. **Register an install hint.** Add the old ``(kind, managerName)`` (id, legacy
   class name, and aliases) to
   ``imswitch.imcontrol.model.plugins.external.KNOWN_EXTERNAL_MANAGERS`` pointing
   at the new package. ImSwitch then tells users to ``pip install`` it instead of
   raising an opaque import error.
#. **Remove from core only after the plugin is published**, and keep the install
   hint for at least two minor releases. Until then, the in-tree manager and the
   plugin can coexist (built-ins win on id collision).
#. **Verify.** The plugin installs and is discovered; ``validate-setup`` passes
   for an existing setup; the core still boots and its tests pass.

The install hint is what turns a removed manager into an actionable error:

.. code-block:: text

   Could not resolve detector manager 'AcmeCamManager'.
   Installed detector managers:
     - AVManager (imswitch-core)
     ...
   'AcmeCamManager' is provided by the external plugin package 'imswitch-device-acme'.
     Install it with: pip install imswitch-device-acme


Compatibility
=============

* Manifest ``schema_version`` (currently ``"0.1"``) and
  ``imswitch_min_version`` gate plugin/host compatibility.
* The ``imswitch.pluginapi`` import surface is the compatibility promise for
  plugin authors; deep internal paths may change without notice.
* Built-in legacy ``managerName`` class names remain valid until a documented
  removal release.

The full design and rationale live in
``docs/design/DEVICE_PLUGINS.md``.
