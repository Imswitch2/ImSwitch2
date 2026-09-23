"""Report and generate manager property schemas from source.

Every manager reads its ``managerProperties`` somewhere in its source. This
tool lifts that out statically -- no manager is imported -- and either reports
what it found or writes it as JSON Schemas into package data, where the editor
and the ``validate-setup`` CLI read it.

Usage
-----

Print the coverage report over the editor's catalog::

    python tools/extract_manager_schemas.py --report [--properties] [--json]

Regenerate the checked-in schemas, fixtures and index
(``imswitch/imcontrol/model/configeditor/schemas/``)::

    python tools/extract_manager_schemas.py --write

Fail if they are out of date with the source (what CI runs)::

    python tools/extract_manager_schemas.py --check

Do the same for an installed device plugin, into the plugin's own package
(``<package>/schemas/``), for the managers its ``imswitch.json`` manifest
declares::

    python tools/extract_manager_schemas.py --package imswitch_my_plugin --write
    python tools/extract_manager_schemas.py --package imswitch_my_plugin --check

The package is located with ``importlib.util.find_spec`` and never imported;
its manifest names the managers (``id``, ``kind``, ``python_name``). Point
each contribution's ``manager_properties_schema`` at the written
``schemas/managers/<id>.json`` and ImSwitch validates and edits against it.

Nothing here imports the manager stack. ``imswitch/imcontrol/model/__init__.py``
imports every manager and the Qt framework on import, so this tool installs a
bare package object for ``imswitch.imcontrol.model`` before importing the
extraction modules beneath it; a test runs the tool in a fresh process with
manager and Qt imports forbidden. The catalog is built from the built-in
registry with plugin discovery **off** and the explicit managers root, so the
output depends on this source tree and nothing installed beside it.

The report covers the catalog's managers. Generation covers the catalog plus
every manager a built-in template names, so ``RS232Manager`` -- selectable by
name in shipped setups, skipped by the catalog's legacy scan as a base class --
gets a schema too. A manager name is resolved to its class through the
registry's ``python_name`` when it has one, else through the module it names
(a re-export, or a single manager class); a name with no class gets no schema
and is listed under ``unresolved`` in the index.

The top-level keys of a device entry (``forAcquisition``, ``axes``, …) come
from the ``SetupInfo`` dataclasses, read from ``SetupInfo.py`` with ``ast`` in
the same run and written as ``kinds/<kind>.json``.

Hand-written overrides in ``schemas/overrides/`` are merged last and never
written by this tool; hand-written role rules in ``schemas/roles/`` are never
written by it either. The rules live in
``imswitch.imcontrol.model.configeditor.extraction`` and are described in
``docs/design/plans/config-editor-schema-extraction.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

MODEL_PACKAGE = "imswitch.imcontrol.model"


def install_light_model_package() -> None:
    """Make ``imswitch.imcontrol.model`` importable without running its ``__init__``.

    The package's ``__init__`` imports the managers and Qt. The modules this
    tool needs (``configeditor.*``, ``plugins.*``) import nothing outside their
    own subpackages, so a bare package object with the right ``__path__`` lets
    them load while the real ``__init__`` never runs. Harmless if the real
    package is already imported.
    """
    if MODEL_PACKAGE in sys.modules:
        return
    package_dir = _REPO_ROOT / "imswitch" / "imcontrol" / "model"
    module = types.ModuleType(MODEL_PACKAGE)
    module.__path__ = [str(package_dir)]
    module.__package__ = MODEL_PACKAGE
    module.__file__ = str(package_dir / "__init__.py")
    sys.modules[MODEL_PACKAGE] = module


install_light_model_package()

from imswitch.imcontrol.model.configeditor import extraction  # noqa: E402
from imswitch.imcontrol.model.configeditor import kinds as kinds_module  # noqa: E402

MANIFEST_FILE = "imswitch.json"
PACKAGE_SCHEMAS_DIR = "schemas"

DEFAULT_MANAGERS_ROOT = _REPO_ROOT / "imswitch" / "imcontrol" / "model" / "managers"
DEFAULT_SETUPS_DIR = _REPO_ROOT / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"
DEFAULT_DOCS_DIR = _REPO_ROOT / "docs" / "devices"
DEFAULT_TEMPLATES_DIR = _REPO_ROOT / "imswitch" / "imcontrol" / "view" / "configeditor" / "builtin_templates"
DEFAULT_SCHEMAS_ROOT = _REPO_ROOT / "imswitch" / "imcontrol" / "model" / "configeditor" / "schemas"
DEFAULT_SETUP_INFO = _REPO_ROOT / "imswitch" / "imcontrol" / "model" / "SetupInfo.py"


def _schemagen():
    """The generator, imported only by the modes that generate."""
    from imswitch.imcontrol.model.configeditor import schemagen

    return schemagen


def catalog_managers(managers_root: Path) -> dict[str, tuple[str, str | None]]:
    """``name -> (editor category, python_name)`` for the core catalog.

    Built-in registry only, discovery off, explicit managers root: the same
    answer on every machine, and no manager package imported to find the
    directory.
    """
    from imswitch.imcontrol.model.configeditor.catalog import build_catalog
    from imswitch.imcontrol.model.plugins.registry import build_default_registry

    registry = build_default_registry(discover=False)
    python_names = {
        contribution.id: contribution.python_name for contribution in registry.list_contributions()
    }
    catalog = build_catalog(registry=registry, managers_root=managers_root)
    return {
        info.manager_name: (info.category, python_names.get(info.manager_name))
        for info in catalog.managers()
    }


def template_managers(templates_dir: Path) -> dict[str, str]:
    """``name -> category`` for every built-in device template."""
    found: dict[str, str] = {}
    for path in sorted(Path(templates_dir).rglob("*.json")):
        if path.parent.name == "sections" or path.stem.startswith("_"):
            continue
        found.setdefault(path.stem, path.parent.name)
    return found


def core_python_names(managers_root: Path, categories: dict[str, str]) -> dict[str, str | None]:
    """``name -> python_name`` for every core manager, exact for registered and legacy alike.

    A registered manager's comes from the registry. An unregistered one's is
    where ``MultiManager`` imports it from (:func:`extraction.legacy_python_name`),
    so a class that merely shares its name elsewhere in the tree is never
    taken for it. ``categories`` is updated with the catalog's category, which
    wins over a template's.
    """
    python_names: dict[str, str | None] = {}
    for name, (category, python_name) in catalog_managers(managers_root).items():
        categories[name] = category
        python_names[name] = python_name
    for name, category in categories.items():
        if not python_names.get(name):
            python_names[name] = extraction.legacy_python_name(name, category)
    return python_names


def generation_inputs(args: argparse.Namespace):
    """Everything ``--write`` and ``--check`` need, computed once."""
    schemagen = _schemagen()
    categories = template_managers(args.templates_dir)
    python_names = core_python_names(args.managers_root, categories)
    tree = extraction.extract_tree_indexed(args.managers_root)
    class_names = {
        name: extraction.resolve_class_name(name, tree, python_names.get(name))
        for name in sorted(categories)
    }
    resolved = [name for name, cls in class_names.items() if cls]
    unresolved = [name for name, cls in class_names.items() if not cls]
    example_kinds = extraction.example_kinds_from_setups(args.setups_dir) if not args.no_examples else {}
    cards = extraction.docs_cards(args.docs_dir) if not args.no_docs else {}
    extractions = {
        name: extraction.merge_manager(
            name, tree.classes, class_name=class_names[name],
            example_kinds=example_kinds, docs_cards=cards,
        )
        for name in resolved
    }
    report = extraction.coverage_report(extractions, docs=cards or None)
    return schemagen.GenerationInputs(
        extractions=extractions,
        classes=tree.classes,
        categories={name: categories[name] for name in resolved},
        overrides=schemagen.load_overrides(args.schemas_root),
        report=report,
        unresolved=tuple(unresolved),
        info_classes=kinds_module.load_info_classes(args.setup_info),
    )


def find_package_spec(package: str):
    """The spec of an installed package, located without running any of its code.

    ``importlib.util.find_spec("a.b")`` imports ``a`` -- executes
    ``a/__init__.py`` -- to learn where ``a.b`` lives, and a hardware
    plugin's ``__init__`` may import a vendor SDK. So each level is located
    by asking the import system's finders directly, handing a subpackage its
    parent's search locations instead of importing the parent. Finding a
    spec never executes a module; the finders are the ones an import would
    consult (editable installs included). None if there is no such package.
    """
    parts = package.split(".")
    if not all(part.isidentifier() for part in parts):
        return None
    spec = None
    locations = None
    for depth in range(1, len(parts) + 1):
        fullname = ".".join(parts[:depth])
        spec = None
        for finder in sys.meta_path:
            find_spec = getattr(finder, "find_spec", None)
            if find_spec is None:
                continue
            try:
                spec = find_spec(fullname, locations)
            except (ImportError, ValueError):
                spec = None
            if spec is not None:
                break
        if spec is None:
            return None
        locations = spec.submodule_search_locations
        if locations is None and depth < len(parts):
            return None  # a module on the way down, not a package
    return spec


def package_root(package: str) -> Path:
    """The directory of an installed package, found without importing it or its parents."""
    spec = find_package_spec(package)
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"package {package!r} is not installed or is not a package")
    return Path(next(iter(spec.submodule_search_locations)))


def package_managers(root: Path, package: str) -> dict[str, tuple[str, str | None]]:
    """``id -> (editor category, python_name)`` from the package's ``imswitch.json``."""
    from imswitch.imcontrol.model.plugins.manifest import parse_manifest
    from imswitch.imcontrol.model.plugins.setup_metadata import KIND_METADATA

    manifest = root / MANIFEST_FILE
    if not manifest.is_file():
        raise SystemExit(f"{package}: no {MANIFEST_FILE} manifest in {root}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    contributions = parse_manifest(
        data, plugin_name=data.get("name", package), plugin_version=data.get("version"), source_package=package,
    )
    found: dict[str, tuple[str, str | None]] = {}
    for contribution in contributions:
        metadata = KIND_METADATA.get(contribution.kind)
        if metadata is None:
            continue
        found[contribution.id] = (metadata.editor_category, contribution.python_name)
    return found


def package_inputs(args: argparse.Namespace):
    """Everything ``--package`` needs: the plugin's managers, from its own tree only."""
    schemagen = _schemagen()
    root = package_root(args.package)
    managers = package_managers(root, args.package)
    # Named as the package it is, so each class's key is its python_name.
    tree = extraction.extract_tree_indexed(root, package=args.package)
    class_names = {
        name: extraction.resolve_class_name(name, tree, python_name)
        for name, (_category, python_name) in sorted(managers.items())
    }
    resolved = [name for name, cls in class_names.items() if cls]
    unresolved = [name for name, cls in class_names.items() if not cls]
    extractions = {
        name: extraction.merge_manager(name, tree.classes, class_name=class_names[name],
                                       example_kinds={}, docs_cards={})
        for name in resolved
    }
    schemas_root = args.schemas_root if args.schemas_root_given else root / PACKAGE_SCHEMAS_DIR
    return schemagen.GenerationInputs(
        extractions=extractions,
        classes=tree.classes,
        categories={name: managers[name][0] for name in resolved},
        overrides=schemagen.load_overrides(schemas_root),
        report=extraction.coverage_report(extractions),
        unresolved=tuple(unresolved),
    ), schemas_root


def run_report(args: argparse.Namespace) -> int:
    if args.package:
        inputs, _root = package_inputs(args)
        if args.json:
            print(json.dumps(inputs.report.snapshot(), indent=2, sort_keys=True))
        else:
            print(extraction.format_report(inputs.report, inputs.extractions if args.properties else None))
        if inputs.unresolved:
            print("no class found in the package (no schema): " + ", ".join(inputs.unresolved))
        return 0
    categories: dict[str, str] = {}
    python_names = core_python_names(args.managers_root, categories)
    names = args.managers or sorted(catalog_managers(args.managers_root))
    extractions = extraction.extract_managers(
        names,
        managers_root=args.managers_root,
        setups_dir=None if args.no_examples else args.setups_dir,
        docs_dir=None if args.no_docs else args.docs_dir,
        python_names=python_names,
    )
    docs = None if args.no_docs else extraction.docs_cards(args.docs_dir)
    report = extraction.coverage_report(extractions, docs=docs)
    if args.json:
        print(json.dumps(report.snapshot(), indent=2, sort_keys=True))
    else:
        print(extraction.format_report(report, extractions if args.properties else None))
    return 0


def _inputs_and_root(args: argparse.Namespace):
    if args.package:
        return package_inputs(args)
    return generation_inputs(args), args.schemas_root


def run_write(args: argparse.Namespace) -> int:
    schemagen = _schemagen()
    inputs, schemas_root = _inputs_and_root(args)
    files = schemagen.generate_all(inputs)
    written = schemagen.write(files, schemas_root)
    print(f"{len(files)} generated files for {len(inputs.extractions)} managers "
          f"in {schemas_root}; {len(written)} changed")
    for path in written:
        print(f"  {path}")
    if inputs.unresolved:
        print("no class found in the tree (no schema written): " + ", ".join(inputs.unresolved))
    return 0


def run_check(args: argparse.Namespace) -> int:
    schemagen = _schemagen()
    inputs, schemas_root = _inputs_and_root(args)
    files = schemagen.generate_all(inputs)
    problems = schemagen.check(files, schemas_root)
    if not problems:
        print(f"schemas in sync with source ({len(inputs.extractions)} managers)")
        return 0
    print("Generated schemas differ from the source tree:")
    for problem in problems:
        print(f"  {problem}")
    print(f"Regenerate with: {schemagen.REGENERATE_COMMAND}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", action="store_true", help="print the coverage report")
    mode.add_argument("--write", action="store_true", help="regenerate the checked-in schemas")
    mode.add_argument("--check", action="store_true", help="exit 1 if the checked-in schemas are out of date")
    parser.add_argument("--properties", action="store_true", help="with --report: list every property and its provenance")
    parser.add_argument("--json", action="store_true", help="with --report: print the snapshot as JSON")
    parser.add_argument("--managers", nargs="*", help="with --report: restrict to these manager names")
    parser.add_argument("--package", help="an installed device plugin package: extract the managers its "
                                          "imswitch.json declares, into <package>/schemas/ (never imported)")
    parser.add_argument("--managers-root", type=Path, default=DEFAULT_MANAGERS_ROOT)
    parser.add_argument("--setups-dir", type=Path, default=DEFAULT_SETUPS_DIR)
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS_DIR)
    parser.add_argument("--templates-dir", type=Path, default=DEFAULT_TEMPLATES_DIR)
    parser.add_argument("--schemas-root", type=Path, default=DEFAULT_SCHEMAS_ROOT)
    parser.add_argument("--setup-info", type=Path, default=DEFAULT_SETUP_INFO,
                        help="the SetupInfo.py whose dataclasses give the top-level device keys")
    parser.add_argument("--no-examples", action="store_true", help="do not read shipped setups for kinds")
    parser.add_argument("--no-docs", action="store_true", help="do not read docs/devices cards")
    args = parser.parse_args(argv)
    args.schemas_root_given = any(arg.startswith("--schemas-root") for arg in (argv if argv is not None else sys.argv[1:]))
    if args.report:
        return run_report(args)
    if args.write:
        return run_write(args)
    return run_check(args)


if __name__ == "__main__":
    sys.exit(main())
