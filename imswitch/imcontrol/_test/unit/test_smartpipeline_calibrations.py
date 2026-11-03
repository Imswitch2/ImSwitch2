"""Calibration files: the folder as a registry, EtSTED's polynomial as a
pipeline transform, and the grid a calibration was made on checked at arm."""
import numpy as np
import pytest

from imswitch.imcommon.model.smartpipeline import (
    Action, FrameIdentity, Gate, Live, PipelineSettings, Polynomial3, ScaleFlip, SmartPipelineDocument,
    Transform, TransformRegistry,
)
from imswitch.imcommon.model.workflow.steps import Process, Workflow
from imswitch.imcontrol.model.EtSTEDTransformService import EtSTEDTransformService
from imswitch.imcontrol.model.measurement.controls import ControlExecutor
from imswitch.imcontrol.model.resources import ResourceRegistry, positioner_key
from imswitch.imcontrol.model.smartpipeline.calibrations import (
    CalibrationError, et_polynomial, identity_problems, load_calibrations, read_et_coefficients,
    save_calibration,
)
from imswitch.imcontrol.model.smartpipeline.engine import ArmError
from imswitch.imcontrol.model.smartpipeline.live_actions import live_backend
from imswitch.imcontrol.model.smartpipeline.live_source import LiveFrameSource
from imswitch.imcontrol.model.smartpipeline.mock import FunctionStepRunner
from imswitch.imcontrol.model.smartpipeline.run import PipelineRun
from imswitch.imcontrol._test.unit.test_smartpipeline_live import (
    FakeDetector, FakeDetectorsManager, FakePositioner, FakePositioners, centroid,
)

CAMERA = "detector:Camera"
STAGE = "stage:XY"


def test_a_folder_of_calibrations_loads_and_saves(tmp_path):
    folder = tmp_path / "calibrations"
    assert load_calibrations(folder).ids() == ()           # no folder yet: nothing, no error
    scale = ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.1, 0.1), calibrated_on={"shape": [64, 64]})
    poly = Polynomial3("et", CAMERA, "scanner:galvo", np.arange(20) / 10)
    assert save_calibration(scale, folder).name == "cam2stage.json"
    assert save_calibration(poly, folder).name == "et.json"
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    registry = load_calibrations(folder)
    assert registry.ids() == ("cam2stage", "et")
    assert registry.get("cam2stage") == scale and registry.get("et") == poly
    assert registry.get("cam2stage").calibrated_on == {"shape": [64, 64]}
    (folder / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(CalibrationError, match="broken.json"):
        load_calibrations(folder)
    (folder / "broken.json").unlink()
    with pytest.raises(CalibrationError, match="exists"):
        save_calibration(scale, folder, overwrite=False)


def test_an_etsted_calibration_file_becomes_a_polynomial_transform(tmp_path):
    coefficients = np.random.default_rng(3).normal(size=20)
    path = tmp_path / "2026-11-03.csv"
    # as EtSTED writes it, at full precision (another test may have narrowed numpy's print options)
    path.write_text("\n".join(repr(float(c)) for c in coefficients) + "\n", encoding="utf-8")
    assert read_et_coefficients(path) == tuple(float(c) for c in coefficients)
    transform = et_polynomial("etsted_2026-11-03", path, detector="Camera", target_frame="scanner:galvo",
                              swap_xy=True, invert_x=True, calibrated_on=FrameIdentity((512, 512), (2, 2)))
    assert transform.source_frame == CAMERA and transform.target_frame == "scanner:galvo"
    assert transform.calibrated_on == {"shape": [512, 512], "binning": [2, 2], "offset": [0, 0], "axis_order": "yx"}
    service = EtSTEDTransformService()
    service.set_coefficients(coefficients)

    class Flags:
        swapXY, invertX, invertY = True, True, False

    points = np.array([[10.0, 20.0], [3.5, -7.0]])        # (y, x) rows
    expected = np.array([service.poly_thirdorder_transform(coefficients, service.prepare_input_coords([x, y], Flags()))
                         for y, x in points])
    out = transform.apply(points)
    assert np.allclose(out[:, 1], expected[:, 0]) and np.allclose(out[:, 0], expected[:, 1])
    with pytest.raises(CalibrationError, match="not found"):
        read_et_coefficients(tmp_path / "nope.csv")
    with pytest.raises(Exception, match="20 coefficients"):
        et_polynomial("x", [1.0, 2.0], detector="Camera", target_frame=STAGE)


def test_identity_problems_name_what_differs():
    transform = ScaleFlip("c", CAMERA, STAGE, calibrated_on=FrameIdentity((64, 64)).to_dict())
    assert identity_problems(transform, FrameIdentity((64, 64))) == []
    assert identity_problems(transform, None) == []
    assert identity_problems(ScaleFlip("u", CAMERA, STAGE), FrameIdentity((1, 1))) == []   # says nothing
    problems = identity_problems(transform, FrameIdentity((32, 64), binning=(2, 2)))
    assert problems == ["transform 'c' was calibrated on another grid: shape (64, 64) -> (32, 64); "
                        "binning (1, 1) -> (2, 2)"]


def _document():
    document = SmartPipelineDocument(Workflow("p", [
        Live("cam", detector="Camera"),
        Process("c", "centroid", {}, inputs=["cam"]),
        Gate("g", "condition", inputs=["c"]),
        Transform("tf", "cam2stage", inputs=["g"]),
        Action("mv", "move-to", {"axes": ["Mock Y:Y", "Mock X:X"], "frame": STAGE}, inputs=["tf"]),
    ]))
    document.settings = PipelineSettings(roles={"scouting": "scout", "idle": "rest"})
    return document


def test_a_calibration_made_on_another_grid_refuses_the_arm_and_releases_everything():
    registry = ResourceRegistry()
    positioners = FakePositioners({"Mock X": FakePositioner("Mock X", "X", registry),
                                   "Mock Y": FakePositioner("Mock Y", "Y", registry)})
    detectors = FakeDetectorsManager([FakeDetector(shape=(64, 64), binning=1, frameStart=(0, 0))])
    transforms = TransformRegistry([ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.5, 0.5),
                                              calibrated_on=FrameIdentity((64, 64), binning=(2, 2)).to_dict())])
    source = LiveFrameSource(detectors, "Camera")
    run = PipelineRun(_document(), source=source, steps=FunctionStepRunner({"centroid": centroid}),
                      actions=live_backend(positioners, executor=ControlExecutor()), transforms=transforms,
                      mode="validate", resource_registry=registry, executor=ControlExecutor())
    with pytest.raises(ArmError, match="calibrated on another grid: binning \\(2, 2\\) -> \\(1, 1\\)"):
        run.arm()
    assert not source.leased and registry.holder(positioner_key("Mock X")) is None

    matching = TransformRegistry([ScaleFlip("cam2stage", CAMERA, STAGE, pixel_size=(0.5, 0.5),
                                            calibrated_on=FrameIdentity((64, 64)).to_dict())])
    source = LiveFrameSource(detectors, "Camera")
    run = PipelineRun(_document(), source=source, steps=FunctionStepRunner({"centroid": centroid}),
                      actions=live_backend(positioners, executor=ControlExecutor()), transforms=matching,
                      mode="validate", resource_registry=registry, executor=ControlExecutor())
    run.arm()
    assert source.leased
    run.stop()
    assert registry.holder(positioner_key("Mock X")) is None
