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

Imported images and their coordinate grid
=========================================

An image coming back from Fiji is placed on a **new coordinate grid** unless
every Fiji step that led to it is known to keep every pixel where it was.
Shape and scale cannot prove that on their own: *Translate* and *Rotate* keep
both, and a crop that happens to preserve the pixel size moves the origin. So
the grid is inherited only when all of these hold:

* the image's history starts at an image that was sent from ImProcess;
* nothing changed the image outside a recorded command;
* the parameters of every step were recorded;
* every step is on the grid policy list below;
* the shape, scale, units and axes still match what was sent (the check the
  import always did).

Otherwise the result gets a new grid, which means it is not automatically
overlaid on the original; nothing is misregistered by accident. The reason is
stored with the import (``grid_policy_reason``).

Processing history
==================

Fiji does not keep a processing log, so ImProcess assembles a best-effort one
from three things ImageJ already exposes, while Fiji runs:

* **Commands.** ImageJ announces every command (menu use and scripted calls
  alike) before it runs, with the image that was current. ImProcess notes the
  command, its start and end, and which plugin (and jar) implements it.
* **Image events.** ImageJ reports when an image is opened or updated, and
  whether its *changed* flag is set. An image that appears while a command runs
  is that command's output; a command that produces none acted in place.
* **The macro recorder.** A hidden recorder, as in *Plugins > Macros > Record*,
  captures the parameters of commands used from the menus (``sigma=2``) and
  the selections made beforehand (``makeRectangle(...)``). Scripted calls
  carry their parameters with the command and are not in the recorder.

Together they tell, for an imported image, which sent image it came from and
the Fiji steps in between, oldest first. This is stored in the import step's
details (``fiji_history``) and in the result's metadata under *Fiji*
(``history``), and the status bar summarises it ("3 Fiji steps, history
recorded"). It is useful scientific provenance, not a replay script: the
import is still not replayable.

How far to trust it
-------------------

Each step is **recorded** when its parameters are known (an empty parameter
string counts) and **partial** when only the command name is. The history as a
whole is:

* **recorded**: the origin is a sent image, every step is recorded, and no
  change went unseen;
* **partial**: a step is partial, or a change is possible (below);
* **unknown**: the origin is not known (the image was opened from a file in
  Fiji, or made by something that bypasses commands), or a change was
  detected.

Changes that no command accounts for are reported separately:

* **possible**: ImageJ reported an update to the image outside any command.
  ImageJ1 also reports display-only actions (changing the slice, Enhance
  Contrast) as updates, with the changed flag left unset, so this may be a
  display refresh and not an edit.
* **detected**: the update showed the changed flag turning on although no
  command changed the image (painting, filling a selection, a script's Java
  calls). The pixels were edited in a way the history cannot describe.

Grid policy ``fiji-grid-v1``
----------------------------

The policy is the list of ImageJ1 commands that, by construction, keep every
pixel in place (same size, same calibration):

* neighbourhood filters: Gaussian Blur, Median, Mean, Minimum, Maximum,
  Variance, Unsharp Mask, Smooth, Sharpen, Find Edges, Subtract Background;
* point operations: Invert, Add, Subtract, Multiply, Divide, Min, Max, Gamma,
  Log, Exp, Square, Square Root, Abs, Reciprocal, Set;
* type conversion: 8-bit, 16-bit, 32-bit;
* binary operations: Convert to Mask, Make Binary, Erode, Dilate, Open,
  Close-, Fill Holes, Skeletonize, Outline;
* Duplicate, but only when no selection was made before it and no range was
  given: a duplicate of a selection is a crop.

Anything else (Crop, Scale, Size, Translate, Rotate, Flip, Canvas Size, Z
Project, Reslice, Stack to Images, Make Substack, and every command the list
does not name) gives a new grid. The version is stored beside each decision, so
a later change to the list can be told apart.

What the history cannot see
---------------------------

* Parameters of menu commands come only from the recorder. A scripted call
  with no parameters looks like menu use and is not in the recorder, so its
  step is partial.
* Scripts and macros run inside Fiji appear as the commands they call; a
  command that runs other commands is marked partial.
* Drawing tools and plugins that bypass the command layer are seen only as
  image updates (*possible* or *detected*).
* ImageJ2 and SciJava commands are not tracked separately; they are seen only
  as far as they run ImageJ1 commands or change images.
* Opening a file is shown as an unknown origin; the file itself is not
  recorded.

Provenance
==========

An imported result records one step, **external import** with tool ``fiji``,
holding the Fiji image id and title, the ImageJ and Java versions, the metadata
notes and the Fiji history above. When the result's source is known (it was
sent from ImProcess, or its lineage hint matches a result), that result is the
step's input. The step is **not replayable**: what happened inside Fiji cannot
be run again by a workflow.

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
