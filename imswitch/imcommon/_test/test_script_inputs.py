"""Finding the scriptInput() declarations of a script without running it."""
import pytest

from imswitch.imcommon.model import convertInputText, findScriptInputs


def test_inputs_are_listed_in_order_with_type_from_the_default():
    scan = findScriptInputs(
        "exposure = scriptInput('exposure_ms', 10.0, 'Exposure per frame')\n"
        "n = scriptInput('frames', 100)\n"
        "save = scriptInput(name='save', default=True)\n"
        "folder = scriptInput('folder', 'D:/data', description='Where to save')\n"
    )

    assert scan.error is None and scan.warnings == []
    assert [(s.name, s.default, s.type, s.line) for s in scan.inputs] == [
        ('exposure_ms', 10.0, float, 1),
        ('frames', 100, int, 2),
        ('save', True, bool, 3),
        ('folder', 'D:/data', str, 4),
    ]
    assert scan.inputs[0].description == 'Exposure per frame'
    assert scan.inputs[3].description == 'Where to save'
    assert [s.defaultText() for s in scan.inputs] == ['10.0', '100', 'True', 'D:/data']


def test_calls_inside_functions_and_loops_are_found_and_nothing_is_run():
    scan = findScriptInputs(
        "raise SystemExit('must not run')\n"
        "def f():\n"
        "    for i in range(3):\n"
        "        x = scriptInput('inner', -1.5)\n"
    )
    assert [(s.name, s.default) for s in scan.inputs] == [('inner', -1.5)]


def test_a_computed_default_is_entered_as_text_with_a_warning():
    scan = findScriptInputs("import numpy as np\nangle = scriptInput('angle', 2 * np.pi)\n")

    (spec,) = scan.inputs
    assert spec.type is str and not spec.defaultKnown and spec.defaultText() == ''
    assert 'not a plain value' in scan.warnings[0]


def test_no_default_means_a_text_input():
    (spec,) = findScriptInputs("x = scriptInput('label')").inputs
    assert spec.type is str and spec.default is None and spec.defaultText() == ''


def test_a_name_that_is_not_a_literal_is_skipped_with_a_warning():
    scan = findScriptInputs("name = 'a'\nx = scriptInput(name, 1)\n")
    assert scan.inputs == []
    assert 'Line 2' in scan.warnings[0]


def test_a_name_declared_twice_is_listed_once():
    scan = findScriptInputs("a = scriptInput('n', 1)\nb = scriptInput('n', 1)\n"
                            "c = scriptInput('n', 2)\n")
    assert [(s.name, s.default) for s in scan.inputs] == [('n', 1)]
    assert len(scan.warnings) == 1 and 'line 1' in scan.warnings[0]


def test_other_calls_named_alike_are_ignored():
    scan = findScriptInputs("x = helpers.scriptInput('a', 1)\ny = myScriptInput('b', 2)\n")
    assert scan.inputs == []


def test_a_syntax_error_is_reported_not_raised():
    scan = findScriptInputs("x = scriptInput('a', 1\n")
    assert scan.inputs == [] and scan.error.startswith('Syntax error on line')


@pytest.mark.parametrize('text, inputType, expected', [
    (' 12 ', int, 12),
    ('1.5', float, 1.5),
    ('3', float, 3.0),
    ('yes', bool, True),
    ('False', bool, False),
    (' keep spaces ', str, ' keep spaces '),
])
def test_text_is_converted_to_the_input_type(text, inputType, expected):
    value = convertInputText(text, inputType)
    assert value == expected and type(value) is inputType


@pytest.mark.parametrize('text, inputType', [('1.5', int), ('abc', float), ('maybe', bool)])
def test_text_that_does_not_fit_raises_a_readable_error(text, inputType):
    with pytest.raises(ValueError, match='Expected'):
        convertInputText(text, inputType)
