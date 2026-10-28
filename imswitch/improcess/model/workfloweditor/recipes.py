"""Python-step snippets as workflows: Source → View only → Python step → Save. Qt-free.

A snippet is a few lines of code with the output ports it sets (``# ports:``)
and, for outputs that are curves, a ``# curves:`` line; its leading comment
says what it does. The workflow made from it reads a recording, shows it as
it is (*View only*), runs the code and saves every output -- a curve as CSV,
anything else as TIFF. The editor's start page offers every installed
snippet this way, and ``tools/make_python_recipe_workflows.py`` writes the
shipped examples with the same function, so they cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from imswitch.improcess.workflows.steps import Process, Reconstruct, Save, Source, Workflow


@dataclass(frozen=True)
class Recipe:
    name: str
    about: tuple[str, ...]
    code: str
    ports: str

    @property
    def title(self) -> str:
        return self.name.replace("_", " ")

    @property
    def summary(self) -> str:
        return self.about[0] if self.about else ""


def snippet_about(code: str) -> list[str]:
    """The snippet's leading comment, as prose (the ``curves:`` line is not prose)."""
    about = []
    for line in str(code).splitlines():
        if not line.startswith("#"):
            break
        if line[1:].strip().lower().startswith("curves:"):
            continue
        about.append(line[1:].strip())
    return about


def recipe_workflow(name: str, code: str, ports_text: str) -> Workflow:
    """The workflow for the snippet ``name``. Raises ``ValueError`` when its
    ``# curves:`` line names a port the snippet does not set."""
    from imswitch.improcess.model.snippets import snippet_curves
    from imswitch.improcess.processors.python_step.context import parse_ports

    ports = parse_ports(ports_text)
    curves = snippet_curves(code)
    unknown = [port for port in curves if port not in ports]
    if unknown:
        raise ValueError(f"{name}: '# curves:' names {unknown}, which the snippet does not declare ({', '.join(ports)})")
    about = snippet_about(code)
    steps = [
        Source("raw"),
        Reconstruct("rec", "view-only", inputs=["raw"]),
        Process("script", "python", {"ports": ports_text, "code": code}, inputs=["rec"]),
    ]
    for port in ports:
        suffix = name if len(ports) == 1 else f"{name}_{port}"
        steps.append(Save(
            f"save_{port}", input=f"script.{port}", fmt="csv" if port in curves else "tiff",
            path_template=f"{{out_dir}}/{{source_stem}}_{suffix}{{ext}}",
        ))
    return Workflow(f"python-{name.replace('_', '-')}", steps, description=about[0] if about else "")


def installed_recipes() -> list[Recipe]:
    """Every snippet in the user's snippet folder (where ImSwitch copies the
    shipped ones), in name order; one that cannot be read is left out."""
    from imswitch.improcess.model import snippets

    found = []
    for name in snippets.list_snippets():
        try:
            code, ports = snippets.load_snippet(name)
        except (OSError, ValueError):
            continue
        found.append(Recipe(name, tuple(snippet_about(code)), code, ports))
    return found


def recipe_document(recipe: Recipe, catalog):
    """A new editor document for ``recipe``."""
    from imswitch.improcess.model.workfloweditor.document import WorkflowDocument

    return WorkflowDocument(recipe_workflow(recipe.name, recipe.code, recipe.ports), catalog=catalog)


__all__ = ["Recipe", "installed_recipes", "recipe_document", "recipe_workflow", "snippet_about"]
