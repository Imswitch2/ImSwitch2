"""Every built-in's declared ``param_spec()`` agrees with what its widget shows.

The probe (:mod:`imswitch.improcess.model.param_probe`) reads each control
off the real widget: the choices behind a combo box, the bounds and unit of
a spin box, whether a control is a check box. Where it can tie a control to
exactly one key, the declared field must say the same thing; a widget that
gains a choice or moves a bound without the spec following fails here, as
a default that moves without the widget fails the contract test.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qtpy import QtWidgets  # noqa: E402

from imswitch.improcess.model.param_probe import probe_plugin  # noqa: E402
from imswitch.improcess.model.param_spec import fields_by_key, spec_for  # noqa: E402
from imswitch.improcess.processors import _AVAILABLE_PROCESSOR_CLASSES  # noqa: E402
from imswitch.improcess.reconstructors import _AVAILABLE_RECONSTRUCTOR_CLASSES  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _plugins():
    for pid, cls in sorted(_AVAILABLE_PROCESSOR_CLASSES.items()):
        yield f"processor:{pid}", cls
    for pid, cls in sorted(_AVAILABLE_RECONSTRUCTOR_CLASSES.items()):
        yield f"reconstructor:{pid}", cls


_IDS = [key for key, _cls in _plugins()]
_CLASSES = [cls for _key, cls in _plugins()]


def _number(value):
    return None if value is None else float(value)


@pytest.mark.parametrize("cls", _CLASSES, ids=_IDS)
def test_the_declared_fields_say_what_the_widget_shows(cls, qapp):
    probed = probe_plugin(cls())
    declared = fields_by_key(spec_for(cls))
    for key, seen in probed.fields.items():
        field = declared[key]
        # Every value the widget can hand over is a value of the declared field.
        for value in seen.options:
            assert field.problem_with(value) is None, f"{cls.__name__}.{key}: widget value {value!r}"
        if seen.type == "bool":
            assert field.type == "bool", f"{cls.__name__}.{key}: a check box, declared {field.type}"
        if seen.type == "select" and field.type == "select":
            widget_options = {str(o) for o in seen.options if o is not None}
            declared_options = {str(o) for o in field.options if o is not None}
            assert declared_options == widget_options, f"{cls.__name__}.{key}: choices differ"
        if seen.type in ("int", "float") and field.type in ("int", "float"):
            assert _number(field.min) == _number(seen.min), f"{cls.__name__}.{key}: min differs"
            assert _number(field.max) == _number(seen.max), f"{cls.__name__}.{key}: max differs"
            assert field.suffix == seen.suffix, f"{cls.__name__}.{key}: unit differs"


def test_every_built_in_declares_more_than_the_fallback(qapp):
    """The point of Phase 3: no built-in leaves its form to the plain fallback."""
    from imswitch.improcess.model.param_spec import spec_from_defaults
    from imswitch.improcess.model.plugin_contract import framework_base

    plain = []
    for key, cls in _plugins():
        base = framework_base(cls)
        if cls.param_spec.__func__ is base.param_spec.__func__ and cls.default_params():
            plain.append(key)
        elif cls.default_params() and tuple(cls.param_spec()) == spec_from_defaults(cls.default_params()):
            plain.append(key)
    assert plain == []
