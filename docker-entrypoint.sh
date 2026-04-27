#!/usr/bin/env sh
set -eu

# Map APP_ENV (set by Railway) → Doppler config name.
case "${APP_ENV:-prd}" in
    dev) DOPPLER_CONFIG="dev" ;;
    stg) DOPPLER_CONFIG="stg" ;;
    prd) DOPPLER_CONFIG="prd" ;;
    *)
        echo "Unknown APP_ENV: ${APP_ENV}" >&2
        exit 1
        ;;
esac

exec doppler run --project hq-x --config "$DOPPLER_CONFIG" -- \
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
