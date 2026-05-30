# Modal Micro App

This Modal app provides micro-operation HTTP endpoints backed by Parallel.ai.

## Deploy

```bash
cd modal && modal deploy app.py
```

## Serve Locally

```bash
cd modal && modal serve app.py
```

## Required Modal Secrets

- `parallel-ai` (provides `PARALLEL_API_KEY`)
- `internal-auth` (provides `MODAL_INTERNAL_AUTH_KEY`)
