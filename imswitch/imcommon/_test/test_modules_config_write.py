"""The modules file is rewritten only when it changes, and then atomically:
importing ImSwitch in several processes at once (parallel test workers, two
instances) used to truncate and rewrite it in each, and a process reading it
in that moment failed with a JSONDecodeError."""
import importlib
import os


def test_importing_again_leaves_an_unchanged_modules_file_alone(tmp_path, monkeypatch):
    from imswitch.imcommon.model import modulesconfigtools
    path = modulesconfigtools._modulesFilePath
    before = os.stat(path).st_mtime_ns
    with open(path) as handle:
        text = handle.read()
    importlib.reload(modulesconfigtools)
    assert os.stat(path).st_mtime_ns == before
    with open(path) as handle:
        assert handle.read() == text
    assert not [name for name in os.listdir(os.path.dirname(path)) if name.endswith('.tmp')]
