"""Command-line interface for device plugin diagnostics."""

import argparse
import sys

from .registry import build_default_registry
from .validation import validate_setup_file, load_jsonschema_validator


def main(argv=None) -> int:
    """Main entry point for the CLI.
    
    Args:
        argv: Command-line arguments (defaults to sys.argv).
    
    Returns:
        Exit code (0 for success, 1 for errors).
    """
    parser = argparse.ArgumentParser(
        prog="python -m imswitch.imcontrol.model.plugins",
        description="Device plugin diagnostics and validation",
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # list command
    list_parser = subparsers.add_parser(
        "list",
        help="List all registered device manager contributions",
    )
    list_parser.add_argument(
        "--kind",
        help="Filter by device kind (e.g., detector, laser)",
    )
    
    # inspect command
    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Inspect a specific contribution by ID",
    )
    inspect_parser.add_argument("id", help="The contribution ID to inspect")
    inspect_parser.add_argument(
        "--kind",
        help="Device kind (optional, searches all kinds if not specified)",
    )
    
    # validate-setup command
    validate_parser = subparsers.add_parser(
        "validate-setup",
        help="Validate a setup JSON file",
    )
    validate_parser.add_argument("path", help="Path to the setup JSON file")
    
    args = parser.parse_args(argv)
    
    if args.command is None:
        parser.print_help()
        return 1
    
    # Build the registry
    registry = build_default_registry(discover=True)
    
    if args.command == "list":
        return cmd_list(registry, kind=args.kind)
    elif args.command == "inspect":
        return cmd_inspect(registry, id=args.id, kind=args.kind)
    elif args.command == "validate-setup":
        return cmd_validate_setup(registry, path=args.path)
    else:
        parser.print_help()
        return 1


def cmd_list(registry, kind=None) -> int:
    """List all contributions, optionally filtered by kind."""
    contributions = registry.list_contributions(kind=kind)
    
    # Sort by (kind, id)
    contributions.sort(key=lambda c: (c.kind, c.id))
    
    for contrib in contributions:
        print(
            f"{contrib.id}  [{contrib.kind}]  {contrib.plugin_name}  — {contrib.display_name}"
        )
    
    return 0


def cmd_inspect(registry, id, kind=None) -> int:
    """Inspect a specific contribution."""
    # Search all kinds if not specified
    if kind is not None:
        contribution = registry.resolve(kind, id)
    else:
        # Search across all kinds
        contribution = None
        all_contribs = registry.list_contributions()
        for c in all_contribs:
            if c.id == id:
                contribution = c
                break
    
    if contribution is None:
        print(f"Contribution '{id}' not found.", file=sys.stderr)
        return 1
    
    # Print all fields
    print(f"ID: {contribution.id}")
    print(f"Kind: {contribution.kind}")
    print(f"Display Name: {contribution.display_name}")
    print(f"Python Name: {contribution.python_name}")
    print(f"Plugin Name: {contribution.plugin_name}")
    print(f"Plugin Version: {contribution.plugin_version or '(not available)'}")
    print(f"Source Package: {contribution.source_package or '(not available)'}")
    print(f"Mock Python Name: {contribution.mock_python_name or '(none)'}")
    
    if contribution.manager_name_aliases:
        print(f"Aliases: {', '.join(contribution.manager_name_aliases)}")
    else:
        print("Aliases: (none)")
    
    print(
        f"Manager Properties Schema: {contribution.manager_properties_schema or '(none)'}"
    )
    
    if contribution.setup_templates:
        print(f"Setup Templates: {', '.join(contribution.setup_templates)}")
    else:
        print("Setup Templates: (none)")
    
    print(f"Docs URL: {contribution.docs_url or '(none)'}")
    
    if contribution.supported_platforms:
        print(f"Supported Platforms: {', '.join(contribution.supported_platforms)}")
    else:
        print("Supported Platforms: (all)")
    
    return 0


def cmd_validate_setup(registry, path) -> int:
    """Validate a setup JSON file."""
    jsonschema = load_jsonschema_validator()
    
    if jsonschema is None:
        print("NOTE: jsonschema not installed, schema validation will be skipped.")
    
    try:
        report = validate_setup_file(path, registry)
    except FileNotFoundError:
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error validating setup file: {e}", file=sys.stderr)
        return 1
    
    print(report.format())
    
    return 1 if report.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
