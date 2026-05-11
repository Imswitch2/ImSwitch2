# ImSwitch2 Governance

## Project Structure

ImSwitch2 is maintained by a small team with clearly defined roles. The project uses AI agents as development assistants, but all final decisions rest with human maintainers.

## Roles

### Project Owner

- Full administrative control over the GitHub organization and repositories.
- Final authority on project direction, releases, and architectural decisions.
- Manages API keys, secrets, and budget for AI agent usage.

### Maintainer

- Can review and merge pull requests.
- Can triage issues and manage milestones.
- Must follow all contribution guidelines and review requirements.

### Contributor

- Can submit issues and pull requests.
- All contributions go through the standard review process.

### AI Agents

AI agents (including OpenHands, Claude, and Microsoft Agent Framework agents) operate under strict constraints defined in `AGENTS.md`. They function as development assistants only:

- Agents never merge code automatically.
- Agents never modify red-zone files without explicit human approval.
- Agents always work on isolated branches.
- All agent output requires human review before merging.

## Decision Making

- Day-to-day decisions: made by maintainers through PR review.
- Architectural decisions: require project owner approval.
- Hardware-related changes: require explicit sign-off from someone with physical access to the hardware.
- Red-zone file changes: require maintainer review with risk assessment.

## AI Agent Budget

- Monthly budget is capped (see project settings).
- Per-task budget limits are enforced.
- Cost tracking is reviewed regularly to ensure value.

## Changing Governance

Changes to this governance document require project owner approval.
