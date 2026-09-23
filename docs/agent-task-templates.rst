Agent Task Templates
====================

This page defines small, bounded task prompts for coding agents working on
ImSwitch2. The goal is to make parallel agent work easier to review and safer
to merge.

General Rules
-------------

Every agent task should be narrow enough to review in one pass. Prefer one
file or one tightly related group of files. The agent must not merge, push, or
deploy without human review.

Agents must follow ``AGENTS.md``. In particular:

1. No direct hardware execution
2. No hardware timing changes unless explicitly requested
3. No API-breaking changes without maintainer approval
4. Tests or documentation checks are required
5. Red-zone files require explicit risk notes

Standard Prompt Format
----------------------

Use this format for most small tasks:

.. code-block:: text

   File: path/file.py

   Task summary
   One concise sentence describing the task.

   Todo list
   - Concrete item
   - Concrete item
   - Concrete item

   Do NOTs
   - Do not touch unrelated files
   - Do not modify hardware timing or hardware initialization
   - Do not stage or commit unrelated changes

   Implementation:
   1. Inspect the relevant file and nearby tests.
   2. Make the smallest scoped change.
   3. Add or update focused tests/docs.
   4. Keep existing public APIs compatible unless explicitly requested.

   Sanity checks
   - ruff check <changed files>
   - python -m compileall -q <changed python files>
   - pytest <focused tests> -q
   - git diff --check -- <changed files>

   Commit instructions
   - Commit only files changed for this task.
   - Use message: <type>: <short summary>

Doc-Only Task Template
----------------------

.. code-block:: text

   File: docs/path.rst

   Task summary
   Add or update documentation for one clearly scoped topic.

   Todo list
   - Inspect nearby docs for style and cross-link conventions
   - Add the new documentation page or update the existing one
   - Link it from docs/index.rst only if it is user/developer facing
   - Update ROADMAP.md if the task completes a roadmap item

   Do NOTs
   - Do not change source code
   - Do not invent unsupported behavior
   - Do not document local-only secrets, paths, or hardware credentials

   Implementation:
   1. Read the relevant source files or existing docs.
   2. Write concise, factual documentation.
   3. Prefer RST pages for Sphinx-indexed docs.
   4. Use Markdown only for internal design docs that are not in the Sphinx toctree.

   Sanity checks
   - git diff --check -- docs/path.rst docs/index.rst ROADMAP.md
   - sphinx-build -b html docs /tmp/imswitch-docs-build, if Sphinx >=5 is available

   Commit instructions
   - Commit only documentation and roadmap changes.
   - Suggested message: docs: <short summary>

Source-Level Contract Test Template
-----------------------------------

.. code-block:: text

   File: imswitch/imcontrol/_test/unit/test_<area>_contract.py

   Task summary
   Add or update a source-level contract test for one compatibility rule.

   Todo list
   - Identify the source file and contract to guard
   - Add a narrow assertion with a clear failure message
   - Avoid brittle checks for formatting unless formatting is the contract
   - Keep the test no-hardware safe

   Do NOTs
   - Do not import full GUI stacks during test collection
   - Do not instantiate physical managers
   - Do not change production code unless explicitly requested

   Implementation:
   1. Read the target source file as text.
   2. Add one focused test.
   3. Prefer checking stable public names, signals, or method calls.
   4. Add allowlists only with clear justification.

   Sanity checks
   - QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin imswitch/imcontrol/_test/unit/test_<area>_contract.py -q
   - ruff check imswitch/imcontrol/_test/unit/test_<area>_contract.py
   - git diff --check -- imswitch/imcontrol/_test/unit/test_<area>_contract.py

   Commit instructions
   - Commit only the focused test and related docs if needed.
   - Suggested message: test: <short summary>

No-Hardware Model or Controller Task Template
---------------------------------------------

.. code-block:: text

   File: imswitch/imcontrol/model/path.py

   Task summary
   Implement one no-hardware-safe model/controller improvement.

   Todo list
   - Add or update pure model logic where possible
   - Add focused unit tests
   - Keep controller changes behind existing signals/APIs
   - Preserve existing hardware behavior

   Do NOTs
   - Do not enable lasers, move stages, start DAQ tasks, or poll physical devices
   - Do not change scan timing, TTL generation, waveform construction, or hardware initialization
   - Do not remove compatibility signals or public APIs

   Implementation:
   1. Prefer pure functions and typed result objects.
   2. Keep live controller changes small and covered by contract tests.
   3. Add error handling that fails safe.
   4. Document any behavior risk in the final summary.

   Sanity checks
   - ruff check <changed python files>
   - python -m compileall -q <changed python files>
   - QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin <focused tests> -q
   - git diff --check -- <changed files>

   Commit instructions
   - Commit only the scoped implementation and tests.
   - Suggested message: <area>: <short summary>

Red-Zone Task Warning Template
------------------------------

Use this template only when a maintainer explicitly asks an agent to inspect or
modify red-zone behavior.

.. code-block:: text

   File: imswitch/imcontrol/<red-zone-file>.py

   Task summary
   Analyze or modify one explicitly approved hardware-adjacent behavior.

   Todo list
   - State why the file is red-zone
   - Identify the exact behavior under review
   - Add no-hardware tests first where possible
   - Document risk and hardware-verification needs

   Do NOTs
   - Do not change timing constants unless explicit values are provided
   - Do not change laser, stage, DAQ, TTL, scanner, or initialization behavior outside the requested scope
   - Do not claim physical safety without hardware verification

   Implementation:
   1. Start with read-only analysis.
   2. Add tests around the current behavior.
   3. Make the smallest approved change.
   4. Preserve defaults unless the maintainer explicitly approves a behavior change.

   Sanity checks
   - ruff check <changed files>
   - python -m compileall -q <changed files>
   - QT_QPA_PLATFORM=offscreen PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -p pytestqt.plugin <focused tests> -q
   - Full no-hardware suite if the change affects shared scan/acquisition behavior

   Commit instructions
   - Commit only after maintainer review of the diff.
   - Commit message must mention the red-zone subsystem.
   - PR description must include risk notes and hardware-verification requirements.

Review Checklist for Returned Agent Work
----------------------------------------

Before accepting an agent commit:

1. Inspect ``git show --stat`` and ``git show`` for scope creep
2. Verify no unrelated files were changed
3. Check red-zone files and risk notes
4. Run focused tests
5. Run the no-hardware suite when shared behavior changed
6. Confirm docs links resolve and examples use current APIs
7. Decide whether to keep, amend, or revert the agent commit before stacking
   new work on top
