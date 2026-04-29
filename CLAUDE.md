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
