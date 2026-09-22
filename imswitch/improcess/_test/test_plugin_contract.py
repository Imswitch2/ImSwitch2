"""The headless parameter contract: what an undeclared or mismatched plugin gets.

Acceptance criteria of the pre-push cleanup, one test each:

1. an inherited framework ``default_params`` makes workflow validation refuse
   the plugin;
2. GUI provenance from it is non-replayable with a clear reason;
3. an explicit ``return {}`` is a valid contract (the Invert example);
4. every example's widget and defaults match (see test_plugin_param_contract);
5. the Gaussian example accepts and records ``sigma`` in a workflow;
6. Photophysics records every effective parameter, hidden ones included;
7. Photophysics saving returns a receipt with the CSV and the companion;
8. the author-facing helper detects a mismatch.
"""

import json
import os
from pathlib import Path

import h5py
import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from imswitch.improcess.model.array_result import ArrayProcessingResult  # noqa: E402
from imswitch.improcess.model.plugin_contract import (  # noqa: E402
    check_plugin_contract,
    contract_problem,
    has_param_contract,
    record_widget_check,
)
from imswitch.improcess.model.provenance import output_node  # noqa: E402
from imswitch.improcess.plugins.user_plugins import discover_processor_plugins  # noqa: E402
from imswitch.improcess.processors.base import Processor  # noqa: E402
from imswitch.improcess.processors.run import run_processor  # noqa: E402
from imswitch.improcess.reconstructors.base import Reconstructor  # noqa: E402
from imswitch.improcess.workflows import (  # noqa: E402
    Process,
    Reconstruct,
    Source,
    Workflow,
    bootstrap_registry,
    run,
    validate,
)
from imswitch.improcess.workflows.runtime import describe_registry  # noqa: E402

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples" / "improcess_plugins"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    from qtpy import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture(scope="module")
def examples():
    classes, errors = discover_processor_plugins(str(_EXAMPLES_DIR))
    assert errors == []
    return classes


@pytest.fixture
def registry(examples):
    registry = bootstrap_registry(user_plugins=False)
    for cls in examples.values():
        registry.register_processor(cls())
    return registry


def _h5(path, shape=(2, 8, 8)):
    with h5py.File(str(path), "w") as handle:
        handle.create_dataset("data", data=np.random.default_rng(0).random(shape).astype(np.float32))
    return path


def _image(shape=(4, 4)):
    return ArrayProcessingResult("img", np.random.default_rng(0).random(shape).astype(np.float32), ["Y", "X"])


class _Undeclared(Processor):
    """A plugin written before the contract existed: widget only."""

    name = "Undeclared"
    id = "test.undeclared"
    kinds = ("image",)

    @property
    def applies_to(self):
        return lambda result: True

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: {"gain": 2.0}
        return widget

    def apply(self, result, params):
        return ArrayProcessingResult(result.name, np.asarray(result.data) * params.get("gain", 2.0), list(result.axis_labels))


class _FamilyBase(Processor):
    """An intermediate base that declares the contract for its subclasses."""

    kinds = ("image",)

    @classmethod
    def default_params(cls):
        return {"gain": 1.0}

    @property
    def applies_to(self):
        return lambda result: True

    def make_param_widget(self, parent):
        from qtpy import QtWidgets

        widget = QtWidgets.QWidget(parent)
        widget.get_values = lambda: dict(self.default_params())
        return widget

    def apply(self, result, params):
        return ArrayProcessingResult(result.name, np.asarray(result.data) * params["gain"], list(result.axis_labels))


class _FamilyMember(_FamilyBase):
    name = "Family member"
    id = "test.family-member"


class _Mismatched(_FamilyBase):
    name = "Mismatched"
    id = "test.mismatched"

    def make_param_widget(self, parent):
        widget = super().make_param_widget(parent)
        widget.get_values = lambda: {"gain": 3.0, "extra": 1}
        return widget


class _UndeclaredReconstructor(Reconstructor):
    name = "Undeclared recon"
    id = "test.undeclared-recon"

    def make_param_widget(self, parent):
        return None

    def make_metadata_dialog(self, parent):
        return None

    def process(self, data_obj, params, context=None):
        return ArrayProcessingResult("r", np.zeros((4, 4), np.float32), ["Y", "X"])


# 1. an inherited framework default is refused by validation ---------------------------------------

def test_an_undeclared_plugin_is_refused_by_workflow_validation(registry, tmp_path):
    registry.register_processor(_Undeclared())
    registry.register_reconstructor(_UndeclaredReconstructor())
    raw = _h5(tmp_path / "a.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                        Process("p", "test.undeclared", inputs=["rec"])])
    issues = [str(i) for i in validate(wf, registry)]
    assert len(issues) == 1 and "declares no parameter contract" in issues[0] and "GUI-only" in issues[0]
    wf_recon = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "test.undeclared-recon", inputs=["raw"])])
    issues = [str(i) for i in validate(wf_recon, registry)]
    assert len(issues) == 1 and "test.undeclared-recon" in issues[0] and "cannot run in a workflow" in issues[0]
    assert not has_param_contract(_Undeclared) and has_param_contract(_FamilyMember)   # resolved through the MRO


def test_the_list_command_marks_gui_only_plugins(registry, capsys, monkeypatch):
    registry.register_processor(_Undeclared())
    description = describe_registry(registry)
    assert description["processors"]["test.undeclared"]["gui_only"]
    assert description["processors"]["example.gaussian-blur"]["gui_only"] is None
    from imswitch.improcess.workflows import __main__ as cli

    monkeypatch.setattr(cli, "_registry", lambda args: registry)
    assert cli.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "test.undeclared" in out and "GUI-ONLY" in out.split("test.undeclared", 1)[1].splitlines()[0]


# 2. GUI provenance from it is non-replayable with the reason ---------------------------------------

def test_gui_provenance_of_an_undeclared_plugin_is_not_replayable():
    results, failures = run_processor(_Undeclared(), [_image()], {"gain": 2.0}, None)
    assert failures == []
    node = output_node(results[0])
    assert node["replayable"] is False
    assert any("declares no parameter contract" in reason for reason in node["reasons"])


def test_a_recorded_widget_mismatch_reaches_the_provenance_and_the_validator(registry, tmp_path):
    from qtpy import QtWidgets

    plugin = _Mismatched()
    problems = record_widget_check(plugin, plugin.make_param_widget(QtWidgets.QWidget()))
    assert problems and contract_problem(plugin)
    results, _ = run_processor(plugin, [_image()], {"gain": 3.0}, None)
    node = output_node(results[0])
    assert node["replayable"] is False and any("disagrees" in r for r in node["reasons"])
    registry.register_processor(plugin)
    raw = _h5(tmp_path / "a.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                        Process("p", "test.mismatched", inputs=["rec"])])
    assert any("disagrees" in str(i) for i in validate(wf, registry))
    delattr(_Mismatched, "_param_contract_problem")


# 3. an explicit empty declaration is a contract ----------------------------------------------------

def test_an_explicit_empty_declaration_is_valid(registry, examples, tmp_path):
    invert = examples["example.invert"]
    assert has_param_contract(invert) and contract_problem(invert) is None
    raw = _h5(tmp_path / "a.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                        Process("inv", "example.invert", inputs=["rec"])])
    assert validate(wf, registry) == []
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        node = output_node(report.result("inv"))
        assert node["replayable"] is True and node["params"] == {}


# 5. the Gaussian example accepts and records sigma ----------------------------------------------------

def test_gaussian_workflows_accept_and_record_sigma(registry, tmp_path):
    pytest.importorskip("scipy")
    raw = _h5(tmp_path / "a.h5")
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                        Process("blur", "example.gaussian-blur", {"sigma": 3.0}, inputs=["rec"])])
    assert validate(wf, registry) == []
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        node = output_node(report.result("blur"))
        assert node["params"] == {"sigma": 3.0} and node["replayable"] is True
    wf_default = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                                Process("blur", "example.gaussian-blur", inputs=["rec"])])
    with run(wf_default, registry=registry, out_dir=tmp_path) as report:
        assert output_node(report.result("blur"))["params"] == {"sigma": 2.0}     # the widget default, recorded
    typo = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                          Process("blur", "example.gaussian-blur", {"sigm": 3.0}, inputs=["rec"])])
    assert any("unknown parameter" in str(i) for i in validate(typo, registry))


# 6./7. Photophysics records every effective parameter and saves through the protocol ---------------

def test_photophysics_records_effective_parameters_and_saves_with_a_receipt(registry, examples, tmp_path):
    raw = _h5(tmp_path / "stack.h5", shape=(40, 8, 8))
    wf = Workflow("w", [Source("raw", path=str(raw)), Reconstruct("rec", "view-only", inputs=["raw"]),
                        Process("ph", "photophysics_suite", {"tail": 5, "roi": [0, 4, 0, 4]}, inputs=["rec"])])
    assert validate(wf, registry) == []
    with run(wf, registry=registry, out_dir=tmp_path) as report:
        result = report.result("ph")
        node = output_node(result)
        declared = examples["photophysics_suite"].default_params()
        assert set(node["params"]) == set(declared)                     # every effective parameter
        assert node["params"]["tail"] == 5 and node["params"]["roi"] == [0, 4, 0, 4]
        assert node["params"]["mode"] == "fatigue" and node["replayable"] is True
        receipt = result.save(tmp_path / "curve.csv")
    assert [p.name for p in receipt.files] == ["curve.csv", "curve.provenance.json"]
    assert all(p.exists() for p in receipt.files)
    document = json.loads((tmp_path / "curve.provenance.json").read_text())
    assert document["artifact"]["primary"] == "curve.csv" and "graph" in document
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".curve.csv.")]   # nothing staged left behind
    with pytest.raises(Exception, match="cannot be written as|supports"):
        result.save(tmp_path / "curve.ome.tif", "tiff")


# 8. the author-facing helper detects a mismatch ----------------------------------------------------------

def test_the_author_helper_detects_a_mismatch_and_passes_a_match():
    problems = check_plugin_contract(_Mismatched)
    assert any("'gain'" in p for p in problems) and any("extra" in p for p in problems)
    assert contract_problem(_Mismatched)                       # remembered on the class
    delattr(_Mismatched, "_param_contract_problem")
    assert check_plugin_contract(_FamilyMember) == []
    assert any("not overridden" in p for p in check_plugin_contract(_Undeclared))


# Copyright (C) 2020-2026 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
