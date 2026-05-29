**********************
HDF5 and Zarr datafiles
**********************

Imswitch2 saves images as either `HDF5
<https://www.hdfgroup.org/solutions/hdf5/>`_ or `Zarr
<https://zarr.readthedocs.io/>`_ files.  HDF5 is the default and the
right choice for single-file portability; Zarr is the right choice for
long timelapses or distributed reading (each chunk is a separate file
on disk).

Both formats share **the same logical layout** so downstream tools —
ImProcess in particular — see the same data and metadata regardless of
how the recording was saved.  Switching format is a one-knob change
(``SaveFormat.HDF5`` ↔ ``SaveFormat.ZARR``) with no code adjustments
required on the reading side.

Snapshot layout
===============

A snap (single shot, no streaming) lays out as follows:

.. code-block:: text

    <file>.{h5|zarr}/
        @timestamp
        @rec_mode = "snap"
        <detectorName>/
            data                 # (T, Y, X) or (Y, X), dtype preserved from frame
                @detector_name
                @element_size_um
                @axes            # e.g. ["T", "Y", "X"]
            metadata/
                @key = value     # uncategorised flat attrs
                <category>/      # e.g. "detector", "lasers", "scan"
                    @key = value # attrs within category

Key properties:

* **Dtype is preserved** from the frame array — ``uint16`` stays
  ``uint16``.  There is no hard-coded ``i2`` cast.
* **Axes are not reversed.**  Frames arrive as ``(T, Y, X)`` and are
  stored as ``(T, Y, X)``.
* **Metadata is grouped by category.**  Attributes whose key follows the
  ``Category:Subkey`` convention are placed under a ``metadata/Category``
  subgroup with the subkey as a leaf attribute.  Uncategorised keys land
  directly on ``metadata/``.  Readers can flatten this back into the
  legacy ``Category:Subkey`` form trivially — :py:class:`ImProcess
  DataObj <imswitch.improcess.model.DataObj>` does this automatically.

Streaming (timelapse) layout
============================

For continuous recording the layout is the same, except:

* ``@rec_mode = "recording"`` on the root.
* The ``data`` array is created with an initial shape of ``(0, Y, X)``
  and resized as frames arrive (no ``append``-style writes — works on
  Zarr v3).
* Each detector's ``data/@writing`` is ``True`` while the recording is
  active and flips to ``False`` on finalise.
* For ``ScanLapse`` recordings with ``singleLapseFile=True``, each scan
  gets its own ``scan0/``, ``scan1/`` … subgroup inside the file before
  the per-detector groups.

Multi-detector behaviour
========================

Single-file mode (``singleMultiDetectorFile=True``)
    One file/store with one top-level detector group per detector.
    Useful when all detectors run on the same clock and you want a
    single file per acquisition.

Per-detector mode (default)
    One file/store per detector.  The filename gets a ``_<detector>``
    suffix.  Useful when detectors have different framerates or you
    want to load only one detector downstream.

Both modes work identically for HDF5 and Zarr.

Metadata categories
===================

Recording attributes are emitted by the various managers and follow
these category conventions:

Detectors
    ``Detector:NameDetector:Property`` — properties listed by the
    relevant ``DetectorManager`` (e.g. binning, model, camera pixel
    size, readout time, ROI for ``HamamatsuManager``).

Lasers
    One pair per laser:

    * ``Laser:LaserName:Enabled`` (boolean)
    * ``Laser:LaserName:Value`` (power)

Positioners
    Per-axis stage position at acquisition time:

    * ``Positioner:PositionerName:Axis:Position``

Recording and scanning
    All parameters from ``RecordingWidget`` and ``ScanWidget``: pulse
    scheme, stage positions, step sizes, recording mode, frame count.
    Setup-dependent, but the prefixes are stable:

    * ``Rec:Property``
    * ``ScanStage:Property``
    * ``ScanTTL:Property``

Reading back
============

In Imswitch2 itself
    From ``File → Load parameters from saved HDF5 file…`` the GUI
    reads the attributes and re-populates every widget.  Works on
    files produced by either storer.

In ImProcess
    Drop a file onto the main window or load via the *Load* button.
    :py:class:`DataObj <imswitch.improcess.model.DataObj>` recognises
    the structured layout for both HDF5 and Zarr and exposes
    ``DataObj.attrs`` as a flat dict in the legacy
    ``Category:Subkey`` form so existing reconstructor metadata
    adapters work unchanged.

In ImageJ
    HDF5 files open with the *HDF5* plugin; Zarr files open via the
    `n5-ij <https://github.com/saalfeldlab/n5-ij>`_ plugin (which also
    reads Zarr v3).  Both honour ``element_size_um`` for voxel size.

In other tools
    Standard HDF5 / Zarr readers (``h5py``, ``zarr``, ``HDFView``)
    work directly.  The on-disk structure is plain-vanilla — no
    Imswitch-specific decoding required.

Known limitation
================

RAM-backed Zarr recording (``SaveMode.RAM`` or ``DiskAndRAM`` with the
Zarr storer) is not implemented yet — it raises
``NotImplementedError`` at stream-open time.  Use HDF5 for in-memory
recording until a ``MemoryStore`` policy is added.
