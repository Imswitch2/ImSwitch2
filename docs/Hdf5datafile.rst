***********************
HDF5 and Zarr datafiles
***********************

ImSwitch2 saves images as `HDF5
<https://www.hdfgroup.org/solutions/hdf5/>`_, `Zarr
<https://zarr.readthedocs.io/>`_ or OME-TIFF files (the **File format**
choice in the Recording panel).  HDF5 is the default and the right choice
for single-file portability; Zarr is the right choice for long timelapses
or distributed reading (each chunk is a separate file on disk).  OME-TIFF
is what stitching tools read natively; it carries the same metadata as an
OME annotation instead of the attribute tree described here.

HDF5 and Zarr share **the same logical layout** so downstream tools —
ImProcess in particular — see the same data and metadata regardless of
how the recording was saved.  The differences are listed where they
occur below.

Snapshot layout
===============

A snap (single shot, no streaming) lays out as follows:

.. code-block:: text

    <name>_<detector>.h5         # HDF5: one file per detector
    <name>.zarr/                 # Zarr: one store for all snapped detectors
        @timestamp
        @rec_mode = "snap"
        <detectorName>/
            @ome_xml             # HDF5: OME-XML of the image
            @ome                 # Zarr: OME-NGFF 0.5 multiscales metadata
            data                 # (1, Y, X) for one frame; dtype of the frame
                @detector_name
                @element_size_um # [z, y, x] in µm
                @axes            # HDF5: a string such as "TYX"; Zarr: a list
                @writing = False
                @recording:*     # see "Recording attributes"
            metadata/
                @key = value     # uncategorised flat attrs
                <Category>/      # e.g. "Detector", "Laser", "Positioner"
                    @key = value # the rest of each Category:... key

Key properties:

* **Dtype is preserved** from the frame array — ``uint16`` stays
  ``uint16``.  There is no hard-coded ``i2`` cast.  Recordings use the
  dtype the detector declares; frames of another dtype are cast, with a
  warning logged once per detector.
* **There is always a leading frame axis.**  A single 2-D frame is stored
  as ``(1, Y, X)``.  The leading axis is named from the OME metadata:
  ``T`` for a time series or a single frame, ``Z`` for a z-stack scan.
  Point-detector recordings that keep their line-step planes have an extra
  ``C`` axis, ``(T, C, Y, X)``.  Axes are not reversed.  HDF5 writes
  ``axes`` only when OME metadata is available.
* **Pixel size.**  HDF5's ``element_size_um`` takes the calibrated sizes
  from the OME metadata, including the scan Z step of a z-stack.  Zarr's
  ``element_size_um`` is the detector's static pixel size; OME-Zarr readers
  take the calibrated scale from the ``ome`` metadata instead.
* **Metadata is grouped by category.**  An attribute whose key follows the
  ``Category:Subkey`` convention is placed in a ``metadata/Category``
  subgroup, keyed by the rest of its name.  Uncategorised keys land
  directly on ``metadata/``.  Readers can flatten this back into the
  legacy ``Category:Subkey`` form — ImProcess's ``DataObj`` does this
  automatically.  In a Zarr snapshot the ``recording:*`` attributes also
  go this way, into ``metadata/recording/``, instead of onto ``data``.

Streaming (recording) layout
============================

For continuous recording the layout is the same, except:

* Recording files are named ``.hdf5`` (snapshots ``.h5``) and ``.zarr``.
* A Zarr store gets ``@rec_mode = "recording"`` and ``@timestamp`` on the
  root.  An HDF5 recording file has no root attributes.
* The ``data`` array is created when the first frames arrive, with an
  initial shape of ``(0, Y, X)``, and is resized as frames arrive (no
  ``append``-style writes — works on Zarr v3).
* Each detector's ``data/@writing`` is ``True`` while the recording is
  active and flips to ``False`` on finalise.
* A recording can be read while it is being written.  HDF5 files on disk
  are written in SWMR mode, with two small datasets next to ``data``:
  ``frames_committed`` (how many frames are guaranteed to be on disk) and
  ``stream_complete`` (set to ``1`` when the recording ends).  Zarr keeps
  the count in ``data/@recording:frames_committed``.  In HDF5 the final
  ``writing = False``, ``recording:*`` values and ``ome_xml`` are written
  after the file is closed; if a live reader still holds it open that step
  is skipped with a warning, and ``stream_complete`` still marks the end.
* For *Timelapse scan* and *Camera timelapse* recordings with *Save all
  timepoints in a single file* checked, each timepoint gets its own
  ``scan0/``, ``scan1/`` … subgroup inside the file, holding the
  per-detector groups.  HDF5 and Zarr only.
* HDF5 datasets are gzip-compressed with the shuffle filter.  Recordings
  kept only in memory are written uncompressed.

Multi-detector behaviour
========================

Recordings with more than one detector can be saved two ways:

Single-file mode (*Save recordings in a single file*)
    One file/store with one top-level detector group per detector
    (``<name>.hdf5``, ``<name>.zarr``).  Useful when all detectors run on
    the same clock and you want a single file per acquisition.

Per-detector mode (default)
    One file/store per detector.  The filename gets a ``_<detector>``
    suffix.  Useful when detectors have different framerates or you
    want to load only one detector downstream.

OME-TIFF recordings are always saved per detector.  Snapshots ignore the
setting: HDF5 writes one ``<name>_<detector>.h5`` per detector and Zarr
one ``<name>.zarr`` holding every detector.

Metadata categories
===================

Recording attributes are emitted by the various managers and follow
these category conventions:

Detectors
    ``Detector:<DetectorName>:<Property>`` — ``Model``, ``Pixel size``,
    ``Binning`` and ``ROI``, plus the properties listed by the relevant
    ``DetectorManager`` as ``Detector:<DetectorName>:Param:<Name>``
    (e.g. readout time for ``HamamatsuManager``).

Lasers
    Per laser:

    * ``Laser:<LaserName>:Enabled`` (boolean)
    * ``Laser:<LaserName>:Value`` (power)
    * ``Laser:<LaserName>:ModulationEnabled``, ``:Frequency`` and
      ``:DutyCycle`` for lasers that support modulation

Positioners
    Per-axis stage position at acquisition time:

    * ``Positioner:<PositionerName>:<Axis>:Position``

Recording and scanning
    All parameters from the Recording and Scan panels: pulse scheme,
    stage positions, step sizes, recording mode, frame count.
    Setup-dependent, but the prefixes are stable:

    * ``Rec:Property``
    * ``ScanStage:Property``
    * ``ScanTTL:Property``
    * ``MS-RESOLFT_Scan:Property`` and ``MS-RESOLFT_Dev:Property`` from
      the TriggerScope and light-sheet multicolor panels

Acquisition and notes
    * ``acquisition:software_version``, ``acquisition:start_time`` and
      ``acquisition:exposure_time_ms``, on recordings
    * ``notes:session``: the text from **Tools → Session notes…**, stored
      as ``metadata/notes/@session``

Values HDF5 cannot store natively (a dict, a ragged list) are saved as
JSON text prefixed with ``__imswitch_json__:``; ImSwitch2's readers decode
them back.

Recording attributes
====================

These sit on the ``data`` array as ``recording:<name>`` and describe the
acquisition itself:

* ``detector_name``, ``dataset_path`` and ``source_format`` (``HDF5`` or
  ``ZARR``)
* ``planned_frames`` (also ``expected_frames`` and ``frames_per_stack``)
  and ``actual_frames``; ``planned_partitions`` and ``actual_partitions``
* ``completion_outcome``: ``complete``, or ``stopped_early`` when fewer
  frames were written than planned — a recording stopped before its end
* ``discarded_frames``: frames the detector delivered beyond the plan
  (a free-running camera, extra trigger pulses) that were not written
* ``num_timepoints``, ``lapse_index``, ``single_lapse_file`` and, where
  set, ``lapse_interval_s`` and ``planned_start_time``, for lapses

A detector that delivered no frame at all leaves a file or store without
``data``; its root then carries ``recording:detector_name``,
``recording:completion_outcome = "stopped_early"`` and
``recording:actual_frames = 0``.  A recording that *fails*, rather than
being stopped, has its partial file removed — except a single-lapse file
that already holds earlier timepoints.

When the recording was started with an acquisition layout (scan
recordings, when the scan panel supplies one), ``data`` also carries
``AcquisitionLayout:schema`` (currently
``"imswitch.acquisition-layout/1"``) and ``AcquisitionLayout:json``, which
maps each stored frame to its scan coordinates.  To see what a file's
layout says without loading any pixels, run from a source checkout:

.. code-block:: bash

   python tools/inspect_acquisition_layout.py <path> [detector]

Reading back
============

In ImSwitch2 itself
    **File → Load parameters from saved HDF5 file…** (``*.h5`` /
    ``*.hdf5``) and **File → Load parameters from saved Zarr store…**
    read the attributes and re-populate every widget.

In ImProcess
    Drop a file onto the main window or open it with **File → Quick load
    data…** (or **Virtual load data…** for a lazy stack).  ``DataObj``
    recognises the structured layout for both HDF5 and Zarr and exposes
    ``DataObj.attrs`` as a flat dict in the legacy ``Category:Subkey``
    form so existing reconstructor metadata adapters work unchanged.

In ImageJ
    HDF5 files open with the *HDF5* plugin, which reads
    ``element_size_um`` for the voxel size.  Zarr files open via the
    `n5-ij <https://github.com/saalfeldlab/n5-ij>`_ plugin; OME-Zarr
    readers take axes and pixel size from the ``ome`` metadata.

In other tools
    Standard HDF5 / Zarr readers (``h5py``, ``zarr``, ``HDFView``)
    work directly.  The on-disk structure is plain-vanilla — no
    ImSwitch2-specific decoding required.

Known limitation
================

Zarr cannot record to memory only: *Save in memory for reconstruction*
with the Zarr format raises ``NotImplementedError`` when the recording
opens.  *Save on disk and keep in memory* works.  Use HDF5 for in-memory
recording until a ``MemoryStore`` policy is added.
