# ImSwitch2

ImSwitch2 is a software solution in Python that aims at generalizing microscope control by providing a solution for flexible control of multiple microscope modalities.

This is a clean-slate continuation of the [ImSwitch](https://github.com/ImSwitch/ImSwitch) project, with a focus on safety, maintainability, and AI-assisted development.

## Project Structure

```
ImSwitch2/
├── .github/                  # GitHub templates and CI workflows
│   ├── ISSUE_TEMPLATE/       # Bug report, feature request, agent task templates
│   ├── PULL_REQUEST_TEMPLATE/ # PR template with risk assessment
│   └── workflows/            # CI pipeline (lint, test, build, smoke test)
├── microscope-kb/            # Microscope knowledge base (YAML)
│   ├── _index.yaml           # KB index and file descriptions
│   ├── safety.yaml           # Safety rules and interlocks
│   ├── hardware.yaml         # Hardware inventory
│   ├── limits.yaml           # Physical limits and safe ranges
│   ├── procedures.yaml       # Standard operating procedures
│   ├── software_config.yaml  # Configuration schema
│   └── troubleshooting.yaml  # Known issues and diagnostics
├── AGENTS.md                 # AI agent coding rules and red-zone definitions
├── CODE_OF_CONDUCT.md        # Contributor Covenant
├── CONTRIBUTING.md            # How to contribute
├── GOVERNANCE.md              # Project governance and decision making
├── LICENSE                    # GPLv3
├── ROADMAP.md                 # Migration milestones
├── .env.example               # Environment variable template
└── .gitignore
```

## AI Agent Workflow

ImSwitch2 uses AI agents as development assistants under strict human oversight:

```
GitHub Issue → Agent plans → Isolated branch → Tests → PR → Human review → Merge/Reject
```

Key constraints:
- All agent code changes require human review — no automatic merges.
- Red-zone files (hardware timing, laser control, DAQ) require explicit maintainer approval.
- Agents work only on isolated branches.
- See [AGENTS.md](AGENTS.md) for full rules.

## Getting Started

1. Clone this repository
2. Copy `.env.example` to `.env` and fill in your API keys
3. Install dependencies: `pip install -e .`
4. Run tests: `pytest`

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.
