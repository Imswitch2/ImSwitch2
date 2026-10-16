from pathlib import Path


def _package_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "slmcore"


def test_public_non_qt_exports_resolve():
    import slmcore
    import slmcore.application as application
    import slmcore.workspace as workspace

    for module in (slmcore,application,workspace):
        missing = [name for name in module.__all__ if not hasattr(module,name)]
        assert missing == []
        assert len(module.__all__) == len(set(module.__all__))


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


def test_calibration_management_has_distinct_persistent_domains():
    root = _package_root()
    ui = (root / "qt" / "calibration" / "management_dialogs.py").read_text(encoding="utf-8")
    manager = (root / "qt" / "application" / "calibration" / "manager.py").read_text(encoding="utf-8")
    assert '"Mapping"' in ui
    assert '"Observation & Geometry"' in ui
    assert '"Orientation by section"' in ui
    assert '"Registration by section"' in ui
    assert "sigObservationSelectionChanged" in ui
    assert "sigObservationRegistrationRequested" in ui
    assert "observation_registration_calibration_service" in manager
    assert "sigCalibrationRequested.connect(self._on_management_requested)" in manager
    assert "sigCalibrationRequested.connect(self.open_dialog)" not in manager


def test_observation_plane_editor_is_shared_by_measurement_selectors():
    root = _package_root()
    calibration = (root / "qt" / "calibration" / "dialog.py").read_text(encoding="utf-8")
    feedback = (root / "qt" / "cgh" / "session_window.py").read_text(encoding="utf-8")
    manager = (root / "qt" / "application" / "calibration" / "manager.py").read_text(encoding="utf-8")
    assert "Define new observation plane…" in calibration
    assert "Define new observation plane…" in feedback
    assert "ObservationPlaneEditorDialog" in manager


def test_editable_lattice_core_is_neutral_and_fov_workflow_is_dedicated():
    root = _package_root()
    neutral = root / "core" / "measurement" / "reference_geometry.py"
    assert neutral.exists()
    reference = (root / "core" / "cgh" / "feedback" / "reference.py").read_text(encoding="utf-8")
    assert "measurement.reference_geometry" in reference
    manager = (root / "qt" / "application" / "calibration" / "manager.py").read_text(encoding="utf-8")
    assert "FOVCalibrationDialog" in manager
    assert "fov_calibration_service" in manager
    assert "controller._feedback.open_fov_calibration" not in manager
    feedback_ui = (root / "qt" / "cgh" / "session_window.py").read_text(encoding="utf-8")
    assert "Build fit from current feedback" not in feedback_ui
    assert "Persistent FOV fitting lives in the dedicated Calibration workspace" in feedback_ui


def test_calibration_workspace_embeds_calibration_modes_in_one_window():
    root = _package_root()
    ui = (root / "qt" / "calibration" / "management_dialogs.py").read_text(encoding="utf-8")
    manager = (root / "qt" / "application" / "calibration" / "manager.py").read_text(encoding="utf-8")
    section_view = (root / "qt" / "sections" / "view.py").read_text(encoding="utf-8")
    base_dialog = (root / "qt" / "calibration" / "dialog.py").read_text(encoding="utf-8")
    fov_dialog = (root / "qt" / "calibration" / "fov_dialog.py").read_text(encoding="utf-8")
    geometry_dialog = (root / "qt" / "calibration" / "geometry_calibration_dialog.py").read_text(encoding="utf-8")

    assert 'QPushButton("Manage…")' in section_view
    assert 'QPushButton("Workspace…")' not in section_view
    assert 'QToolButton()' not in section_view
    assert section_view.index('self.calibration_label = QtWidgets.QLabel()') < section_view.index('QtWidgets.QLabel("Mapping:")')
    assert "mount_workspace" in ui
    assert '"Mapping"' in ui
    assert '"Observation & Geometry"' in ui
    assert "open_dialog(section_key,embedded=True)" in manager
    assert "open_fov_calibration(section_key,mapping_name,embedded=True)" in manager
    assert "embedded=True" in manager
    coordinator = (root / "qt" / "application" / "workspace_coordinator.py").read_text(encoding="utf-8")
    assert "SLMSectionAddress" in coordinator
    assert "attach_management_view" in coordinator
    assert "sigSlmChanged" in coordinator
    assert "set_context_switching_enabled" in coordinator
    assert "open_mapping" in coordinator
    assert "select_mapping_tab" in coordinator
    for source in (base_dialog,fov_dialog,geometry_dialog):
        assert "embedded: bool=False" in source
        assert "QtCore.Qt.Widget" in source
