"""Write examples/improcess_workflows/python_step_<recipe>.yaml from the shipped snippets.

    python tools/make_python_recipe_workflows.py           # write the files
    python tools/make_python_recipe_workflows.py --check   # fail if any is out of date

The Python step recipes ship twice: as snippets, which ImSwitch copies into
~/ImSwitchConfig/improcess_snippets so they show up under *Load snippet...* in
the panel (imswitch/_data/user_defaults/improcess_snippets/*.py), and as
workflow files to run over a folder from the command line. The snippet is the
one to edit; this derives the workflow file from it, and
improcess/_test/test_python_step_recipes.py fails when they disagree.
"""

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SNIPPETS = REPO / "imswitch" / "_data" / "user_defaults" / "improcess_snippets"
EXAMPLES = REPO / "examples" / "improcess_workflows"

#: How many frames of the synthetic recording to try a recipe on. Most take any
#: stack; the two that work on a pair of channels need exactly two planes.
TRY_FRAMES = {"crosstalk": 2, "ratio_mask": 2}
DEFAULT_TRY_FRAMES = 12


def workflow_path(recipe: str) -> Path:
    return EXAMPLES / f"python_step_{recipe}.yaml"


def recipes() -> list[str]:
    return sorted(path.stem for path in SNIPPETS.glob("*.py"))


def workflow_text(recipe: str) -> str:
    """The workflow file for the snippet ``recipe``: its header comment, then the steps."""
    from imswitch.improcess.model.snippets import parse_snippet
    from imswitch.improcess.processors.python_step.context import parse_ports
    from imswitch.improcess.workflows import Process, Reconstruct, Save, Source, Workflow

    text = (SNIPPETS / f"{recipe}.py").read_text(encoding="utf-8")
    code, ports_text = parse_snippet(text)
    ports = parse_ports(ports_text)
    about = []
    for line in code.splitlines():
        if not line.startswith("#"):
            break
        about.append(line[1:].strip())

    slug = recipe.replace("_", "-")
    steps = [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("script", "python", {"ports": ports_text, "code": code}, inputs=["rec"]),
    ]
    for port in ports:
        suffix = recipe if len(ports) == 1 else f"{recipe}_{port}"
        steps.append(Save(
            f"save_{port}", input=f"script.{port}", fmt="tiff",
            path_template=f"{{out_dir}}/{{source_stem}}_{suffix}{{ext}}",
        ))
    workflow = Workflow(f"python-{slug}", steps, description=about[0] if about else "")

    frames = TRY_FRAMES.get(recipe, DEFAULT_TRY_FRAMES)
    header = [f"# {line}".rstrip() for line in about]
    header += [
        "#",
        f"# Run it on a synthetic recording ({frames} frames):",
        "#   python -c \"import sys; sys.path.insert(0, 'examples/improcess_workflows'); \\",
        f"#     from _synthetic import write_synthetic_recording as w; w('cells.h5', frames={frames})\"",
        f"#   python -m imswitch.improcess.workflows run python_step_{recipe}.yaml \\",
        "#       --input cells.h5 --out results/",
        "# or paste the code of the step into Tools -> Python step (it is also in",
        f"# Load snippet... as '{recipe}').",
        "#",
        f"# Derived from imswitch/_data/user_defaults/improcess_snippets/{recipe}.py by",
        "# tools/make_python_recipe_workflows.py: edit the snippet, then run the tool.",
    ]
    return "\n".join(header) + "\n" + workflow.to_yaml()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="report files that are out of date instead of writing them")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(REPO))
    stale = []
    for recipe in recipes():
        path = workflow_path(recipe)
        text = workflow_text(recipe)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(path.name)
        if not args.check:
            path.write_text(text, encoding="utf-8")
    if args.check and stale:
        print("out of date (run tools/make_python_recipe_workflows.py):", ", ".join(stale))
        return 1
    if stale:
        print("wrote:", ", ".join(stale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
