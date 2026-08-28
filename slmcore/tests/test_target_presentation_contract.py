from __future__ import annotations

from slmcore import (
    DEFAULT_REGISTRIES,
    TargetPresentation,
    TargetPresentationField,
    TargetPresentationFieldKind,
)
from slmcore.core.engine.parameters import EditorKind,METRIC_UNIT,ParamSpec,SLM_UNIT
from slmcore.core.engine.registry import TargetRegistration


def _summary_values(registration,params):
    return {
        field.key:tuple(params[key] for key in field.parameter_keys)
        for field in registration.presentation.summary_fields
    }


def test_multi_foci_targets_declare_user_facing_presentation():
    for target_key in ("multi_foci","multi_foci_vector"):
        registration = DEFAULT_REGISTRIES.targets[target_key]
        presentation = registration.presentation

        assert presentation.title == "Multi Foci"
        assert presentation.title != target_key
        assert presentation.title not in (
            "mf_51x51_px780_py785",
            "mfvec_51x51_px780_py785",
        )

        fields = presentation.summary_fields
        assert tuple(field.key for field in fields) == (
            "foci_count","period_x","period_y",
        )
        assert fields[0].kind is TargetPresentationFieldKind.DIMENSIONS
        assert fields[0].parameter_keys == ("n_foci_x","n_foci_y")
        assert fields[0].label == "Foci"

        assert fields[1].parameter_keys == ("period_x_px",)
        assert fields[1].compact_label == "Px"
        assert fields[2].parameter_keys == ("period_y_px",)
        assert fields[2].compact_label == "Py"

        for field in fields:
            for parameter_key in field.parameter_keys:
                assert parameter_key in registration.params


def test_target_presentation_is_consumable_without_target_class_knowledge():
    registration = DEFAULT_REGISTRIES.targets["multi_foci_vector"]
    params = {
        key:spec.default
        for key,spec in registration.params.items()
    }
    params.update({
        "n_foci_x":51,
        "n_foci_y":51,
        "period_x_px":780.0,
        "period_y_px":785.0,
    })

    assert _summary_values(registration,params) == {
        "foci_count":(51,51),
        "period_x":(780.0,),
        "period_y":(785.0,),
    }

    period_x_spec = registration.params[
        registration.presentation.summary_fields[1].parameter_keys[0]
    ]
    assert period_x_spec.conversion_available


def test_multi_foci_target_params_declare_spinbox_presentation():
    for target_key in ("multi_foci","multi_foci_vector"):
        specs = DEFAULT_REGISTRIES.targets[target_key].params

        for key in ("period_x_px","period_y_px"):
            spec = specs[key]
            assert spec.editor is EditorKind.DOUBLE_SPIN_BOX
            assert spec.step_for_unit(SLM_UNIT) == 0.05
            assert spec.step_for_unit(METRIC_UNIT) == 0.005
            assert spec.decimals_for_unit(SLM_UNIT) == 2
            assert spec.decimals_for_unit(METRIC_UNIT) == 4

        for key in ("fov_x_px","fov_y_px"):
            spec = specs[key]
            assert spec.editor is EditorKind.DOUBLE_SPIN_BOX
            assert spec.step_for_unit(SLM_UNIT) == 1.0
            assert spec.step_for_unit(METRIC_UNIT) == 1.0
            assert spec.decimals_for_unit(SLM_UNIT) == 0
            assert spec.decimals_for_unit(METRIC_UNIT) == 1

        for key in ("n_foci_x","n_foci_y"):
            assert specs[key].editor is EditorKind.SPIN_BOX
            assert specs[key].step_for_unit() == 1

        stagger = specs["stagger"]
        assert stagger.editor is EditorKind.DOUBLE_SPIN_BOX
        assert stagger.step_for_unit() == 0.05
        assert stagger.decimals_for_unit() == 2

    vector_specs = DEFAULT_REGISTRIES.targets["multi_foci_vector"].params
    for key in ("rotation_deg","skew_deg"):
        assert vector_specs[key].editor is EditorKind.DOUBLE_SPIN_BOX
        assert vector_specs[key].step_for_unit() == 0.1
        assert vector_specs[key].decimals_for_unit() == 2

    raster_specs = DEFAULT_REGISTRIES.targets["multi_foci"].params
    assert raster_specs["min_foci_delta"].editor is EditorKind.SPIN_BOX
    assert raster_specs["min_foci_delta"].max_value == 0
    assert raster_specs["max_foci_delta"].editor is EditorKind.SPIN_BOX
    assert raster_specs["max_foci_delta"].min_value == 0


def test_target_registration_rejects_missing_presentation():
    try:
        TargetRegistration(
            key="dummy",
            target_class=object,
            params={"value":ParamSpec(1,int)},
            metadata={},
            algorithm="algorithm",
            presentation=None,
        )
    except TypeError as error:
        assert "TargetPresentation" in str(error)
    else:
        raise AssertionError("TargetRegistration accepted missing presentation")


def test_target_registration_rejects_unknown_presentation_parameter():
    try:
        TargetRegistration(
            key="dummy",
            target_class=object,
            params={"value":ParamSpec(1,int)},
            metadata={},
            algorithm="algorithm",
            presentation=TargetPresentation(
                title="Dummy",
                summary_fields=(
                    TargetPresentationField(
                        key="missing",
                        parameter_keys=("missing",),
                    ),
                ),
            ),
        )
    except KeyError as error:
        assert "unknown parameter" in str(error)
    else:
        raise AssertionError(
            "TargetRegistration accepted unknown presentation parameter"
        )
