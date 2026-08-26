from dataclasses import replace

import pytest

from slmcore import (
    DEFAULT_REGISTRIES,
    SectionPresentation,
    SLMConfig,
    SLMGeometry,
    SLMIdentity,
    SLMRuntime,
)
from slmcore.calibration import SLMSectionCalibration
from slmcore.engine.parameters import (
    EditorKind,
    FourierDisplacementConverter,
    METRIC_UNIT,
    ParamSpec,
    PeriodDisplacementConverter,
    SLM_UNIT,
)
from slmcore.engine.section import split_slm_geometry


def _runtime() -> SLMRuntime:
    geometry = SLMGeometry(width=8,height=6,pixel_size_um=1.0)
    return SLMRuntime(
        identity=SLMIdentity("slm","SER123"),
        geometry=geometry,
        section_geometries=split_slm_geometry(geometry,1),
        registries=DEFAULT_REGISTRIES,
    )

def test_param_field_unit_switch_is_signal_silent_and_does_not_convert_bounds():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.widgets.fields import ParamField
    except Exception as error:
        pytest.skip(f"ParamField dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    calibration = SLMSectionCalibration(
        kx_per_um=0.01,
        ky_per_um=0.01,
    )

    definition = ParamSpec(
        6.0,
        float,
        min_value=1e-9,
        converter=FourierDisplacementConverter("x"),
        step_by_unit={
            SLM_UNIT:0.05,
            METRIC_UNIT:0.005,
        },
        decimals_by_unit={
            SLM_UNIT:2,
            METRIC_UNIT:4,
        },
        editor=EditorKind.DOUBLE_SPIN_BOX,
    )

    field = ParamField(
        "period_x_px",
        definition,
        conversion_context=lambda:calibration,
    )

    changes = []
    field.sigValueChanged.connect(
        lambda key,value: changes.append((key,value))
    )

    canonical_before = field.value()
    expected_metric = definition.to_unit(
        canonical_before,
        METRIC_UNIT,
        calibration,
    )

    try:
        field.set_unit_mode(METRIC_UNIT)
        app.processEvents()

        # Presentation changes must never become parameter edits.
        assert changes == []
        assert field.value() == canonical_before

        # Converted editors do not infer bounds from canonical min/max.
        assert field.editor.minimum() < -1e99
        assert field.editor.maximum() > 1e99

        # The canonical value is still rendered correctly in metric units.
        assert field.editor.value() == pytest.approx(
            expected_metric,
            abs=1e-4,
        )

        # Switching back must also be signal-silent.
        field.set_unit_mode(SLM_UNIT)
        app.processEvents()

        assert changes == []
        assert field.value() == canonical_before
        assert field.editor.value() == pytest.approx(canonical_before)

    finally:
        field.deleteLater()
        
def test_set_section_presentation_is_lightweight():
    runtime = _runtime()
    section_key = "sec_0"

    before_slm_revision = runtime.revision
    before_section_revision = runtime.get_section_snapshot(section_key).revision
    before_slm_artifacts = runtime.artifacts
    before_frame = runtime.artifacts.eightbit
    before_section_artifacts = runtime.get_section_artifacts(section_key)
    before_cgh_status = runtime.get_section_cgh_status(section_key)

    snapshot = runtime.set_section_presentation(
        section_key,
        SectionPresentation(show_calibration_interface=False),
    )

    assert snapshot is not None
    assert snapshot.revision == before_section_revision
    assert runtime.revision == before_slm_revision
    assert runtime.artifacts is before_slm_artifacts
    assert runtime.artifacts.eightbit is before_frame
    assert runtime.get_section_artifacts(section_key) is before_section_artifacts
    assert runtime.get_section_cgh_status(section_key) == before_cgh_status
    assert not snapshot.presentation.show_calibration_interface
    assert not runtime.get_section_snapshot(
        section_key,
    ).presentation.show_calibration_interface

    assert runtime.set_section_presentation(
        section_key,
        SectionPresentation(show_calibration_interface=False),
    ) is None


def test_section_presentation_roundtrips_and_old_configs_default_visible():
    runtime = _runtime()
    section_key = "sec_0"
    runtime.set_section_presentation(
        section_key,
        SectionPresentation(show_calibration_interface=False),
    )

    config = runtime.create_config()
    data = config.to_dict()

    assert data["sections"][section_key]["presentation"] == {
        "show_calibration_interface":False,
    }

    loaded,warnings = SLMConfig.from_dict(data,DEFAULT_REGISTRIES)

    assert warnings == ()
    assert not loaded.sections[
        section_key
    ].presentation.show_calibration_interface

    old_data = config.to_dict()
    del old_data["sections"][section_key]["presentation"]

    loaded_old,warnings = SLMConfig.from_dict(old_data,DEFAULT_REGISTRIES)

    assert warnings == ()
    assert loaded_old.sections[
        section_key
    ].presentation.show_calibration_interface


def test_section_presentation_title_roundtrips_and_legacy_tab_names_migrate():
    runtime = _runtime()
    section_key = "sec_0"
    runtime.set_section_presentation(
        section_key,
        SectionPresentation(title="Full SLM"),
    )

    config = runtime.create_config()
    data = config.to_dict()

    assert data["sections"][section_key]["presentation"] == {
        "show_calibration_interface":True,
        "title":"Full SLM",
    }

    loaded,warnings = SLMConfig.from_dict(data,DEFAULT_REGISTRIES)

    assert warnings == ()
    assert loaded.sections[section_key].presentation.title == "Full SLM"

    legacy_data = config.to_dict()
    del legacy_data["sections"][section_key]["presentation"]["title"]
    legacy_data["tab_names"] = {section_key:"Migrated title"}

    from slmcore.config import migrate_slm_config_dict

    migrated = migrate_slm_config_dict(legacy_data)
    loaded_legacy,warnings = SLMConfig.from_dict(migrated,DEFAULT_REGISTRIES)

    assert warnings == ()
    assert loaded_legacy.sections[
        section_key
    ].presentation.title == "Migrated title"


def test_config_load_applies_presentation_with_existing_revision_semantics():
    source = _runtime()
    target = _runtime()
    section_key = "sec_0"

    source.set_section_presentation(
        section_key,
        SectionPresentation(show_calibration_interface=False),
    )
    before_revision = target.get_section_snapshot(section_key).revision

    report = target.load_config(source.create_config())
    result = report.section_results[section_key]

    assert result.snapshot.revision == before_revision + 1
    assert not result.frame_changed
    assert not result.snapshot.presentation.show_calibration_interface
    assert not target.get_section_snapshot(
        section_key,
    ).presentation.show_calibration_interface


def test_section_view_resets_to_slm_units_before_hiding():
    pytest.importorskip("qtpy")
    try:
        from qtpy import QtWidgets
    except Exception as error:
        pytest.skip(f"Qt bindings are unavailable: {error}")
    pytest.importorskip("pyqtgraph")
    try:
        from slmcore.qt.sections.view import SectionView
    except Exception as error:
        pytest.skip(f"SectionView dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    runtime = _runtime()
    snapshot = runtime.get_section_snapshot("sec_0")
    snapshot = replace(
        snapshot,
        calibration=SLMSectionCalibration(kx_per_um=0.01,ky_per_um=0.02),
    )
    view = SectionView(section_key="sec_0",snapshot=snapshot)
    try:
        assert view.set_unit_mode(METRIC_UNIT)

        view.apply_presentation(
            SectionPresentation(show_calibration_interface=False),
        )

        assert view.unit_mode == SLM_UNIT
        assert view.calibration_interface_widget is not None
        assert view.calibration_interface_widget.isHidden()
        assert not view.set_unit_mode(METRIC_UNIT)
    finally:
        view.deleteLater()


def test_param_field_spinbox_commits_live_text_without_enter():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.widgets.fields import ParamField
    except Exception as error:
        pytest.skip(f"ParamField dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    field = ParamField(
        "period",
        ParamSpec(
            0,int,
            min_value=-1000,
            max_value=1000,
            editor=EditorKind.SPIN_BOX,
        ),
    )
    changes = []
    field.sigValueChanged.connect(
        lambda key,value: changes.append((key,value))
    )

    line_edit = field.editor.lineEdit()
    line_edit.setText("123")
    line_edit.textEdited.emit("123")
    app.processEvents()

    try:
        assert changes == [("period",123)]
        assert field.value() == 123
    finally:
        field.deleteLater()


def test_param_field_double_spinbox_commits_dot_decimal_live_text():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.widgets.fields import ParamField
    except Exception as error:
        pytest.skip(f"ParamField dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    field = ParamField(
        "phase",
        ParamSpec(
            0.0,float,
            min_value=-10.0,
            max_value=10.0,
            editor=EditorKind.DOUBLE_SPIN_BOX,
        ),
    )
    changes = []
    field.sigValueChanged.connect(
        lambda key,value: changes.append((key,value))
    )

    line_edit = field.editor.lineEdit()
    line_edit.setText("1.25")
    line_edit.textEdited.emit("1.25")
    app.processEvents()

    try:
        assert changes == [("phase",1.25)]
        assert field.value() == 1.25
    finally:
        field.deleteLater()


def test_param_field_double_spinbox_uses_param_spec_step_and_decimals():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.widgets.fields import ParamField
    except Exception as error:
        pytest.skip(f"ParamField dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    field = ParamField(
        "phase",
        ParamSpec(
            0.0,float,
            step=0.25,
            decimals=2,
            editor=EditorKind.DOUBLE_SPIN_BOX,
        ),
    )

    try:
        assert field.editor.singleStep() == 0.25
        assert field.editor.decimals() == 2
    finally:
        field.deleteLater()


def test_param_field_uses_param_spec_unit_steps_and_decimals():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.widgets.fields import ParamField
    except Exception as error:
        pytest.skip(f"ParamField dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    calibration = SLMSectionCalibration(kx_per_um=0.01,ky_per_um=0.01)
    field = ParamField(
        "period",
        ParamSpec(
            0,int,
            step_by_unit={SLM_UNIT:3,METRIC_UNIT:0.01},
            decimals_by_unit={SLM_UNIT:0,METRIC_UNIT:2},
            converter=PeriodDisplacementConverter(axis="x"),
            editor=EditorKind.SPIN_BOX,
        ),
        conversion_context=lambda:calibration,
    )

    try:
        assert field.editor.singleStep() == 3.0
        assert field.editor.decimals() == 0
        field.set_unit_mode(METRIC_UNIT)
        assert field.editor.singleStep() == 0.01
        assert field.editor.decimals() == 2
    finally:
        field.deleteLater()


def test_param_spec_rejects_mixed_or_invalid_unit_presentation():
    converter = PeriodDisplacementConverter(axis="x")

    with pytest.raises(ValueError,match="step is only valid without"):
        ParamSpec(0,int,step=1,converter=converter)

    with pytest.raises(ValueError,match="decimals is only valid without"):
        ParamSpec(0,int,decimals=0,converter=converter)

    with pytest.raises(ValueError,match="require a converter"):
        ParamSpec(0,int,step_by_unit={SLM_UNIT:1,METRIC_UNIT:0.01})

    with pytest.raises(ValueError,match="supported units"):
        ParamSpec(
            0,int,
            converter=converter,
            step_by_unit={SLM_UNIT:1},
        )


def test_cgh_square_activation_captures_current_unit_in_one_patch():
    pytest.importorskip("qtpy")
    pytest.importorskip("pyqtgraph")
    try:
        from qtpy import QtWidgets
        from slmcore.qt.sections.view import SectionView
    except Exception as error:
        pytest.skip(f"SectionView dependencies are unavailable: {error}")

    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])

    runtime = _runtime()
    snapshot = replace(
        runtime.get_section_snapshot("sec_0"),
        calibration=SLMSectionCalibration(
            kx_per_um=0.01,
            ky_per_um=0.012,
        ),
    )
    view = SectionView(section_key="sec_0",snapshot=snapshot)
    patches = []
    view.sigPatchRequested.connect(
        lambda section_key,changes:patches.append(
            (section_key,dict(changes)),
        )
    )

    try:
        assert view.set_unit_mode(METRIC_UNIT)
        app.processEvents()
        assert patches == []

        cgh_view = view.groups["cgh"]
        square_field = cgh_view.binding.fields[
            ("multi_foci","params","square")
        ]
        square_field.editor.setChecked(True)
        app.processEvents()

        assert patches == [
            (
                "sec_0",
                {
                    ("cgh","multi_foci","params","square"):True,
                    (
                        "cgh","multi_foci","params","square_unit"
                    ):METRIC_UNIT,
                },
            )
        ]
    finally:
        view.deleteLater()
