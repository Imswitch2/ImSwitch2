"""Finding an etSTED analysis pipeline file, and reading its parameters.

Qt-free and free of ImControl: a pipeline's parameters are read from the
file's *syntax tree* (the signature of ``def <name>(img, prev_frames,
binary_mask, testmode, exinfo, **numeric_params)`` and its literal defaults),
so a document that names a pipeline can be opened, validated and its
parameters exposed without running the user's code. The code runs when the
pipeline runs (:func:`load_runner`), as it does in etSTED.

The analysis folders are those of the three event-triggered panels::

    <ImSwitchConfig>/imcontrol_etsted/analysis_pipelines/<name>.py
    <ImSwitchConfig>/imcontrol_etsnouty/analysis_pipelines/<name>.py
    <ImSwitchConfig>/imcontrol_etmonalisa/analysis_pipelines/<name>.py
"""

from __future__ import annotations

import ast
import os
import traceback
from dataclasses import dataclass, field
from pathlib import Path

#: Panel (the ``folder`` parameter) -> its folder below the user's ImSwitch folder.
FOLDERS = {
    "etsted": "imcontrol_etsted",
    "etsnouty": "imcontrol_etsnouty",
    "etmonalisa": "imcontrol_etmonalisa",
}
ANALYSIS_SUBFOLDER = "analysis_pipelines"

#: The arguments every pipeline starts with (the runner's own list).
REQUIRED = ("img", "prev_frames", "binary_mask", "testmode", "exinfo")


class PipelineFileError(RuntimeError):
    """The pipeline file cannot be found, read or used; the message names the file (and line)."""


@dataclass(frozen=True)
class PipelineParam:
    """One numeric parameter of a pipeline function (an argument after ``exinfo``)."""

    name: str
    #: The literal default, or ``None`` when the argument has none (etSTED's panel starts it at 0)
    #: or its default is an expression that is not a literal.
    default: object = None
    has_default: bool = False
    #: Whether a form can show it: a number or a flag.
    exposed: bool = True
    note: str = ""

    @property
    def is_bool(self) -> bool:
        return isinstance(self.default, bool)

    @property
    def start_value(self) -> float | bool:
        """What a field starts at: the default as the pipeline would get it, else 0 (as etSTED's panel)."""
        if isinstance(self.default, bool):
            return self.default
        if isinstance(self.default, (int, float)):
            return float(self.default)
        return 0.0


@dataclass
class PipelineInfo:
    """What the file's text says about the pipeline."""

    path: Path
    name: str
    params: list[PipelineParam] = field(default_factory=list)
    #: Reasons the file cannot work as an etSTED pipeline (missing required arguments, ...).
    problems: list[str] = field(default_factory=list)
    doc: str = ""

    @property
    def exposed(self) -> list[PipelineParam]:
        return [p for p in self.params if p.exposed]


def analysis_folder(folder: str = "etsted") -> Path:
    """The ``analysis_pipelines`` folder of a panel (``etsted``, ``etsnouty``, ``etmonalisa``),
    or the folder itself when ``folder`` is a path."""
    key = str(folder or "etsted")
    if key in FOLDERS:
        from imswitch.imcommon.model import dirtools

        return Path(dirtools.UserFileDirs.Root) / FOLDERS[key] / ANALYSIS_SUBFOLDER
    return Path(key)


def list_pipelines(folder: str = "etsted") -> list[str]:
    """The pipeline names (file names without ``.py``) the panel's list would offer, sorted."""
    where = analysis_folder(folder)
    try:
        names = os.listdir(where)
    except OSError:
        return []
    return sorted(n[:-3] for n in names
                  if n.endswith(".py") and not n.startswith("_") and (where / n).is_file())


def resolve_pipeline(pipeline: str, folder: str = "etsted") -> Path:
    """The file of ``pipeline``: a name found in ``folder``, or a path to a ``.py`` file."""
    text = str(pipeline or "").strip()
    if not text:
        raise PipelineFileError("no analysis pipeline chosen (set the 'pipeline' parameter)")
    candidate = Path(text).expanduser()
    if candidate.suffix == ".py" or os.sep in text or (os.altsep and os.altsep in text):
        path = candidate if candidate.suffix == ".py" else candidate.with_suffix(".py")
        if not path.is_file():
            raise PipelineFileError(f"pipeline file not found: {path}")
        return path
    where = analysis_folder(folder)
    path = where / f"{text}.py"
    if not path.is_file():
        have = ", ".join(list_pipelines(folder)) or "none"
        raise PipelineFileError(f"no pipeline {text!r} in {where} (have: {have})")
    return path


def _literal(node):
    try:
        return True, ast.literal_eval(node)
    except Exception:  # noqa: BLE001 - an expression that is not a literal
        return False, None


def inspect_pipeline_file(path) -> PipelineInfo:
    """Read ``path``'s pipeline function signature without running the file.

    Raises :class:`PipelineFileError` (file and line) for a file that is not Python or does not define
    the function; a function that is not a valid etSTED pipeline is returned with its ``problems``.
    """
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PipelineFileError(f"cannot read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise PipelineFileError(f"{path.name}:{exc.lineno}: syntax error: {exc.msg}") from exc
    name = path.stem
    function = next((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name), None)
    if function is None:
        raise PipelineFileError(f"{path.name} must define a function called {name!r} (the file name without .py)")
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    info = PipelineInfo(path=path, name=name, doc=ast.get_docstring(tree) or "")
    names = [a.arg for a in positional]
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        info.problems.append(f"missing required arguments: {', '.join(missing)}")
    elif tuple(names[:len(REQUIRED)]) != REQUIRED:
        info.problems.append(f"the first arguments must be {', '.join(REQUIRED)} in this order "
                             f"(etSTED passes them positionally); found {', '.join(names[:len(REQUIRED)])}")
    for argument, default in zip(positional, defaults):
        if argument.arg in REQUIRED:
            continue
        info.params.append(_param(argument.arg, default))
    for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults):
        info.problems.append(f"keyword-only argument {argument.arg!r}: etSTED passes the parameters "
                             "positionally, so it cannot be set")
        info.params.append(PipelineParam(argument.arg, exposed=False, note="keyword-only"))
    if arguments.vararg is not None:
        info.problems.append(f"*{arguments.vararg.arg}: a pipeline takes a fixed list of numeric parameters")
    if arguments.kwarg is not None:
        info.problems.append(f"**{arguments.kwarg.arg}: a pipeline takes a fixed list of numeric parameters")
    return info


def _param(name: str, default_node) -> PipelineParam:
    if default_node is None:
        return PipelineParam(name, None, False, True, "no default: starts at 0, as in etSTED's panel")
    ok, value = _literal(default_node)
    if not ok:
        return PipelineParam(name, None, True, True, "default is an expression: starts at 0 here, the pipeline's own "
                                                    "default applies until you set it")
    if isinstance(value, bool) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return PipelineParam(name, value, True, True)
    return PipelineParam(name, value, True, False, f"default {value!r} is not a number: etSTED's panel cannot set it")


def error_text(path, exc: BaseException) -> str:
    """``<file>:<line> in <function>: <Type>: <message>`` for an error raised by (or while loading)
    the pipeline file; the innermost frame in that file, else just the file name."""
    path = Path(path)
    where = ""
    if isinstance(exc, SyntaxError) and exc.lineno:
        where = f"{path.name}:{exc.lineno}"
        function = ""
    else:
        function = ""
        try:
            resolved = str(path.resolve())
            for frame in reversed(traceback.extract_tb(exc.__traceback__)):
                if os.path.abspath(frame.filename) == resolved or str(frame.filename) == str(path):
                    where, function = f"{path.name}:{frame.lineno}", f" in {frame.name}"
                    break
        except Exception:  # noqa: BLE001 - never fail while reporting
            where = ""
    message = exc.msg if isinstance(exc, SyntaxError) else str(exc)
    prefix = f"{where}{function}" if where else path.name
    return f"{prefix}: {type(exc).__name__}: {message}"
