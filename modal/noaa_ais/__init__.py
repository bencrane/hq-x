"""NOAA AIS (vessel position pings) bulk ingest — daily CSV path.

Lands raw pings to R2 (dex-raw-landing-zone) as ZSTD-compressed Parquet.
RisingWave reads from R2 via S3 connector; Postgres holds metadata only
(ops.ais_pings_ingest_runs). See:

  - apps/data-engine-x/modal/noaa_ais_ingest_app.py — Modal orchestrator
  - apps/data-engine-x/supabase/migrations/20260505220000_create_source_ais_pings.sql
    — column-shape reference (the partitioned Postgres table is unused at runtime;
    we only INSERT into the sibling ops.ais_pings_ingest_runs manifest)
  - apps/data-engine-x/risingwave/source_noaa_ais.sql — RW source over R2 keys
  - apps/data-engine-x/risingwave/mv_vessel_arrivals.sql — port-bbox MV
"""
