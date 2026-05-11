# Contributing to ImSwitch2

Thank you for considering contributing to ImSwitch2! This document outlines how to contribute safely and effectively to a project that controls real microscope hardware.

## Important Safety Notice

ImSwitch2 controls physical hardware including lasers, stages, detectors, and data acquisition systems. Incorrect changes can damage equipment or produce unsafe conditions. Please read `AGENTS.md` for red-zone file designations before making any changes.

## How to Contribute

### Reporting Bugs

1. Check existing issues to avoid duplicates.
2. Open a new issue using the **Bug Report** template.
3. Include your OS, Python version, hardware configuration, and steps to reproduce.

### Suggesting Features

1. Open an issue using the **Feature Request** template.
2. Describe the use case, expected behavior, and any hardware implications.

### Submitting Code Changes

1. **Fork** the repository and create a feature branch from `main`.
2. Keep changes small and focused — one concern per PR.
3. Write or update tests for your changes.
4. Ensure all CI checks pass (linting, tests, build).
5. Add a risk explanation in your PR description (see PR template).
6. Submit a pull request for human review.

### Code Style

- Follow PEP 8 and existing project conventions.
- Use type hints for all new functions and methods.
- Add docstrings to all public classes and functions.
- Run `ruff` for linting before submitting.

### Testing

- All new code must include tests.
- Run `pytest` locally before pushing.
- Hardware-dependent tests should be clearly marked and skippable.

## Review Process

All pull requests require at least one human reviewer. Automated merges are never allowed. Changes touching red-zone files (see `AGENTS.md`) require additional scrutiny and explicit approval from a maintainer.

## Code of Conduct

Please read and follow our [Code of Conduct](CODE_OF_CONDUCT.md).

## License

By contributing, you agree that your contributions will be licensed under the GNU General Public License v3.0.
