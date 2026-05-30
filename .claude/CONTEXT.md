# Standalone AI baseline — hq-x

This repository is the **standalone `hq-x`** application. It was extracted from a
former `hq-all` monorepo via `git filter-repo` and is now flat and
self-contained.

## Hard ban — legacy `hq-all` / sibling systems

The AI agent working in this repo is **prohibited** from:

- Referencing, importing from, or assuming the existence of any `hq-all`
  monorepo system.
- Referencing or assuming any sibling application — including but not limited to
  `data-engine-x` (DEX), `managed-agents-x`, `hq-command`, `ae-platform-api`,
  `partner-platform`, `polaris`.
- Resolving or fabricating any `apps/<name>/...` path. There is no `apps/`
  directory here; application code lives at the repository root (`app/`,
  `scripts/`, `migrations/`, `views/`, `mcp/`, `modal/`).
- Assuming a `DEX` / `api.dataengine.run` service is reachable, or that
  `DEX_BASE_URL` / `DEX_SERVICE_TOKEN` integrations are live.

If a task appears to require any of the above, **stop and surface it to the
operator** rather than inventing a path or assuming a service exists.

> Note: Claude Code loads project instructions from the root `CLAUDE.md`. The
> functional copy of this ban lives there (section "Standing rule — no legacy
> monorepo assumptions"); this file documents the standalone AI baseline for the
> `.claude/` context layer.
