# hq-x

## What this is

Single business backend for outbound operations. Owns webhook intake (Cal.com,
EmailBison), operator-facing admin routes (called by the HQ frontend), and a
scheduler tick endpoint (called by Trigger.dev). This repo contains only the
foundation; routes and integrations are added in subsequent directives.

## Stack

- FastAPI + Uvicorn (Python 3.12)
- Postgres (Supabase) via psycopg v3 async pool
- Doppler for secrets
- Docker
- Railway for deploy

## Local dev

1. Install [Doppler CLI](https://docs.doppler.com/docs/install-cli).
2. `doppler login`
3. `doppler setup --project hq-x --config dev`
4. `make install`
5. `make dev`

Then `curl http://localhost:8000/healthz` should return
`{"status":"ok","env":"dev"}`.

## Deploy

Handled by Railway. Pushes to `main` build and deploy via the `Dockerfile`. The
container reads secrets at runtime via `doppler run`; provide `DOPPLER_TOKEN`
(service token) and `APP_ENV` (`dev` | `stg` | `prd`) as Railway env vars.

## Doppler config

The Doppler project name is `hq-x` (hardcoded in the Dockerfile entrypoint).
Configs: `dev`, `stg`, `prd`. The entrypoint maps `APP_ENV` → Doppler config
of the same name.

### Tier-1 secrets (boot-required)

The app fails to boot if any of these are missing from the active Doppler
config:

- `HQX_DB_URL_POOLED` — Supabase pooled connection (port 6543)
- `HQX_DB_URL_DIRECT` — Supabase direct connection (port 5432)
- `HQX_SUPABASE_URL`
- `HQX_SUPABASE_SERVICE_ROLE_KEY`
- `HQX_SUPABASE_PUBLISHABLE_KEY`
- `HQX_SUPABASE_PROJECT_REF`
- `APP_ENV` (`dev` | `stg` | `prd`)

`LOG_LEVEL` is optional and defaults to `INFO`.

### Trigger.dev secrets (optional)

Used by the `/internal/scheduler/*` routes that Trigger.dev tasks call. If
`TRIGGER_SHARED_SECRET` is unset, those routes return 503.

- `TRIGGER_SHARED_SECRET` — bearer token. Same value lives in the
  Trigger.dev project's env vars.
- `TRIGGER_PROJECT_ID`, `TRIGGER_SECRET_KEY`, `TRIGGER_ACCESS_TOKEN` — used
  by the Trigger.dev CLI / SDK, not by the FastAPI app.

## Trigger.dev tasks

Scheduled tasks live in `src/trigger/`. They run on Trigger.dev cloud (not
on Railway) and call hq-x's `/internal/*` routes via a static shared
secret (`TRIGGER_SHARED_SECRET`). The Trigger.dev project id is pinned in
`trigger.config.ts`.

Currently shipped: `hqx.health_check` — daily at 14:00 UTC, posts to
`/internal/scheduler/tick` to prove the round-trip is healthy.

### Local dev

```sh
npm install
doppler run --project hq-x --config dev -- npm run trigger:dev
```

The CLI prints a dashboard URL where you can manually fire tasks.

### Deploy

```sh
doppler run --project hq-x --config dev -- npm run trigger:deploy
```

The CLI version is pinned to `4.4.4` in `package.json` scripts.
