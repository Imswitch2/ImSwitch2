"""Report (and, from Phase 1, generate) manager property schemas from source.

Every manager reads its ``managerProperties`` somewhere in its source. This
tool lifts that out statically -- no manager is imported -- and reports what
was found: which keys, which are required, what kind of value the editor
should offer, and where the code proves a constraint validation may enforce.

Usage
-----

Print the coverage report over the live tree::

    python tools/extract_manager_schemas.py --report

Add every property with its provenance::

    python tools/extract_manager_schemas.py --report --properties

Emit the report's snapshot as JSON (what the drift test pins)::

    python tools/extract_manager_schemas.py --report --json

The rules live in ``imswitch.imcontrol.model.configeditor.extraction`` and are
described in ``docs/design/plans/config-editor-schema-extraction.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from imswitch.imcontrol.model.configeditor import extraction  # noqa: E402

DEFAULT_MANAGERS_ROOT = _REPO_ROOT / "imswitch" / "imcontrol" / "model" / "managers"
DEFAULT_SETUPS_DIR = _REPO_ROOT / "imswitch" / "_data" / "user_defaults" / "imcontrol_setups"
DEFAULT_DOCS_DIR = _REPO_ROOT / "docs" / "devices"


def selectable_manager_names() -> list[str]:
    """The managers the editor offers: the catalog, registry plus legacy scan."""
    from imswitch.imcontrol.model.configeditor.catalog import build_catalog

    return sorted(info.manager_name for info in build_catalog().managers())


def run_report(args: argparse.Namespace) -> int:
    names = args.managers or selectable_manager_names()
    extractions = extraction.extract_managers(
        names,
        managers_root=args.managers_root,
        setups_dir=None if args.no_examples else args.setups_dir,
        docs_dir=None if args.no_docs else args.docs_dir,
    )
    docs = None if args.no_docs else extraction.docs_cards(args.docs_dir)
    report = extraction.coverage_report(extractions, docs=docs)
    if args.json:
        print(json.dumps(report.snapshot(), indent=2, sort_keys=True))
    else:
        print(extraction.format_report(report, extractions if args.properties else None))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="print the coverage report")
    parser.add_argument("--properties", action="store_true", help="with --report: list every property and its provenance")
    parser.add_argument("--json", action="store_true", help="with --report: print the snapshot as JSON")
    parser.add_argument("--managers", nargs="*", help="restrict to these manager names (default: the editor catalog)")
    parser.add_argument("--managers-root", type=Path, default=DEFAULT_MANAGERS_ROOT)
    parser.add_argument("--setups-dir", type=Path, default=DEFAULT_SETUPS_DIR)
    parser.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS_DIR)
    parser.add_argument("--no-examples", action="store_true", help="do not read shipped setups for kinds")
    parser.add_argument("--no-docs", action="store_true", help="do not read docs/devices cards")
    args = parser.parse_args(argv)
    if not args.report:
        parser.error("nothing to do: pass --report (schema writing arrives with Phase 1)")
    return run_report(args)


if __name__ == "__main__":
    sys.exit(main())
