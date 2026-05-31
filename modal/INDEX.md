# Modal app index

> # ⛔ GEN-2 FLEET FROZEN — 2026-05-31
> **The entire Gen-2 Modal app fleet has been officially archived and its
> execution suspended pending Gen-3 rebuilds.** All 104 root-level apps
> previously catalogued below were moved verbatim into
> [`_archived_gen2/`](_archived_gen2/) and are now **read-only reference
> material** — preserved for their API URLs, column mappings, and extraction
> logic so the managed-agent fleet can rebuild each feed on the Gen-3
> Universal Dispatcher ([`../core/modal_dispatcher.py`](../core/modal_dispatcher.py)) +
> Trigger v4 substrate. **Do not deploy, import from, or schedule any path
> under `_archived_gen2/`.** The cron cadences listed in the tables below are
> historical; no Gen-2 `@modal.Cron` is an active control-plane source of
> truth anymore. The only live pipeline is Gen-3 SAM.gov opps
> (`sam-gov-pipelines`). Catalog rows are retained as a feed-rebuild backlog;
> every filename below now resolves under `_archived_gen2/<filename>`.

> Closes P2-1 from the 2026-05-25 systemic Modal critique (audit §"P2-1").
> Per Meta-KernelEvolve §1 "Filesystem-as-knowledge-base, navigated via
> index.md": flat directories with 95+ files force every agent + the
> operator to grep for related apps. This file is the canonical map from
> Modal-app filename → topology + domain + sister-cron relationships.

**Active app count:** 0 active — **104 archived under [`_archived_gen2/`](_archived_gen2/)** (Gen-2 freeze, 2026-05-31) + 4 legacy under `_archived/`. Gen-3 live pipelines are tracked outside this index.
**Companion docs:** [`SECRETS.md`](SECRETS.md), [`RETRIES.md`](RETRIES.md), [`QUALITY_SCORE.md`](QUALITY_SCORE.md), [`_lib/`](\_lib/) (scaffolds), [`landing/`](landing/) (writers + ledger helper).

Each topology label maps to a section in [`~/Desktop/hq/inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md`](../../../Desktop/hq/inventory/DATA-FACTORY-ARCHITECTURE-PATTERNS.md). Apps that use the shared scaffold (P1-4) are marked **★scaffold**.

---

## Pattern A — Lance emit crons (re-emit derived Parquet → Lance dataset)

| App | Domain | Cron | Notes |
|---|---|---|---|
| `cms_open_payments_general_lance_emit_app.py` ★scaffold | CMS Open Payments — General | `30 7 * * *` | |
| `cms_open_payments_research_lance_emit_app.py` ★scaffold | CMS Open Payments — Research | `0 8 * * *` | |
| `fmcsa_authhist_essentials_lance_emit_app.py` ★scaffold | FMCSA — Authority History | `30 6 * * *` | |
| `fmcsa_carrier_essentials_embedding_emit_app.py` | FMCSA — Carrier embeddings (vector layer) | bespoke | OpenAI embedding pipeline; not scaffolded |
| `fmcsa_carrier_essentials_lance_emit_app.py` ★scaffold | FMCSA — Carrier Essentials | `30 6 * * *` | The Wave 3 canary; 8GB / ~4.4M rows |
| `fmcsa_crash_essentials_lance_emit_app.py` ★scaffold | FMCSA — Crash Essentials | `30 6 * * *` | |
| `fmcsa_inspections_recent_lance_emit_app.py` ★scaffold | FMCSA — Recent Inspections | `30 6 * * *` | |
| `fmcsa_insurance_active_lance_emit_app.py` ★scaffold | FMCSA — Active Insurance | `30 6 * * *` | |
| `fmcsa_insurance_history_lance_emit_app.py` ★scaffold | FMCSA — Insurance History | `30 6 * * *` | |
| `fmcsa_safety_basics_lance_emit_app.py` ★scaffold | FMCSA — Safety Basics | `0 7 * * *` | |
| `gleif_lei_records_lance_emit_app.py` ★scaffold | GLEIF — LEI Records | `0 8 * * 0` | Weekly Sunday |
| `sam_opps_active_lance_emit_app.py` ★scaffold | SAM.gov — Active Opps | `30 12 * * *` | |
| `usaspending_contracts_lance_emit_app.py` ★scaffold | USAspending — Contracts (monthly archive) | `0 7 16 * *` | Monthly on the 16th |
| `usaspending_recipient_grain_lance_emit_app.py` ★scaffold | USAspending — Recipient grain | `0 4 * * *` | |
| `clay_enriched_person_lance_emit_app.py` | Clay — enriched persons | webhook-triggered | Not scaffolded (Clay webhook pattern) |
| `clay_find_people_ae_us_lance_emit_app.py` | Clay — find-people AE/US | webhook-triggered | Not scaffolded (Clay webhook pattern) |
| `usaspending_btree_action_date_emit_app.py` | USAspending — BTREE index emit | on-demand | One-shot index emit |
| `usaspending_db_dump_lance_emit_sweep_app.py` | USAspending — DB dump sweep | on-demand | Multi-table emit sweep |

---

## Pattern B — Bridge generation crons (cross-source identity bridges)

| App | Domain | Cron | Notes |
|---|---|---|---|
| `openfda_device_pdl_bridge_app.py` | OpenFDA × PDL device-firm bridge | weekly | |
| `pdl_sba_fuzzy_match_emit_app.py` | PDL × SBA loans fuzzy bridge | on-demand | |
| `ppp_sos_ca_bridge_app.py` | PPP × CA SoS owner bridge | one-shot historical | |
| `ppp_sos_fl_bridge_app.py` | PPP × FL Sunbiz owner bridge | one-shot historical | |
| `ppp_sos_ny_bridge_app.py` | PPP × NY SoS owner bridge | one-shot historical | |
| `ppp_ucc_ca_debtor_bridge_app.py` | PPP × UCC-CA debtor bridge | one-shot historical | |
| `sam_sos_ca_entities_bridge_app.py` | SAM × CA SoS entities | one-shot historical | |
| `sam_sos_ca_principals_cohort_app.py` | SAM × CA SoS principals cohort | one-shot historical | |
| `sam_sos_fl_entities_bridge_app.py` | SAM × FL Sunbiz entities | one-shot historical | |
| `sam_sos_fl_officers_cohort_app.py` | SAM × FL Sunbiz officers cohort | one-shot historical | |
| `sam_sos_ny_entities_bridge_app.py` | SAM × NY SoS entities | one-shot historical | |
| `sba_sos_ny_owner_bridge_app.py` | SBA × NY SoS owner bridge | one-shot historical | |
| `usaspending_sos_fl_owner_bridge_app.py` | USAspending × FL Sunbiz owner | one-shot historical | |
| `usaspending_sos_ny_owner_bridge_app.py` | USAspending × NY SoS owner | one-shot historical | |

---

## 3-stage ingest crons (search → fan-out → write)

| App | Domain | Cron | Sister crons | Topology |
|---|---|---|---|---|
| `usaspending_api_daily_app.py` | USAspending — contracts FPDS delta | `0 6 * * *` | `_assistance`, `_contracts_lance` | single-thread Stage 1 only |
| `usaspending_api_daily_assistance_app.py` | USAspending — assistance FABS delta | `0 7 * * *` | `_app`, `_contracts_lance` | single-thread Stage 1 only |
| `usaspending_api_daily_contracts_lance_app.py` | USAspending — Lance rebuild | `0 8 * * *` | `_app`, `_assistance` | **modal.Function.map() per-batch** (Stage 2 fan-out) |

The three USAspending sisters share `SOURCE_ID='usaspending_api_daily'` and write to `bulk_ingest.feed_ingest_runs` via the canonical `landing.ledger.record_run` helper (P0-2 sweep).

---

## R2 raw-ingest crons (upstream → Cloudflare R2 Parquet)

| App | Domain | Cron |
|---|---|---|
| `bts_t100_segment_ingest_app.py` | BTS T-100 segment data | monthly |
| `epiq_ingest_app.py` | Epiq11 cases / claims / dockets → R2 + Lance (all 946 cases) | daily |
| `faa_aircraft_registry_ingest_app.py` | FAA aircraft registry | monthly |
| `faa_airmen_ingest_app.py` | FAA airmen | monthly |
| `fdic_call_report_app.py` | FDIC call reports | quarterly |
| `finra_brokercheck_ingest_app.py` | FINRA BrokerCheck | weekly |
| `fl_cilb_daily_app.py` | FL CILB construction industry licensing | daily |
| `fmcsa_ingest_app.py` | FMCSA raw ingest (28 feeds) | daily |
| `grants_gov_daily_app.py` | Grants.gov daily | daily |
| `noaa_ais_ingest_app.py` | NOAA AIS vessel pings | streaming |
| `ny_data_construction_ingest_app.py` | NY State construction | weekly |
| `ny_nyc_local_awards_ingest_app.py` | NY/NYC local awards | weekly |
| `openfda_device_app.py` | OpenFDA devices | weekly |
| `overture_places_ingest_app.py` | Overture Maps places | monthly |
| `sam_construction_opps_sized_app.py` | SAM.gov construction opps | daily |
| `sam_entities_longitudinal_v2_emit_app.py` | SAM.gov entities historical | one-shot |
| `sam_opps_active_daily_app.py` | SAM.gov active opps daily | daily |
| `sam_opps_api_uei_enrichment_app.py` | SAM.gov opps UEI enrichment | daily |
| `sam_opps_archived_weekly_app.py` | SAM.gov archived opps | weekly |
| `sbir_awards_ingest_app.py` | SBIR awards | weekly |
| `txdot_letting_ingest_app.py` | TxDOT letting | weekly |
| `ucc_ca_ingest_app.py` | UCC CA filings | quarterly |
| `uspto_patents_app.py` | USPTO patents | monthly |
| `warn_notices_app.py` | WARN notices (FL/NV/TX/NJ) | weekly |
| `usaspending_daily_app.py` | USAspending bulk archive daily | daily |
| `usaspending_monthly_app.py` | USAspending bulk archive monthly | monthly |

---

## State SoS pipelines — Operator-Only Bulk Run (Quarterly Batch)

> **Policy (2026-05-25):** State Secretary-of-State corporate registries (CA / FL /
> NY / CO) are slow-moving, compute-burning on high-frequency crons, and prone to
> upstream-feed CSV-bleed degradation (PR [#731](https://github.com/bencrane/hq-all/pull/731)
> post-mortem). All state SoS pipelines are retired from automated schedules.
> No `schedule=Cron(...)` arg on any decorator. Pipelines remain fully executable
> via explicit CLI parameters as controlled, point-in-time snapshot refreshes.
> Do NOT re-add a `schedule=` arg without explicit operator-policy reversal.

| State | Stage | Script / app | Cron | Manual invocation |
|---|---|---|---|---|
| CA | s1: zip → R2 Parquet         | `scripts/run_ca_sos_master_unload_to_r2.py`           | none | `modal run scripts/run_ca_sos_master_unload_to_r2.py::run` |
| CA | s2: R2 → Lance (entities)    | `scripts/run_ca_sos_entities_lance_emit.py`           | none | `modal run scripts/run_ca_sos_entities_lance_emit.py::run` |
| CA | s3: R2 → Lance (principals)  | `scripts/run_ca_sos_principals_lance_emit.py`         | none | `modal run scripts/run_ca_sos_principals_lance_emit.py::run` |
| CA | s4: R2 → Lance (agents)      | `scripts/run_ca_sos_agents_lance_emit.py`             | none | `modal run scripts/run_ca_sos_agents_lance_emit.py::run` |
| FL | s1: zip → R2 Parquet         | `scripts/run_fl_sunbiz_master_unload_to_r2.py`        | none | `modal run scripts/run_fl_sunbiz_master_unload_to_r2.py::run` |
| FL | s2: R2 → Lance (entities)    | `scripts/run_fl_sunbiz_entities_lance_emit.py`        | none | `modal run scripts/run_fl_sunbiz_entities_lance_emit.py::run` |
| FL | s3: R2 → Lance (officers)    | `scripts/run_fl_sunbiz_officers_lance_emit.py`        | none | `modal run scripts/run_fl_sunbiz_officers_lance_emit.py::run` |
| FL | s4: R2 → Lance (events)      | `scripts/run_fl_sunbiz_events_lance_emit.py`          | none | `modal run scripts/run_fl_sunbiz_events_lance_emit.py::run` |
| NY | s1: CSV → R2 Parquet         | `scripts/run_ny_sos_active_corporations_to_r2.py`     | none (was `0 14 * * *`, removed 2026-05-25) | `modal run modal/ny_sos_active_corporations_app.py::operator_refresh` |
| NY | s2: R2 → Lance               | `scripts/run_ny_sos_active_corporations_lance_emit.py`| none | same `operator_refresh` (chained) |
| CO | s1: CSVs → R2 Parquet        | `scripts/run_co_sos_to_r2.py`                         | none | `doppler run -- python scripts/run_co_sos_to_r2.py` |
| CO | s2: R2 → Lance               | `scripts/run_co_sos_lance_emit.py`                    | none | `doppler run -- python scripts/run_co_sos_lance_emit.py --apply` |
| ALL | per-state spine emit        | `scripts/build_sos_state_entity_spines_lance.py`      | none | `doppler run -- uv run python scripts/build_sos_state_entity_spines_lance.py --state {CA\|FL\|NY\|CO\|all}` |

**Modal-app status (the only state SoS pipeline that ever carried a cron decorator):**

- `modal/ny_sos_active_corporations_app.py` — `schedule=Cron("0 14 * * *")` removed
  2026-05-25; Modal deployment `data-engine-x-ny-sos-active-corporations`
  permanently stopped (`modal app stop`). Function renamed
  `daily_refresh` → `operator_refresh`. Manual invocation only.

The CA / FL / CO ingest scripts have no companion Modal app and have never been
scheduled — they're invoked directly via `modal run scripts/<name>.py::run` or
`python scripts/<name>.py` on demand.

---

## SEC EDGAR / DERA ingest crons

| App | Form | Cron |
|---|---|---|
| `sec_bdc_soi_app.py` | BDC SOI | monthly |
| `bdc_soi_parse_v2_app.py` | BDC SOI parse v2 | on-demand |
| `sec_dera_form_d_app.py` | Form D | quarterly |
| `sec_dera_fsds_app.py` | DERA Financial Statement Datasets | quarterly |
| `sec_edgar_def_14a_app.py` | DEF 14A | weekly |
| `sec_edgar_form_10k_app.py` | Form 10-K | weekly |
| `sec_edgar_form_13f_app.py` | Form 13F | quarterly |
| `sec_edgar_form_8k_app.py` | Form 8-K | weekly |
| `sec_edgar_form_abs_15g_app.py` | Form ABS-15G | quarterly |
| `sec_edgar_schedule_13d_13g_app.py` | Schedules 13D/13G | weekly |
| `sec_iapd_brochure_parse_app.py` | IAPD ADV Part 2 brochure parse | one-shot (~60min) |

---

## Operational / observability crons

| App | Purpose | Cron |
|---|---|---|
| `alerter_cron_app.py` | calls DEX `/alerts/run-cycle` + checks stale heartbeats | `*/15 * * * *` |
| `all_sources_verify_app.py` | portfolio-wide ledger reconciliation | daily |
| `coverage_stats_emit_app.py` | DMaaS audience coverage stats | daily |
| `dex_ingest_app.py` | FastAPI-on-Modal Clay webhook ingest | HTTP-triggered |
| `dex_modal_app.py` | DEX FastAPI ASGI wrapper | HTTP-triggered |
| `fmcsa_daily_verify_app.py` | FMCSA factory daily verifier | daily |
| `fmcsa_factory_daily_app.py` | FMCSA daily derivation orchestrator | `0 6 * * *` |
| `fmcsa_weekly_coverage_app.py` | FMCSA weekly coverage check | weekly |
| `material_change_detection_app.py` | diff-capture across all sources | `0 */6 * * *` |
| `polaris_health_check_app.py` | Polaris catalog smoke | hourly |
| `reap_orphans_app.py` | GC orphaned ingest runs | daily |
| `usaspending_daily_verify_app.py` | USAspending daily verifier | `0 8 * * *` |
| `usaspending_db_dump_to_r2.py` | USAspending DB dump → R2 | on-demand |
| `usaspending_derived_views_daily_app.py` | USAspending derived MV refresh | daily |
| `usaspending_recipient_features_app.py` | USAspending recipient feature emit | daily |
| `usaspending_weekly_coverage_app.py` | USAspending weekly coverage check | weekly |

---

## CA / state ingest crons (non-SoS)

| App | Domain | Cron |
|---|---|---|
| `az_app_rfp_public_app.py` | AZ App.RFP public records | weekly |
| `cal_eprocure_archived_app.py` | CA eProcure archived | one-shot |
| `caltrans_ccop_app.py` | Caltrans CCOP letting | weekly |
| `clinicaltrials_device_studies_app.py` | ClinicalTrials.gov device studies | weekly |
| `opsc_school_facility_funding_app.py` | OPSC school facility funding | weekly |

---

## Archived (do not use)

See [`_archived/README.md`](_archived/README.md) for the canonical list + reasons. Currently:
- `fmcsa_refresh_app.py` (RisingWave cutover 2026-05-07)
- `data_source_catalog_refresh_app.py` (retired risingwave-prd secret)
- `db_secret_probe_app.py` (P0-3 probe, kept as reference shape)
- `usaspending_lance_diag_app.py` (Modal architecture audit probe)

---

## How to add a new Modal app

1. Pick a topology from the audit's pattern catalog: Pattern A Lance emit, Pattern B bridge, 3-stage ingest, R2 raw-ingest, SEC ingest, operational cron.
2. If Pattern A Lance-emit: use the scaffold at [`_lib/pattern_a_lance_emit.py`](\_lib/pattern_a_lance_emit.py) — your app shrinks from ~250 LOC to ~30 LOC of CONFIG.
3. Pick a secret from [`SECRETS.md`](SECRETS.md). If you need a new secret, add a row there first.
4. Pick a retry policy from [`RETRIES.md`](RETRIES.md). Add `# retry-policy: <class>` comment in the decorator block — the ratchet test at `tests/test_modal_retry_audit.py` enforces this.
5. Add a row to this INDEX.md in the matching topology section.
6. Wire heartbeats via `landing.ledger.HeartbeatLoop` if expected wall-clock > 5 min (P1-1).
7. Commit + `modal deploy` from the operator's terminal.
