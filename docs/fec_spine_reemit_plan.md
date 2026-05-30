# FEC Individual-Contributions Spine — Re-emit Plan

**Status:** DRAFT for adversarial review · **Date:** 2026-05-29
**Owner:** data-engine-x

## 1. Problem

- `spines/fec_individual_contributions_modern_lance` is a **manifest-less orphan**: 122 data fragments (~1.42 GB) under the prefix but **no `_versions/` directory** → `lance.dataset()` raises, dataset is unreadable.
- Downstream FEC bridges (`build_bridge_fec_sba_employer.py`, `build_bridge_fec_990_namestate.py`) read **raw parquet directly** (`read_parquet('s3://.../fec/cycle=*/indiv.parquet')`) and do their own aggregation — bypassing the Lance substrate. Anti-pattern: Lance is the system of record; bridges must read from it, not from transport parquet.
- **Goal:** emit a correct, indexed, registered FEC Lance spine from canonical raw, then re-point the bridges to read from it.

## 2. Source ground truth (verified)

- Raw: `s3://dex-raw-landing-zone/fec/cycle=*/indiv.parquet` — 24 cycles (1980–2026), **281,657,666 rows**, 11.9 GB ZSTD parquet.
- Schema (27 cols): `cmte_id, amndt_ind, rpt_tp, transaction_pgi, image_num, transaction_tp, entity_tp, name, city, state, zip_code, employer, occupation, transaction_dt, transaction_amt, other_id, tran_id, file_num, memo_cd, memo_text, sub_id, name_normalized, employer_normalized, zip5, occupation_normalized, cycle_year, cycle`.
- **No donor identifier** in bulk data. `sub_id` is per-transaction; `cmte_id` is the committee, not the donor. Donor identity must be **heuristic** (name + geography).
- **No street address.** Finest address grain is `zip5` (+ `city`). Street lives only on filed PDF images (`image_num`), not in structured data.
- Recent-cycle volume (post-2014 small-dollar era): 2016=20.5M, 2018=21.7M, 2020=69.4M, 2022=63.9M, 2024=58.2M, 2026=23.6M (partial).
- 2024 cycle distinct grains: `(name,employer,state)` = 5.78M · `(name,city,state,zip5)` = 4.77M.

## 3. Design decisions — **CHALLENGE THESE**

### D1. Grain → donor-identity rollup
One row per **`(donor_name_normalized, state, zip5)`**. Rationale: this is the reusable "FEC donor" entity that every consumer (SBA-employer bridge, SAM POC/officer enrichment, 990 bridge) needs; aggregation belongs in the spine, not re-run per bridge.
- **Caveat (must be stated in dataset doc):** with no donor ID, this key (a) merges distinct people who share a normalized name in the same zip5, and (b) splits one person across zip5 moves. Accepted as the least-bad heuristic; downstream joins are name+geo or name+employer, which tolerate this.
- **Alternative rejected:** contribution-grain Lance mirror (281M rows) — that's just raw-in-Lance; bridges would still aggregate. Could be added later as a separate `fec_contributions_lance` if a time-series consumer appears, but it is NOT the spine.

### D2. Scope → ALL 24 cycles (drop "modern" filter)
Aggregate across all cycles; capture recency in the rollup (`first_cycle`, `last_cycle`, `distinct_cycle_count`) and take address/employer/occupation from the **most-recent** contribution per donor. Rationale: a cycle filter throws away the `cycles_active` signal the SBA bridge already uses, and "most-recent address" gives freshness without dropping history.
- **Naming consequence:** rename to **`spines/fec_individual_donors_lance`** (donor-grain, all cycles). The orphan `..._contributions_modern_lance` is deprecated + deleted (§7). Avoids a misleading name.

### D3. Schema (exact output columns)
| column | type | source |
|---|---|---|
| `donor_name_normalized` | str (key) | `name_normalized` |
| `donor_name_sample` | str | `ANY_VALUE(name)` |
| `state` | str (key) | `state` |
| `zip5` | str (key) | `zip5` (most-recent) |
| `city_most_recent` | str | `city` at `MAX(transaction_dt)` |
| `employer_normalized_most_recent` | str | `employer_normalized` at `MAX(transaction_dt)` |
| `employer_set_pipe` | str | distinct `employer_normalized`, pipe-delimited (Lance LIST caveat L54) |
| `occupation_most_recent` | str | `occupation_normalized` at `MAX(transaction_dt)` |
| `total_contribution_amt` | double | `SUM(transaction_amt)` |
| `contribution_count` | int64 | `COUNT(*)` |
| `distinct_committee_count` | int64 | `COUNT(DISTINCT cmte_id)` |
| `first_cycle` | int64 | `MIN(cycle)` |
| `last_cycle` | int64 | `MAX(cycle)` |
| `distinct_cycle_count` | int64 | `COUNT(DISTINCT cycle)` |
| `generated_at` | timestamp | run constant |
| `spine_version` | str | `"1.0.0"` |
| `spine_run_id` | str | uuid4 |

"Most-recent" fields resolved via `arg_max(col, transaction_dt)` in DuckDB.

### D4. Execution → Modal, out-of-core (NOT the in-memory PDL pattern)
281M rows cannot be `scanner().to_table()`-ed into Arrow like the 8.8M PDL spine. Plan:
- Modal one-shot app `modal/fec_donors_spine_emit_app.py` (no cron), `memory=65536` (64 GB), `timeout=60*180` (3 h), ephemeral disk for spill.
- DuckDB reads raw parquet over httpfs (`read_parquet('s3://.../fec/cycle=*/indiv.parquet', hive_partitioning=true)`), runs the GROUP BY out-of-core: `SET memory_limit='48GB'; SET temp_directory='/tmp/lance'; SET max_temp_directory_size='200GB'; SET preserve_insertion_order=false`.
- Stream result to Lance via `con.execute(SQL).fetch_record_batch()` → `lance.write_dataset(reader, ...)` — never materialize the full result set in memory.
- **This emit reads parquet by design** — that is the canonical raw→Lance ingest path (CLAUDE.md §"bulk-historical Volume-King"). The anti-pattern is *bridges* reading parquet, which §6 fixes.

### D5. Write discipline (canonical, per `emit_spines_pdl_b2b_firmographics_lance.py`)
- `LANCE_BYPASS_SPILLING=true`, `TMPDIR=/tmp/lance`, tmp free-space floor check.
- `lance_commit_lock("spines_fec_individual_donors_lance")`.
- `lance.write_dataset(reader, LANCE_URI, mode="overwrite", max_rows_per_file=1_000_000)`.
- BTREE scalar indexes on `donor_name_normalized`, `zip5`, `state`, `employer_normalized_most_recent`.
- Index failure → hard abort + `_rollback` (restore prior version, or delete prefix on first emit).
- `ds.optimize.compact_files()`, `ds.cleanup_old_versions(older_than=7d)`.

### D6. Registration
- Polaris generic table: `init_polaris_lance_generic.py --namespace spines --table fec_individual_donors_lance --doc "<...>"`.
- `ops.data_sources`: register `spines.fec_individual_donors_lance` (dot-separated convention, `format='lance'`, `status='active'`, `owner_app='data-engine-x'`).

### D7. Verification gates (hard-fail)
- **Dry-run first:** `SELECT COUNT(*) FROM (<grouped query>)` to calibrate the expected donor-grain row count; record it.
- Hard row floor: `lance_rows >= 25_000_000` (catastrophic-regression catch; refine to ~90% of dry-run count).
- Sum-reconciliation: `SUM(total_contribution_amt)` in spine == `SUM(transaction_amt)` in raw (±0.01%).
- Count-reconciliation: `SUM(contribution_count)` in spine == raw row count (281,657,666).
- All 4 BTREE indexes present in `ds.list_indices()`.

## 4. Files to create
- `apps/data-engine-x/scripts/emit_spines_fec_individual_donors_lance.py` — the emit (mirrors PDL emit structure, out-of-core variant).
- `apps/data-engine-x/modal/fec_donors_spine_emit_app.py` — Modal one-shot wrapper (Pattern A scaffold; 64 GB / 3 h).

## 5. Files to modify (Phase 2 — after spine lands + verifies)
- `build_bridge_fec_sba_employer.py`: replace the `fec_raw`/`fec_donors`/`fec_home` parquet reads with a read from `spines/fec_individual_donors_lance`. Re-point the three match methods (`employer_eq_borrname`, `employer_eq_franchisename`, `home_city_zip5_state_eq_business`) to the spine's pre-aggregated columns.
- `build_bridge_fec_990_namestate.py`: same re-point.
- Re-run both bridges; verify row counts within tolerance of prior versions.

## 6. Sequence
1. Dry-run donor-grain COUNT + sum/count reconciliation numbers (local DuckDB or Modal).
2. **Delete orphan prefix** `polaris-warehouse/spines/fec_individual_contributions_modern_lance/` (manifest-less; cannot be `mode="overwrite"`-ed cleanly).
3. Write + deploy `emit_spines_fec_individual_donors_lance.py` + Modal app.
4. `modal run --detach` the emit. Watch to completion.
5. Verify §3-D7 gates against R2.
6. Register Polaris + `ops.data_sources`.
7. Phase 2: re-point bridges, re-run, verify.
8. Commit → PR → merge → pull.

## 7. Cleanup
- Delete the orphaned `fec_individual_contributions_modern_lance` prefix (full `_delete_r2_prefix`).
- If it was ever registered in `ops.data_sources`, flip to `status='retired'` (verify first — it was not in the registry as of last check).

## 8. Open questions for review
- Q1: Is `(donor_name_normalized, state, zip5)` the right identity key, or should employer be part of the key (people who donate from work vs home addresses split)?
- Q2: Should we also emit a contribution-grain `fec_contributions_lance` now, or defer until a time-series consumer exists?
- Q3: 64 GB / 48 GB DuckDB memory — is the GROUP BY over ~280M rows / tens-of-millions of groups safe out-of-core, or do we need to chunk by cycle and merge?
- Q4: Row floor of 25M — calibrate from dry-run; what's the real distinct-donor-grain count across all cycles?
- Q5: `arg_max(col, transaction_dt)` for "most-recent" fields — ties on date resolved arbitrarily; acceptable?
