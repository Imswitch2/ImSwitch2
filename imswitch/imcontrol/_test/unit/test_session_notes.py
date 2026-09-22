"""The operator's free-text session note, from the dialog to the saved file.

The note is one string that has to survive four different journeys: into the
shared attributes, into the structured metadata group of HDF5 and Zarr, and --
the one that needs its own machinery -- into OME-TIFF, which carries no shared
attributes at all and can only hold the text as an OME ``Description``.
"""
import numpy as np
import pytest
import tifffile
import zarr
from qtpy import QtWidgets

from imswitch.imcommon.model import SESSION_NOTE_KEY, SharedAttributes
from imswitch.imcommon.model.ome_metadata import (
    ANNOTATION_NAMESPACE, NOTE_KEY, OmeAxis, OmeImageMeta, build_ome_xml,
)
from imswitch.imcontrol.model.managers.RecordingManager import (
    HDF5Storer, TiffStorer, ZarrStorer, annotationsFromAttrs,
)
from imswitch.imcontrol.model.managers.recording_metadata import (
    MODE_SNAP, build_ome_image_meta,
)

NOTE = 'measured 10 mW in the BFP for the 405 laser'


class _StubDetector:
    def __init__(self, dtype=np.uint16, pixelSizeUm=(1.0, 0.2, 0.1)):
        self.dtype = np.dtype(dtype)
        self.pixelSizeUm = list(pixelSizeUm)


class _StubDetectorManager:
    def __init__(self, det):
        self._det = det

    def __getitem__(self, name):
        return self._det


@pytest.fixture
def detman():
    return _StubDetectorManager(_StubDetector())


# --- shared attributes ------------------------------------------------------

def test_session_note_is_a_shared_attribute():
    attrs = SharedAttributes()
    assert attrs.getSessionNote() == ''

    attrs.setSessionNote(NOTE)

    assert attrs.getSessionNote() == NOTE
    assert attrs.getHDF5Attributes()['notes:session'] == NOTE


def test_setting_the_note_announces_it():
    attrs = SharedAttributes()
    seen = []
    attrs.sigAttributeSet.connect(lambda key, value: seen.append((key, value)))

    attrs.setSessionNote(NOTE)

    assert seen == [(SESSION_NOTE_KEY, NOTE)]


def test_clearing_the_note_does_not_leave_the_old_text_behind():
    """A recording made after the note was cleared must not inherit it."""
    attrs = SharedAttributes()
    attrs.setSessionNote(NOTE)

    attrs.setSessionNote('')

    assert attrs.getSessionNote() == ''
    assert annotationsFromAttrs(attrs.getHDF5Attributes()) == {}


def test_untouched_attributes_carry_no_note():
    """Never opening the dialog leaves the metadata exactly as it was."""
    assert 'notes:session' not in SharedAttributes().getHDF5Attributes()


# --- attrs -> OME annotations ----------------------------------------------

def test_annotations_split_free_text_from_key_value_pairs():
    annotations = annotationsFromAttrs({
        'notes:session': NOTE,
        'notes:operator': 'lenny',
        'detector:exposure': 0.1,
    })

    assert annotations == {NOTE_KEY: NOTE, 'operator': 'lenny'}


def test_blank_notes_are_not_annotations():
    assert annotationsFromAttrs({'notes:session': '   '}) == {}
    assert annotationsFromAttrs({}) == {}
    assert annotationsFromAttrs(None) == {}


def test_annotations_become_ome_description_and_map_annotation():
    meta = OmeImageMeta(
        'Cam', [OmeAxis('y', 'space'), OmeAxis('x', 'space')], [0.2, 0.1],
        dtype=np.dtype(np.uint16),
        annotations={NOTE_KEY: NOTE, 'operator': 'lenny'},
    )

    md = meta.tiff_metadata((48, 32))

    assert md['Description'] == NOTE
    assert md['MapAnnotation'] == {
        'Namespace': ANNOTATION_NAMESPACE, 'operator': 'lenny',
    }


def test_no_annotations_changes_nothing():
    """A recording made without notes emits the OME it always emitted."""
    axes = [OmeAxis('y', 'space'), OmeAxis('x', 'space')]
    plain = OmeImageMeta('Cam', axes, [0.2, 0.1], dtype=np.dtype(np.uint16))

    assert plain.annotation_metadata() == {}
    assert 'Description' not in plain.tiff_metadata((48, 32))
    assert 'Description' not in build_ome_xml(plain, (48, 32))


# --- the note in a saved file ----------------------------------------------

def test_note_reaches_an_ome_tiff_snapshot(detman, tmp_path):
    """OME-TIFF holds no shared attributes, so Description is the only route."""
    storer = TiffStorer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16,
        annotations={NOTE_KEY: NOTE})}

    storer.snap({'Cam': np.zeros((48, 32), np.uint16)})

    with tifffile.TiffFile(str(tmp_path / 'snap_Cam.ome.tiff')) as t:
        assert NOTE in (t.ome_metadata or '')


def test_note_reaches_a_streamed_ome_tiff(detman, tmp_path):
    """The stream embeds its OME-XML at finalize, after the note was set."""
    path = str(tmp_path / 'rec_Cam.tiff')
    storer = TiffStorer(str(tmp_path / 'rec'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 2, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16,
        annotations={NOTE_KEY: NOTE})}
    storer.openStream({'Cam': path}, ['Cam'], {'Cam': (48, 32)}, {'Cam': {}},
                      singleMultiDetectorFile=False, singleLapseFile=False,
                      saveMode=None)
    storer.writeFrames('Cam', np.zeros((4, 48, 32), np.uint16))
    storer.finalizeStream({'Cam': 4}, {'Cam': path}, None, None)

    with tifffile.TiffFile(path) as t:
        assert NOTE in (t.ome_metadata or '')


def test_note_reaches_an_hdf5_snapshot(detman, tmp_path):
    import h5py

    storer = HDF5Storer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16,
        annotations={NOTE_KEY: NOTE})}

    storer.snap({'Cam': np.zeros((48, 32), np.uint16)},
                {'Cam': {'notes:session': NOTE}})

    with h5py.File(str(tmp_path / 'snap_Cam.h5'), 'r') as f:
        attrs = SharedAttributes.fromHDF5File(f, 'Cam')
        assert attrs.getSessionNote() == NOTE


def test_note_reaches_a_zarr_snapshot(detman, tmp_path):
    storer = ZarrStorer(str(tmp_path / 'snap'), detman)
    storer.omeMeta = {'Cam': build_ome_image_meta(
        'Cam', MODE_SNAP, 1, pixel_size_yx_um=(0.2, 0.1), dtype=np.uint16,
        annotations={NOTE_KEY: NOTE})}

    storer.snap({'Cam': np.zeros((48, 32), np.uint16)},
                {'Cam': {'notes:session': NOTE}})

    root = zarr.open_group(str(tmp_path / 'snap.zarr'), mode='r')
    attrs = SharedAttributes.fromZarrStore(root, 'Cam')
    assert attrs.getSessionNote() == NOTE


# --- the dialog -------------------------------------------------------------

@pytest.fixture
def qapp():
    """Ensure QApplication exists."""
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    return app


@pytest.fixture
def dialog(qapp):
    from imswitch.imcontrol.view.SessionNotesDialog import SessionNotesDialog

    widget = SessionNotesDialog()
    yield widget
    widget.deleteLater()


def test_dialog_publishes_every_edit(dialog):
    """No Apply button to forget: what the box shows is what gets attached."""
    published = []
    dialog.sigNotesChanged.connect(published.append)

    dialog.notesEdit.setPlainText(NOTE)

    assert published == [NOTE]
    assert dialog.getNotes() == NOTE


def test_dialog_clear_button_publishes_the_empty_note(dialog):
    dialog.notesEdit.setPlainText(NOTE)
    published = []
    dialog.sigNotesChanged.connect(published.append)

    dialog.clearButton.click()

    assert published == ['']


def test_seeding_the_dialog_is_not_an_edit(dialog):
    """Opening the dialog shows the current note without re-publishing it."""
    published = []
    dialog.sigNotesChanged.connect(published.append)

    dialog.setNotes(NOTE)

    assert dialog.getNotes() == NOTE
    assert published == []


def test_dialog_is_modeless(dialog):
    """The point is to jot something down while an experiment is running."""
    assert not dialog.isModal()
