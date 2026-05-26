# How-to guides

Task-oriented recipes for ImSwitch.  Each guide answers a single
question (*"how do I X?"*) with concrete steps you can copy/paste.

For deeper "how is this built" explanations, see
[../design/](../design/).  These guides are also rendered as part of
the official Sphinx documentation site (see `../index.rst`).

## Contents

| Guide | Audience |
|---|---|
| [wire-teensy.rst](wire-teensy.rst) | Adding a Teensy / Arduino pulse generator to an existing setup. End-to-end, hardware-free testable. |
| [add-pulse-generator-backend.rst](add-pulse-generator-backend.rst) | Writing a new `PulseGeneratorManager` backend for a different timing device (NI, FPGA, …). |
| [port-from-third-party.rst](port-from-third-party.rst) | Wrapping a driver from a sibling project as an ImSwitch manager. Patterns and pitfalls from the WidefieldStarss integration. |

For the canonical "add a device manager" reference, see
[`../adding-device-support.rst`](../adding-device-support.rst) — it
covers the abstract bases, low-level managers, and autodoc-generated
API for all the relevant base classes.

## When to add a guide here

- The recipe answers a single, well-bounded question.
- A user (operator or contributor) might Google for the title.
- It has copy/pasteable steps, not just principles.

For principles, decision rationale, or "how the system works
internally" content, see `../design/` instead.
