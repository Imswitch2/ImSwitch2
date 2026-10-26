"""What happened to an image in Fiji, rebuilt from what ImageJ already exposes.

This is best-effort provenance, not a replay script. It joins three sources the
host records (:class:`~imswitch.improcess.fiji.host.EventLog`), each of which
sees part of the truth:

* the **CommandListener** event: the command's name, the image that was current
  when it started, its start (``time``), its end (``end``, when known) and the
  length of the macro recorder's text at that moment (``recorder_offset``);
* the **ImageListener** events: ``opened`` for a new image, ``updated`` (with the
  ``changes`` flag) for a pixel or display change;
* the **macro recorder** text: the only place where the parameters of a menu
  command appear. It names images by title, never by id, and never records a
  scripted ``IJ.run`` call; those carry their options in ``Macro.getOptions``,
  which the command event keeps as ``options``.

This module is pure Python: it imports nothing from Qt, ImageJ or ImProcess's
model, and it reads plain dicts, so it is tested against the raw event logs.
Logs written before the schema grew ``seq``, ``end``, ``recorder_offset``,
``plugin_*`` and ``changes`` are accepted (the P0 logs use ``t`` for ``time``);
the rules below say what each missing field costs.

Rules
-----

1. **Order.** Events are sorted by time; ties and missing times keep list order.
2. **Windows.** A command owns the events after it up to its ``end`` (when
   known) or up to the next command's start. An event inside several windows
   (a command that runs other commands, such as a macro) belongs to the
   latest-starting one, and the outer command is marked partial.
3. **Output.** The first ``opened`` event a command owns, for an id that had not
   been opened before (images that were sent count as opened) and is not the
   command's own input, is its output: a new image derived from the input. With
   none, the command acted in place: ``in_place`` and output = input. A command
   in :data:`ORIGINATING_COMMANDS_V1` (File > Open and friends) creates an image
   from no input at all, whatever image happened to be current.
4. **Options.** ``options`` of the command event (a scripted call) win:
   ``options_source`` is ``"macro"``. Otherwise the recorder is read. With
   offsets, the slice of text between this command's offset and the next one's
   is searched for ``run("<command>", "<options>");``. Without offsets (old
   logs) the recorder's run lines are matched to the menu-driven commands in
   order, by name. The options are verbatim, so ImageJ's own trailing blank and
   the lone blank of ``run("Duplicate...", " ")`` are kept. A step is
   ``"recorded"`` when its options are known (an empty string counts) and
   ``"partial"`` when they are not.
5. **Context.** The recorder lines that are no command's own run line and that
   fall between the previous command's run line and this one's (makeRectangle,
   selectImage, setSlice ...) are the step's ``context``. A selection made
   before a Crop shows up here, since ImageJ does not record it anywhere else.
6. **Chain.** From the image, the in-place steps on it and the step that
   created it; then the same from that step's input, taking only what happened
   to the input before the creating step. The walk stops at a sent image
   (``origin="sent"``) or at an image no command created (``origin="unknown"``:
   opened from a file, or made by something that bypasses commands). The id
   walk is guarded against cycles and capped by the number of events, which is
   the longest chain the log can hold.
7. **Unrecorded changes**, looked for on every image of the chain at the moments
   that matter to the image: an ``updated`` event outside every command window
   and after the image's own ``opened``.

   * ``"detected"``: that event shows ``changes`` True, no recorded command
     modified the image in place before, and the flag was last seen False (or
     never seen on an image no command made). The flag turned True without a
     command.
   * ``"possible"``: any other such event. ImageJ1 also fires ``updated`` for
     display-only actions (a slice change, Enhance Contrast) with the flag left
     False, so this may be a display refresh and not an edit.

   Old logs have no flag, so only ``"possible"`` can be reported.
8. **Completeness.** ``"unknown"`` when the origin is unknown or a change is
   detected; else ``"partial"`` when a step is partial or a change is possible;
   else ``"recorded"``.

Grid policy
-----------

:func:`grid_preserved` answers one question for the importer: did every step
provably leave every pixel where it was. It is a NECESSARY condition; the import
layer's own grid decision still checks shape, scale, units and axes on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Collection, Iterable

#: Bumped whenever the list below, the Duplicate rule or the chain rules change
#: in a way that can flip a grid decision; stored beside every decision.
GRID_POLICY_VERSION = "fiji-grid-v1"

#: Layout of ``FijiHistory.to_details()``.
HISTORY_SCHEMA = "fiji-history-v1"

_GRID_GROUPS_V1: dict[str, tuple[str, tuple[str, ...]]] = {
    "filters": (
        "Neighbourhood filters write each output pixel from the input neighbourhood of the same pixel: "
        "same dimensions, same calibration, no resampling.",
        ("Gaussian Blur...", "Median...", "Mean...", "Minimum...", "Maximum...", "Variance...",
         "Unsharp Mask...", "Smooth", "Sharpen", "Find Edges", "Subtract Background..."),
    ),
    "point operations": (
        "A value map applied to every pixel independently; the geometry is not touched.",
        ("Invert", "Add...", "Subtract...", "Multiply...", "Divide...", "Min...", "Max...", "Gamma...",
         "Log", "Exp", "Square", "Square Root", "Abs", "Reciprocal", "Set..."),
    ),
    "type conversion": (
        "Changes the pixel type only; width, height, stack size and calibration stay.",
        ("8-bit", "16-bit", "32-bit"),
    ),
    "binary": (
        "Binary operations work in place on the pixel grid of the mask they are given.",
        ("Convert to Mask", "Make Binary", "Erode", "Dilate", "Open", "Close-", "Fill Holes",
         "Skeletonize", "Outline"),
    ),
}

#: ImageJ1 commands that, by construction, keep every pixel where it was.
GRID_PRESERVING_COMMANDS_V1: frozenset[str] = frozenset(
    command for _rationale, commands in _GRID_GROUPS_V1.values() for command in commands
)

#: "Duplicate..." is grid-preserving only without a selection or a sub-range:
#: a duplicate of a selection is a crop. The rule needs the step's context.
DUPLICATE_COMMAND = "Duplicate..."

#: Commands that create an image from no input image (File > Open and friends).
#: ImageJ reports whatever image was current as their input, which would link
#: an opened file to an unrelated image. A best-effort list, versioned with the
#: grid policy because it decides what an image's origin is.
ORIGINATING_COMMANDS_V1: frozenset[str] = frozenset({
    "Open...", "Open Next", "Image Sequence...", "Raw...", "Text Image...", "URL...",
    "Bio-Formats", "Bio-Formats Importer", "Image...",
})

_SELECTION_LINE = re.compile(r"^\s*(make\w*|setSelection\w*|doWand)\s*\(")
#: Selection-making menu commands that appear as ``run("Select ...")``; the
#: only "Select ..." that does not select anything is "Select None".
_RUN_SELECTION_PREFIX = "Select "
_RUN_SELECTION_EXTRA = frozenset({"Restore Selection"})
_RUN_SELECTION_NONE = "Select None"
_DUPLICATE_RANGE_OPTION = "range="

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


# --------------------------------------------------------------------------
# the recorder's run(...) lines
# --------------------------------------------------------------------------

def _read_literal(text: str, pos: int) -> tuple[str, int] | None:
    """The macro string literal starting at ``text[pos]`` and the index after it, or ``None``."""
    if pos >= len(text) or text[pos] not in "\"'":
        return None
    quote, out, i = text[pos], [], pos + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(_ESCAPES.get(text[i + 1], "\\" + text[i + 1]))
            i += 2
        elif ch == quote:
            return "".join(out), i + 1
        else:
            out.append(ch)
            i += 1
    return None


def _skip_blank(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def parse_run_line(line: str) -> tuple[str, str] | None:
    """``(command, options)`` of a recorded ``run("a", "b");`` line, or ``None`` if it is not one.

    ``run("a");`` has options ``""``. Escaped quotes and backslashes are
    undone. Any other shape (a trailing expression, a missing parenthesis) is
    refused rather than guessed at.
    """
    text = line.strip()
    if not text.startswith("run("):
        return None
    first = _read_literal(text, _skip_blank(text, 4))
    if first is None:
        return None
    command, pos = first
    pos = _skip_blank(text, pos)
    options = ""
    if pos < len(text) and text[pos] == ",":
        second = _read_literal(text, _skip_blank(text, pos + 1))
        if second is None:
            return None
        options, pos = second
        pos = _skip_blank(text, pos)
    if pos >= len(text) or text[pos] != ")":
        return None
    if text[pos + 1:].strip() not in ("", ";"):
        return None
    return command, options


def _makes_selection(line: str) -> bool:
    if _SELECTION_LINE.match(line):
        return True
    parsed = parse_run_line(line)
    if parsed is None:
        return False
    name = parsed[0]
    return (name.startswith(_RUN_SELECTION_PREFIX) and name != _RUN_SELECTION_NONE) or name in _RUN_SELECTION_EXTRA


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FijiStep:
    """One Fiji command applied to an image."""

    command: str
    input_id: int | None
    output_id: int | None
    options: str | None
    options_source: str | None      # "macro" | "recorder" | None
    context: tuple[str, ...]
    plugin_class: str | None
    plugin_jar: str | None
    start: float | None
    end: float | None
    completeness: str               # "recorded" | "partial"
    in_place: bool

    def to_dict(self) -> dict:
        return {
            "command": self.command, "input_id": self.input_id, "output_id": self.output_id,
            "options": self.options, "options_source": self.options_source, "context": list(self.context),
            "plugin_class": self.plugin_class, "plugin_jar": self.plugin_jar,
            "start": self.start, "end": self.end, "completeness": self.completeness, "in_place": self.in_place,
        }


@dataclass(frozen=True)
class FijiHistory:
    """The steps that led to ``image_id``, oldest first, and how far to trust them."""

    image_id: int
    origin_id: int | None
    origin: str                     # "sent" | "unknown"
    steps: tuple[FijiStep, ...]
    unrecorded: str                 # "none observed" | "possible" | "detected"
    unrecorded_notes: tuple[str, ...]
    completeness: str               # "recorded" | "partial" | "unknown"

    def to_details(self) -> dict:
        """Plain JSON, for the provenance node's ``details``."""
        return {
            "schema": HISTORY_SCHEMA, "image_id": self.image_id, "origin": self.origin, "origin_id": self.origin_id,
            "completeness": self.completeness, "unrecorded": self.unrecorded,
            "unrecorded_notes": list(self.unrecorded_notes), "steps": [s.to_dict() for s in self.steps],
        }

    def summary(self) -> str:
        """One line per step, for a tooltip or a log."""
        origin = (f"sent image {self.origin_id}" if self.origin == "sent"
                  else "an image whose origin Fiji did not show")
        lines = [f"Fiji history of image {self.image_id} ({self.completeness}): from {origin}"]
        for number, step in enumerate(self.steps, 1):
            options = "?" if step.options is None else step.options.strip()
            where = "in place" if step.in_place else f"-> {step.output_id}"
            lines.append(f"{number}. {step.command} [{options}] on {step.input_id} {where} ({step.completeness})")
        lines.extend(f"! {note}" for note in self.unrecorded_notes)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# reading the events
# --------------------------------------------------------------------------

def _time(event: dict) -> float | None:
    value = event.get("time", event.get("t"))
    return float(value) if value is not None else None


@dataclass
class _Command:
    pos: int
    event: dict
    start: float | None
    end: float | None
    input_id: int | None
    options: str | None = None
    options_source: str | None = None
    context: tuple[str, ...] = ()
    output_id: int | None = None
    nested: bool = False

    @property
    def name(self) -> str:
        return str(self.event.get("command", ""))

    @property
    def in_place(self) -> bool:
        return self.output_id is None


def _ordered(events: Iterable[dict]) -> list[dict]:
    """Events by time; an event without a time (or a tie) keeps its place."""
    listed = list(events)
    keyed, last = [], 0.0
    for index, event in enumerate(listed):
        stamp = _time(event)
        last = stamp if stamp is not None else last
        keyed.append((last, index, event))
    keyed.sort(key=lambda item: (item[0], item[1]))
    return [event for _, _, event in keyed]


def _owners(events: list[dict], commands: list[_Command]) -> list[_Command | None]:
    """For every event position, the command whose window holds it (the latest-starting one)."""
    owner: list[_Command | None] = [None] * len(events)
    for index, cmd in enumerate(commands):
        stop = commands[index + 1].pos if index + 1 < len(commands) else len(events)
        for pos in range(cmd.pos + 1, len(events)):
            stamp = _time(events[pos])
            if cmd.end is not None:
                if stamp is not None and stamp > cmd.end:
                    break                   # events are sorted by time
            elif pos >= stop:
                break
            owner[pos] = cmd
    return owner


def _recorder_options(commands: list[_Command], recorder: str | None) -> None:
    """Fill options, options_source and context of the menu-driven commands from the recorder text."""
    menu = [c for c in commands if c.event.get("options") is None]
    for cmd in commands:
        if cmd.event.get("options") is not None:
            cmd.options, cmd.options_source = str(cmd.event["options"]), "macro"
    if recorder is None:
        return
    offsets = [c.event.get("recorder_offset") for c in commands]
    if commands and all(isinstance(o, int) for o in offsets):
        _from_slices(commands, recorder, [int(o) for o in offsets])
    else:
        _from_sequence(menu, recorder)


def _from_slices(commands: list[_Command], recorder: str, offsets: list[int]) -> None:
    bounds = [max(min(o, len(recorder)), 0) for o in offsets]
    for i in range(1, len(bounds)):
        bounds[i] = max(bounds[i], bounds[i - 1])
    edges = [*bounds, len(recorder)]
    carried = tuple(recorder[: edges[0]].splitlines())      # recorded before the first command: its context
    for i, cmd in enumerate(commands):
        lines = recorder[edges[i]: edges[i + 1]].splitlines()
        match = None
        if cmd.event.get("options") is None:
            for index, line in enumerate(lines):
                parsed = parse_run_line(line)
                if parsed is not None and parsed[0] == cmd.name:
                    match = index
                    cmd.options, cmd.options_source = parsed[1], "recorder"
                    break
        if match is None:
            # No run line of its own (a scripted call, which the recorder never
            # sees): everything here was recorded after it started.
            cmd.context = carried
            carried = tuple(lines)
        else:
            cmd.context = carried + tuple(lines[:match])
            carried = tuple(lines[match + 1:])


def _from_sequence(menu: list[_Command], recorder: str) -> None:
    lines = recorder.splitlines()
    runs = [parse_run_line(line) for line in lines]
    pointer, previous = 0, -1
    for cmd in menu:
        found = next((k for k in range(pointer, len(runs)) if runs[k] is not None and runs[k][0] == cmd.name), None)
        if found is None:
            continue            # no line to take the options from; the context stays empty too
        cmd.options, cmd.options_source = runs[found][1], "recorder"
        cmd.context = tuple(lines[previous + 1: found])
        pointer, previous = found + 1, found


def _commands(events: list[dict], recorder: str | None, sent: set[int]) -> tuple[list[_Command], list[_Command | None]]:
    commands = [
        _Command(pos, event, _time(event), event.get("end"), event.get("current_id"))
        for pos, event in enumerate(events) if event.get("kind") == "command"
    ]
    owner = _owners(events, commands)
    opened: set[int] = set(sent)
    for pos, event in enumerate(events):
        if event.get("kind") != "opened":
            continue
        image, cmd = event.get("id"), owner[pos]
        fresh = image not in opened
        opened.add(image)
        if cmd is None or not fresh or cmd.output_id is not None or image == cmd.input_id:
            continue
        cmd.output_id = image
    for cmd in commands:
        if cmd.name in ORIGINATING_COMMANDS_V1:
            cmd.input_id = None
        if cmd.end is not None:
            cmd.nested = any(
                other.pos > cmd.pos and other.start is not None and other.start < cmd.end for other in commands
            )
    _recorder_options(commands, recorder)
    return commands, owner


# --------------------------------------------------------------------------
# the chain
# --------------------------------------------------------------------------

def _step(cmd: _Command) -> FijiStep:
    known = cmd.options is not None and not cmd.nested
    event = cmd.event
    return FijiStep(
        command=cmd.name, input_id=cmd.input_id,
        output_id=cmd.input_id if cmd.in_place else cmd.output_id,
        options=cmd.options, options_source=cmd.options_source, context=cmd.context,
        plugin_class=event.get("plugin_class"), plugin_jar=event.get("plugin_jar"),
        start=cmd.start, end=cmd.end, completeness="recorded" if known else "partial", in_place=cmd.in_place,
    )


def history_for(image_id: int, events: list[dict], recorder: str | None, *, sent_ids: Collection[int]) -> FijiHistory:
    """Everything Fiji did to ``image_id`` since it was sent, as far as ImageJ showed it."""
    sent = {int(i) for i in sent_ids}
    ordered = _ordered(events)
    commands, owner = _commands(ordered, recorder, sent)
    creators: dict[int, _Command] = {}
    in_place: dict[int, list[_Command]] = {}
    for cmd in commands:
        if cmd.output_id is not None:
            creators.setdefault(cmd.output_id, cmd)
        elif cmd.input_id is not None:
            in_place.setdefault(cmd.input_id, []).append(cmd)

    segments: list[list[_Command]] = []
    chain: list[tuple[int, int]] = []           # (image id, only events before this position count)
    notes: list[str] = []
    visited: set[int] = set()
    cursor, cutoff = int(image_id), len(ordered)
    origin, origin_id = "unknown", None
    for _ in range(len(ordered) + 1):
        if cursor in visited:
            notes.append(f"the history of image {image_id} loops back to image {cursor}; it was cut there")
            origin_id = cursor
            break
        visited.add(cursor)
        chain.append((cursor, cutoff))
        creator = creators.get(cursor)
        if creator is not None and creator.pos >= cutoff:
            creator = None
        floor = creator.pos if creator is not None else -1
        modified = [c for c in in_place.get(cursor, ()) if floor < c.pos < cutoff]
        segments.append(([creator] if creator is not None else []) + modified)
        if creator is None:
            origin_id = cursor
            if cursor in sent:
                origin = "sent"
            break
        if creator.input_id is None:
            origin_id = None
            break
        cursor, cutoff = creator.input_id, creator.pos
    else:
        notes.append("the walk through the log did not end; the origin is not known")
    steps = tuple(_step(cmd) for segment in reversed(segments) for cmd in segment)

    unrecorded, change_notes = _unrecorded(ordered, owner, chain, creators, in_place)
    notes.extend(change_notes)
    if origin == "unknown":
        completeness = "unknown"
    elif unrecorded == "detected":
        completeness = "unknown"
    elif unrecorded == "possible" or any(s.completeness == "partial" for s in steps):
        completeness = "partial"
    else:
        completeness = "recorded"
    return FijiHistory(
        image_id=int(image_id), origin_id=origin_id, origin=origin, steps=steps,
        unrecorded=unrecorded, unrecorded_notes=tuple(notes), completeness=completeness,
    )


_POSSIBLE_HINT = (
    "Fiji also reports display-only actions (a slice change, Enhance Contrast) as an update with the changed flag unset, "
    "so this may be a display refresh and not an edit"
)


def _unrecorded(events, owner, chain, creators, in_place) -> tuple[str, list[str]]:
    detected: list[str] = []
    possible: list[str] = []
    for image, cutoff in chain:
        # Searched in the whole log: ImageJ delivers "opened" on the AWT thread
        # and it can land after a command that started right behind it.
        opened_at = next((p for p, e in enumerate(events) if e.get("kind") == "opened" and e.get("id") == image), None)
        made_at = creators[image].pos if image in creators else None
        modified_at = [c.pos for c in in_place.get(image, ())]
        previous = None
        for pos in range(cutoff):
            event = events[pos]
            if event.get("id") != image or event.get("kind") not in ("opened", "updated"):
                continue
            flag = event.get("changes")
            outside = (
                event["kind"] == "updated" and owner[pos] is None
                and (opened_at is None or pos > opened_at)
            )
            if outside:
                when = _time(event)
                label = f"image {image} was updated at {when:g} s outside any recorded command" if when is not None \
                    else f"image {image} was updated outside any recorded command"
                touched = any(p < pos for p in modified_at)
                if (flag is True and not touched
                        and (previous is False or (previous is None and made_at is None))):
                    detected.append(f"{label}, and its changed flag turned on")
                else:
                    possible.append(label)
            if flag is not None:
                previous = bool(flag)
    if detected:
        return "detected", [*detected, *possible]
    if possible:
        return "possible", [*possible, _POSSIBLE_HINT]
    return "none observed", []


# --------------------------------------------------------------------------
# the grid decision
# --------------------------------------------------------------------------

def _step_keeps_grid(step: FijiStep) -> tuple[bool, str]:
    if step.command == DUPLICATE_COMMAND:
        selection = next((line for line in step.context if _makes_selection(line)), None)
        if selection is not None:
            return False, f"Duplicate of a selection is a crop ({selection.strip()})"
        if _DUPLICATE_RANGE_OPTION in (step.options or ""):
            return False, "Duplicate with a range keeps only part of the stack"
        return True, ""
    if step.command in GRID_PRESERVING_COMMANDS_V1:
        return True, ""
    return False, f"{step.command} is not known to keep every pixel where it was ({GRID_POLICY_VERSION})"


def grid_preserved(history: FijiHistory) -> tuple[bool, str]:
    """``(True, "")`` only when every step provably kept the pixel grid, else ``(False, first reason)``.

    A NECESSARY condition: the import layer's grid decision still checks shape,
    scale, units and axes on top of it.
    """
    if history.origin != "sent":
        return False, "the image's origin in ImProcess is not known"
    if history.unrecorded != "none observed":
        return False, f"the image may have been changed outside a recorded command ({history.unrecorded})"
    for step in history.steps:
        if step.completeness != "recorded":
            return False, f"the parameters of {step.command} were not recorded"
        keeps, reason = _step_keeps_grid(step)
        if not keeps:
            return False, reason
    return True, ""


__all__ = [
    "DUPLICATE_COMMAND", "GRID_POLICY_VERSION", "GRID_PRESERVING_COMMANDS_V1", "HISTORY_SCHEMA",
    "ORIGINATING_COMMANDS_V1", "FijiHistory", "FijiStep", "grid_preserved", "history_for", "parse_run_line",
]
