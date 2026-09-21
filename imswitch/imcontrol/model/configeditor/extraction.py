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

Two ideas run through everything:

* **Evidence is not a constraint.** A default literal or an ``int()`` call says
  what a manager *prefers*, not what it accepts (``BSC203StageManager`` divides
  an integer-defaulted ``travelRangeUm``). So a property's *kind* -- the
  editor's widget preference -- is inferred from anything, while its
  *constraint* -- what validation may reject -- is emitted only where the code
  proves it (``Path(...)``, a nested subscript, ``.items()``).
* **A subscript is not "required".** ``AAAOTFLaserManager`` reads
  ``calibCsvPath`` inside ``try/except KeyError``; a shipped setup omits it.
  Requiredness is decided over every read of a key, guards included, and what
  the analysis cannot classify is reported as ``uncertain`` rather than guessed.
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

#: Mapping-only methods: calling one on a read proves the value is an object.
_MAPPING_METHODS = {"items", "keys", "values", "get", "setdefault", "update", "pop"}

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
    #: The read is itself subscripted or has a mapping method called on it.
    used_as_mapping: bool = False


@dataclass(frozen=True)
class DiscardedRead:
    """A ``managerProperties`` read the strict receiver rule refused.

    Only reads whose receiver traces to the constructor's ``*Info`` parameter
    count as this manager's own properties. ``TriggerScopeManager`` reading
    ``targetInfo.managerProperties["minVolt"]`` is reading a *positioner's*
    contract, and belongs in a role rule, not here.
    """

    key: str
    receiver: str
    lineno: int


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

    @property
    def typed_by_code(self) -> bool:
        return any(tag.startswith("code:") for tag in self.kind_source)


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
    #: The whole dict was handed to something opaque (a driver, ``serial.Serial``).
    open_passthrough: bool = False


@dataclass
class ManagerExtraction:
    """A manager's contract, merged over its base classes and other sources."""

    name: str
    classes: tuple[str, ...]  # resolution order, subclass first
    properties: dict[str, PropertySpec]
    discarded: list[DiscardedRead]
    open_passthrough: bool

    @property
    def reads_any(self) -> bool:
        return bool(self.properties) or self.open_passthrough or bool(self.discarded)


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


def _name_of(node: ast.AST) -> Optional[str]:
    """The simple name a ``Name`` or ``Attribute`` node ends in."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _unwrap_props_expr(node: ast.AST) -> ast.AST:
    """See through ``X or {}`` and ``getattr(X, "managerProperties", ...)``."""
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


def _is_low_level_collection(node: ast.AST) -> bool:
    return _name_of(node) in _LOW_LEVEL_NAMES


def _handler_tolerates_missing_key(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_name_of(name) in _MISSING_KEY_HANDLERS for name in names)


# =============================================================================
# Per-class extraction
# =============================================================================

class _ClassScanner:
    """Scan one ``ClassDef`` for reads of its own ``managerProperties``."""

    def __init__(self, cls: ast.ClassDef, parents: dict, module: str):
        self.cls = cls
        self.parents = parents
        self.module = module
        self.info_params = self._constructor_info_params()
        self.info_aliases = set(self.info_params)
        self.props_aliases: set[str] = set()
        self.foreign_props_aliases: set[str] = set()
        self._bind_aliases()
        self.result = ClassExtraction(
            name=cls.name,
            bases=tuple(_name_of(base) or "?" for base in cls.bases),
            module=module,
            info_params=self.info_params,
        )

    # -- what counts as "this manager's" properties object -------------------

    def _constructor_info_params(self) -> tuple[str, ...]:
        for node in self.cls.body:
            if isinstance(node, ast.FunctionDef) and node.name == "__init__":
                return tuple(
                    arg.arg for arg in node.args.args[1:] if arg.arg.lower().endswith("info")
                )
        return ()

    def _receiver_is_info(self, receiver: ast.AST) -> bool:
        """Whether ``receiver.managerProperties`` is this manager's own dict."""
        name = _name_of(receiver)
        if name is None:
            return False  # a subscript, a call: some other device's Info
        if name in self.info_aliases:
            return True
        # ``self._laserInfo`` bound by a base class __init__ this class does
        # not repeat: the attribute name says what it is.
        return isinstance(receiver, ast.Attribute) and name.lower().endswith("info")

    def _bind_aliases(self) -> None:
        """Record names bound to the Info parameter or to its properties dict."""
        for node in ast.walk(self.cls):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [name for name in (_name_of(target) for target in targets) if name]
            if isinstance(value, ast.Name) and value.id in self.info_aliases:
                self.info_aliases.update(names)
                continue
            props = _unwrap_props_expr(value)
            if isinstance(props, ast.Attribute) and props.attr == PROPS_ATTR:
                if self._receiver_is_info(props.value):
                    self.props_aliases.update(names)
                else:
                    self.foreign_props_aliases.update(names)
            elif _name_of(props) in self.props_aliases:
                self.props_aliases.update(names)

    def _classify_props_node(self, node: ast.AST) -> tuple[str, Optional[str]]:
        """``("own", None)``, ``("foreign", receiver)`` or ``("no", None)``."""
        node = _unwrap_props_expr(node)
        if isinstance(node, ast.Attribute) and node.attr == PROPS_ATTR:
            if self._receiver_is_info(node.value):
                return "own", None
            return "foreign", ast.unparse(node.value)
        name = _name_of(node)
        if name in self.props_aliases:
            return "own", None
        if name in self.foreign_props_aliases:
            return "foreign", name
        return "no", None

    # -- reads ----------------------------------------------------------------

    def scan(self) -> ClassExtraction:
        bound_from_read: dict[str, str] = {}  # name -> key, for ref data-flow
        for node in ast.walk(self.cls):
            read = self._read_at(node)
            if read is None:
                continue
            key, props_node, verdict, receiver = read
            if verdict == "foreign":
                self.result.discarded.append(DiscardedRead(key, receiver or "?", node.lineno))
                continue
            if isinstance(node, ast.Compare):
                self._record(PropertyRead(key, "in", node.lineno, guarded=True, guard="in"))
                continue
            if isinstance(node, ast.Call):
                self._record_get(node, key)
            else:
                self._record_subscript(node, key)
            # Data flow for device references: ``name = props["k"]``.
            parent = self.parents.get(node)
            if isinstance(parent, ast.Call) and _name_of(parent.func) in _WRAPPER_KINDS:
                parent = self.parents.get(parent)
            if isinstance(parent, (ast.Assign, ast.AnnAssign)):
                targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
                for name in (_name_of(target) for target in targets):
                    if name:
                        bound_from_read[name] = key
        self._scan_refs(bound_from_read)
        self._scan_passthrough()
        return self.result

    def _read_at(self, node: ast.AST):
        """Return ``(key, props_node, verdict, receiver)`` if ``node`` reads a key."""
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            verdict, receiver = self._classify_props_node(node.value)
            if verdict != "no":
                return node.slice.value, node.value, verdict, receiver
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            verdict, receiver = self._classify_props_node(node.func.value)
            if verdict != "no":
                return node.args[0].value, node.func.value, verdict, receiver
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
        ):
            verdict, receiver = self._classify_props_node(node.comparators[0])
            if verdict != "no":
                return node.left.value, node.comparators[0], verdict, receiver
        return None

    def _record(self, read: PropertyRead) -> None:
        self.result.reads.setdefault(read.key, []).append(read)

    def _wrapper_and_mapping(self, node: ast.AST) -> tuple[Optional[str], bool]:
        parent = self.parents.get(node)
        wrapper = None
        if isinstance(parent, ast.Call) and node in parent.args:
            name = _name_of(parent.func)
            if name in _WRAPPER_KINDS:
                wrapper = name
        used_as_mapping = (
            (isinstance(parent, ast.Subscript) and parent.value is node)
            or (isinstance(parent, ast.Attribute) and parent.attr in _MAPPING_METHODS)
        )
        return wrapper, used_as_mapping

    def _record_get(self, node: ast.Call, key: str) -> None:
        default = node.args[1] if len(node.args) > 1 else None
        # ``props.get("camelK", props.get("snake_k", d))``: one property, two
        # spellings, the outer one preferred by the manager.
        if (
            isinstance(default, ast.Call)
            and isinstance(default.func, ast.Attribute)
            and default.func.attr == "get"
            and default.args
            and isinstance(default.args[0], ast.Constant)
            and self._classify_props_node(default.func.value)[0] == "own"
        ):
            alias = default.args[0].value
            self.result.aliases[alias] = key
            default = default.args[1] if len(default.args) > 1 else None
        wrapper, used_as_mapping = self._wrapper_and_mapping(node)
        self._record(PropertyRead(
            key, "get", node.lineno, guarded=True, guard="get",
            default_kind=_constant_kind(default) if default is not None else None,
            nullable=default is None or (isinstance(default, ast.Constant) and default.value is None),
            wrapper=wrapper, used_as_mapping=used_as_mapping,
        ))

    def _record_subscript(self, node: ast.Subscript, key: str) -> None:
        guarded, guard, uncertain = self._guard_status(node, key)
        wrapper, used_as_mapping = self._wrapper_and_mapping(node)
        self._record(PropertyRead(
            key, "subscript", node.lineno, guarded=guarded, guard=guard,
            uncertain=uncertain, wrapper=wrapper, used_as_mapping=used_as_mapping,
        ))

    def _guard_status(self, node: ast.AST, key: str) -> tuple[bool, Optional[str], bool]:
        """Whether a missing ``key`` can raise at this subscript.

        Walks up the tree. A ``try`` whose handlers tolerate a missing key, or
        an ``if "key" in props:``, guards a read in its body. A condition that
        mentions the properties object but is not a recognised guard for this
        key makes the read *uncertain* rather than required.
        """
        uncertain = False
        for ancestor, child in _ancestry(node, self.parents):
            if isinstance(ancestor, ast.Try):
                if child in ancestor.body and any(
                    _handler_tolerates_missing_key(handler) for handler in ancestor.handlers
                ):
                    handled = ", ".join(
                        _name_of(h.type) or "bare" if h.type is not None else "bare"
                        for h in ancestor.handlers
                    )
                    return True, f"try/except {handled}", False
            elif isinstance(ancestor, (ast.If, ast.IfExp)):
                in_body = child is ancestor.body or (
                    isinstance(ancestor.body, list) and child in ancestor.body
                )
                if in_body and self._test_guards_key(ancestor.test, key):
                    return True, "in-guard", False
                if self._test_mentions_props(ancestor.test) and not (
                    in_body and self._test_guards_key(ancestor.test, key)
                ):
                    uncertain = True
            elif isinstance(ancestor, (ast.FunctionDef, ast.ClassDef)):
                break
        return False, None, uncertain

    def _test_guards_key(self, test: ast.AST, key: str) -> bool:
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return any(self._test_guards_key(value, key) for value in test.values)
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Constant)
            and test.left.value == key
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
        ):
            return self._classify_props_node(test.comparators[0])[0] == "own"
        if (
            isinstance(test, ast.Call)
            and isinstance(test.func, ast.Attribute)
            and test.func.attr == "get"
            and test.args
            and isinstance(test.args[0], ast.Constant)
            and test.args[0].value == key
        ):
            return self._classify_props_node(test.func.value)[0] == "own"
        return False

    def _test_mentions_props(self, test: ast.AST) -> bool:
        return any(
            self._classify_props_node(sub)[0] == "own"
            for sub in ast.walk(test)
            if isinstance(sub, (ast.Name, ast.Attribute, ast.Call, ast.BoolOp))
        )

    # -- device references and pass-through -----------------------------------

    def _scan_refs(self, bound_from_read: dict[str, str]) -> None:
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
            index = node.slice
            key = None
            if isinstance(index, ast.Subscript) and isinstance(index.slice, ast.Constant):
                if self._classify_props_node(index.value)[0] == "own":
                    key = index.slice.value
            elif isinstance(index, ast.Call) and isinstance(index.func, ast.Attribute) \
                    and index.func.attr == "get" and index.args \
                    and isinstance(index.args[0], ast.Constant) \
                    and self._classify_props_node(index.func.value)[0] == "own":
                key = index.args[0].value
            else:
                key = bound_from_read.get(_name_of(index) or "")
            if key is not None:
                self.result.refs[key] = category

    def _scan_passthrough(self) -> None:
        """The whole dict handed to a call: the driver decides the keys."""
        for node in ast.walk(self.cls):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
                continue
            if _name_of(node.func) in _COPY_CALLS:
                continue
            candidates = list(node.args) + [kw.value for kw in node.keywords if kw.arg is None]
            for candidate in candidates:
                if self._classify_props_node(candidate)[0] == "own":
                    self.result.open_passthrough = True
                    return


def _bucket_category(bucket: str) -> Optional[str]:
    """``rs232sManager`` -> ``rs232devices``; singleton buckets are not refs."""
    if not bucket.endswith("sManager"):
        return None
    kind = bucket[: -len("sManager")]
    metadata = KIND_METADATA.get(kind)
    return metadata.editor_category if metadata else None


def extract_module(source: str, module: str = "<string>") -> dict[str, ClassExtraction]:
    """Extract every class in one Python source text."""
    tree = ast.parse(source)
    parents = _parent_map(tree)
    return {
        node.name: _ClassScanner(node, parents, module).scan()
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }


def extract_tree(root: Path) -> dict[str, ClassExtraction]:
    """Extract every class under ``root`` (recursively), keyed by class name."""
    classes: dict[str, ClassExtraction] = {}
    for path in sorted(Path(root).rglob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            module_classes = extract_module(source, str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for name, extraction in module_classes.items():
            # Same class name in two files (a mock beside its real manager):
            # the first in sorted order wins, and the report shows the module.
            classes.setdefault(name, extraction)
    return classes


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
    example_kinds: Optional[dict[tuple[str, str], set[str]]] = None,
    docs_cards: Optional[dict[str, dict[str, "DocsField"]]] = None,
) -> ManagerExtraction:
    """One manager's contract: its own reads plus every base class's."""
    order = resolution_order(name, classes)
    reads: dict[str, list[PropertyRead]] = {}
    aliases: dict[str, str] = {}
    refs: dict[str, str] = {}
    discarded: list[DiscardedRead] = []
    passthrough = False
    for cls_name in reversed(order):  # base first, so a subclass can add
        extraction = classes[cls_name]
        for key, key_reads in extraction.reads.items():
            reads.setdefault(key, []).extend(key_reads)
        aliases.update(extraction.aliases)
        refs.update(extraction.refs)
        discarded.extend(extraction.discarded)
        passthrough = passthrough or extraction.open_passthrough

    # Fold each alias's reads into its canonical property.
    for alias, canonical in aliases.items():
        if alias in reads:
            reads.setdefault(canonical, []).extend(reads.pop(alias))

    properties: dict[str, PropertySpec] = {}
    example_kinds = example_kinds or {}
    card = (docs_cards or {}).get(name, {})
    for key in sorted(reads):
        key_reads = reads[key]
        spec = PropertySpec(key=key, reads=list(key_reads))
        spec.required = _decide_required(key_reads)
        spec.nullable = any(read.nullable for read in key_reads)
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
_CARD_CELL = re.compile(r"^\s+- (.*)$")


def docs_cards(docs_dir: Path) -> dict[str, dict[str, DocsField]]:
    """``manager -> key -> DocsField`` from the ``docs/devices/*.rst`` cards."""
    cards: dict[str, dict[str, DocsField]] = {}
    for path in sorted(Path(docs_dir).glob("*.rst")):
        lines = path.read_text(encoding="utf-8").splitlines()
        current: Optional[str] = None
        for index, line in enumerate(lines):
            heading = _CARD_HEADING.match(line)
            if heading:
                current = heading.group(1)
                cards.setdefault(current, {})
                continue
            row = _CARD_ROW.match(line)
            if not row or current is None:
                continue
            key = row.group(1).split(".")[0]
            type_text = description = ""
            if index + 1 < len(lines):
                cell = _CARD_CELL.match(lines[index + 1])
                type_text = cell.group(1).strip() if cell else ""
            if index + 2 < len(lines):
                cell = _CARD_CELL.match(lines[index + 2])
                description = cell.group(1).strip() if cell else ""
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
    docs_documented: int
    docs_agree: int
    rows: list[ManagerRow]
    uncertain_keys: list[str]
    unconstrained_keys: list[str]
    discarded: list[str]

    def snapshot(self) -> dict:
        """The part of the report a checked-in snapshot pins."""
        totals = {
            name: getattr(self, name)
            for name in (
                "managers", "reads_any", "with_keys", "keys", "required", "optional",
                "uncertain", "typed_by_code", "typed_with_examples", "typed_with_docs",
                "none_default_only", "none_only", "refs", "alias_spellings", "constrained",
                "docs_documented", "docs_agree",
            )
        }
        return {
            "totals": totals,
            "managers": [
                {"name": row.name, "keys": row.keys, "required": row.required,
                 "uncertain": row.uncertain, "refs": row.refs, "constrained": row.constrained}
                for row in self.rows
            ],
        }


def extract_managers(
    manager_names: Iterable[str],
    *,
    managers_root: Path,
    setups_dir: Optional[Path] = None,
    docs_dir: Optional[Path] = None,
) -> dict[str, ManagerExtraction]:
    classes = extract_tree(managers_root)
    example_kinds = example_kinds_from_setups(setups_dir) if setups_dir else {}
    cards = docs_cards(docs_dir) if docs_dir else {}
    return {
        name: merge_manager(name, classes, example_kinds=example_kinds, docs_cards=cards)
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
    none_default_only = alias_spellings = 0
    uncertain_keys: list[str] = []
    unconstrained_keys: list[str] = []
    discarded: list[str] = []
    docs_documented = docs_agree = 0
    for name, extraction in extractions.items():
        row = ManagerRow(name, 0, 0, 0, 0, 0, extraction.reads_any, extraction.open_passthrough)
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
        if docs and name in docs and extraction.properties:
            documented = set(docs[name])
            docs_documented += len(documented)
            docs_agree += len(documented & set(extraction.properties))
        rows.append(row)
    return CoverageReport(
        managers=len(extractions),
        reads_any=sum(1 for e in extractions.values() if e.reads_any),
        with_keys=sum(1 for e in extractions.values() if e.properties),
        keys=keys, required=required, optional=optional, uncertain=uncertain,
        typed_by_code=typed_code, typed_with_examples=typed_examples,
        typed_with_docs=typed_docs, none_default_only=none_default_only,
        none_only=none_only, refs=refs, alias_spellings=alias_spellings,
        constrained=constrained, docs_documented=docs_documented, docs_agree=docs_agree,
        rows=rows, uncertain_keys=uncertain_keys,
        unconstrained_keys=unconstrained_keys, discarded=discarded,
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
        f"{'Docs fields agreeing with extraction':52s} {report.docs_agree}/{report.docs_documented}",
        "",
        f"{'manager':34s} keys  req  unc  refs  constr  notes",
    ]
    for row in report.rows:
        notes = []
        if row.passthrough:
            notes.append("open pass-through")
        if not row.reads_any:
            notes.append("reads nothing")
        lines.append(
            f"{row.name:34s} {row.keys:4d} {row.required:4d} {row.uncertain:4d} "
            f"{row.refs:5d} {row.constrained:7d}  {', '.join(notes)}"
        )
    if report.uncertain_keys:
        lines += ["", "Uncertain requiredness (treated as optional):"] + [f"  {k}" for k in report.uncertain_keys]
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
