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
