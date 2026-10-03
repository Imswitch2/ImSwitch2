from pathlib import Path


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "slmcore"


def test_non_qt_package_does_not_import_qt():
    root = _package_root()
    offenders = []
    for path in root.rglob("*.py"):
        if "qt" in path.relative_to(root).parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "from qtpy" in text or "import qtpy" in text or "PyQt" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []



def test_core_package_has_no_upward_architecture_imports():
    root = _package_root()
    core = root / "core"
    forbidden = (
        "slmcore.application",
        "slmcore.workspace",
        "slmcore.host",
        "slmcore.qt",
        "from ...application",
        "from ...workspace",
        "from ...host",
        "from ...qt",
        "from ..application",
        "from ..workspace",
        "from ..host",
        "from ..qt",
    )
    offenders = []
    for path in core.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append((str(path.relative_to(root)),token))
    assert offenders == []


def test_removed_pre_release_package_boundaries_do_not_exist():
    root = _package_root()
    for name in (
        "engine","config","calibration","corrections","cgh","measurement","patterns",
    ):
        assert not (root / name).exists()

def test_normal_qt_application_mutations_route_through_application_session():
    root = _package_root() / "qt"
    forbidden = (
        "runtime.set_section_calibration(",
        "runtime.clear_section_cgh_session(",
        "runtime.apply_section_topology(",
        "runtime.set_section_presentation(",
        "runtime.load_config(",
        "runtime.commit_section_cgh(",
    )
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                offenders.append((str(path.relative_to(root)),token))
    assert offenders == []

    # RuntimeViewBinding deliberately retains exactly two low-level fallbacks
    # for use without an SLMSession: ordinary patching and CGH-target restore.
    binding = (root / "application" / "runtime_binding.py").read_text(
        encoding="utf-8"
    )
    assert binding.count("self.runtime.apply_section_patch(") == 1
    assert binding.count("self.runtime.restore_section_current_cgh_target(") == 1


def test_patch8_calibration_management_has_distinct_persistent_domains():
    root = _package_root()
    ui = (root / "qt" / "calibration" / "management_dialogs.py").read_text(encoding="utf-8")
    manager = (root / "qt" / "application" / "calibration" / "manager.py").read_text(encoding="utf-8")
    assert '"Mapping Calibration"' in ui
    assert '"Observation Geometry"' in ui
    assert '"Observation Planes…"' in ui
    assert "sigCalibrationRequested.connect(self.open_management)" in manager
    assert "sigCalibrationRequested.connect(self.open_dialog)" not in manager


def test_patch8_observation_plane_editor_is_shared_by_measurement_selectors():
    root = _package_root()
    calibration = (root / "qt" / "calibration" / "dialog.py").read_text(encoding="utf-8")
    feedback = (root / "qt" / "cgh" / "session_window.py").read_text(encoding="utf-8")
    manager = (root / "qt" / "application" / "calibration" / "manager.py").read_text(encoding="utf-8")
    assert "Define new observation plane…" in calibration
    assert "Define new observation plane…" in feedback
    assert "ObservationPlaneEditorDialog" in manager
