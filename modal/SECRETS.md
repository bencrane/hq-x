# Modal secrets — canonical scope per cron

> **Source of truth.** When a new Modal app is added, the agent reads this file to pick the right secret. If a binding doesn't fit any documented row, ratchet test `apps/data-engine-x/tests/test_modal_secrets_scoped.py` fails CI.

## Canonical secrets (use these)

| Secret name | Purpose | Who needs it | Rotation cadence |
|---|---|---|---|
| `dex-db` | Injects `DATABASE_URL` + `DEX_DB_URL_POOLED` + `DEX_DB_URL_DIRECT`, all pointing at the DEX Supabase prod Postgres (`db.vmncuibejimzskzjmpgx.supabase.co:5432`, role `postgres`). Connect via `DEX_DB_URL_POOLED` for app reads/writes; `DEX_DB_URL_DIRECT` for DDL and `lance_commit_lock`'s `pg_advisory_xact_lock`. | Orchestrator containers that write `bulk_ingest.*` ledger rows OR Lance datasets (commit-lock needs DIRECT). | 90d |
| `bulk-ingest-r2` | Injects `R2_ENDPOINT` + `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY` for the `dex-raw-landing-zone` R2 bucket. | Orchestrator containers that read/write `dex-raw-landing-zone` (Pattern A Lance emit writers, Pattern B bridge writers, raw-Parquet ingest writers). | 90d |
| `dex-material-change-cron` | Injects `DEX_API_BASE_URL` + `DEX_SERVICE_TOKEN` + `HQX_API_BASE_URL` + `HQX_TRIGGER_SHARED_SECRET` so the cron can POST to DEX and HQ-X. | The material-change-detection cron only (`modal/material_change_detection_app.py`). | 30d |
| `dex-alerter-telegram` | Injects `DEX_API_BASE_URL` + `DEX_SERVICE_TOKEN` so the alerter can call `/alerts/run-cycle` on DEX. | The alerter cron only (`modal/alerter_cron_app.py`). | 30d |
| `polaris-health-check` | Injects `POLARIS_PUBLIC_URL` + `POLARIS_ROOT_PRINCIPAL_ID` + `POLARIS_ROOT_PRINCIPAL_SECRET` for the Polaris catalog smoke test. | The Polaris health-check cron only (`modal/polaris_health_check_app.py`). | 90d |
| `internal-auth` | Injects internal DEX service auth (used by Modal apps that call DEX HTTP routes guarded by `require_flexible_auth`). | Modal apps that POST to DEX `/api/internal/*` or `/api/v1/*` routes. | 30d |

## Per-provider API keys (use as-needed)

| Secret name | Purpose | Who needs it |
|---|---|---|
| `parallel-ai` | Parallel.ai API token | apps that call Parallel.ai endpoints |
| `openai-secret` | OpenAI API key | apps that call OpenAI (embedding-emit pipelines, GPT inference) |
| `sam-api-key` | `data.gov` SAM.gov API key | SAM.gov ingest apps (`sam_opps_*`, `sam_entities_longitudinal_v2_emit_app.py`) |

## Per-source DB secrets — 2026-05-25 probe results

Probed via `modal/db_secret_consolidation_probe_app.py` on 2026-05-25
(Modal run `ap-6GSr9wfvBlTlCVpaCphlo8`). Every secret was deployed into a
single-secret container, the injected env vars were introspected, and a
psycopg connect was attempted with privilege checks against
`bulk_ingest.feed_ingest_runs` and `ops.cron_heartbeats`.

**Headline finding.** All 9 legacy per-source DB secrets currently fail
authentication with `FATAL: password authentication failed for user
"postgres"`. The credentials embedded in each Modal secret are stale (or
the per-source DBs were retired / rotated without flipping the Modal
secret). Apps that depend on these secrets cannot write to their declared
DBs in production. Heartbeat writes were rerouted to `dex-db` as part of
the 2026-05-25 HeartbeatLoop wiring sweep — those land successfully.

**Identity tuples** (host : port / database / user) injected by each
secret, captured by the probe:

| Secret name | Used by | Injected host | Same Supabase project as dex-db? | Connect status |
|---|---|---|---|---|
| `dex-db` (baseline) | (canonical) | `db.vmncuibejimzskzjmpgx.supabase.co:5432` / postgres / postgres | YES (this IS dex-db) | OK — `ops.cron_heartbeats` INSERT verified |
| `bts-t100-db` | `modal/bts_t100_segment_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.214.78.123` | NO (pooler routes to a different Supabase project) | FAIL — password auth |
| `epiq-claims-db` | `modal/epiq_claims_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.213.155.45` | NO | FAIL — password auth |
| `epiq-dockets-db` | `modal/epiq_dockets_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.213.155.45` | NO | FAIL — password auth |
| `faa-aircraft-registry-db` | `modal/faa_aircraft_registry_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.214.78.123` | NO | FAIL — password auth |
| `faa-airmen-db` | `modal/faa_airmen_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.213.155.45` | NO | FAIL — password auth |
| `finra-brokercheck-db` | `modal/finra_brokercheck_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.213.155.45` | NO | FAIL — password auth |
| `fmcsa-refresh-db` | `modal/_archived/fmcsa_refresh_app.py` (DISABLED) | `db.vmncuibejimzskzjmpgx.supabase.co:5432` → `3.218.119.135` | **YES** (same DEX Supabase host) | FAIL — password auth (stale creds) |
| `noaa-ais-db` | `modal/noaa_ais_ingest_app.py` | **DOES NOT EXIST IN MODAL WORKSPACE** | n/a | n/a — `modal secret list` returns 0 rows |
| `overture-places-db` | `modal/overture_places_ingest_app.py` | `aws-1-us-east-1.pooler.supabase.com:5432` → `18.214.78.123` | NO | FAIL — password auth |
| `warn-notices-db` (bonus) | (unbound — no current app reference) | `aws-1-us-east-1.pooler.supabase.com:5432` → `3.227.209.82` | NO | FAIL — password auth |

**Verdict per secret** (consolidation decision):

1. **`fmcsa-refresh-db`** — same Supabase project as `dex-db`. Once credentials are refreshed, this one is **SAFE TO CONSOLIDATE** to `dex-db`. Action: app is already archived (`_archived/fmcsa_refresh_app.py`); the operator may delete the Modal secret entirely.

2. **`bts-t100-db`, `epiq-claims-db`, `epiq-dockets-db`, `faa-aircraft-registry-db`, `faa-airmen-db`, `finra-brokercheck-db`, `overture-places-db`, `warn-notices-db`** — each routes to a different Supabase project than `dex-db` (different IPs behind the shared pooler hostname). **DO NOT CONSOLIDATE blindly** — these secrets either (a) point at legitimately separate per-source DBs that the ingest scripts write to, or (b) point at retired DBs that no longer matter. Either way the consolidation requires per-secret operator decision: is the per-source DB still load-bearing?

3. **`noaa-ais-db`** — does not exist in the Modal workspace at all. The `modal/noaa_ais_ingest_app.py` runbook docstring tells the operator to create it at deploy time; that has not been done. Action: either create the secret (per the docstring) or remove the `noaa-ais-db` binding from `noaa_ais_ingest_app.py` so it relies on `dex-db` only.

**Heartbeat-write status (independent of the consolidation question):** the 2026-05-25 HeartbeatLoop wiring sweep added `dex-db` alongside the per-source secret in every app I wired (`faa_airmen_ingest_app.py`, `faa_aircraft_registry_ingest_app.py`, `finra_brokercheck_ingest_app.py`, `noaa_ais_ingest_app.py`, `overture_places_ingest_app.py`, `bts_t100_segment_ingest_app.py`). The heartbeat loop's DB write reads `DEX_DB_URL_POOLED` first, which comes from `dex-db`, so heartbeats land in `ops.cron_heartbeats` even though the per-source DB credentials are stale.

**Probe app** is `modal/db_secret_consolidation_probe_app.py` — leave it deployed so the operator can re-run it after rotating credentials. Once the consolidation decisions are made and the legacy secret bindings are removed from all apps, delete the probe app + this file.

## Retired (DO NOT USE)

| Secret name | Status | Used by |
|---|---|---|
| `risingwave-prd` | RETIRED — RisingWave substrate decommissioned per CLAUDE.md §"Post-2026-05-13 substrate". Reference for archaeology only. | `modal/data_source_catalog_refresh_app.py` (still references; remove on next touch). |
| `fmcsa-ingest-db` | RENAMED to `dex-db` at 2026-05-25 (PR `fix/modal-p0a-secret-rename-sweep`). The legacy Modal secret may still exist for safety; do NOT bind in new code. | None (all 84 references swept). |

## Scope anti-patterns

The ratchet test `apps/data-engine-x/tests/test_modal_secrets_scoped.py` fails CI on these patterns:

- A "worker"-shape function (`cpu=1.0` AND `timeout < 600`) that binds `dex-db` or `bulk-ingest-r2` without an explicit `# scope-allow: <reason>` comment. Worker functions in `modal.Function.map()` topologies typically need pure outbound (httpx, R2 reads) and should be given `secrets=[]` or a narrower secret. Binding the orchestrator's DB credentials into a worker container is a credential surface bound for no benefit.
- A new Modal `.py` file that binds a secret not listed above (other than per-provider API keys). If the secret is intentional, add a row.

## Conventions

- Worker functions (small CPU, short timeout, used in `.map()`) → `secrets=[]` when possible; specify per-call secrets only.
- Orchestrator functions (scheduled, long timeout, DB + R2 + downstream API surface) → `secrets=[modal.Secret.from_name("dex-db"), modal.Secret.from_name("bulk-ingest-r2")]` + any per-provider keys.
- One-shot test/probe apps → bind only the secret being probed; clean up the app after the probe.
