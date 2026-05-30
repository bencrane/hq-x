# Bridge: `fec_990_namestate`

Match FEC individual contributors to IRS 990 officers/directors/trustees/key-employees on **(last_normalized, first_normalized, state)**. The cross-source intersection is the load-bearing HNW spine connector — the major-donor FEC cohort is almost-perfectly contained within "people on at least one nonprofit board."

## Method

- **Method name:** `person_name_namestate`
- **Method semver:** `1.0.0`
- **Normalizer:** `_lib/person_name_normalize.py` v1.0.0 (vendored Carlton Northern nicknames CSV at upstream SHA `3d450a015c39a79c305fb9d447c9adeb7dfdace0`, `nameparser==1.1.3`, `unidecode==1.3.8`)
- **State filter:** strict equality. FEC home-state == IRS 990 org-state (canonical org-address state from `filings_990` / `filings_990pf`, NOT the per-row `person_address_state` which is sparsely populated).

## Match key

```
(last_normalized, first_normalized, state)
```

`last` is more selective than `first` and indexes first; `state` strict-equality controls fan-out. Cross-state board service (e.g. CA donor on a TX foundation board) is deliberately out of scope for v1 — looser-state matching is a v2 concern.

## Confidence tiers

```
fec_donor_count_at_key   = count(DISTINCT raw_name)              at canonical key (FEC side)
person_990_count_at_key  = count(DISTINCT org_ein_normalized)    at canonical key (990 side)

CASE
  WHEN fec_donor_count_at_key > 50 OR person_990_count_at_key > 50  THEN 'rejected'
  WHEN fec_donor_count_at_key = 1 AND person_990_count_at_key = 1   THEN 'platinum'
  WHEN fec_donor_count_at_key = 1 OR  person_990_count_at_key = 1   THEN 'gold'
  ELSE 'silver'
END
```

`rejected` rows are excluded from the bridge Parquet entirely. The downstream MV `mv_990_principal_with_fec_giving` filters to `platinum + gold` only.

## Inputs

| side | source | grain | volume |
|---|---|---|---|
| FEC | `fec/cycle=*/indiv.parquet` (24 cycles 1980–2026) | one contribution row | ~280M |
| 990 persons | `irs-990/year=*/persons_990.parquet` + `persons_990pf.parquet` (years 2019–2025) | one (person, ein, year, role) row | ~35M |
| 990 filings | `irs-990/year=*/filings_990.parquet` + `filings_990pf.parquet` | one (ein, year) row | ~700K |
| 990 comp | `irs-990/year=*/compensation_990.parquet` + `compensation_990pf.parquet` | one (person, ein, year, role) row | ~6M |

## Pipeline

1. **990 side**
   - UNION ALL `persons_990` + `persons_990pf` with `is_pf` discriminator.
   - INNER JOIN `filings_*` on `(org_ein_normalized, tax_period_year)` → canonical `org_state` + `total_assets` (per-row `person_address_state` only ~2% populated).
   - LEFT JOIN `compensation_*` on `(ein, person_first_norm_old, person_last_norm_old, person_role, tax_period_year)` → max comp.
   - Apply `normalize_person_name(person_name_raw)` → `(last_v1, first_v1)`. Drop blacklisted/short rejections.
   - Aggregate by `(last_v1, first_v1, org_state, ein)` first to dedupe per-EIN total_assets.
   - Re-aggregate by `(last_v1, first_v1, org_state)`.

2. **FEC side**
   - Filter `name IS NOT NULL`, `length(state) = 2`.
   - Pre-aggregate by raw `(name, state)` first (L34 mitigation: ~280M → ~10–20M unique tuples), computing per-tuple lifetime metrics in SQL.
   - Apply `normalize_person_name(name)` UDF on the unique tuples only.
   - Re-aggregate by `(last_v1, first_v1, state)`.

3. **JOIN**
   - INNER JOIN both canonical tables on `(last_v1, first_v1, state)`.
   - Compute fan-out + tier classification.
   - Drop `rejected` tier.

4. **Write**
   - Write typed Parquet with `bridge_run_id UUID` per row → `bridges/fec_990_namestate/snapshot=<YYYY-MM-DD>/data.parquet` (ZSTD, ROW_GROUP_SIZE=100000).

## Output schema

| column | type | description |
|---|---|---|
| `bridge_run_id` | UUID | per-run trace key (FK to `ops.bridge_generation_runs`) |
| `match_method` | VARCHAR | always `person_name_namestate` |
| `match_method_semver` | VARCHAR | always `1.0.0` |
| `last_normalized` | VARCHAR | match key |
| `first_normalized` | VARCHAR | match key |
| `state` | VARCHAR | match key (2-letter, FEC home-state == 990 org-state) |
| `confidence_tier` | VARCHAR | `platinum` / `gold` / `silver` |
| `fec_donor_count_at_key` | BIGINT | distinct raw FEC names at canonical key (fan-out) |
| `person_990_count_at_key` | BIGINT | distinct EINs at canonical key (fan-out) |
| `fec_donor_raw_name_sample` | VARCHAR | top-amt raw FEC name string |
| `fec_donor_employer` | VARCHAR | top-amt employer |
| `fec_donor_occupation` | VARCHAR | top-amt occupation |
| `fec_donor_zip5` | VARCHAR | top-amt zip5 |
| `fec_donor_city` | VARCHAR | top-amt city |
| `fec_donor_lifetime_giving_total` | DOUBLE | sum across all FEC cycles |
| `fec_donor_lifetime_giving_count` | BIGINT | count of itemized contributions |
| `fec_donor_first_giving_date` | DATE | min transaction_dt |
| `fec_donor_latest_giving_date` | DATE | max transaction_dt |
| `fec_donor_cycles_active` | INTEGER[] | distinct cycle_year values |
| `person_990_raw_name_sample` | VARCHAR | full raw 990 name string for the row at the highest comp |
| `person_990_role_set` | VARCHAR[] | distinct roles across the EIN set |
| `person_990_ein_set` | VARCHAR[] | distinct EINs the person serves at |
| `person_990_org_count` | INTEGER | length of EIN set (= `person_990_count_at_key`) |
| `person_990_max_compensation` | DOUBLE | max comp_total_all across rows (NULL if undisclosed) |
| `person_990_total_org_assets` | DOUBLE | sum of max(total_assets) across distinct EINs |
| `person_990_is_pf_any` | BOOLEAN | true if any of the person's roles is at a 990-PF foundation |
| `generated_at` | TIMESTAMP | bridge-run start time |

## Downstream MVs

- `mv_990_principal_with_fec_giving` — RisingWave Pattern C trivial filter (`WHERE confidence_tier IN ('platinum', 'gold')`) over the bridge source. Hydrated via `BACKGROUND_DDL`. Carries the headline cohort: foundation principals (`is_pf_any = TRUE`) with major lifetime political giving (`lifetime_giving_total > 100000`).

## Validation

- **Bridge floor:** `rows_platinum >= 100,000` (HARD FAIL if not).
- **Tier sanity:** platinum share within `[40%, 80%]`. Outside that range surfaces to operator.
- **MV row count:** `count(*) FROM mv_990_principal_with_fec_giving > 50,000` after BACKGROUND_DDL hydration completes.
- **Headline cohort:** `count(*) WHERE is_pf_any = TRUE AND lifetime_giving_total > 100000` returns at least several thousand rows.

## Out of scope (v1)

- Looser-state matching (cross-state board service)
- Person-name middle-name disambiguation (v2 of normalizer)
- Per-cycle / per-year incremental updates (full corpus regeneration each run)
- Address-grain bridges (FEC home address × USPS canonical, etc.)

## Lineage

Bridge identity registered in:
- `ops.match_methods.person_name_namestate` (registered by PR #282)
- `ops.match_method_versions` row `(person_name_namestate, 1.0.0)` referencing `_lib/person_name_normalize.py` v1.0.0
- `ops.bridges.fec_990_namestate` (registered by `build_bridge_fec_990_namestate.py` on first run, idempotent UPSERT)
- `ops.bridge_generation_runs` row per generation
