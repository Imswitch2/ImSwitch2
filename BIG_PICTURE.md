# Working on ImSwitch2: the big picture

For anyone, human or agent, who starts work on this code base. It is meant to
stay true for years, so it names ideas, not files. The commands, checklists
and file maps live in `AGENTS.md`; this page is what to keep in your head
while you use them.

---

## 1. The shape of the system

ImSwitch2 is one program that becomes a different microscope depending on a
single description, the **setup file**. Everything follows from it:

- The setup file names the devices. Each device is driven by a **manager**.
- **Controllers** combine managers into behaviour; **widgets** are their
  panels. Which panels exist is also decided by the setup.
- Panels do not call each other. They talk through **signals** and shared
  state on a common channel, and the scripting API is the same set of calls.
- Acquisition is a **lifecycle** (prepare, built, started, done, stopped)
  that several parties follow at once: the scanner, the detectors, the
  recorder, the viewer.
- Processing is a separate module built from **plugins** with declared
  parameters, chained into **workflows**.
- A shared layer holds what both sides need. Control and processing do not
  import each other.

If you can say where your task sits in that picture, you know where to look.
If you cannot, find out before writing code.

## 2. How to orient yourself

1. **Start from the setup file** of the microscope the task is about. It tells
   you which managers, controllers and panels are involved.
2. **Find the nearest existing sibling** of what you are asked to build, and
   copy its shape. The code base is consistent; a new thing that looks unlike
   its siblings is usually wrong.
3. **Read the tests of that area before the code.** Their names are sentences
   and state the contract, including the failures the code was written to
   survive.
4. **Follow one signal end to end** when behaviour crosses panels. Most
   "it does nothing" bugs are a name or a moment that two sides disagree on.

## 3. Verify in proportion

The aim is confidence per minute, not the largest number of passed tests.

- **A local change** (one plugin, one panel, one manager): run the tests of
  that area and the few contract checks your change can affect. Stop there.
  The full suite is the integration server's job.
- **A change that crosses areas** (a shared signal, a base class, the setup
  format, a merge of two branches): run the suites of every area it touches.
  This is where a narrow run hides real breakage.
- **Know why you run each test.** If you cannot say what a run could tell you,
  do not run it.
- **A red test you did not cause still needs an answer**: show that it is red
  without your change, then leave it alone and say so.
- **Never test against the real configuration or real hardware by accident.**
  Tests get a throwaway home; hardware gets a simulation.

## 4. What is special about this code base

- **It moves real hardware.** A wrong sign or unit can drive an objective into
  a sample. Every commanded move is bounded, a refusal leaves the device where
  it is and says why, and nothing moves at startup. When in doubt, hold and
  report.
- **Safety guards must have a way back.** "Never report idle while a task is
  open" is right; having no path to recover after one failed close is a bug.
  When you add or meet a guard, ask how the system leaves the guarded state.
- **Units and coordinate order are a recurring trap.** Pixels and micrometres,
  (row, column) and (x, y), absolute and relative positions all coexist. State
  the frame in the interface and pin it with a test.
- **Some files are generated from others**, including from documentation.
  A change in one place can require a regeneration elsewhere; the contract
  checks tell you which.
- **The same lifecycle is observed by many parties.** A fix that is correct
  for the scanner can strand the recorder or the viewer. Think of every
  listener when you change when something happens.
- **Nothing an agent does here has touched a microscope.** Say "simulated
  only" until someone has tried it on a rig, and hand over a short list of
  what to check there, safest step first.

## 5. How work reaches users

- Features start from the release line and return to it through their own
  pull request. The integration branch exists to test features together and
  is never merged back; work that is built on it has no way home.
- A fix belongs on the branch that owns the code it changes.
- Keep the number of branches small: fold into an existing change when the
  code belongs there, stack only on a real dependency.
- Whoever writes a change does not also approve it. Delegated work is read
  before it is published.

## 6. Working with the people

- **Ask one question before a long task** when a short request could mean two
  things. The lab's words for features are not always the code's words.
- **Ask for the exact message** when a failure is reported from memory, and
  make the software's own messages name what went wrong and who holds what.
- **A behaviour change is the maintainer's decision.** Build it, then present
  it with the alternative.
- **Report plainly**: what was done, what was verified and how, what was not.
  Numbers, not adjectives.

## 7. Keeping this useful

- This page should change rarely. If a lesson needs a file name or a command,
  it belongs in `AGENTS.md`.
- The guides must be on the release line, where every branch inherits them.
- What can be enforced by a test should be, so the guides can stay short.
