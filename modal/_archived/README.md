# Archived Modal apps

Apps that have been retired (cron disabled, replaced by another app, or
substrate decommissioned) live here instead of the active `modal/` directory.
Closes P2-2 from the 2026-05-25 systemic Modal critique (audit §"P2-2").

Anti-pattern this fixes: keeping retired apps in the active `modal/` directory
forces every new agent (and the operator) to read past them to find the live
crons. Each archived app carries a one-line `Why archived:` note below.

Apps move OUT of this directory only when they're being un-retired — in which
case the operator + an agent should re-validate them against the current
substrate (helper imports, ledger shape, retry policy, secrets).

## Currently archived

| App | Archived | Why archived |
|---|---|---|
| `fmcsa_refresh_app.py` | 2026-05-25 | Modal nightly cron disabled 2026-05-07 (RisingWave cutover, commit 766f861a) per `apps/data-engine-x/CLAUDE.md §"FMCSA pipeline status"`. Replaced by `fmcsa_factory_daily_app.py` (different app, sequentially applies 19 FMCSA derivations). DO NOT re-enable without operator authorization. |
| `data_source_catalog_refresh_app.py` | 2026-05-25 | Binds the retired `risingwave-prd` secret. RisingWave substrate decommissioned per `apps/data-engine-x/CLAUDE.md §"Post-2026-05-13 substrate"` and `RETIRED-AND-DECOMMISSIONED.md`. If the catalog-refresh behavior is needed, write a fresh app against the current Lance/DuckDB substrate. |
| `db_secret_probe_app.py` | 2026-05-25 | Throwaway diagnostic app from the 2026-05-25 P0-3 cycle. Used to probe both `fmcsa-ingest-db` and `dex-db` Modal secrets and confirm they alias the same DEX Supabase Postgres (same host, IP, role, grants). Single-purpose; the probe shipped its findings into PR #709 + SECRETS.md. Kept here for future per-source-DB-secret probes (`epiq-claims-db`, `bts-t100-db`, etc.) following the same pattern. |
| `usaspending_lance_diag_app.py` | 2026-05-25 | Throwaway diagnostic app from the 2026-05-25 Modal architecture audit (PR #708 predecessor). Used to compare three Stage 2 fan-out topologies against USAspending's F5 BotDefense (Variant A canonical / Variant B `.map()` per-batch / Variant C sustained single client). Established that `.map()` per-batch is the right topology. Findings shipped into the canonical USAspending Lance cron rewrite. Kept here as the reference probe shape for future "is the fan-out topology fit for this upstream" audits. |

## How to add a new archived app

1. `git mv apps/data-engine-x/modal/<app>.py apps/data-engine-x/modal/_archived/<app>.py`
2. If the Modal app is still deployed: `modal app stop <app-id>` to free resources.
3. Add a row to the table above with `Archived: YYYY-MM-DD` + the one-line `Why archived:` reason.
4. Update `apps/data-engine-x/modal/INDEX.md` if the app was listed there.
5. Commit with message `chore(data-engine-x): archive modal/<app>.py — <reason>`.
