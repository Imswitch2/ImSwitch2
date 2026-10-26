************************************
ImProcess and Fiji (the Fiji link)
************************************

ImProcess can hand a result to **Fiji** (ImageJ) and take an image back. Fiji
runs in its own process, started and stopped from **Plugins → Fiji**; nothing
is synchronised behind your back. You send an image when you want it in Fiji,
you work in Fiji as you normally would, and you import the image when you want
it back. There is no live link: an image you changed in Fiji after importing
it is not imported again until you ask.

The link is optional. Without the ``fiji`` extra or a Fiji folder, the menu
entries are disabled and say why.

Installing
==========

#. Install the extra, which brings PyImageJ (and, on macOS, the AppKit
   bridge)::

     pip install "imswitch2[fiji]"

#. Download **Fiji Latest** for your platform from https://fiji.sc and unpack
   it anywhere. Fiji Latest ships its own Java; ImSwitch2 uses that Java and
   never a system one. Older Fiji downloads, which bundle another Java, are
   refused with a message.
#. Choose the folder in **Plugins → Fiji → Fiji location…**. Pick the folder
   that holds the ``jars``, ``plugins`` and ``java`` folders. On macOS that is
   the folder that *contains* ``Fiji.app``, not ``Fiji.app`` itself (choosing
   ``Fiji.app`` is understood and corrected, with a note). The choice is kept
   per computer in ``improcess_options.json`` in the ImSwitch config folder.

An environment that holds PyImageJ when the ImSwitch environment does not can
run the host: set the advanced key ``fijiPython`` in ``improcess_options.json``
to that interpreter. It has no menu entry; when it is set, ImSwitch no longer
checks for PyImageJ itself and a missing package shows up as the host's own
start failure, with its log.

The menu
========

**Plugins → Fiji** has a status line (disabled; it reads *not running*,
*starting…*, the ImageJ and Java versions when ready, *stopped unexpectedly*
or *failed to start*) and these entries.

.. list-table::
   :header-rows: 1
   :widths: 24 76

   * - Entry
     - What it does
   * - **Start Fiji**
     - Starts Fiji in its own process and opens its window. Starting takes
       seconds (about 10 s warm); the ImProcess window stays usable. A failed
       start shows the reason with the tail of the host's log.
   * - **Send to Fiji**
     - Opens the current result in Fiji as a hyperstack, starting Fiji first
       if needed. A result with no honest ImageJ form is refused with the
       reason (see below) and nothing is started.
   * - **Import from Fiji…**
     - Lists the images open in Fiji and imports the ones you tick. The list
       shows each image's title, ImageJ id, size, type, **origin** (*sent from*
       a result, *derived from* a result by lineage hint, or *unknown*) and
       whether it was imported before. Images you did not send and have not
       imported are ticked by default. *Refresh* re-reads Fiji's windows.
   * - **Import new images**
     - Imports every open image that was neither sent from ImProcess nor
       imported before, without asking. Says so when there is none.
   * - **Stop Fiji**
     - Closes Fiji and its process. Closing ImProcess does the same.
   * - **Fiji location…**
     - Chooses the Fiji folder (see above). A running Fiji keeps its old
       location until the next start.

Imports arrive as new results, selected like any other; nothing in Fiji is
closed or changed by importing.

What crosses
============

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Item
     - ImProcess → Fiji
     - Fiji → ImProcess
   * - Axes
     - ``T``, ``Z``, ``C``, ``Y``, ``X`` in any order become the hyperstack's
       XYCZT dimensions.
     - Hyperstack dimensions of size one are dropped. For an image you sent,
       the original axis order is restored.
   * - Pixel type
     - ``uint8``, ``uint16``, ``float32``. Any other type is refused: nothing
       is cast.
     - 8-bit, 16-bit and 32-bit images; the values are exact.
   * - Pixel size and unit
     - ``X`` and ``Y`` scales set the calibration; the unit is written out
       explicitly (µm, nm, mm, pixel).
     - Read back and converted to one ImProcess unit per result. Axes whose
       units cannot be reconciled refuse the import with a message.
   * - Z step
     - The ``Z`` scale becomes the voxel depth.
     - Voxel depth becomes the ``Z`` scale.
   * - Frame interval
     - The ``T`` scale (seconds) becomes the frame interval.
     - Converted to seconds. When Fiji's interval is unset, the ``T`` scale
       is the frame index, and the import notes say so.
   * - Channel names
     - Become the channel slice labels.
     - Slice labels become channel names when every channel has one.
   * - Result id
     - Stored as an image property.
     - Read back as a **lineage hint**: ImageJ copies properties onto
       duplicates and crops, so a matching id says where an image probably
       came from, not that its pixels still match it.

What does not cross
-------------------

* **The calibration origin.** A crop in Fiji moves the image's origin; that
  offset is not carried back, and the import notes list it.
* **Other axes.** Axes such as dataset, angle or base have no ImageJ
  equivalent. Sending such a result is refused with a request to project or
  split first; nothing is folded together for you.
* **RGB images.** They are shown in the import list but cannot be imported;
  convert them to an 8-bit stack in Fiji first.
* **OME annotations and other metadata.** The original metadata stays on the
  ImProcess side with the result you sent; Fiji is not trusted to carry it.
  Everything that could not be carried is listed under the imported result's
  *Fiji* entry in the metadata (``dropped``).

Imported images get a fresh coordinate grid
===========================================

An image coming back from Fiji is always placed on a **new coordinate grid**,
even when its shape and pixel size match the image you sent. Shape and scale
cannot prove that pixels still sit where they were: *Translate* and *Rotate*
keep both, and a crop that happens to preserve the pixel size moves the
origin. A new grid is the safe default; it means the result is not
automatically overlaid on the original, and nothing is misregistered by
accident. A later release will inherit the grid only when every operation
recorded on the image is known to preserve it.

Provenance
==========

An imported result records one step, **external import** with tool ``fiji``,
holding the Fiji image id and title, the ImageJ and Java versions, and the
metadata notes. When the result's source is known (it was sent from ImProcess,
or its lineage hint matches a result), that result is the step's input. The
step is **not replayable**: what happened inside Fiji cannot be run again by a
workflow.

Known limitations
=================

* Tested on macOS (Apple silicon). Windows and Linux are supported by the
  design but have not been tested yet.
* When Fiji is in the background, a send waits for its window to activate
  before the image appears.
* Each Fiji session is a Java virtual machine of its own, sized by Fiji's own
  heap policy; stopping and starting Fiji starts a new one, and image ids do
  not survive that. After Fiji stops unexpectedly the sent-image bookkeeping
  is cleared.
* A very large stack is copied through a temporary file on the way to and
  from Fiji, so it needs the disk space and takes time.

See also
========

* :doc:`improcess` — the ImProcess module
* :doc:`improcess-napari-plugins` — sending results to napari plugins
