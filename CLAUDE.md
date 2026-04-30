# hq-x — Claude Code working notes

## Verifying spec data (DMaaS / Lob mailer specs)

`data/lob_mailer_specs.json` is the canonical Lob print-spec data. Migrations
0017 + 0019 seed `direct_mail_specs` from it, and `app/dmaas/service.py`
turns rows into solver-ready zone bindings.

When you change the spec JSON or a face/folding rule, run the sync script
to verify the data still passes both PDF MediaBox checks and zone-catalog
sanity checks (non-overlap, panel-derivation, glue/fold geometry):

```
uv run python -m scripts.sync_lob_specs
```

The script:

1. Downloads each spec's `template_pdf_url` and compares MediaBox to the
   declared bleed/trim dims (±0.01" tolerance).
2. For each v1 spec (4 postcards + 3 self_mailer bifolds), runs
   `bind_spec_zones` and asserts every required zone is present, all
   `*_safe` rectangles fit inside their parent surface, and the
   directive's mutual-non-overlap invariants hold on the back face
   (postcards) / cover panel (self-mailers).

Exit code is non-zero on any failure. Run before committing migrations or
JSON edits in this area.

Pytest also exercises the same invariants:

```
uv run pytest tests/test_dmaas_spec_binding.py
```

## Verifying scaffold briefs (DMaaS v1 scaffold library)

`data/dmaas_scaffold_briefs/*.json` holds the human-reviewable briefs the
`dmaas-scaffold-author` managed agent (in `managed-agents-x`) authors
against. `data/dmaas_v1_scaffolds.json` carries the resulting scaffold
DSL + prop_schema + placeholder content, one entry per brief. The two
must stay in sync.

When you change either file (add a brief, retune a strategy, edit a DSL),
run the verifier — it runs offline (no DB / no managed-agent session
required) and is the CI gate:

```
uv run python -m scripts.verify_scaffold_briefs
```

It loads each brief, finds the matching scaffold by slug, runs the
solver against every entry in `compatible_specs`, then re-runs the
brief's `acceptance_rules` against the resolved positions. Exit
non-zero on any failure.

Pytest covers the brief library invariants statically (no DB):

```
uv run pytest tests/test_dmaas_scaffold_briefs.py tests/test_dmaas_briefs.py
```

To persist the scaffolds to `dmaas_scaffolds` (idempotent, with audit
trail rows in `dmaas_scaffold_authoring_sessions`):

```
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_dmaas_v1_scaffolds
```

## Migration filename convention

`scripts/migrate.py` applies `migrations/*.sql` in lexical order. New
migrations should use a UTC-timestamp prefix (`YYYYMMDDTHHMMSS_<slug>.sql`)
rather than a numeric prefix — timestamps avoid collisions when multiple
agents work in parallel and lex-sort cleanly after the legacy `00NN_*` files.
