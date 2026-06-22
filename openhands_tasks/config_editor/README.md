# Config-editor improvement tasks (OpenHands)

Two delegated tasks improving `utility_scripts/imswitch_config_editor.py`.
Both edit the same file — run them **sequentially on one checkout** (this repo's
agent workflow shares a single checkout; never run them in parallel).

## Run order

1. `TASK_A_device_coverage.md` — every device category always present; managers
   auto-discovered from `imswitch/imcontrol/model/managers/<dir>/*Manager.py`;
   generic per-category blank scaffolds so a manager with no template can still
   be added.
2. `TASK_B_widget_section_helper.md` — enabling a widget that needs a system
   section auto-offers the existing `SectionEditorDialog`; validation warnings
   become clickable "Configure…" actions. Rebase onto Task A first.

## Invocation

```
openhands -f openhands_tasks/config_editor/TASK_A_device_coverage.md --headless
# audit / review Task A's diff, then:
openhands -f openhands_tasks/config_editor/TASK_B_widget_section_helper.md --headless
```

## Audit checklist (for the human review between/after runs)

- Editor still imports and launches with **no** imswitch tree present (standalone)
  and with it present (auto-discovery active).
- New unit tests pass:
  `python -m pytest imswitch/imcontrol/_test/unit/test_config_editor_*.py -q`.
- Saved-JSON shape unchanged for existing devices and sections.
- No `import imswitch`, no new third-party deps.
- Task B reuses `SectionEditorDialog` (no parallel editor) and the
  `requires_widget`-derived map (no stale hardcoded duplicate).
