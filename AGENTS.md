# AGENTS.md — AI Agent Coding Rules for ImSwitch2

This document defines the rules, constraints, and boundaries for all AI agents working on ImSwitch2. Every agent (OpenHands, Claude, Microsoft Agent Framework agents, or any future agent) MUST follow these rules.

## Core Principles

1. **Human-in-the-loop is mandatory.** No agent may merge code, push to main, or deploy without human review and approval.
2. **Safety first.** ImSwitch2 controls real microscope hardware. Incorrect changes can damage equipment or create unsafe conditions.
3. **Bounded tasks only.** Agents work on narrowly scoped, well-defined tasks — never open-ended refactors.

## Coding Rules

### Mandatory for All Changes

- **No API-breaking changes** without explicit maintainer approval.
- **No hardware timing modifications** unless explicitly requested and approved by someone with physical hardware access.
- **All changes require tests.** No PR will be accepted without corresponding test coverage.
- **No direct hardware execution.** Agents must never trigger physical hardware actions (laser firing, stage movement, DAQ acquisition).
- **All PRs require risk explanations.** Every pull request must include a description of what could go wrong.

### Code Quality

- Follow PEP 8 and existing project conventions.
- Add type hints to all new functions and methods.
- Add docstrings to all public classes and functions.
- Keep changes small and focused — one concern per PR.
- Run linting (`ruff`) before submitting.

### Branch Discipline

- Agents work ONLY on isolated feature branches, never on `main`.
- Branch naming: `agent/<agent-type>/<short-description>` (e.g., `agent/openhands/cleanup-imports`).
- Agents must never force-push or rewrite history.

## Red-Zone Files

The following code areas are considered **red-zone** — they require **mandatory human review** and **explicit maintainer approval** before any modification. Agents should avoid touching these unless the task specifically requires it.

### Hardware Control
- **DAQ timing** — Any code controlling data acquisition timing, synchronization, or triggering.
- **Laser control** — Laser power, enable/disable, modulation, safety interlocks.
- **Galvo scan generation** — Scan patterns, voltage generation, waveform construction.
- **TTL generation** — Digital pulse timing, trigger sequences, synchronization signals.
- **Stage movement** — Positioning commands, velocity, acceleration, limit handling.
- **Hardware initialization** — Device discovery, connection, configuration, calibration.

### Identification Patterns

Red-zone files typically include (but are not limited to):
- Files containing `DAQ`, `NI`, `nidaq` in their names or imports
- Files in `laser`, `positioner`, `stage`, `scanner`, `galvo` directories
- Files with `TTL`, `trigger`, `pulse`, `waveform` in their names
- Hardware manager initialization code
- Any file importing `nidaqmx`, `pyvisa`, or direct serial communication libraries

### What Agents Must Do with Red-Zone Files

1. **Flag the file** as red-zone in the PR description.
2. **Explain the risk** of every change in detail.
3. **Never modify timing constants** or hardware parameters without explicit values provided by a maintainer.
4. **Request review** from someone with physical access to the relevant hardware.

## Agent Workflow

The expected workflow for agent-generated changes:

```
GitHub Issue (scoped task)
  → Agent plans approach
  → Agent creates isolated branch
  → Agent implements changes
  → Agent runs tests
  → Agent opens PR with risk explanation
  → Human reviews PR
  → Human merges or rejects
```

Automatic merges are **never allowed**.

## Microscope Knowledge Base

Before modifying hardware-related code, agents MUST consult the microscope knowledge base in `microscope-kb/`. This provides context about hardware limits, safety procedures, and configuration constraints.

## Cost Tracking

Each agent task should track:
- Estimated cost before starting
- Actual cost after completion
- Whether the output was useful

Budget limits (configured in `.env`):
- Monthly budget: ~$100
- Per-task budget: $2–10
- Daily soft limit: $10–20
