#!/usr/bin/env sh
set -eu

# The Doppler service token (DOPPLER_TOKEN) is scoped to a single config
# (dev/stg/prd). `doppler run` reads the project + config from the token,
# so we don't pass --project or --config flags here. APP_ENV is set inside
# the Doppler config itself and gets injected into the spawned process.
# Railway only needs DOPPLER_TOKEN — no APP_ENV var required.
exec doppler run -- \
    uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
