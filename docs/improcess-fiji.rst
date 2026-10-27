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

The Fiji link is part of ImSwitch2 itself, not a drop-in plugin or a plugin
from the online store: there is nothing to copy into a plugins folder. It
needs two things that ImSwitch2 does not install by default, the **fiji
extra** (the Python packages that talk to Java) and **Fiji itself**, and one
setting, **where Fiji is**. The steps below take about ten minutes, most of
it the Fiji download (about 700 MB).

Before you start
----------------

* ImSwitch2 must be **installed with pip**, from PyPI or from a source
  checkout (see :doc:`installation`). The installer builds (the macOS ``.dmg``
  and the Windows ``setup.exe``) cannot use the Fiji link yet: they carry
  neither the Python packages nor the files the Fiji process runs. In an
  installer build the menu entries are disabled and say so.
* Know which Python environment ImSwitch2 runs from (the conda environment or
  virtual environment you start it from). The extra is installed into that
  same environment.
* Java does not need to be installed: Fiji brings its own, and ImSwitch2 uses
  only Fiji's.

Step 1: install the fiji extra
------------------------------

Activate the environment ImSwitch2 runs from, then install the extra:

.. code-block:: bash

   # ImSwitch2 from PyPI
   pip install "imswitch2[fiji]"

   # ImSwitch2 from a source checkout (run in the checkout folder)
   pip install -e ".[fiji]"

This installs **PyImageJ** 1.8.0 or newer, which brings JPype, scyjava and
imglyb. On macOS it also installs **PyObjC** (``pyobjc-framework-Cocoa``),
which Fiji's window needs there. It adds packages and is not expected to
upgrade anything ImSwitch2 already uses. Check that it worked:

.. code-block:: bash

   python -c "import imagej; print(imagej.__version__)"

If your ImSwitch2 environment cannot take these packages, use a separate
environment for Fiji instead; see `Running Fiji from another Python
environment`_ below.

Step 2: download Fiji Latest
----------------------------

Open https://imagej.net/software/fiji/downloads and download **Fiji Latest**
for your computer, the version **with Java bundled**:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Computer
     - File
   * - Mac with Apple silicon (M1 and later)
     - ``fiji-latest-macos-arm64-jdk.zip``
   * - Mac with an Intel processor
     - ``fiji-latest-macos64-jdk.zip``
   * - Windows, 64-bit (most PCs)
     - ``fiji-latest-win64-jdk.zip``
   * - Windows on ARM
     - ``fiji-latest-win-arm64-jdk.zip``
   * - Linux, 64-bit
     - ``fiji-latest-linux64-jdk.zip``
   * - Linux on ARM
     - ``fiji-latest-linux-arm64-jdk.zip``

The same files are listed at https://downloads.imagej.net/fiji/latest/.

Do **not** use *Fiji Stable* or an older Fiji you already have: they bundle
Java 8, and the Fiji link needs Java 11 or newer (Fiji Latest bundles Java
21). ImSwitch2 checks this and refuses such a folder with that reason. The
``portable-nojava`` download has no Java at all and is refused too.

Step 3: unpack Fiji somewhere permanent
---------------------------------------

Unzip the file into a folder you keep, not into *Downloads* or a temporary
folder, since ImSwitch2 starts Fiji from there every time:

* **macOS**: for example ``~/Applications`` or ``~/software``. Unzipping
  gives a folder named ``Fiji`` that contains ``Fiji.app`` (the launcher you
  would double-click), ``jars``, ``plugins``, ``java`` and more.
* **Windows**: for example ``C:\Users\<you>\Fiji``. Avoid
  ``C:\Program Files``: Fiji's own updater needs to write into its folder.
* **Linux**: for example ``~/Fiji``.

You do not have to start Fiji by itself first, but you can, to check the
download. ImSwitch2 starts its own copy of Fiji either way.

Step 4: tell ImSwitch2 where Fiji is
------------------------------------

#. Start ImProcess and open **Plugins → Fiji → Fiji location…**.
#. Choose the ``Fiji`` folder from step 3, the one that contains ``jars``,
   ``plugins`` and ``java``. On macOS, choose the folder that *contains*
   ``Fiji.app``, not ``Fiji.app`` itself (if you do pick ``Fiji.app``,
   ImSwitch2 uses its parent folder and says so).
#. ImSwitch2 checks the folder at once: a folder it cannot use is refused with
   the reason (see `Troubleshooting`_), and the old setting stays.

The location is saved per computer in ``improcess_options.json`` in the
ImSwitch2 configuration folder (``~/ImSwitchConfig/config`` on macOS and
Linux, ``Documents\ImSwitchConfig\config`` on Windows), next to the default
folders. ImProcess does not need to be restarted.

Step 5: check that it works
---------------------------

#. Open **Plugins → Fiji**. The first line reads *Fiji: not running* and
   **Start Fiji** is enabled. If it is disabled, hover over it: the tooltip
   says what is missing.
#. Click **Start Fiji**. Fiji's window opens after a few seconds (the first
   start after unpacking can take longer while Fiji indexes its plugins); the
   ImProcess window stays usable meanwhile. The first line of the menu then
   names the ImageJ and Java versions, for example *Fiji: ImageJ
   2.18.0/1.54p (Java 21.0.7)*.
#. Select a result in ImProcess and click **Send to Fiji**. It opens in Fiji
   as a hyperstack with its pixel size, Z step and channel names.
#. In Fiji, run something on it, for example *Image › Duplicate…* and then
   *Process › Filters › Gaussian Blur…*.
#. Back in ImProcess, click **Plugins → Fiji → Import new images**. The
   duplicate arrives as a new result, and the status bar says how many Fiji
   steps were recorded.

Running Fiji from another Python environment
--------------------------------------------

The Fiji process can run in a different Python environment from ImSwitch2,
for example when the ImSwitch2 environment cannot take PyImageJ. ImSwitch2
gives that process its own code automatically; the other environment needs
only PyImageJ (and NumPy, which PyImageJ brings):

#. Create the environment and install PyImageJ into it:

   .. code-block:: bash

      python -m venv ~/fiji-env
      ~/fiji-env/bin/pip install "pyimagej>=1.8.0"
      # macOS only:
      ~/fiji-env/bin/pip install pyobjc-framework-Cocoa

   On Windows the interpreter is ``%USERPROFILE%\fiji-env\Scripts\python.exe``
   and pip is ``%USERPROFILE%\fiji-env\Scripts\pip.exe``. A conda environment
   works the same way.
#. Open ``improcess_options.json`` (see step 4 for the folder) in a text editor
   and add the key ``fijiPython`` with the full path to that interpreter. Keep
   the keys already in the file:

   .. code-block:: json

      {
          "fijiDir": "/Users/you/software/Fiji",
          "fijiPython": "/Users/you/fiji-env/bin/python"
      }

#. Open **Plugins → Fiji** again; the change takes effect at the next start.

With ``fijiPython`` set, ImSwitch2 no longer checks its own environment for
PyImageJ; if the other environment lacks it, **Start Fiji** fails and the
message shows the Fiji process's own error. Remove the key, or set it to
``""``, to go back to ImSwitch2's own environment.

Uninstalling
------------

* Stop Fiji (**Plugins → Fiji → Stop Fiji**) and delete the ``Fiji`` folder.
* ``pip uninstall pyimagej`` removes the main package; its dependencies
  (``jpype1``, ``scyjava``, ``imglyb``, ``jgo``, ``xarray`` and, on macOS,
  ``pyobjc-framework-Cocoa``) can be removed the same way if nothing else uses
  them.
* Delete the ``fijiDir`` and ``fijiPython`` keys from
  ``improcess_options.json`` if you like; without the extra the menu simply
  stays disabled.

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
       reason (see below), in a message box as well as on the status bar, and
       nothing is started.
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
     - Closes Fiji and its process. Closing ImProcess does the same. It also
       works while Fiji is still starting (at once) and while Fiji is busy in
       a dialog (Fiji gets about ten seconds to quit, then its process is
       ended).
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
  split first; nothing is folded together for you. One exception: a result
  whose *save* layout has ImageJ axes is sent in that layout. A MoNaLISA
  reconstruction, held in memory as Dataset, Base, T, Z, Y, X, is saved as
  T, Z, C, Y, X with C = Dataset × Base and named channels, so that is what
  Fiji receives (the send notes say so, see below), exactly what you would
  see opening the saved TIFF in Fiji. Coming back, such an image lands on a
  fresh grid, because its axes differ from the in-memory result's.
* **RGB images.** They are shown in the import list but cannot be imported;
  convert them to an 8-bit stack in Fiji first.
* **OME annotations and other metadata.** The original metadata stays on the
  ImProcess side with the result you sent; Fiji is not trusted to carry it.
  What could not be carried is reported in both directions. A send that could
  not carry everything (the save layout was sent, lazy data was materialised, an
  axis has no scale, channels have no names) says so on the status line, as
  "Sent X to Fiji (image N); 2 notes: ..." (the same text is in the log). When
  you import an image that comes from that send (the image itself or a
  duplicate of it), those notes lead the imported result's *Fiji* entry in the
  metadata (``dropped``) as ``on send: ...``, followed by what the import itself
  could not carry. An image whose send is not known (it was opened in Fiji)
  lists only the import's notes.

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
the Fiji steps in between, oldest first. There is ONE place to read this: the
result's **provenance**. Its history (Metadata → Provenance → steps, the save
files' history, the workflow editor's notes) lists one line per Fiji step, such
as ``Fiji: Gaussian Blur... (sigma=2)``, followed by ``Imported from Fiji``.
The full record (commands, options, where the options came from, plugin jars,
completeness) is stored in the import step's details (``fiji_history``); there
is no second copy under the result's *Fiji* metadata, which holds only the
image id, title and the notes of what could not be carried. The status bar
summarises it ("3 Fiji steps, history recorded"). The Fiji lines are readable
history, not replay steps: they are not nodes of the provenance graph and a
workflow never sees them.

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

* **possible**: ImageJ reported an update to the image outside any command
  that the history cannot explain. ImageJ1 also reports display-only actions
  (changing the slice, Enhance Contrast) as updates, so scrolling through a
  result is not an edit. Two things clear those updates, and each is counted
  in a note instead of lowering the history:

  * the update left ImageJ's *changed* flag unset on an image that was never
    marked changed;
  * **a pixel fingerprint.** The host fingerprints an image's pixels (a
    hash of every plane, a change detector and not a cryptographic hash)
    when each command ends and again when you import. When the two match, the
    pixels are exactly what the last command left, and every later update was
    a display refresh. Only an update that neither clears (a changed flag
    already set by an earlier edit, with no fingerprint to compare) stays
    *possible*.
* **detected**: the pixels changed although no command changed them: the
  fingerprint at import differs from the one after the last recorded command,
  or the changed flag turned on outside every command (painting, filling a
  selection, a script's Java calls). The pixels were edited in a way the
  history cannot describe.

  A fingerprint counts only while its command is the last one that changed the
  image. When a later command has none (Fiji was still hashing a large stack
  when you imported, the fingerprint could not be read, or the command is
  still running), the pixels may differ because of that command, so the
  history is *possible*, not *detected*, and a note names the command and says
  its fingerprint was not available. Fiji waits, for a bounded time that grows
  with its memory size, for the fingerprints of commands that have finished
  before it answers, so this is rare; a command that is still running (a
  dialog is open) is not waited for.

Grid policy ``fiji-grid-v1``
----------------------------

The policy is the list of ImageJ1 commands that, by construction, keep every
pixel in place (same size, same calibration):

* neighbourhood filters: Gaussian Blur, Median, Mean, Minimum, Maximum,
  Variance, Unsharp Mask, Smooth, Sharpen, Find Edges, Subtract Background;
* point operations: Invert, Add, Subtract, Multiply, Divide, Min, Max, Gamma,
  Log, Exp, Square, Square Root, Abs, Reciprocal, Set;
* type conversion: 8-bit, 16-bit, 32-bit;
* display and navigation: Next and Previous Slice, Set Slice, Enhance
  Contrast, Apply LUT;
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

* The fingerprint is a change detector: two different images can in principle
  share one, which only costs a missed *possible* change.
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

That does not stop you exporting the rest. *Export workflow* (and *Edit
workflow*) on a result that went through Fiji writes a workflow of
everything except the Fiji step: the steps that produced what you sent are
kept, with a ``Save`` that writes it as ``..._for_fiji``, and the Fiji step is
replaced by an **input** called ``from_fiji`` that you bind when running (a
file, or a result in the GUI), after which the steps that came after Fiji run
on it. A message lists what was left out and what to bind. When the import was
the last step the workflow ends with that save. Other problems (a plugin that
is not installed, for example) still refuse the export. See *Results with an
interactive step* in :doc:`improcess-workflows`.

Known limitations
=================

* Tested on macOS (Apple silicon). Windows and Linux are supported by the
  design but have not been tested yet.
* When Fiji is in the background, a send waits for its window to activate
  before the image appears.
* Each Fiji session is a Java virtual machine of its own, sized by Fiji's own
  heap policy; stopping and starting Fiji starts a new one, and image ids do
  not survive that. When Fiji stops unexpectedly, or you close it, the
  sent-image bookkeeping is cleared.
* A very large stack is copied through a temporary file on the way to and
  from Fiji, so it needs the disk space and takes time.

Troubleshooting
===============

When an entry in **Plugins → Fiji** is disabled, its tooltip gives the reason;
when Fiji fails to start, a message box gives it, followed by the last lines of
the Fiji process's log. The full log is ``host.log`` in a folder named
``imswitch-fiji-…`` in the system's temporary folder; it is kept until Fiji is
started or stopped again.

.. list-table::
   :header-rows: 1
   :widths: 45 55

   * - Message
     - What to do
   * - *pyimagej is not installed: pip install "imswitch2[fiji]"*
     - Step 1 was not done in the environment ImSwitch2 runs from. Install the
       extra there, or use `Running Fiji from another Python environment`_.
   * - *Choose the Fiji folder: Plugins → Fiji → Fiji location…*
     - No Fiji folder is set yet; see step 4.
   * - *… has no 'jars' folder; choose the Fiji folder …*
     - The folder chosen is not Fiji's top folder. Choose the folder that
       contains ``jars``, ``plugins`` and ``java``.
   * - *this Fiji has no bundled Java; install Fiji Latest*
     - The folder is a Fiji without Java (the ``portable-nojava`` download).
       Download the ``-jdk`` file for your computer (step 2).
   * - *this Fiji bundles no Java for this platform …*
     - The Fiji download is for another kind of computer (for example the
       Intel Mac download on an Apple silicon Mac), or an old Fiji. Download
       the file for your computer from the table in step 2.
   * - *this Fiji bundles Java 8 …; the Fiji link needs Java 11 or newer …*
     - This is Fiji Stable or an old Fiji. Download Fiji Latest (step 2).
   * - *The Fiji link is not available in the installer build of ImSwitch2 …*
     - Install ImSwitch2 with pip (:doc:`installation`) to use the Fiji link.
   * - Start fails with *imglyb requests imglib2-imglyb … but …* in the log
     - The installed imglyb wants a different bridge jar version from the one
       ImSwitch2 ships. Install the versions ImSwitch2 was tested with:
       ``pip install "pyimagej==1.8.0" "imglyb==2.1.0"``.
   * - Start fails with *ModuleNotFoundError: No module named 'scyjava'* (or
       ``'imagej'``) in the log
     - ``fijiPython`` points at an environment without PyImageJ; install it
       there or remove the key.
   * - The menu says *Fiji stopped unexpectedly*, or the status bar said *Fiji was closed (exit code N)*
     - Fiji's window was closed (File → Quit included), or Fiji crashed.
       ImProcess notices at once, without a request, and shows no dialog:
       closing Fiji is often deliberate. **Start Fiji**, or **Send to Fiji**
       (which starts it), launches a new one. Images sent before are no
       longer known to that new Fiji.
   * - Fiji runs out of memory on a large stack
     - Fiji gets the share of memory its own launcher would give it, set by
       the line ``cfg.max-heap`` in ``config/jaunch/fiji.toml`` inside the
       Fiji folder (``'75%'`` by default). Change that line and restart Fiji.
   * - A send takes several seconds before the image appears
     - Fiji is in the background and waits for its window to be activated;
       bring Fiji to the front.

See also
========

* :doc:`improcess` — the ImProcess module
* :doc:`improcess-napari-plugins` — sending results to napari plugins
