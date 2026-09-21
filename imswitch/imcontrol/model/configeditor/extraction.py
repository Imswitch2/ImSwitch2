"""Static extraction of ``managerProperties`` contracts from manager source.

The config editor needs to know, for every manager, which properties it reads,
which of them it cannot do without, and what kind of value each one holds.
Nobody has hand-written that (zero of 64 managers carry a schema), and the
templates that stand in for it drift. But the information is in the source:
every manager reads ``managerProperties["key"]`` or ``.get("key", default)``.

This module lifts it out with :mod:`ast`. Nothing here imports a manager --
managers import vendor SDKs and touch hardware-adjacent modules, which is why
the discovery plan rejected runtime introspection -- and nothing here needs Qt.
See ``docs/design/plans/config-editor-schema-extraction.md`` for the rules the
code below implements; the tests pin them idiom by idiom.

Three ideas run through everything:

* **Evidence is not a constraint.** A default literal or an ``int()`` call says
  what a manager *prefers*, not what it accepts (``BSC203StageManager`` divides
  an integer-defaulted ``travelRangeUm``). So a property's *kind* -- the
  editor's widget preference -- is inferred from anything, while its
  *constraint* -- what validation may reject -- is emitted only where the code
  proves it (``Path(...)``, a string-keyed nested subscript, ``.items()``).
* **A subscript is not "required".** ``AAAOTFLaserManager`` reads
  ``calibCsvPath`` inside ``try/except KeyError``; a shipped setup omits it.
  Requiredness is decided over every read of a key, guards included, and what
  the analysis cannot classify is reported as ``uncertain`` rather than guessed.
* **What cannot be read is reported, not dropped.** A key that is a variable
  the module does not define as a string constant, a helper the analysis does
  not follow: each is an *unresolved read*, listed in the report and stamped
  on the schema, so "54 managers, zero uncertain" never hides an omission.

Receivers are strict. Only a properties dict that traces to an ``*Info``
parameter -- of the constructor or of the method reading it -- or to an
attribute a method of this class (or a base) bound from one, is this
manager's own. ``TriggerScopeManager`` reading ``targetInfo.managerProperties``
is reading a positioner's contract; it is listed as discarded. Name bindings
are scoped to the function that made them, so a ``props`` parameter of an
unrelated method is not the constructor's ``props``.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from imswitch.imcontrol.model.plugins.setup_metadata import (
    CATEGORY_METADATA,
    KIND_METADATA,
)

# =============================================================================
# Vocabulary
# =============================================================================

#: The property dict's attribute on every ``*Info`` dataclass.
PROPS_ATTR = "managerProperties"

#: JSON kinds, as the editor's widget preference names them.
KIND_INTEGER = "integer"
KIND_NUMBER = "number"
KIND_BOOLEAN = "boolean"
KIND_STRING = "string"
KIND_ARRAY = "array"
KIND_OBJECT = "object"

#: Requiredness verdicts.
REQUIRED = "required"
OPTIONAL = "optional"
UNCERTAIN = "uncertain"

#: Wrapper calls around a read, and the kind they express.
_WRAPPER_KINDS = {
    "int": KIND_INTEGER,
    "float": KIND_NUMBER,
    "bool": KIND_BOOLEAN,
    "str": KIND_STRING,
    "Path": KIND_STRING,
    "open": KIND_STRING,
}

#: Wrapper calls that *prove* something about the accepted value.
#: ``int()``/``float()`` take a number or a numeric string; ``bool()`` takes
#: anything; ``Path()``/``open()`` take a path-like, which in JSON is a string.
_WRAPPER_CONSTRAINTS = {
    "int": {"type": [KIND_INTEGER, KIND_NUMBER, KIND_STRING]},
    "float": {"type": [KIND_INTEGER, KIND_NUMBER, KIND_STRING]},
    "Path": {"type": KIND_STRING},
    "open": {"type": KIND_STRING},
}

#: Methods only a mapping has. ``pop`` is not one: lists have it too.
_MAPPING_METHODS = {"items", "keys", "values", "get", "setdefault", "update"}

#: Exception handlers under which a missing key is tolerated.
_MISSING_KEY_HANDLERS = {"KeyError", "LookupError", "Exception", "BaseException"}

#: Aliasing constructors: ``dict(props)``, ``copy.deepcopy(props)``.
_COPY_CALLS = {"dict", "copy", "deepcopy"}

#: The low-level manager collection, as managers receive it.
_LOW_LEVEL_NAMES = {"lowLevelManagers", "_lowLevelManagers"}

#: Free-text ``Type`` column vocabulary of ``docs/devices/*.rst``, lowercased.
_DOCS_KINDS = {
    "str": KIND_STRING,
    "string": KIND_STRING,
    "int": KIND_INTEGER,
    "integer": KIND_INTEGER,
    "float": KIND_NUMBER,
    "number": KIND_NUMBER,
    "bool": KIND_BOOLEAN,
    "boolean": KIND_BOOLEAN,
    "dict": KIND_OBJECT,
    "object": KIND_OBJECT,
    "list": KIND_ARRAY,
    "array": KIND_ARRAY,
}


def _is_info_name(name: Optional[str]) -> bool:
    return bool(name) and name.lower().endswith("info")


# =============================================================================
# Results
# =============================================================================

@dataclass(frozen=True)
class PropertyRead:
    """One place a manager reads one key."""

    key: str
    access: str  # "subscript" | "get" | "in"
    lineno: int
    #: True when a missing key cannot raise here: inside ``try/except KeyError``
    #: or under ``if "key" in props:``.
    guarded: bool = False
    guard: Optional[str] = None
    #: True when a subscript sits under a condition the analysis does not
    #: model -- it may or may not be guarded.
    uncertain: bool = False
    #: Kind of a non-``None`` ``.get`` default literal.
    default_kind: Optional[str] = None
    #: ``.get`` with no default or a ``None`` default.
    nullable: bool = False
    #: ``int(...)``, ``float(...)``, ``Path(...)`` ... around the read.
    wrapper: Optional[str] = None
    #: The read is subscripted by a string or has a mapping-only method called.
    used_as_mapping: bool = False
    #: How the key was known: a literal, a module constant, a helper call.
    via: Optional[str] = None
    #: For ``defaults = props.get("defaults", {})`` then ``defaults.get("x")``:
    #: the read is of sub-key ``x`` of property ``defaults``.
    subkey: Optional[str] = None


@dataclass(frozen=True)
class DiscardedRead:
    """A ``managerProperties`` read the strict receiver rule refused."""

    key: str
    receiver: str
    lineno: int


@dataclass(frozen=True)
class UnresolvedRead:
    """A read whose key the analysis could not name.

    A variable that is not a module string constant, or a helper call with a
    non-literal key. Reported, never guessed: the schema carries their count.
    """

    expr: str
    lineno: int
    reason: str


@dataclass(frozen=True)
class PropertyWrite:
    """``props["k"] = v`` or ``del props["k"]``: not a configuration input."""

    key: str
    lineno: int
    kind: str  # "store" | "delete"


@dataclass
class PropertySpec:
    """Everything the extractor knows about one property of one manager."""

    key: str
    required: str = OPTIONAL
    kind: Optional[object] = None  # a kind string, or a sorted list for a union
    kind_source: list[str] = field(default_factory=list)
    constraint: Optional[dict] = None
    constraint_source: Optional[str] = None
    nullable: bool = False
    aliases: tuple[str, ...] = ()
    ref_category: Optional[str] = None
    widget: Optional[str] = None
    description: Optional[str] = None
    reads: list[PropertyRead] = field(default_factory=list)
    #: Keys the manager reads *inside* this property's dict, with their own
    #: kinds: ``defaults.exposure_us``. A dict read this way is an object.
    sub_properties: dict[str, "PropertySpec"] = field(default_factory=dict)

    @property
    def typed_by_code(self) -> bool:
        return any(tag.startswith("code:") for tag in self.kind_source)


@dataclass
class KeyHelper:
    """A method that reads a key given as a parameter: ``self._read_info("k")``.

    ``access`` is the most permissive read the body makes of that parameter --
    ``get`` or ``in`` make every call site optional, a guarded subscript too,
    and only a bare subscript makes the key a call site requires.
    """

    method: str
    key_index: int
    default_index: Optional[int]
    default_kind: Optional[str]
    default_nullable: bool
    access: str
    guarded: bool = False
    guard: Optional[str] = None


@dataclass
class ClassExtraction:
    """What one class, on its own, reads from its ``managerProperties``."""

    name: str
    bases: tuple[str, ...]
    module: str
    info_params: tuple[str, ...]
    reads: dict[str, list[PropertyRead]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)  # alias -> canonical
    refs: dict[str, str] = field(default_factory=dict)  # key -> category
    discarded: list[DiscardedRead] = field(default_factory=list)
    unresolved: list[UnresolvedRead] = field(default_factory=list)
    writes: list[PropertyWrite] = field(default_factory=list)
    #: The whole dict was handed to something opaque (a driver, ``serial.Serial``).
    open_passthrough: bool = False
    #: Attributes a method bound from an Info parameter / from the dict.
    info_attrs: set[str] = field(default_factory=set)
    props_attrs: set[str] = field(default_factory=set)
    key_helpers: dict[str, KeyHelper] = field(default_factory=dict)


@dataclass
class ManagerExtraction:
    """A manager's contract, merged over its base classes and other sources."""

    name: str
    classes: tuple[str, ...]  # resolution order, subclass first
    properties: dict[str, PropertySpec]
    discarded: list[DiscardedRead]
    open_passthrough: bool
    unresolved: list[UnresolvedRead] = field(default_factory=list)
    writes: list[PropertyWrite] = field(default_factory=list)

    @property
    def reads_any(self) -> bool:
        return (bool(self.properties) or self.open_passthrough
                or bool(self.discarded) or bool(self.unresolved))


# =============================================================================
# AST helpers
# =============================================================================

def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _ancestry(node: ast.AST, parents: dict) -> Iterable[tuple[ast.AST, ast.AST]]:
    """Yield ``(ancestor, child_on_path)`` pairs from the node upward."""
    child = node
    while child in parents:
        parent = parents[child]
        yield parent, child
        child = parent


def _enclosing_function(node: ast.AST, parents: dict) -> Optional[ast.FunctionDef]:
    for ancestor, _ in _ancestry(node, parents):
        if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ancestor
        if isinstance(ancestor, ast.ClassDef):
            return None
    return None


def _name_of(node: ast.AST) -> Optional[str]:
    """The simple name a ``Name`` or ``Attribute`` node ends in."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_self_attr(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self"


def _unwrap_props_expr(node: ast.AST) -> ast.AST:
    """See through ``X or {}``, ``getattr(X, "managerProperties", ...)``, ``dict(X)``."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and node.values:
        return _unwrap_props_expr(node.values[0])
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == PROPS_ATTR
    ):
        return ast.Attribute(value=node.args[0], attr=PROPS_ATTR, ctx=ast.Load())
    if (
        isinstance(node, ast.Call)
        and _name_of(node.func) in _COPY_CALLS
        and len(node.args) == 1
    ):
        return _unwrap_props_expr(node.args[0])
    return node


def _constant_kind(node: ast.AST) -> Optional[str]:
    """The JSON kind of a literal, or None (including for ``None`` itself)."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return KIND_BOOLEAN
        if isinstance(value, int):
            return KIND_INTEGER
        if isinstance(value, float):
            return KIND_NUMBER
        if isinstance(value, str):
            return KIND_STRING
        return None
    if isinstance(node, (ast.List, ast.Tuple)):
        return KIND_ARRAY
    if isinstance(node, ast.Dict):
        return KIND_OBJECT
    return None


def _is_none(node: Optional[ast.AST]) -> bool:
    return node is None or (isinstance(node, ast.Constant) and node.value is None)


def _is_low_level_collection(node: ast.AST) -> bool:
    return _name_of(node) in _LOW_LEVEL_NAMES


def _handler_tolerates_missing_key(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_name_of(name) in _MISSING_KEY_HANDLERS for name in names)


def _string_constants(body: list[ast.stmt]) -> dict[str, str]:
    """``NAME = "text"`` assignments in a module or class body."""
    constants: dict[str, str] = {}
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[target.id] = value.value
    return constants


# =============================================================================
# Module context: constants and functions that read a properties parameter
# =============================================================================

@dataclass
class _ParamReads:
    """Reads a function makes on one of its parameters, by parameter index."""

    reads: dict[int, list[PropertyRead]] = field(default_factory=dict)
    unresolved: dict[int, list[UnresolvedRead]] = field(default_factory=dict)


class _ModuleContext:
    """Everything a class scan needs to know about the module around it."""

    def __init__(self, tree: ast.Module, module: str):
        self.tree = tree
        self.module = module
        self.parents = _parent_map(tree)
        self.constants = _string_constants(tree.body)
        self.functions: dict[str, ast.FunctionDef] = {
            node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
        }
        self.param_reads: dict[str, _ParamReads] = {}
        for name, function in self.functions.items():
            reads = _scan_parameter_reads(function, self)
            if reads.reads or reads.unresolved:
                self.param_reads[name] = reads

    def resolve_key(self, node: ast.AST, class_constants: Optional[dict[str, str]] = None):
        """``(key, via)`` for a literal or a known string constant, else ``None``."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value, "literal"
        name = _name_of(node)
        if name and class_constants and name in class_constants:
            return class_constants[name], f"constant:{name}"
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self.constants[node.id], f"constant:{node.id}"
        return None


def _scan_parameter_reads(function: ast.FunctionDef, context: _ModuleContext) -> _ParamReads:
    """Reads a module-level function makes on its parameters (a props dict or an Info)."""
    params = [arg.arg for arg in function.args.args]
    result = _ParamReads()

    def index_of(receiver: ast.AST) -> Optional[int]:
        receiver = _unwrap_props_expr(receiver)
        if isinstance(receiver, ast.Attribute) and receiver.attr == PROPS_ATTR:
            receiver = receiver.value
        if isinstance(receiver, ast.Name) and receiver.id in params:
            return params.index(receiver.id)
        return None

    for node in ast.walk(function):
        found = _read_shape(node)
        if found is None:
            continue
        key_node, receiver, access = found
        index = index_of(receiver)
        if index is None:
            continue
        resolved = context.resolve_key(key_node)
        if resolved is None:
            result.unresolved.setdefault(index, []).append(
                UnresolvedRead(ast.unparse(node), node.lineno, "key is not a literal or a module string constant")
            )
            continue
        key, via = resolved
        result.reads.setdefault(index, []).append(_make_read(node, key, access, via, context.parents))
    return result


def _read_shape(node: ast.AST):
    """``(key_node, receiver_node, access)`` if ``node`` has the shape of a read."""
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return node.slice, node.value, "subscript"
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
    ):
        return node.args[0], node.func.value, "get"
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], (ast.In, ast.NotIn))
        and len(node.comparators) == 1
    ):
        return node.left, node.comparators[0], "in"
    return None


def _wrapper_and_mapping(node: ast.AST, parents: dict) -> tuple[Optional[str], bool]:
    parent = parents.get(node)
    wrapper = None
    if isinstance(parent, ast.Call) and node in parent.args:
        name = _name_of(parent.func)
        if name in _WRAPPER_KINDS:
            wrapper = name
    used_as_mapping = (
        (isinstance(parent, ast.Subscript) and parent.value is node
         and isinstance(parent.slice, ast.Constant) and isinstance(parent.slice.value, str))
        or (isinstance(parent, ast.Attribute) and parent.attr in _MAPPING_METHODS)
    )
    return wrapper, used_as_mapping


def _make_read(node: ast.AST, key: str, access: str, via: str, parents: dict,
               guard=(False, None, False)) -> PropertyRead:
    """A ``PropertyRead`` for a node already known to read ``key``."""
    wrapper, used_as_mapping = _wrapper_and_mapping(node, parents)
    if access == "get":
        default = node.args[1] if len(node.args) > 1 else None
        return PropertyRead(
            key, "get", node.lineno, guarded=True, guard="get",
            default_kind=_constant_kind(default) if default is not None else None,
            nullable=_is_none(default), wrapper=wrapper, used_as_mapping=used_as_mapping, via=via,
        )
    if access == "in":
        return PropertyRead(key, "in", node.lineno, guarded=True, guard="in", via=via)
    guarded, guard_text, uncertain = guard
    return PropertyRead(
        key, "subscript", node.lineno, guarded=guarded, guard=guard_text, uncertain=uncertain,
        wrapper=wrapper, used_as_mapping=used_as_mapping, via=via,
    )


# =============================================================================
# Per-class extraction
# =============================================================================

class _ClassScanner:
    """Scan one ``ClassDef`` for reads of its own ``managerProperties``.

    Two passes. :meth:`bind` records what names and attributes stand for --
    it needs no other class. :meth:`scan` reads, and is run once attribute
    aliases and key helpers inherited from base classes are known.
    """

    def __init__(self, cls: ast.ClassDef, context: _ModuleContext):
        self.cls = cls
        self.context = context
        self.parents = context.parents
        self.class_constants = _string_constants(cls.body)
        self.methods: dict[str, ast.FunctionDef] = {
            node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)
        }
        # Per function: parameters that are Info objects, and names bound to
        # the properties dict inside that function only.
        self.info_params_by_func: dict[ast.FunctionDef, set[str]] = {}
        self.props_names_by_func: dict[ast.FunctionDef, set[str]] = {}
        #: ``defaults = props.get("defaults", {})``: a name standing for one key's dict.
        self.nested_names_by_func: dict[ast.FunctionDef, dict[str, str]] = {}
        #: Reads each method makes on its own parameters (a dict or an Info).
        self.method_param_reads: dict[str, _ParamReads] = {}
        self.result = ClassExtraction(
            name=cls.name,
            bases=tuple(_name_of(base) or "?" for base in cls.bases),
            module=context.module,
            info_params=tuple(
                arg.arg for arg in self.methods["__init__"].args.args[1:]
                if _is_info_name(arg.arg)
            ) if "__init__" in self.methods else (),
        )
        # Filled before scan(): attributes bound in bases, and their helpers.
        self.inherited_info_attrs: set[str] = set()
        self.inherited_props_attrs: set[str] = set()
        self.inherited_helpers: dict[str, KeyHelper] = {}
        self.bind()

    # -- pass 1: what stands for what ----------------------------------------

    def bind(self) -> None:
        for function in self.methods.values():
            self.info_params_by_func[function] = {
                arg.arg for arg in function.args.args[1:] if _is_info_name(arg.arg)
            }
            self.props_names_by_func[function] = set()
            self.nested_names_by_func[function] = {}
        # Fixed point: ``p = props`` after ``props = info.managerProperties``.
        changed = True
        while changed:
            changed = False
            for node in ast.walk(self.cls):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                    continue
                function = _enclosing_function(node, self.parents)
                if function is None or function not in self.info_params_by_func:
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if isinstance(value, ast.Name) and value.id in self.info_params_by_func[function]:
                    for target in targets:
                        if _is_self_attr(target) and target.attr not in self.result.info_attrs:
                            self.result.info_attrs.add(target.attr)
                            changed = True
                        elif isinstance(target, ast.Name) and target.id not in self.info_params_by_func[function]:
                            self.info_params_by_func[function].add(target.id)
                            changed = True
                    continue
                if self._classify(value, function)[0] == "own":
                    for target in targets:
                        if _is_self_attr(target) and target.attr not in self.result.props_attrs:
                            self.result.props_attrs.add(target.attr)
                            changed = True
                        elif isinstance(target, ast.Name) and target.id not in self.props_names_by_func[function]:
                            self.props_names_by_func[function].add(target.id)
                            changed = True
                    continue
                nested_key = self._nested_dict_key(value, function)
                if nested_key is not None:
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id not in self.nested_names_by_func[function]:
                            self.nested_names_by_func[function][target.id] = nested_key
                            changed = True

    def _nested_dict_key(self, value: ast.AST, function) -> Optional[str]:
        """``props.get("k", {})``, ``props.get("k") or {}``, ``props["k"]``: the key ``k``."""
        value = _unwrap_props_expr(value) if isinstance(value, ast.BoolOp) else value
        found = _read_shape(value)
        if found is None:
            return None
        key_node, receiver, access = found
        if access == "in" or self._classify(receiver, function)[0] != "own":
            return None
        resolved = self.context.resolve_key(key_node, self.class_constants)
        return resolved[0] if resolved else None

    def _nested_alias(self, node: ast.AST, function) -> Optional[str]:
        """The property key a nested-dict alias name stands for, if ``node`` is one."""
        if isinstance(node, ast.Name) and function is not None:
            return self.nested_names_by_func.get(function, {}).get(node.id)
        return None

    def _classify(self, node: ast.AST, function: Optional[ast.FunctionDef]) -> tuple[str, Optional[str]]:
        """``("own", None)``, ``("foreign", receiver)`` or ``("no", None)``."""
        node = _unwrap_props_expr(node)
        if isinstance(node, ast.Attribute) and node.attr == PROPS_ATTR:
            receiver = node.value
            if isinstance(receiver, ast.Name) and function is not None \
                    and receiver.id in self.info_params_by_func.get(function, set()):
                return "own", None
            if _is_self_attr(receiver) and receiver.attr in (self.result.info_attrs | self.inherited_info_attrs):
                return "own", None
            return "foreign", ast.unparse(receiver)
        if isinstance(node, ast.Name):
            if function is not None and node.id in self.props_names_by_func.get(function, set()):
                return "own", None
            return "no", None
        if _is_self_attr(node) and node.attr in (self.result.props_attrs | self.inherited_props_attrs):
            return "own", None
        return "no", None

    def _is_own_info(self, node: ast.AST, function) -> bool:
        """Whether ``node`` is this manager's Info object (not its dict)."""
        node = _unwrap_props_expr(node)
        if isinstance(node, ast.Name):
            return function is not None and node.id in self.info_params_by_func.get(function, set())
        return _is_self_attr(node) and node.attr in (self.result.info_attrs | self.inherited_info_attrs)

    # -- pass 2: reads -------------------------------------------------------

    def scan(self) -> ClassExtraction:
        bound_from_read: dict[tuple, str] = {}
        for node in ast.walk(self.cls):
            function = _enclosing_function(node, self.parents)
            # Writes and deletions are not configuration inputs.
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) \
                    and self._classify(node.value, function)[0] == "own":
                resolved = self.context.resolve_key(node.slice, self.class_constants)
                key = resolved[0] if resolved else ast.unparse(node.slice)
                self.result.writes.append(PropertyWrite(
                    key, node.lineno, "store" if isinstance(node.ctx, ast.Store) else "delete"))
                continue
            found = _read_shape(node)
            if found is not None:
                key_node, receiver, access = found
                parent_key = self._nested_alias(_unwrap_props_expr(receiver), function)
                if parent_key is not None and access != "in":
                    resolved = self.context.resolve_key(key_node, self.class_constants)
                    if resolved is None:
                        self.result.unresolved.append(UnresolvedRead(
                            ast.unparse(node), node.lineno, f"sub-key of {parent_key!r} is not a literal"))
                        continue
                    read = _make_read(node, parent_key, access, resolved[1], self.parents)
                    self._record(PropertyRead(**{**read.__dict__, "subkey": resolved[0]}))
                    continue
                verdict, receiver_text = self._classify(receiver, function)
                if verdict == "no":
                    self._maybe_helper_call(node, function, bound_from_read)
                    continue
                if self._is_helper_definition_read(key_node, function):
                    continue  # the helper's call sites carry the keys
                resolved = self.context.resolve_key(key_node, self.class_constants)
                if verdict == "foreign":
                    key = resolved[0] if resolved else ast.unparse(key_node)
                    self.result.discarded.append(DiscardedRead(key, receiver_text or "?", node.lineno))
                    continue
                if resolved is None:
                    self.result.unresolved.append(UnresolvedRead(
                        ast.unparse(node), node.lineno, "key is not a literal or a known string constant"))
                    continue
                key, via = resolved
                guard = self._guard_status(node, key, function) if access == "subscript" else (False, None, False)
                read = _make_read(node, key, access, via, self.parents, guard)
                if access == "get":
                    read = self._fold_alias(node, key, read, function)
                self._record(read)
                self._note_binding(node, key, function, bound_from_read)
                continue
            self._maybe_helper_call(node, function, bound_from_read)
        self._scan_refs(bound_from_read)
        self._scan_passthrough()
        return self.result

    def _record(self, read: PropertyRead) -> None:
        self.result.reads.setdefault(read.key, []).append(read)

    @staticmethod
    def _is_helper_definition_read(key_node: ast.AST, function) -> bool:
        """``props.get(key, default)`` where ``key`` is the function's own parameter."""
        return (
            function is not None
            and isinstance(key_node, ast.Name)
            and key_node.id in {arg.arg for arg in function.args.args}
        )

    def _note_binding(self, node, key, function, bound_from_read) -> None:
        """``name = props["k"]`` (possibly wrapped): remember for ref data-flow."""
        parent = self.parents.get(node)
        if isinstance(parent, ast.Call) and _name_of(parent.func) in _WRAPPER_KINDS:
            parent = self.parents.get(parent)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)):
            targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            for target in targets:
                name = _name_of(target)
                if not name:
                    continue
                if _is_self_attr(target):
                    bound_from_read[("attr", name)] = key  # attributes carry across methods
                else:
                    bound_from_read[(function, name)] = key

    def _fold_alias(self, node: ast.Call, key: str, read: PropertyRead, function) -> PropertyRead:
        """``props.get("camelK", props.get("snake_k", d))``: one property, two spellings."""
        default = node.args[1] if len(node.args) > 1 else None
        if (
            isinstance(default, ast.Call)
            and isinstance(default.func, ast.Attribute)
            and default.func.attr == "get"
            and default.args
            and self._classify(default.func.value, function)[0] == "own"
        ):
            inner = self.context.resolve_key(default.args[0], self.class_constants)
            if inner is not None:
                self.result.aliases[inner[0]] = key
                inner_default = default.args[1] if len(default.args) > 1 else None
                return PropertyRead(
                    key, "get", read.lineno, guarded=True, guard="get",
                    default_kind=_constant_kind(inner_default) if inner_default is not None else None,
                    nullable=_is_none(inner_default), wrapper=read.wrapper,
                    used_as_mapping=read.used_as_mapping, via=read.via,
                )
        return read

    # -- helpers: a key given as a parameter, a dict given as an argument -----

    def find_key_helpers(self) -> None:
        """Methods like ``def _read_info(self, key, default=None)``, and methods
        that read a parameter handed the dict (``_resolve(self, manager_properties)``)."""
        for name, function in self.methods.items():
            reads = _scan_parameter_reads(function, self.context)
            if reads.reads or reads.unresolved:
                self.method_param_reads[name] = reads
        for name, function in self.methods.items():
            params = [arg.arg for arg in function.args.args[1:]]
            defaults = function.args.defaults
            default_nodes = {
                params[len(params) - len(defaults) + i]: default for i, default in enumerate(defaults)
            }
            helper: Optional[KeyHelper] = None
            for node in ast.walk(function):
                found = _read_shape(node)
                if found is None:
                    continue
                key_node, receiver, access = found
                if not (isinstance(key_node, ast.Name) and key_node.id in params):
                    continue
                if self._classify(receiver, function)[0] != "own":
                    continue
                default_index = None
                default_kind = None
                default_nullable = False
                if access == "get" and len(node.args) > 1 and isinstance(node.args[1], ast.Name) \
                        and node.args[1].id in params:
                    default_param = node.args[1].id
                    default_index = params.index(default_param) + 1
                    literal = default_nodes.get(default_param)
                    default_kind = _constant_kind(literal) if literal is not None else None
                    default_nullable = literal is None or _is_none(literal)
                guarded, guard, _uncertain = (
                    self._guard_status(node, key_node.id, function) if access == "subscript" else (True, access, False)
                )
                candidate = KeyHelper(
                    name, params.index(key_node.id) + 1, default_index, default_kind, default_nullable,
                    access, guarded, guard,
                )
                # Keep the most permissive read of the parameter.
                if helper is None or (candidate.guarded and not helper.guarded) \
                        or (candidate.access == "get" and helper.access != "get"):
                    helper = candidate
            if helper is not None:
                self.result.key_helpers[name] = helper

    def _maybe_helper_call(self, node: ast.AST, function, bound_from_read) -> None:
        """Resolve ``self._read_info("k")`` and ``helper(props)`` call sites."""
        if not isinstance(node, ast.Call):
            return
        # A key-parameter helper of this class or a base.
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "self":
            helper = self.result.key_helpers.get(node.func.attr) or self.inherited_helpers.get(node.func.attr)
            if helper is not None:
                args = [None] + list(node.args)  # index 0 is self
                key_node = args[helper.key_index] if helper.key_index < len(args) else None
                if key_node is None:
                    return
                resolved = self.context.resolve_key(key_node, self.class_constants)
                if resolved is None:
                    self.result.unresolved.append(UnresolvedRead(
                        ast.unparse(node), node.lineno, f"key passed to {helper.method}() is not a literal"))
                    return
                key, _via = resolved
                default = args[helper.default_index] if helper.default_index and helper.default_index < len(args) else None
                default_kind = _constant_kind(default) if default is not None else helper.default_kind
                nullable = _is_none(default) if default is not None else helper.default_nullable
                if helper.access == "get":
                    read = PropertyRead(key, "get", node.lineno, guarded=True, guard="get",
                                        default_kind=default_kind, nullable=nullable, via=f"helper:{helper.method}")
                elif helper.access == "in":
                    read = PropertyRead(key, "in", node.lineno, guarded=True, guard="in",
                                        via=f"helper:{helper.method}")
                else:
                    read = PropertyRead(key, "subscript", node.lineno, guarded=helper.guarded,
                                        guard=helper.guard, via=f"helper:{helper.method}")
                self._record(read)
                self._note_binding(node, key, function, bound_from_read)
                return
        # A method of this class handed the dict (or the Info) as an argument.
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "self" and node.func.attr in self.method_param_reads:
            reads = self.method_param_reads[node.func.attr]
            for index, arg in enumerate(node.args, start=1):  # index 0 is self
                if not (self._classify(arg, function)[0] == "own" or self._is_own_info(arg, function)):
                    continue
                for read in reads.reads.get(index, []):
                    self._record(PropertyRead(**{**read.__dict__, "lineno": node.lineno,
                                                 "via": f"method:{node.func.attr}"}))
                for unresolved in reads.unresolved.get(index, []):
                    self.result.unresolved.append(UnresolvedRead(
                        unresolved.expr, unresolved.lineno, f"in {node.func.attr}(): {unresolved.reason}"))
            return
        # A module-level function handed the dict (or the Info) as an argument.
        if isinstance(node.func, ast.Name) and node.func.id in self.context.param_reads:
            reads = self.context.param_reads[node.func.id]
            for index, arg in enumerate(node.args):
                if not (self._classify(arg, function)[0] == "own" or self._is_own_info(arg, function)):
                    continue
                for read in reads.reads.get(index, []):
                    self._record(PropertyRead(
                        read.key, read.access, node.lineno, guarded=read.guarded, guard=read.guard,
                        uncertain=read.uncertain, default_kind=read.default_kind, nullable=read.nullable,
                        wrapper=read.wrapper, used_as_mapping=read.used_as_mapping,
                        via=f"function:{node.func.id}",
                    ))
                for unresolved in reads.unresolved.get(index, []):
                    self.result.unresolved.append(UnresolvedRead(
                        unresolved.expr, unresolved.lineno, f"in {node.func.id}(): {unresolved.reason}"))

    # -- guards ---------------------------------------------------------------

    def _guard_status(self, node: ast.AST, key: str, function) -> tuple[bool, Optional[str], bool]:
        """Whether a missing ``key`` can raise at this subscript."""
        uncertain = False
        for ancestor, child in _ancestry(node, self.parents):
            if isinstance(ancestor, ast.Try):
                if child in ancestor.body and any(
                    _handler_tolerates_missing_key(handler) for handler in ancestor.handlers
                ):
                    handled = ", ".join(
                        (_name_of(h.type) or "bare") if h.type is not None else "bare"
                        for h in ancestor.handlers
                    )
                    return True, f"try/except {handled}", False
            elif isinstance(ancestor, (ast.If, ast.IfExp)):
                in_body = child is ancestor.body or (
                    isinstance(ancestor.body, list) and child in ancestor.body
                )
                if in_body and self._test_guards_key(ancestor.test, key, function):
                    return True, "in-guard", False
                if self._test_mentions_props(ancestor.test, function):
                    uncertain = True
            elif isinstance(ancestor, (ast.FunctionDef, ast.ClassDef)):
                break
        return False, None, uncertain

    def _test_guards_key(self, test: ast.AST, key: str, function) -> bool:
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return any(self._test_guards_key(value, key, function) for value in test.values)
        found = _read_shape(test)
        if found is None:
            return False
        key_node, receiver, access = found
        if access not in ("in", "get"):
            return False
        if isinstance(test, ast.Compare) and isinstance(test.ops[0], ast.NotIn):
            return False
        resolved = self.context.resolve_key(key_node, self.class_constants)
        return resolved is not None and resolved[0] == key and self._classify(receiver, function)[0] == "own"

    def _test_mentions_props(self, test: ast.AST, function) -> bool:
        return any(
            self._classify(sub, function)[0] == "own"
            for sub in ast.walk(test)
            if isinstance(sub, (ast.Name, ast.Attribute, ast.Call, ast.BoolOp))
        )

    # -- device references and pass-through -----------------------------------

    def _scan_refs(self, bound_from_read: dict) -> None:
        """``lowLevelManagers["rs232sManager"][name]`` where ``name`` came from a read."""
        for node in ast.walk(self.cls):
            if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Subscript)):
                continue
            bucket_node = node.value
            if not (_is_low_level_collection(bucket_node.value)
                    and isinstance(bucket_node.slice, ast.Constant)):
                continue
            category = _bucket_category(str(bucket_node.slice.value))
            if category is None:
                continue
            function = _enclosing_function(node, self.parents)
            index = node.slice
            key = None
            found = _read_shape(index)
            if found is not None and self._classify(found[1], function)[0] == "own":
                resolved = self.context.resolve_key(found[0], self.class_constants)
                key = resolved[0] if resolved else None
            else:
                name = _name_of(index)
                if _is_self_attr(index):
                    key = bound_from_read.get(("attr", name))
                else:
                    key = bound_from_read.get((function, name))
            if key is not None:
                self.result.refs[key] = category

    def _scan_passthrough(self) -> None:
        """The whole dict handed to a call the analysis does not follow."""
        for node in ast.walk(self.cls):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                continue
            if _name_of(node.func) in _COPY_CALLS:
                continue
            if isinstance(node.func, ast.Name) and node.func.id in self.context.param_reads:
                continue  # followed: its reads were imported
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "self" and node.func.attr in self.method_param_reads:
                continue  # followed likewise
            function = _enclosing_function(node, self.parents)
            candidates = list(node.args) + [kw.value for kw in node.keywords if kw.arg is None]
            for candidate in candidates:
                if self._classify(candidate, function)[0] == "own":
                    self.result.open_passthrough = True
                    return


def _bucket_category(bucket: str) -> Optional[str]:
    """``rs232sManager`` -> ``rs232devices``; singleton buckets are not refs."""
    if not bucket.endswith("sManager"):
        return None
    kind = bucket[: -len("sManager")]
    metadata = KIND_METADATA.get(kind)
    return metadata.editor_category if metadata else None


# =============================================================================
# Module and tree extraction
# =============================================================================

@dataclass
class ModuleIndex:
    """What a module defines and re-exports, for name -> class resolution."""

    classes: list[str]
    reexports: dict[str, str]  # local name -> imported name


def _scanners_for_module(source: str, module: str) -> tuple[dict[str, _ClassScanner], ModuleIndex]:
    tree = ast.parse(source)
    context = _ModuleContext(tree, module)
    scanners = {
        node.name: _ClassScanner(node, context)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    reexports = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                reexports[alias.asname or alias.name] = alias.name
    index = ModuleIndex(classes=[n.name for n in tree.body if isinstance(n, ast.ClassDef)], reexports=reexports)
    return scanners, index


def _finish(scanners: dict[str, _ClassScanner]) -> dict[str, ClassExtraction]:
    """Run pass 2 with attribute aliases and key helpers inherited from bases."""
    for scanner in scanners.values():
        scanner.find_key_helpers()

    def inherited(name: str, seen=()) -> tuple[set[str], set[str], dict[str, KeyHelper]]:
        scanner = scanners.get(name)
        if scanner is None or name in seen:
            return set(), set(), {}
        info_attrs = set(scanner.result.info_attrs)
        props_attrs = set(scanner.result.props_attrs)
        helpers = dict(scanner.result.key_helpers)
        for base in scanner.result.bases:
            base_info, base_props, base_helpers = inherited(base, seen + (name,))
            info_attrs |= base_info
            props_attrs |= base_props
            for helper_name, helper in base_helpers.items():
                helpers.setdefault(helper_name, helper)
        return info_attrs, props_attrs, helpers

    for name, scanner in scanners.items():
        info_attrs, props_attrs, helpers = set(), set(), {}
        for base in scanner.result.bases:
            base_info, base_props, base_helpers = inherited(base, (name,))
            info_attrs |= base_info
            props_attrs |= base_props
            for helper_name, helper in base_helpers.items():
                helpers.setdefault(helper_name, helper)
        scanner.inherited_info_attrs = info_attrs
        scanner.inherited_props_attrs = props_attrs
        scanner.inherited_helpers = helpers
    return {name: scanner.scan() for name, scanner in scanners.items()}


def extract_module(source: str, module: str = "<string>") -> dict[str, ClassExtraction]:
    """Extract every class in one Python source text (bases resolve within it)."""
    scanners, _ = _scanners_for_module(source, module)
    return _finish(scanners)


@dataclass
class TreeExtraction:
    classes: dict[str, ClassExtraction]
    modules: dict[str, ModuleIndex]  # file stem -> what it defines


def extract_tree_indexed(root: Path) -> TreeExtraction:
    """Extract every class under ``root`` and index what each module defines."""
    scanners: dict[str, _ClassScanner] = {}
    modules: dict[str, ModuleIndex] = {}
    for path in sorted(Path(root).rglob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            module_scanners, index = _scanners_for_module(path.read_text(encoding="utf-8"), str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        modules.setdefault(path.stem, index)
        for name, scanner in module_scanners.items():
            # Same class name in two files (a mock beside its real manager):
            # the first in sorted order wins.
            scanners.setdefault(name, scanner)
    return TreeExtraction(classes=_finish(scanners), modules=modules)


def extract_tree(root: Path) -> dict[str, ClassExtraction]:
    """Extract every class under ``root`` (recursively), keyed by class name."""
    return extract_tree_indexed(root).classes


def resolve_class_name(
    name: str,
    tree: TreeExtraction,
    python_name: Optional[str] = None,
) -> Optional[str]:
    """The class that implements manager ``name``, or None.

    A registry contribution says so directly (``module:Class``). A legacy
    stem may be the class name, or a module that re-exports it
    (``ThorlabsMFFManager.py`` is ``from .ThorlabsMFF import ThorlabsMFFManager``),
    or a module defining exactly one manager class. ``PyCoboltManager.py``
    defines a vendor driver and no manager: it stays unresolved, on purpose.
    """
    if python_name and ":" in python_name:
        candidate = python_name.rsplit(":", 1)[1]
        if candidate in tree.classes:
            return candidate
    if name in tree.classes:
        return name
    module = tree.modules.get(name)
    if module is None:
        return None
    if name in module.reexports and module.reexports[name] in tree.classes:
        return module.reexports[name]
    if len(module.classes) == 1 and module.classes[0] in tree.classes and module.classes[0].endswith("Manager"):
        return module.classes[0]
    return None


# =============================================================================
# Merging over base classes
# =============================================================================

def resolution_order(name: str, classes: dict[str, ClassExtraction]) -> list[str]:
    """The class and its bases, subclass first, restricted to what we scanned."""
    order: list[str] = []

    def visit(current: str) -> None:
        if current in order or current not in classes:
            return
        order.append(current)
        for base in classes[current].bases:
            visit(base)

    visit(name)
    return order


def _decide_required(reads: list[PropertyRead]) -> str:
    """The guard-aware rule, over every read of a key in the whole chain."""
    if any(read.access in ("get", "in") for read in reads):
        return OPTIONAL
    subscripts = [read for read in reads if read.access == "subscript"]
    if not subscripts:
        return OPTIONAL
    if all(read.guarded for read in subscripts):
        return OPTIONAL
    if any(not read.guarded and read.uncertain for read in subscripts) and not any(
        not read.guarded and not read.uncertain for read in subscripts
    ):
        return UNCERTAIN
    return REQUIRED


def _code_kinds(reads: list[PropertyRead]) -> tuple[list[str], list[str]]:
    """Kinds the code itself expresses, with provenance tags."""
    kinds: list[str] = []
    tags: list[str] = []
    for read in reads:
        if read.default_kind and read.default_kind not in kinds:
            kinds.append(read.default_kind)
            tags.append(f"code:default:{read.default_kind}")
        if read.wrapper and _WRAPPER_KINDS[read.wrapper] not in kinds:
            kinds.append(_WRAPPER_KINDS[read.wrapper])
            tags.append(f"code:{read.wrapper}()")
    return kinds, tags


def _provable_constraint(reads: list[PropertyRead]) -> tuple[Optional[dict], Optional[str]]:
    """Only what the code proves: mapping use > path-like > numeric-or-string."""
    if any(read.used_as_mapping for read in reads):
        return {"type": KIND_OBJECT}, "code:mapping-use"
    for read in reads:
        if read.wrapper in ("Path", "open"):
            return dict(_WRAPPER_CONSTRAINTS[read.wrapper]), f"code:{read.wrapper}()"
    for read in reads:
        if read.wrapper in ("int", "float"):
            return dict(_WRAPPER_CONSTRAINTS[read.wrapper]), f"code:{read.wrapper}()"
    return None, None


def _as_kind(kinds: Iterable[str]) -> Optional[object]:
    unique = sorted(set(kinds))
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else unique


def merge_manager(
    name: str,
    classes: dict[str, ClassExtraction],
    *,
    class_name: Optional[str] = None,
    example_kinds: Optional[dict[tuple[str, str], set[str]]] = None,
    docs_cards: Optional[dict[str, dict[str, "DocsField"]]] = None,
) -> ManagerExtraction:
    """One manager's contract: its own reads plus every base class's.

    ``class_name`` is the implementing class when it differs from the
    manager's name (see :func:`resolve_class_name`).
    """
    order = resolution_order(class_name or name, classes)
    reads: dict[str, list[PropertyRead]] = {}
    aliases: dict[str, str] = {}
    refs: dict[str, str] = {}
    discarded: list[DiscardedRead] = []
    unresolved: list[UnresolvedRead] = []
    writes: list[PropertyWrite] = []
    passthrough = False
    for cls_name in reversed(order):  # base first, so a subclass can add
        extraction = classes[cls_name]
        for key, key_reads in extraction.reads.items():
            reads.setdefault(key, []).extend(key_reads)
        aliases.update(extraction.aliases)
        refs.update(extraction.refs)
        discarded.extend(extraction.discarded)
        unresolved.extend(extraction.unresolved)
        writes.extend(extraction.writes)
        passthrough = passthrough or extraction.open_passthrough

    # Fold each alias's reads into its canonical property.
    for alias, canonical in aliases.items():
        if alias in reads:
            reads.setdefault(canonical, []).extend(reads.pop(alias))

    properties: dict[str, PropertySpec] = {}
    example_kinds = example_kinds or {}
    card = (docs_cards or {}).get(name, {})
    for key in sorted(reads):
        sub_reads = [read for read in reads[key] if read.subkey]
        key_reads = [read for read in reads[key] if not read.subkey]
        if not key_reads:
            # Only ever read through an alias of the dict: the alias binding
            # was itself a read of the key; treat it as a guarded one.
            key_reads = [PropertyRead(key, "get", sub_reads[0].lineno, guarded=True, guard="get",
                                      nullable=False, via="nested")]
        spec = PropertySpec(key=key, reads=list(key_reads))
        spec.required = _decide_required(key_reads)
        spec.nullable = any(read.nullable for read in key_reads)
        if sub_reads:
            for subkey in sorted({read.subkey for read in sub_reads}):
                these = [read for read in sub_reads if read.subkey == subkey]
                sub = PropertySpec(key=subkey, reads=list(these))
                sub.required = _decide_required(these)
                sub.nullable = any(read.nullable for read in these)
                sub_kinds, sub_tags = _code_kinds(these)
                sub.kind, sub.kind_source = _as_kind(sub_kinds), sub_tags
                sub.constraint, sub.constraint_source = _provable_constraint(these)
                spec.sub_properties[subkey] = sub
        spec.aliases = tuple(sorted(alias for alias, canonical in aliases.items() if canonical == key))

        kinds, tags = _code_kinds(key_reads)
        if not kinds and (name, key) in example_kinds:
            kinds = sorted(k for k in example_kinds[(name, key)] if k != "null")
            tags = [f"example:{k}" for k in kinds]
            if "null" in example_kinds[(name, key)]:
                spec.nullable = True
        docs_field = card.get(key)
        if not kinds and docs_field is not None and docs_field.kinds:
            kinds = list(docs_field.kinds)
            tags = [f"docs:{k}" for k in kinds]
            spec.nullable = spec.nullable or docs_field.nullable
        spec.kind = _as_kind(kinds)
        spec.kind_source = tags
        if docs_field is not None and docs_field.description:
            spec.description = docs_field.description

        spec.constraint, spec.constraint_source = _provable_constraint(key_reads)
        if spec.sub_properties and spec.constraint is None:
            spec.constraint, spec.constraint_source = {"type": KIND_OBJECT}, "code:sub-keys"
            if spec.kind is None:
                spec.kind, spec.kind_source = KIND_OBJECT, ["code:sub-keys"]
        if key in refs:
            spec.ref_category = refs[key]
            spec.widget = "ref"
            if spec.kind is None:
                spec.kind = KIND_STRING
                spec.kind_source = ["code:ref"]
        elif any(read.wrapper in ("Path", "open") for read in key_reads):
            spec.widget = "path"
        properties[key] = spec

    return ManagerExtraction(
        name=name,
        classes=tuple(order),
        properties=properties,
        discarded=discarded,
        open_passthrough=passthrough,
        unresolved=unresolved,
        writes=writes,
    )


# =============================================================================
# Other sources: shipped setups, docs cards
# =============================================================================

_SETUP_SECTIONS = tuple(CATEGORY_METADATA)


def _json_kind(value) -> Optional[str]:
    if isinstance(value, bool):
        return KIND_BOOLEAN
    if isinstance(value, int):
        return KIND_INTEGER
    if isinstance(value, float):
        return KIND_NUMBER
    if isinstance(value, str):
        return KIND_STRING
    if isinstance(value, list):
        return KIND_ARRAY
    if isinstance(value, dict):
        return KIND_OBJECT
    if value is None:
        return "null"
    return None


def example_kinds_from_setups(setups_dir: Path) -> dict[tuple[str, str], set[str]]:
    """``(managerName, key) -> {kinds}`` over every device in every setup file.

    Usage, not contract: this only ever *types* a key the code already reads,
    and it never becomes a validation constraint.
    """
    kinds: dict[tuple[str, str], set[str]] = {}
    for path in sorted(Path(setups_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        for section in _SETUP_SECTIONS:
            devices = data.get(section)
            if not isinstance(devices, dict):
                continue
            for device in devices.values():
                if not isinstance(device, dict):
                    continue
                manager = device.get("managerName")
                props = device.get(PROPS_ATTR)
                if not isinstance(manager, str) or not isinstance(props, dict):
                    continue
                for key, value in props.items():
                    kind = _json_kind(value)
                    if kind is not None:
                        kinds.setdefault((manager, key), set()).add(kind)
    return kinds


@dataclass(frozen=True)
class DocsField:
    """One row of a manager card's ``managerProperties`` table."""

    key: str
    type_text: str
    kinds: tuple[str, ...]
    nullable: bool
    description: str


def parse_docs_type(text: str) -> tuple[tuple[str, ...], bool]:
    """Map the cards' free-text *Type* to kinds, against a fixed vocabulary.

    Unrecognised text yields no kinds -- a card is never guessed at.
    """
    cleaned = re.sub(r"\(.*?\)", "", text).strip().lower()
    cleaned = re.sub(r"\[.*?\]", "", cleaned)  # list[str] -> list
    kinds: list[str] = []
    nullable = False
    for part in re.split(r"\s+or\s+|\s*\|\s*|\s*,\s*", cleaned):
        part = part.strip()
        if not part:
            continue
        if part in ("null", "none"):
            nullable = True
            continue
        kind = _DOCS_KINDS.get(part)
        if kind is None:
            return (), False
        if kind not in kinds:
            kinds.append(kind)
    return tuple(kinds), nullable


_CARD_HEADING = re.compile(r"^([A-Za-z0-9]+Manager)$")
_CARD_ROW = re.compile(r"^\s+\* - ``([A-Za-z0-9_.]+)``")
_CARD_HEADER = re.compile(r"^\s+\* - (Field|Key|Property)\s*$")
_CARD_CELL = re.compile(r"^\s+- (.*)$")

#: Header labels, lowercased, that name the two columns a card is read for.
_TYPE_HEADERS = {"type"}
_MEANING_HEADERS = {"meaning", "description", "purpose"}


def _row_cells(lines: list[str], start: int) -> list[str]:
    """The cells of one list-table row: the ``* -`` cell and the ``-`` cells after it."""
    first = re.match(r"^\s+\* - (.*)$", lines[start])
    cells = [first.group(1).strip() if first else ""]
    index = start + 1
    while index < len(lines):
        cell = _CARD_CELL.match(lines[index])
        if not cell or lines[index].lstrip().startswith("* -"):
            break
        cells.append(cell.group(1).strip())
        index += 1
    return cells


def docs_cards(docs_dir: Path) -> dict[str, dict[str, DocsField]]:
    """``manager -> key -> DocsField`` from the ``docs/devices/*.rst`` cards.

    Cards are ``list-table`` blocks whose header row names the columns. Most
    are ``Field / Type / Meaning``; some carry a ``Default`` column between --
    reading cells by position took AAAOTF's defaults for its descriptions. The
    header decides which cell is the type and which the meaning.
    """
    cards: dict[str, dict[str, DocsField]] = {}
    for path in sorted(Path(docs_dir).glob("*.rst")):
        lines = path.read_text(encoding="utf-8").splitlines()
        current: Optional[str] = None
        type_col, meaning_col = 1, 2
        for index, line in enumerate(lines):
            heading = _CARD_HEADING.match(line)
            if heading:
                current = heading.group(1)
                cards.setdefault(current, {})
                type_col, meaning_col = 1, 2
                continue
            if _CARD_HEADER.match(line):
                header = [cell.lower() for cell in _row_cells(lines, index)]
                type_col = next((i for i, h in enumerate(header) if h in _TYPE_HEADERS), 1)
                meaning_col = next((i for i, h in enumerate(header) if h in _MEANING_HEADERS), len(header) - 1)
                continue
            row = _CARD_ROW.match(line)
            if not row or current is None:
                continue
            cells = _row_cells(lines, index)
            key = row.group(1).split(".")[0]
            type_text = cells[type_col] if type_col < len(cells) else ""
            description = cells[meaning_col] if meaning_col < len(cells) else ""
            kinds, nullable = parse_docs_type(type_text)
            cards[current].setdefault(
                key, DocsField(key, type_text, kinds, nullable, description)
            )
    return cards


# =============================================================================
# The coverage report
# =============================================================================

@dataclass
class ManagerRow:
    name: str
    keys: int
    required: int
    uncertain: int
    refs: int
    constrained: int
    unresolved: int
    reads_any: bool
    passthrough: bool


@dataclass
class CoverageReport:
    managers: int
    reads_any: int
    with_keys: int
    keys: int
    required: int
    optional: int
    uncertain: int
    typed_by_code: int
    typed_with_examples: int
    typed_with_docs: int
    #: Keys whose only code default is ``None`` (nullability, no kind from code).
    none_default_only: int
    #: ... of which no other source could type either.
    none_only: int
    refs: int
    #: Alias spellings folded into canonical properties (``mock_random_seed``).
    alias_spellings: int
    constrained: int
    #: Reads whose key the analysis could not name, and writes it ignored.
    unresolved_reads: int
    writes: int
    docs_documented: int
    docs_agree: int
    rows: list[ManagerRow]
    uncertain_keys: list[str]
    unconstrained_keys: list[str]
    discarded: list[str]
    unresolved: list[str]

    def snapshot(self) -> dict:
        """The part of the report a checked-in snapshot pins."""
        totals = {
            name: getattr(self, name)
            for name in (
                "managers", "reads_any", "with_keys", "keys", "required", "optional",
                "uncertain", "typed_by_code", "typed_with_examples", "typed_with_docs",
                "none_default_only", "none_only", "refs", "alias_spellings", "constrained",
                "unresolved_reads", "writes", "docs_documented", "docs_agree",
            )
        }
        return {
            "totals": totals,
            "managers": [
                {"name": row.name, "keys": row.keys, "required": row.required,
                 "uncertain": row.uncertain, "refs": row.refs, "constrained": row.constrained,
                 "unresolved": row.unresolved}
                for row in self.rows
            ],
        }


def extract_managers(
    manager_names: Iterable[str],
    *,
    managers_root: Path,
    setups_dir: Optional[Path] = None,
    docs_dir: Optional[Path] = None,
    class_names: Optional[dict[str, Optional[str]]] = None,
) -> dict[str, ManagerExtraction]:
    """Extract the named managers; ``class_names`` maps a name to its class when they differ."""
    tree = extract_tree_indexed(managers_root)
    example_kinds = example_kinds_from_setups(setups_dir) if setups_dir else {}
    cards = docs_cards(docs_dir) if docs_dir else {}
    class_names = class_names or {}
    return {
        name: merge_manager(
            name, tree.classes,
            class_name=class_names.get(name) or resolve_class_name(name, tree),
            example_kinds=example_kinds, docs_cards=cards,
        )
        for name in sorted(manager_names)
    }


def coverage_report(
    extractions: dict[str, ManagerExtraction],
    *,
    docs: Optional[dict[str, dict[str, DocsField]]] = None,
) -> CoverageReport:
    rows: list[ManagerRow] = []
    keys = required = optional = uncertain = 0
    typed_code = typed_examples = typed_docs = none_only = refs = constrained = 0
    none_default_only = alias_spellings = unresolved_reads = writes = 0
    uncertain_keys: list[str] = []
    unconstrained_keys: list[str] = []
    discarded: list[str] = []
    unresolved: list[str] = []
    docs_documented = docs_agree = 0
    for name, extraction in extractions.items():
        row = ManagerRow(name, 0, 0, 0, 0, 0, len(extraction.unresolved),
                         extraction.reads_any, extraction.open_passthrough)
        for key, spec in extraction.properties.items():
            row.keys += 1
            keys += 1
            if spec.required == REQUIRED:
                required += 1
                row.required += 1
            elif spec.required == UNCERTAIN:
                uncertain += 1
                optional += 1
                row.uncertain += 1
                uncertain_keys.append(f"{name}.{key}")
            else:
                optional += 1
            if spec.typed_by_code:
                typed_code += 1
                typed_examples += 1
                typed_docs += 1
            elif any(tag.startswith("example:") for tag in spec.kind_source):
                typed_examples += 1
                typed_docs += 1
            elif any(tag.startswith("docs:") for tag in spec.kind_source):
                typed_docs += 1
            if spec.nullable and not spec.typed_by_code:
                none_default_only += 1
            if spec.nullable and not spec.kind:
                none_only += 1
            alias_spellings += len(spec.aliases)
            if spec.ref_category:
                refs += 1
                row.refs += 1
            if spec.constraint:
                constrained += 1
                row.constrained += 1
            else:
                unconstrained_keys.append(f"{name}.{key}")
        for item in extraction.discarded:
            discarded.append(f"{name}.{item.key} via {item.receiver} (line {item.lineno})")
        for item in extraction.unresolved:
            unresolved.append(f"{name}: {item.expr} (line {item.lineno}; {item.reason})")
        unresolved_reads += len(extraction.unresolved)
        writes += len(extraction.writes)
        if docs and name in docs and extraction.properties:
            documented = set(docs[name])
            docs_documented += len(documented)
            # Cards list nested keys flat (ThorCamTSI's ``exposure_us`` lives
            # under ``defaults``), so a sub-property counts as agreement.
            known = set(extraction.properties) | {
                sub for spec in extraction.properties.values() for sub in spec.sub_properties
            }
            docs_agree += len(documented & known)
        rows.append(row)
    return CoverageReport(
        managers=len(extractions),
        reads_any=sum(1 for e in extractions.values() if e.reads_any),
        with_keys=sum(1 for e in extractions.values() if e.properties),
        keys=keys, required=required, optional=optional, uncertain=uncertain,
        typed_by_code=typed_code, typed_with_examples=typed_examples,
        typed_with_docs=typed_docs, none_default_only=none_default_only,
        none_only=none_only, refs=refs, alias_spellings=alias_spellings,
        constrained=constrained, unresolved_reads=unresolved_reads, writes=writes,
        docs_documented=docs_documented, docs_agree=docs_agree,
        rows=rows, uncertain_keys=uncertain_keys,
        unconstrained_keys=unconstrained_keys, discarded=discarded, unresolved=unresolved,
    )


def format_report(report: CoverageReport, extractions: Optional[dict[str, ManagerExtraction]] = None) -> str:
    def pct(n: int) -> str:
        return f"{100 * n // report.keys}%" if report.keys else "-"

    lines = [
        "Config editor -- managerProperties extraction coverage",
        "",
        f"{'Selectable managers':52s} {report.managers}",
        f"{'... that read any managerProperties':52s} {report.reads_any}",
        f"{'... whose keys static extraction recovers':52s} {report.with_keys}",
        f"{'Distinct keys recovered':52s} {report.keys}  (+{report.alias_spellings} alias spellings folded in)",
        f"{'Required (guard-aware)':52s} {report.required}",
        f"{'Optional':52s} {report.optional}  (of which uncertain: {report.uncertain})",
        f"{'Kind from code alone':52s} {report.typed_by_code} ({pct(report.typed_by_code)})",
        f"{'... plus shipped setups':52s} {report.typed_with_examples} ({pct(report.typed_with_examples)})",
        f"{'... plus docs Type column':52s} {report.typed_with_docs} ({pct(report.typed_with_docs)})",
        f"{'Only a None default in code':52s} {report.none_default_only}  (still untyped after all sources: {report.none_only})",
        f"{'Device references (data-flow)':52s} {report.refs}",
        f"{'Validation-constrained (provable)':52s} {report.constrained}",
        f"{'Unresolved reads (key not nameable)':52s} {report.unresolved_reads}",
        f"{'Writes/deletes ignored':52s} {report.writes}",
        f"{'Docs fields agreeing with extraction':52s} {report.docs_agree}/{report.docs_documented}",
        "",
        f"{'manager':34s} keys  req  unc  refs  constr  unres  notes",
    ]
    for row in report.rows:
        notes = []
        if row.passthrough:
            notes.append("open pass-through")
        if not row.reads_any:
            notes.append("reads nothing")
        lines.append(
            f"{row.name:34s} {row.keys:4d} {row.required:4d} {row.uncertain:4d} "
            f"{row.refs:5d} {row.constrained:7d} {row.unresolved:6d}  {', '.join(notes)}"
        )
    if report.uncertain_keys:
        lines += ["", "Uncertain requiredness (treated as optional):"] + [f"  {k}" for k in report.uncertain_keys]
    if report.unresolved:
        lines += ["", "Unresolved reads (reported, never guessed):"] + [f"  {u}" for u in report.unresolved]
    if report.discarded:
        lines += ["", "Reads discarded (receiver is not this manager's Info):"] + [f"  {d}" for d in report.discarded]
    if extractions:
        lines += ["", "Properties (kind <- source; constraint):"]
        for name, extraction in extractions.items():
            for key, spec in extraction.properties.items():
                kind = spec.kind if spec.kind is not None else "-"
                constraint = json.dumps(spec.constraint["type"]) if spec.constraint else "none"
                flags = []
                if spec.nullable:
                    flags.append("nullable")
                if spec.aliases:
                    flags.append("aliases=" + ",".join(spec.aliases))
                if spec.ref_category:
                    flags.append(f"ref->{spec.ref_category}")
                vias = sorted({read.via for read in spec.reads if read.via and read.via != "literal"})
                if vias:
                    flags.append("via=" + ",".join(vias))
                lines.append(
                    f"  {name}.{key}: {spec.required}; kind={kind} <- {','.join(spec.kind_source) or '-'};"
                    f" constraint={constraint}{'; ' + ' '.join(flags) if flags else ''}"
                )
    return "\n".join(lines)


# Copyright (C) 2020-2021 ImSwitch developers
# This file is part of ImSwitch.
#
# ImSwitch is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ImSwitch is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
