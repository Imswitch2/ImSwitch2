"""Static tripwire: API exports that are not marked ``runOnUIThread`` must
not reach Qt widgets, directly or through ``self.<method>`` calls.

Limits (documented in plan A-06): the audit follows ``self.<name>()`` calls
up to three hops within the class bodies found in the same module; it cannot
see through registries, signals, ``getattr`` or calls into managers. The
explicit regression tests in ``imcontrol/_test/unit/test_api_export_thread_
affinity.py`` are the guarantee for the two known cases; this test is the
tripwire for the common one. Opting a method out requires an entry in
``ALLOWED`` with a reason.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]  # imswitch/
WIDGET_TOKENS = ('self._widget', 'self.__widget', 'QMessageBox', 'QtWidgets.')
MAX_HOPS = 3

#: {'module.py::Class.method': 'reason'}
ALLOWED = {}


def _is_api_export(decorator):
    func = decorator.func if isinstance(decorator, ast.Call) else decorator
    name = getattr(func, 'id', getattr(func, 'attr', ''))
    return name == 'APIExport'


def _runs_on_ui_thread(decorator):
    if not isinstance(decorator, ast.Call):
        return False
    for keyword in decorator.keywords:
        if keyword.arg == 'runOnUIThread' and getattr(keyword.value, 'value', False) is True:
            return True
    return False


def _class_methods(tree):
    """{className: {methodName: FunctionDef}} for every class in a module."""
    classes = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes[node.name] = {
                item.name: item for item in node.body if isinstance(item, ast.FunctionDef)
            }
    return classes


def _self_calls(fn):
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'self'
        ):
            yield node.func.attr


def _touches_widget(source, fn):
    text = ast.get_source_segment(source, fn) or ''
    return any(token in text for token in WIDGET_TOKENS)


def _findings():
    findings = []
    for path in sorted(ROOT.rglob('*.py')):
        if '_test' in path.parts or 'worktrees' in path.parts:
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):
            continue
        classes = _class_methods(tree)
        for className, methods in classes.items():
            for methodName, fn in methods.items():
                exports = [d for d in fn.decorator_list if _is_api_export(d)]
                if not exports or any(_runs_on_ui_thread(d) for d in exports):
                    continue
                # Breadth-first over self.<name>() calls, up to MAX_HOPS.
                seen = {methodName}
                frontier = [(methodName, 0)]
                chain = None
                while frontier and chain is None:
                    current, depth = frontier.pop(0)
                    body = methods.get(current)
                    if body is None:
                        continue
                    if _touches_widget(source, body):
                        chain = f'{methodName} -> {current}' if current != methodName else methodName
                        break
                    if depth >= MAX_HOPS:
                        continue
                    for callee in _self_calls(body):
                        if callee not in seen:
                            seen.add(callee)
                            frontier.append((callee, depth + 1))
                if chain is not None:
                    key = f'{path.relative_to(ROOT)}::{className}.{methodName}'
                    if key not in ALLOWED:
                        findings.append((key, chain))
    return findings


def test_no_off_thread_api_export_reaches_a_widget():
    findings = _findings()
    assert not findings, (
        'These API exports run on the caller\'s thread but reach a widget; '
        'mark them runOnUIThread=True or add a justified ALLOWED entry:\n'
        + '\n'.join(f'  {key}: {chain}' for key, chain in findings)
    )
