# Standalone AI baseline — hq-x

This repository is the **standalone `hq-x`** application. It is flat and
self-contained.

## Hard rule — self-contained only

All application code lives at the repository root (`app/`, `scripts/`,
`migrations/`, `views/`, `mcp/`, `modal/`). There is no `apps/` directory and
there are no sibling applications.

The AI agent working in this repo must **not**:

- Reference, import from, or assume the existence of any external application,
  service, or codebase outside this repository.
- Resolve or fabricate any path that does not exist in this repository.
- Assume any external service or integration is reachable unless it is
  configured in this repository's own environment.

If a task appears to require any of the above, **stop and surface it to the
operator** rather than inventing a path or assuming a service exists.

> Note: Claude Code loads project instructions from the root `CLAUDE.md`. The
> functional copy of this rule lives there (section "Standing rule — this repo
> is self-contained"); this file documents the standalone AI baseline for the
> `.claude/` context layer.
