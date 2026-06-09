"""Contract tests for the file-watcher output subdirectory.

Exercises Reconstructor.default_save_subdir and the helpers on
WatcherFrameController that consume it. The watcher's full Qt-coupled
toggleWatch path isn't called here; we only verify the contract that the
main controller relies on when it routes the active reconstructor's
default_save_subdir to the watcher.
"""

from types import SimpleNamespace

from imswitch.improcess.controller.WatcherFrameController import (
    WatcherFrameController,
)
from imswitch.improcess.reconstructors.base import Reconstructor


# --- base contract ---------------------------------------------------------


def test_base_reconstructor_default_save_subdir_is_rec():
    assert Reconstructor.default_save_subdir == 'rec'


def test_view_only_inherits_default_save_subdir():
    from imswitch.improcess.reconstructors.view_only import ViewOnlyReconstructor

    assert ViewOnlyReconstructor.default_save_subdir == 'rec'


def test_modality_reconstructors_inherit_default_save_subdir():
    from imswitch.improcess.reconstructors.monalisa import MonalisaReconstructor
    from imswitch.improcess.reconstructors.snouty import SnoutyReconstructor

    # Plugins are free to override later — this just locks in that the
    # current built-ins inherit the base 'rec' default, so changing the
    # base attribute name in the future is a single-line edit.
    for cls in (MonalisaReconstructor, SnoutyReconstructor):
        assert cls.default_save_subdir == 'rec', cls.__name__


# --- watcher setters -------------------------------------------------------


def _make_watcher_stub():
    """Bind the setter methods to a bare stub — the full controller pulls
    in Qt + comm channel + the FileWatcher background thread, which is far
    beyond what these tests need."""
    stub = SimpleNamespace(_saveSubdir='rec', _savePrefix='rec_')
    stub.setSaveSubdir = WatcherFrameController.setSaveSubdir.__get__(stub)
    stub.setSavePrefix = WatcherFrameController.setSavePrefix.__get__(stub)
    return stub


def test_set_save_subdir_overrides_default():
    stub = _make_watcher_stub()
    stub.setSaveSubdir('deskew')
    assert stub._saveSubdir == 'deskew'


def test_set_save_subdir_falls_back_when_empty():
    stub = _make_watcher_stub()
    stub.setSaveSubdir('')
    assert stub._saveSubdir == 'rec'
    stub.setSaveSubdir(None)
    assert stub._saveSubdir == 'rec'


def test_set_save_subdir_strips_separators_and_whitespace():
    stub = _make_watcher_stub()
    stub.setSaveSubdir('  /out/  ')
    assert stub._saveSubdir == 'out'


def test_set_save_prefix_overrides_default():
    stub = _make_watcher_stub()
    stub.setSavePrefix('deskew_')
    assert stub._savePrefix == 'deskew_'


def test_set_save_prefix_accepts_empty_string():
    stub = _make_watcher_stub()
    stub.setSavePrefix('')
    assert stub._savePrefix == ''
    stub.setSavePrefix(None)
    assert stub._savePrefix == ''
