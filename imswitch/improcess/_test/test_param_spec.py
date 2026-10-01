"""The field spec: derived from defaults, declared by a plugin, and checked
against ``default_params()``. Qt-free."""

import json

import numpy as np
import pytest

from imswitch.improcess.model.param_spec import (
    FIELD_TYPES,
    ParamField,
    field_from_default,
    fields_by_key,
    spec_for,
    spec_from_defaults,
    spec_problems,
)


# -- ParamField ---------------------------------------------------------------

def test_a_field_validates_its_own_shape():
    with pytest.raises(ValueError):
        ParamField("", "int", 0)
    with pytest.raises(ValueError):
        ParamField("k", "integer", 0)
    with pytest.raises(ValueError):
        ParamField("k", "int", 0, min=5, max=1)
    with pytest.raises(ValueError):
        ParamField("k", "float", 0.0, step=0)
    with pytest.raises(ValueError):
        ParamField("k", "int", 0, decimals=-1)
    with pytest.raises(ValueError):
        ParamField("k", "select", "a")          # no options
    with pytest.raises(ValueError):
        ParamField("k", "int", 0, min=True)     # a bool is not a bound
    assert ParamField("k", "select", "a", options=["a", "b"]).options == ("a", "b")


def test_title_falls_back_to_the_key():
    assert ParamField("radius", "float", 1.0).title == "radius"
    assert ParamField("radius", "float", 1.0, label="Radius").title == "Radius"


@pytest.mark.parametrize("field, good, bad", [
    (ParamField("k", "bool", False), [True, False], [0, "yes", None]),
    (ParamField("k", "int", 0, min=0, max=10), [0, 10, np.int64(3)], [-1, 11, 2.5, True, "3", None]),
    (ParamField("k", "float", 0.0, min=0.1), [0.1, 2, np.float32(1.5)], [0.0, True, "x", None]),
    (ParamField("k", "text", ""), ["", "abc"], [1, None, ["a"]]),
    (ParamField("k", "path", ""), ["/tmp/x"], [None, 3]),
    (ParamField("k", "select", "a", options=("a", "b", 3)), ["a", "b", 3], ["c", None]),
    (ParamField("k", "multiselect", [], options=("a", "b")), [[], ["a"], ("a", "b")], ["a", ["c"], None]),
    (ParamField("k", "json", None), [None, {"a": 1}, [1, 2], "s", 3], []),
    (ParamField("k", "float", None, nullable=True), [None, 1.0], ["x"]),
    (ParamField("k", "select", None, options=("a",), nullable=True), [None, "a"], ["b"]),
])
def test_problem_with_checks_type_bounds_and_options(field, good, bad):
    for value in good:
        assert field.problem_with(value) is None, (field, value)
    for value in bad:
        assert field.problem_with(value), (field, value)


def test_to_dict_is_short_and_round_trips():
    plain = ParamField("radius", "float", 2.0)
    assert plain.to_dict() == {"key": "radius", "type": "float", "default": 2.0}
    rich = ParamField(
        "method", "select", "gaussian", label="Filter", help="Which kernel", options=("gaussian", "median"),
        group="Kernel", advanced=True,
    )
    payload = json.loads(json.dumps(rich.to_dict()))
    assert ParamField.from_dict(payload) == rich
    numeric = ParamField("radius", "float", 2.0, min=0.1, max=1000.0, step=0.5, decimals=2, suffix="px",
                         nullable=True)
    assert ParamField.from_dict(json.loads(json.dumps(numeric.to_dict()))) == numeric
    with pytest.raises(ValueError):
        ParamField.from_dict({"key": "k", "type": "int", "default": 0, "widget": "spin"})


# -- deriving from defaults ---------------------------------------------------

def test_field_from_default_types_by_value():
    assert field_from_default("k", True).type == "bool"
    assert field_from_default("k", 3).type == "int"
    assert field_from_default("k", np.int32(3)) == ParamField("k", "int", 3)
    assert field_from_default("k", 2.5).type == "float"
    assert field_from_default("k", np.float64(2.5)) == ParamField("k", "float", 2.5)
    assert field_from_default("k", "gaussian").type == "text"
    none = field_from_default("k", None)
    assert none.type == "json" and none.nullable
    assert field_from_default("k", [1, 2]).type == "json"
    assert field_from_default("k", {"a": 1}).type == "json"
    assert field_from_default("k", (1, 2)).type == "json"


def test_spec_from_defaults_keeps_order_and_every_key():
    defaults = {"method": "gaussian", "radius": 2.0, "amount": 0.6, "start": None, "ranges": []}
    spec = spec_from_defaults(defaults)
    assert [f.key for f in spec] == list(defaults)
    assert all(f.problem_with(defaults[f.key]) is None for f in spec)
    assert fields_by_key(spec)["start"].nullable


# -- the plugin contract ------------------------------------------------------

def _plugin(defaults, spec=None, *, base=None):
    from imswitch.improcess.processors.base import Processor

    class _P(base or Processor):
        id = "t.spec"
        name = "Spec"

        @classmethod
        def default_params(cls):
            return dict(defaults)

        if spec is not None:
            @classmethod
            def param_spec(cls):
                return spec() if callable(spec) else spec

        @property
        def applies_to(self):
            return lambda r: True

        def make_param_widget(self, parent):
            return None

        def apply(self, result, params):
            return result

    return _P


def test_the_framework_default_spec_agrees_with_the_defaults():
    cls = _plugin({"method": "gaussian", "radius": 2.0, "on": True, "start": None})
    assert spec_problems(cls) == []
    assert spec_for(cls) == spec_from_defaults(cls.default_params())
    assert [f.type for f in cls.param_spec()] == ["text", "float", "bool", "json"]


def test_a_declared_spec_must_match_keys_and_defaults():
    ok = _plugin({"method": "gaussian", "radius": 2.0}, (
        ParamField("method", "select", "gaussian", options=("gaussian", "median")),
        ParamField("radius", "float", 2.0, min=0.1, max=1000.0, suffix="px"),
    ))
    assert spec_problems(ok) == []
    assert spec_for(ok)[0].options == ("gaussian", "median")

    missing = _plugin({"method": "gaussian", "radius": 2.0}, (
        ParamField("method", "select", "gaussian", options=("gaussian",)),
    ))
    assert spec_problems(missing) == ["default_params() keys without a field: ['radius']"]

    extra = _plugin({"radius": 2.0}, (
        ParamField("radius", "float", 2.0), ParamField("sigma", "float", 1.0),
    ))
    assert spec_problems(extra) == ["fields not in default_params(): ['sigma']"]

    wrong_default = _plugin({"radius": 2.0}, (ParamField("radius", "float", 3.0),))
    assert spec_problems(wrong_default) == ["'radius': field default 3.0 != declared 2.0"]

    out_of_bounds = _plugin({"radius": 2.0}, (ParamField("radius", "float", 2.0, min=5.0),))
    assert spec_problems(out_of_bounds) == ["'radius': default 2.0 is below the minimum 5.0"]

    not_an_option = _plugin({"method": "gaussian"}, (
        ParamField("method", "select", "gaussian", options=("median",)),
    ))
    assert spec_problems(not_an_option) == [
        "'method': default 'gaussian' is not one of ['median']",
    ]

    none_not_nullable = _plugin({"start": None}, (ParamField("start", "int", None),))
    assert spec_problems(none_not_nullable) == ["'start': default None must not be None"]

    duplicated = _plugin({"radius": 2.0}, (ParamField("radius", "float", 2.0),) * 2)
    assert spec_problems(duplicated) == ["duplicate field(s): ['radius']"]

    wrong_kind = _plugin({"radius": 2.0}, ({"key": "radius"},))
    assert spec_problems(wrong_kind) == ["param_spec() must return ParamField objects, got dict"]


def test_spec_for_falls_back_when_the_declaration_is_broken():
    def boom():
        raise RuntimeError("bug")

    broken = _plugin({"radius": 2.0}, boom)
    assert spec_problems(broken) == ["param_spec() failed: bug"]
    assert spec_for(broken) == (ParamField("radius", "float", 2.0),)
    assert spec_for(broken()) == spec_for(broken)


def test_every_field_type_is_constructible():
    for kind in FIELD_TYPES:
        if kind in ("select", "multiselect"):
            ParamField("k", kind, None, options=("a",), nullable=True)
        else:
            ParamField("k", kind, None, nullable=True)
