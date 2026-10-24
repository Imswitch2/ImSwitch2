"""_properties: the Micro-Manager property -> ImSwitch2 parameter mapping, on the demo camera's real properties."""
import pytest

from imswitch.pluginapi import DetectorListParameter, DetectorNumberParameter
from imswitch_device_micromanager import _properties as props

LABEL = "Camera"


@pytest.fixture
def core(host):
    return host.core


@pytest.fixture
def parameters(core):
    return props.build_parameters(core, LABEL, exclude=("Exposure", "Binning"))


def test_every_parameter_is_in_the_camera_properties_group(parameters):
    assert parameters
    assert {p.group for p in parameters.values()} == {"Camera properties"}


def test_exposure_and_binning_are_excluded_and_nothing_else_named_like_them(core, parameters):
    assert "Exposure" in core.getDevicePropertyNames(LABEL)          # it exists on the device ...
    assert "Exposure" not in parameters and "Binning" not in parameters   # ... but is first-class elsewhere


def test_exclude_defaults_to_nothing(core):
    everything = props.build_parameters(core, LABEL)
    assert "Exposure" in everything and "Binning" in everything


def test_pre_init_properties_are_skipped(core, parameters):
    pre_init = [n for n in core.getDevicePropertyNames(LABEL) if core.isPropertyPreInit(LABEL, n)]
    assert pre_init, "the demo camera has pre-init properties (MaximumExposureMs)"
    assert not set(pre_init) & set(parameters)


def test_allowed_values_become_a_list_parameter_with_the_current_value(parameters):
    mode = parameters["Mode"]
    assert isinstance(mode, DetectorListParameter)
    assert mode.value == "Artificial Waves"
    assert mode.options == ["Artificial Waves", "Color Test Pattern", "Fluorescent Beads", "Noise"]
    assert mode.editable


def test_integer_property_with_allowed_values_is_a_list_of_strings(parameters):
    bit_depth = parameters["BitDepth"]
    assert isinstance(bit_depth, DetectorListParameter)
    assert bit_depth.value == "16" and "16" in bit_depth.options and all(isinstance(o, str) for o in bit_depth.options)


def test_pixel_type_is_a_list_parameter(parameters):
    assert parameters["PixelType"].options == ["16bit", "32bit", "32bitRGB", "64bitRGB", "8bit"]


def test_float_with_limits_is_a_number_parameter_without_units(parameters):
    param = parameters["BeadSize"]
    assert isinstance(param, DetectorNumberParameter)
    assert param.value == 2.0 and isinstance(param.value, float)
    assert param.valueUnits == "" and param.editable


def test_integer_with_limits_is_an_int_number_parameter(parameters):
    param = parameters["Gain"]
    assert isinstance(param, DetectorNumberParameter)
    assert param.value == 0 and isinstance(param.value, int)


def test_numbers_without_limits_or_allowed_values_are_still_number_parameters(core, parameters):
    assert not core.hasPropertyLimits(LABEL, "TestProperty5")
    assert isinstance(parameters["TestProperty5"], DetectorNumberParameter)
    assert not core.hasPropertyLimits(LABEL, "OnCameraCCDXSize")
    assert isinstance(parameters["OnCameraCCDXSize"], DetectorNumberParameter)
    assert parameters["OnCameraCCDXSize"].value == 512


def test_read_only_number_is_not_editable(core, parameters):
    assert core.isPropertyReadOnly(LABEL, "CCDTemperature RO")
    param = parameters["CCDTemperature RO"]
    assert isinstance(param, DetectorNumberParameter) and not param.editable


def test_read_only_string_is_a_one_option_list_that_is_not_editable(parameters):
    param = parameters["CameraName"]
    assert isinstance(param, DetectorListParameter)
    assert param.value == "DemoCamera-MultiMode" and param.options == ["DemoCamera-MultiMode"]
    assert not param.editable


def test_string_without_allowed_values_is_a_non_editable_list_even_when_writable(core, parameters):
    assert not core.isPropertyReadOnly(LABEL, "AsyncPropertyLeader")
    param = parameters["AsyncPropertyLeader"]
    assert isinstance(param, DetectorListParameter) and not param.editable and param.options == [param.value]


# ------------------------------------------------------------------- read_back
def test_read_back_types(core):
    assert props.read_back(core, LABEL, "Gain") == 0 and isinstance(props.read_back(core, LABEL, "Gain"), int)
    assert isinstance(props.read_back(core, LABEL, "BeadSize"), float)
    assert props.read_back(core, LABEL, "Mode") == "Artificial Waves"
    assert props.read_back(core, LABEL, "Binning") == "1"            # choice property: always a string
    assert props.read_back(core, LABEL, "CameraName") == "DemoCamera-MultiMode"


def test_read_back_follows_the_device(core):
    core.setProperty(LABEL, "BeadSize", "3.5")
    assert props.read_back(core, LABEL, "BeadSize") == 3.5


# ---------------------------------------------------------------- coerce_for_mm
def test_coerce_list_value_must_be_an_option(parameters):
    assert props.coerce_for_mm(parameters["Mode"], "Noise") == "Noise"
    with pytest.raises(ValueError, match="not one of the allowed values"):
        props.coerce_for_mm(parameters["Mode"], "Sparkles")


def test_coerce_integral_float_selects_a_numeric_choice(parameters):
    assert props.coerce_for_mm(parameters["BitDepth"], 12.0) == "12"
    assert props.coerce_for_mm(parameters["BitDepth"], 12) == "12"


def test_coerce_float_number_is_a_round_trippable_string(parameters):
    text = props.coerce_for_mm(parameters["BeadSize"], 2.75)
    assert float(text) == 2.75
    assert props.coerce_for_mm(parameters["BeadSize"], 3) == "3.0"


def test_coerce_integer_number_takes_whole_numbers_only(parameters):
    assert props.coerce_for_mm(parameters["Gain"], 4) == "4"
    assert props.coerce_for_mm(parameters["Gain"], 4.0) == "4"
    with pytest.raises(ValueError, match="not an integer"):
        props.coerce_for_mm(parameters["Gain"], 4.5)


def test_coerce_refuses_non_finite_and_non_numbers(parameters):
    with pytest.raises(ValueError, match="finite"):
        props.coerce_for_mm(parameters["BeadSize"], float("nan"))
    with pytest.raises(ValueError, match="finite"):
        props.coerce_for_mm(parameters["BeadSize"], float("inf"))
    with pytest.raises(TypeError, match="not a number"):
        props.coerce_for_mm(parameters["BeadSize"], "big")


def test_coerce_accepts_a_numeric_string_for_a_number(parameters):
    assert props.coerce_for_mm(parameters["BeadSize"], "2.5") == "2.5"


def test_coerce_refuses_a_read_only_parameter(parameters):
    with pytest.raises(ValueError, match="read-only"):
        props.coerce_for_mm(parameters["CameraName"], "x")


def test_coerced_values_are_accepted_by_mmcore(core, parameters):
    for name, value in (("Mode", "Noise"), ("BeadSize", 4), ("Gain", 3)):
        core.setProperty(LABEL, name, props.coerce_for_mm(parameters[name], value))
    assert props.read_back(core, LABEL, "Mode") == "Noise"
    assert props.read_back(core, LABEL, "BeadSize") == 4.0
    assert props.read_back(core, LABEL, "Gain") == 3


# ------------------------------------------------------------ refresh_parameters
def test_refresh_updates_changed_values_in_place_and_reports_them(core, parameters):
    core.setProperty(LABEL, "Mode", "Noise")
    core.setProperty(LABEL, "BeadSize", "5")
    changed = props.refresh_parameters(core, LABEL, parameters, parameters)
    assert sorted(changed) == ["BeadSize", "Mode"]
    assert parameters["Mode"].value == "Noise" and parameters["BeadSize"].value == 5.0
    assert props.refresh_parameters(core, LABEL, parameters, parameters) == []


def test_refresh_adds_an_unlisted_value_to_a_list_parameter(core, parameters):
    parameters["Mode"].options.remove("Noise")
    core.setProperty(LABEL, "Mode", "Noise")
    props.refresh_parameters(core, LABEL, parameters, ["Mode"])
    assert "Noise" in parameters["Mode"].options and parameters["Mode"].value == "Noise"
