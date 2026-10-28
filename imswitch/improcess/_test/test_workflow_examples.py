"""The example scripts in examples/improcess_workflows run as their headers say.

They broke silently once: View-only started reading an untagged recording as
Frame, Y, X, and all three channel examples failed on "axis 'C' not in
['Frame', 'Y', 'X']" with nothing in the suite noticing. Each is now run here,
on the synthetic recording its own no-argument path would write.
"""

import importlib.util
import sys
from pathlib import Path

import h5py
import pytest

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "improcess_workflows"


def _module(name):
    if str(EXAMPLES) not in sys.path:
        sys.path.insert(0, str(EXAMPLES))     # the scripts import _synthetic and each other
    spec = importlib.util.spec_from_file_location(f"wf_example_{name}", EXAMPLES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_synthetic_recording_names_its_axes_only_when_asked(tmp_path):
    synthetic = _module("_synthetic")
    plain = synthetic.write_synthetic_recording(tmp_path / "plain.h5", frames=2)
    tagged = synthetic.write_synthetic_recording(tmp_path / "tagged.h5", frames=2, axes="CYX")
    with h5py.File(plain, "r") as handle:
        assert "axes" not in handle["data"].attrs
    with h5py.File(tagged, "r") as handle:
        assert handle["data"].attrs["axes"] == "CYX"
    with pytest.raises(ValueError, match="name 2 axes; the recording has 3"):
        synthetic.write_synthetic_recording(tmp_path / "bad.h5", axes="YX")


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """The scripts' no-argument path makes its own folder; keep it in tmp_path."""
    import tempfile

    made = []

    def mkdtemp(prefix=""):
        folder = tmp_path / f"{prefix.strip('_')}{len(made)}"
        folder.mkdir()
        made.append(folder)
        return str(folder)

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    return made


def test_the_diamond_example_runs_and_merges_both_branches(scratch, capsys):
    assert _module("split_process_merge_diamond").main([]) == 0
    printed = capsys.readouterr().out
    assert "merged shape (2, 96, 96)" in printed and "2 filter branches" in printed
    assert (scratch[0] / "results" / "cells_diamond.h5").exists()


def test_the_batch_example_runs_over_a_folder(scratch):
    assert _module("batch_view_only_project_save").main([]) == 0
    assert sorted(p.name for p in (scratch[0] / "results").glob("*.ome.tif")) == [
        "cell_0_maxproj_blur.ome.tif", "cell_1_maxproj_blur.ome.tif", "cell_2_maxproj_blur.ome.tif",
    ]


def test_the_replay_example_reproduces_the_diamond_pixel_for_pixel(scratch):
    assert _module("replay_from_file").main([]) == 0
