# `_archived_gen2/` — frozen Gen-2 Modal fleet (read-only reference)

**Frozen 2026-05-31. Execution suspended pending Gen-3 rebuilds.**

This directory holds the **104 Gen-2 Modal apps** (101 `*_app.py` + 3 loose
fleet helpers) moved verbatim out of `modal/` root when the Gen-2 data
infrastructure was officially frozen. They are preserved **as reference
material only** — their value is the API URLs, request shapes, column
mappings, casting/filter logic, and `@modal.Cron` cadences that the
managed-agent fleet reads when rebuilding each feed on the Gen-3 substrate.

## Rules

- **Do not deploy, import from, or schedule anything under this path.** These
  files are frozen. Relative imports to `modal/_lib`, `modal/landing`, etc.
  will not resolve from here, by design — nothing here is meant to run.
- The live control plane is Trigger v4 → the Universal Dispatcher
  ([`../../core/modal_dispatcher.py`](../../core/modal_dispatcher.py)). The
  only migrated feed is Gen-3 SAM.gov opps (`sam-gov-pipelines`,
  [`../../scripts/ingest/sam_gov/sam_opps_bulk_canonical.py`](../../scripts/ingest/sam_gov/sam_opps_bulk_canonical.py)).
- The per-feed rebuild backlog is the catalog in
  [`../INDEX.md`](../INDEX.md) — each row there now resolves under
  `_archived_gen2/<filename>`.

## What stayed live in `modal/`

`_lib/`, `landing/`, `fmcsa/`, `noaa_ais/` (shared scaffolds/writers/parsers),
plus `_archived/` (the earlier 4-app archive). Gen-3 code lives in `core/`,
`src/trigger/`, and `scripts/ingest/`.
