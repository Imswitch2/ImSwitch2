***********************************************
ImProcess results in napari plugins (endpoints)
***********************************************

ImProcess's viewer *is* a napari viewer, embedded in the ImProcess window.
That makes any installed napari plugin a possible **endpoint** for a result:
a reconstruction, a filtered stack, a label mask or a localization table can
be handed to the plugin and the plugin does what it does. This page says
exactly what is supported, how a result is handed over, how (and whether)
something comes back, and how to add an adapter for a plugin of your own.

The direction is deliberate. **ImProcess → plugin is the street.** ImProcess
provides layers or a file; it never drives the plugin, never reads its
widgets, and takes a layer back only when you explicitly ask for it.

Scope: what "any installed plugin" means here
=============================================

napari plugins declare *contributions*. Two kinds are endpoints; the rest are
not, and no amount of adapter code makes them one.

.. list-table::
   :header-rows: 1
   :widths: 28 18 54

   * - npe2 contribution
     - Lane
     - Support
   * - ``widgets`` (dock widget, magicgui)
     - ``dock``
     - Yes. Discovered on the installed plugins and offered as **unverified**;
       an adapter that declares the result kinds makes it **verified**.
   * - ``readers``
     - ``reader`` / ``detached``
     - Only through a **verified adapter** naming which of ImProcess's export
       formats the reader understands. A filename pattern match is not proof
       that a reader understands a file.
   * - ``writers``
     - export format
     - Offered as extra formats when saving a session's layers.
   * - commands without a widget, sample data, themes, menu entries
     - —
     - Not endpoints. Plugins built on ``napari-tools-menu`` (for example
       ``napari-skimage-regionprops``) register their entries in napari's own
       *Tools* menu at import time; they may still appear there in the
       embedded viewer, but ImProcess does not manage them.

Opening a dock widget hands the plugin ImProcess's layers. It does not run
anything: you operate the plugin as you would in napari.

Sending a result
================

**Plugins → napari plugins → Send current result to → …** lists the endpoints
that accept the *current* result. The list depends on the result's kind and
on what is installed; an endpoint whose plugin is missing is shown disabled
with the ``pip install`` hint.

What the plugin receives, by result kind:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Result kind
     - napari layers
   * - image, composite, rgb
     - Image layer(s): the result's display layers when it has them,
       otherwise the primary array. Scale, units (``micrometer`` /
       ``nanometer``), contrast limits and a portable colormap come along.
       Layer metadata carries the result uid, the axis labels and the
       provenance graph.
   * - labels
     - One Labels layer (integer values).
   * - localization
     - One **Points** layer built from the localization *table*, not from
       the histogram preview. Coordinates are in pixel-index units of the
       source grid (``nm / pixel_size_nm``) and the layer ``scale`` restores
       nanometres, so the points land on the same world coordinates as an
       image of the same field. Axis order is napari's: ``(y, x)`` in 2D,
       ``(z, y, x)`` in 3D, where ``z`` is divided by ``z_step_nm``. When a 3D
       table has no axial step the lateral pixel size is used and the layer
       metadata carries ``z_scale_assumed: true``. Every other table column
       (frame, photons, sigma, uncertainties) becomes a point property. The
       preview histogram is added as a context layer only when the endpoint
       asks for it, translated to the table's minimum coordinate because
       that is where the preview was binned.
   * - table, curve
     - No layer form. These results reach no endpoint; save them to a file
       instead.

The three lanes
---------------

``dock``
   The layers are added to ImProcess's own viewer and the plugin's dock
   widget is opened in the same window. Nothing is written to disk.

``reader``
   The result is exported to a temporary file in the format the adapter
   names, then opened *in ImProcess's viewer* with ``viewer.open(path,
   plugin=<reader>)``. Export runs off the GUI thread; opening runs on it.

``detached``
   Same export, opened in a separate napari window. For plugins that insist
   on owning a top-level napari window.

Sessions
--------

Every send creates an **endpoint session** that owns what the send created:
the exact layer objects it added, the dock widget, the exported files and
their temporary directory, and the detached viewer if any. A session closes
when you close it from **Plugins → napari plugins → Endpoint sessions**,
when every layer it added has been removed from the viewer, when its
detached window is closed, or when ImProcess exits. Temporary files live
until then, so a reader that memory-maps its file keeps working. A failed
export or a reader error is reported in the status bar and the session is
cleaned up at once.

Taking a layer back
===================

**Plugins → napari plugins → Import layer from napari as result…** asks for
two things: *which layer* and *which result it was derived from*. Both are
your choice; ImProcess does not guess from which layers appeared when,
because a plugin can add anything and the session only knows what ImProcess
added. When a verified adapter declared an *output mapping* for the layer,
the dialog pre-selects the source and says what the plugin calls the layer.

.. list-table::
   :header-rows: 1
   :widths: 18 30 52

   * - napari layer
     - Becomes
     - Notes
   * - Image
     - image result
     - fresh coordinate space by default (see below)
   * - Labels
     - labels result
     - fresh coordinate space by default
   * - Points
     - table result (coordinates + properties)
     - **never** a localization result; convert explicitly with the
       *table → localizations* processor, which asks for the column mapping
   * - Shapes
     - ROIs in the ROI manager
     - rectangles, ellipses, polygons, paths, lines
   * - Surface, Tracks, Vectors
     - refused
     -

**Grid identity.** An imported image or mask gets its own coordinate space
unless the endpoint adapter declares ``preserves_grid`` for that output
*and* the layer's transform is the identity: same dimensionality and axis
order as the source, equal scale, zero translation, identity rotation and
affine, zero shear, and the same shape. ImProcess results store a scale and
nothing else, so any other transform cannot be represented and is not
claimed. Two arrays of the same shape are not the same grid, and the same
numbers in another unit are not either: a layer whose unit differs from
the source's gets a fresh grid even when the adapter vouches for it.

**Units.** napari carries a unit per axis; a result carries one unit for
all axes. Length units that differ between axes (micrometre Y, nanometre X)
are converted onto one unit, with the per-axis scales converted along, so
each scale keeps meaning what it meant. Axes whose units are not all
lengths and not all the same are refused; set one unit per layer in napari
first. A Points layer that is rotated, sheared or carries an affine is
refused whether or not it is also translated; each transform component is
checked on its own.

**Attribution.** A name pattern cannot say which plugin made a layer, and
neither can "it appeared after the session opened": an unrelated layer
added later would inherit the session's grid. A mapping is therefore
*pre-selected* in the import dialog only when napari's own record of where
the layer came from (``layer.source``) points at the session — the dock
widget the session opened produced it, or its parent layer is one the
session added — and exactly one open session's adapter claims it. Every
other mapping an open session could offer is listed under *Adapter mapping*
for you to choose explicitly; the default is none, a fresh coordinate space.
Changing the source result away from the mapping's session drops the
mapping rather than carrying it over, and a mapping whose session result
is no longer loaded is not offered at all.

**Lifetime of what a session shows.** A session's layers read the result's
data, which may be a lazy view over the file it came from. While a session
is open on a result, the workflow controller keeps that file handle open
even after the result leaves the reconstruction list; the handle is
released once no session and no loaded result (including results derived
from it) still needs it. Closing a session whose export is still writing
cancels it: the export finishes on its own, its files are discarded when
it reports back, and at shutdown ImProcess waits a bounded time for such
exports and keeps a reference to any that outlive the wait rather than
destroying a running thread.

**Points → localizations.** The ``table-to-localizations`` processor is the
explicit promotion: it asks which columns are ``x`` and ``y`` (optionally
``z``, ``frame``, ``photons``, ``sigma``), the coordinate unit and the pixel
size, and refuses a column it cannot find. Units are per axis: with
``unit: px`` the lateral columns are camera pixels (``pixel_size_nm`` each)
and a ``z`` column is in Z steps (``z_step_nm`` each, required); ``nm`` and
``um`` take every column as physical; ``table`` uses the table's own
per-axis ``coordinate_scale`` and ``scale_unit``, which is what an imported
Points layer carries. An imported Points layer has its translation folded
into the coordinates (``translate / scale``, so ``coordinates × scale`` is
again the world position); a rotated, sheared or affine-transformed Points
layer is refused, because a per-axis scale cannot represent it. Nothing
else turns a points table into emitters.

**Writers.** Plugins that contribute writers appear under **Plugins → napari
plugins → Endpoint sessions** as *Save layers of … with <writer>*; only that
session's layers are handed to the writer, never the whole viewer. A writer
is offered only when its npe2 layer-type constraints (``image``, ``image+``,
``image?``, ``image{2}``, ``image{1,3}`` …) accept exactly the session's
layers by type *and count*, and the writer command you picked is the one
dispatched, not the plugin's first compatible writer.

Every import records a ``napari-import`` step in the result's provenance,
naming the plugin, the widget, the layer and the grid decision. Such a step
is not replayable by definition.

Adapters
========

An adapter is a :class:`~imswitch.improcess.model.napari_endpoints.NapariEndpoint`.
The built-in ones live in that module; yours go in the setup file or in a
drop-in plugin.

.. code-block:: json

   "processing": {
       "napariEndpoints": [
           {
               "id": "napari-skimage:threshold",
               "label": "napari-skimage: Automated threshold",
               "lane": "dock",
               "plugin": "napari-skimage",
               "widget": "Automated Threshold",
               "kinds": ["image"],
               "outputMappings": [
                   {"pattern": "*", "layerType": "labels", "kind": "labels", "preservesGrid": true}
               ]
           },
           {
               "id": "napari-storm:reader",
               "label": "napari-storm (open exported Picasso file)",
               "lane": "reader",
               "plugin": "napari-storm",
               "reader": "napari-storm",
               "export": "picasso-hdf5",
               "kinds": ["localization"]
           }
       ]
   }

Fields:

``id``, ``label``
   Menu identity. A config entry with a built-in id replaces the built-in.
``lane``
   ``dock``, ``reader`` or ``detached``.
``plugin``
   The npe2 manifest name (what ``npe2 list`` prints, usually the
   distribution name).
``widget``
   Dock lane: the widget's display name, or an ``fnmatch`` pattern such as
   ``"Gaussian*"`` resolved against the installed plugin.
``reader``, ``export``
   Reader lanes: the reader plugin, and the export format it understands.
   Formats: ``ome-tiff``, ``hdf5``, ``ome-zarr`` (image kinds), ``labels-tiff``,
   ``hdf5`` (labels), ``picasso-hdf5``, ``localizations-csv`` (localization).
``kinds``
   Result kinds the endpoint accepts. Required for verified endpoints.
``outputMappings``
   Dock lane, optional: how layers the plugin creates map back
   (``pattern`` against the layer name, ``layerType``, ``kind``,
   ``preservesGrid``).

Worked example 1: a dock widget (``napari-skimage``)
------------------------------------------------------

``napari-skimage`` is a pure npe2 plugin from the napari organisation with
one dock widget per scikit-image operation. Install it into the ImSwitch
environment::

   pip install napari-skimage

Open a reconstruction in ImProcess, then **Plugins → napari plugins → Send
current result to → napari-skimage: Automated threshold**. The result's
image layer is added to the viewer and the threshold widget opens beside
it; pick the layer in the widget and run it. The plugin adds a Labels layer.
**Import layer from napari as result…** pre-selects that layer (the built-in
adapter declares the mapping), and because a threshold is pixel-aligned with
its input and the layer transform is the identity, the imported labels
result inherits the reconstruction's grid, so ROIs drawn on one measure the
other correctly.

Three built-in adapters cover ``napari-skimage`` (Gaussian filter, Automated
Threshold, Label connected components); its other widgets are offered as
unverified.

Worked example 2: a file-based plugin (``napari-storm``)
----------------------------------------------------------

``napari-storm`` renders SMLM data. It contributes both a dock widget and a
reader, and the built-in adapters use both: the dock endpoint sends a
localization result as a Points layer; the reader endpoint exports a
Picasso-style HDF5 (with its YAML info sidecar) to a temporary directory
and opens it with napari-storm's reader in ImProcess's viewer. The session
keeps the files until its layers are gone.

Adding an adapter for another plugin (recipe)
---------------------------------------------

This recipe is written so that it can be handed, together with this page,
to a coding agent.

1. **Identify the plugin's contributions.** ``npe2 list`` after
   ``pip install <plugin>``; or read its ``napari.yaml``. Note the manifest
   ``name`` and the ``display_name`` of each widget, and whether it has
   readers. A plugin with no widget and no reader is not an endpoint.
2. **Choose the lane.** Widget → ``dock``. Reader only → ``reader``; use
   ``detached`` only if the plugin fails inside an embedded viewer.
3. **Decide the kinds.** Which ImProcess result kinds make sense (image,
   composite, rgb, labels, localization). For a reader, pick the export
   format the reader actually parses; verify by opening an exported file in
   plain napari first.
4. **Declare output mappings** if the plugin creates layers worth taking
   back, and set ``preservesGrid`` only for operations that are pixel-aligned
   with their input (filters, thresholds, labelling), never for anything
   that crops, resamples or registers.
5. **Write the entry**: a ``napariEndpoints`` item in the setup file, or a
   Python ``NapariEndpoint`` in a drop-in plugin (see
   ``examples/improcess_napari_endpoints/``).
6. **Test without the real plugin** using npe2's ``DynamicPlugin`` to
   register a fake widget or reader under the same manifest name, and the
   stubbed controller fixtures in
   ``imswitch/improcess/_test/test_napari_endpoint_controller.py``.
7. **Run the native smoke test once** on a workstation
   (``IMSWITCH_NATIVE_GUI_TESTS=1``) to see the dock in the embedded window.
8. **Document** the adapter in this page's list.

Troubleshooting
===============

The widget does not receive the viewer
   napari injects the viewer only into a *class* ``__init__`` whose
   parameter is named ``napari_viewer`` (or annotated ``napari.Viewer``), or
   into a magicgui factory. A bare function is instantiated with no
   arguments. This is the plugin's contract, not ImProcess's.

The plugin wants its own window
   Use the ``detached`` lane. ``napari.current_viewer()`` returns
   ImProcess's embedded viewer while no detached viewer exists.

Qt binding mismatch
   ImProcess runs on PyQt5. A plugin that pins PyQt6 or PySide6 cannot load
   in the same process; this is documented, not worked around.

Layers appear at the wrong scale
   The layers carry ``scale`` and ``units`` from the result. A plugin that
   ignores units and assumes pixels will display them at the numeric scale;
   check the plugin, not the result.

Installing plugins
   **Plugins → napari plugins → Install napari plugins…** opens napari's own
   plugin manager if ``napari-plugin-manager`` is installed; otherwise
   ``pip install <plugin>`` into the ImSwitch environment, then **Rescan
   installed napari plugins**.

Testing tiers
=============

Endpoint code is tested at three levels, and the names mean what they say:

* **GUI-independent unit tests**: layer conversion by kind (including the
  2D/3D/anisotropic/assumed-z localization transforms and the translated
  preview), endpoint discovery and gating, import rules, sessions.
* **Stubbed controller tests** (offscreen Qt, stub viewer): the viewer calls
  made by each lane, ownership, cleanup on every exit path, the menu.
* **Native smoke test** (``IMSWITCH_NATIVE_GUI_TESTS=1``): the only test that
  builds the embedded viewer and observes a plugin dock inside it. It is
  skipped everywhere else because the offscreen Qt platform has no OpenGL.
