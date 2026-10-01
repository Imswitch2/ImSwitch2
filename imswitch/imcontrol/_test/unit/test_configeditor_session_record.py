"""What an editing session tells its embedder it changed.

imcontrol can only offer a useful restart prompt if the editor is honest about
two things: which files it wrote, and whether it repointed ImSwitch at a
different setup. Both are recorded as they happen rather than inferred at close
time, because by then the file on disk looks the same whoever wrote it.
"""

import json

import pytest

pytest.importorskip("PyQt5")

from imswitch.imcontrol.view.configeditor import editor


class _Session(editor.MainWindow):
    """A MainWindow's bookkeeping without its 1300 widgets.

    Constructing the real window needs a schema load, a catalog and a Qt
    event loop; none of that is what these assertions are about.
    """

    def __init__(self):  # noqa: D107 - deliberately does not call super()
        self._saved_files = set()
        self._active_config_changed = False


def test_a_session_that_wrote_nothing_reports_nothing():
    session = _Session()
    assert session.saved_files() == frozenset()
    assert session.active_config_changed is False


def test_saved_paths_are_recorded_in_a_comparable_spelling(tmp_path):
    """The embedder compares against a resolved path, so this must be one."""
    setups = tmp_path / "imcontrol_setups"
    setups.mkdir()
    target = setups / "scope.json"
    target.write_text("{}", encoding="utf-8")

    session = _Session()
    # The same file, reached the way a file dialog might hand it over.
    session._note_saved(str(setups / ".." / "imcontrol_setups" / "scope.json"))

    assert session.saved_files() == frozenset({str(target.resolve())})


def test_an_unresolvable_path_is_still_recorded():
    """Better a path that may not match than a save that went unnoticed."""
    session = _Session()
    session._note_saved("\0not-a-path")
    assert len(session.saved_files()) == 1


def test_writing_a_file_records_it(tmp_path, monkeypatch):
    """_write_file is the single funnel every save goes through."""
    session = _Session()
    session._data = {"detectors": {}}
    session._status = _Label()
    session.setWindowTitle = lambda title: None

    target = tmp_path / "scope.json"
    session._write_file(str(target))

    assert json.loads(target.read_text(encoding="utf-8")) == {"detectors": {}}
    assert session.saved_files() == frozenset({str(target.resolve())})
    assert session._modified is False


def test_a_failed_write_is_not_recorded(tmp_path, monkeypatch):
    session = _Session()
    session._data = {}
    session._status = _Label()

    shown = []
    monkeypatch.setattr(editor.QMessageBox, "critical",
                        lambda *args: shown.append(args))

    session._write_file(str(tmp_path / "no_such_dir" / "scope.json"))

    assert shown, "the operator must be told the save failed"
    assert session.saved_files() == frozenset()


def test_switching_the_active_config_is_recorded(tmp_path):
    options = tmp_path / "imcontrol_options.json"
    options.write_text(json.dumps({"setupFileName": "old.json"}), encoding="utf-8")
    setup = tmp_path / "new.json"
    setup.write_text("{}", encoding="utf-8")

    session = _Session()
    session._modified = False
    session._path = str(setup)
    session._options_path = str(options)
    session._status = _Label()
    session._update_active_banner = lambda: None

    session._set_as_active_config()

    assert session.active_config_changed is True
    assert json.loads(options.read_text(encoding="utf-8"))["setupFileName"] == "new.json"


class _Label:
    """The one status-bar method _write_file and friends reach for."""

    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text
