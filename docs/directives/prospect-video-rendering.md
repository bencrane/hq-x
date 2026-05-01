# Directive: Prospect-video rendering — backend

**Status:** Active. Backend-only — frontend is a separate directive in `hq-command`.

**Context:** Read [CLAUDE.md](../../CLAUDE.md) and [docs/strategic-direction-owned-brand-leadgen.md](../strategic-direction-owned-brand-leadgen.md) §5 Phase 0 (pre-payment prospect outreach). The strategic doc references "Remotion-rendered personalized video" as the prospect-outreach asset for cold outreach to demand-side partners. This directive ships the rendering pipeline.

**Scope clarification on autonomy:** Make strong engineering calls within the scope below. Do not build any frontend artifact — no player page, no admin UI, no upload UX, no styling, no autoplay decisions, no schedule-a-call surface. Do not couple to the post-payment pipeline (no `gtm_initiatives` references, no `recipients` references — this is pre-payment, target_account-shaped). Do not modify the Remotion package version or upgrade Trigger.dev. Do not attempt to integrate ElevenLabs or any TTS service — audio is pre-recorded and lives inside the source asset.

---

## 1. Why this directive exists

Prospect outreach (your outreach to prospective demand-side partners) needs a personalized-video asset per prospect. The user-facing pattern: a Loom-style link in a cold email; the prospect clicks → lands on a player page → watches a personalized video that names them, their company, an earmarked audience query relevant to their vertical.

The video itself is a **composite**: a pre-recorded screen + face-cam demo (you, querying the audience tool) plus per-prospect personalization layers (intro slate with their name + company, mid-video overlays referencing their specific earmarked audience, outro CTA). Remotion is the right tool — composes React components into video, supports per-render input props, runs cheaply on AWS Lambda.

What this directive ships:

- The Remotion composition stub (typed prop contract; placeholder layout — actual scene design iterates separately)
- Remotion Lambda deployment + render orchestration
- Source-asset storage in Supabase (you upload the screen+face MP4 via frontend later)
- DB schema for tracking renders
- Trigger.dev render task
- Internal + admin API endpoints
- Dub link minting per render
- End-to-end smoke against fixture data

What this directive **does not** ship:

- Any frontend. Player page (`/v/<short_code>`), admin video pages, upload UX → all separate hq-command directive.
- Composition visual design — stub renders placeholder; iterate later in Remotion's local studio.
- Audio integration beyond what the source asset already carries (you record audio with the demo).
- TTS / ElevenLabs / per-prospect generated voiceover.

---

## 2. Existing-state facts to verify before starting

- This repo has no Remotion dependency today. You will add `remotion`, `@remotion/cli`, `@remotion/lambda`, `@remotion/lambda-client`, and any peer deps to a new `remotion/` subdirectory at the repo root (kept separate from `src/trigger/` to avoid Trigger.dev's TS build picking up Remotion's React tree).
- `business.target_accounts` — referenced as the FK target on the new render row. **Verify this table exists** before depending on it. Per [docs/handoff-pre-payment-pipeline-2026-04-30.md](../handoff-pre-payment-pipeline-2026-04-30.md) §5.2, `business.target_accounts` is **not yet built** — it's deferred to the pre-payment pipeline directive. If the table is absent at start time, write the new render-row table with `target_account_id UUID NOT NULL` (no FK), document the dangling reference, and proceed. The FK is added later when target_accounts ships.
- `app/providers/dub/client.py` exists with the link-mint primitive used by `step_link_minting.py`. Reuse the underlying client; do not call Dub's HTTP API directly from this directive's code.
- Trigger.dev task pattern: TS shims that POST to hq-x `/internal/*` endpoints carrying `TRIGGER_SHARED_SECRET`. Mirror exactly.
- Supabase Storage is provisioned (per `MAGS_SUPABASE_*` env-var pattern in `managed-agents-x` README, hq-x has analogous Supabase config). Verify the existing supabase client wiring or add minimal client setup (storage-only — auth not needed; backend uses service-role key).
- AWS credentials for Lambda invoke must be added to hq-x Doppler — they are NEW. See §5.

---

## 3. Migration

Filename convention: UTC-timestamp prefix per `CLAUDE.md`.

### 3.1 `<ts>_recipient_video_renders.sql`

```sql
CREATE TABLE business.recipient_video_renders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Pre-payment context. target_accounts may not exist at directive
    -- start; if so, leave this as an unenforced UUID, add the FK in a
    -- follow-up migration once the pre-payment pipeline lands.
    target_account_id UUID NOT NULL,

    -- Composition slug — repo-committed React file, packaged into a
    -- Remotion bundle and deployed to S3 via the Remotion Lambda flow.
    composition_slug TEXT NOT NULL,

    -- Bundle URL: the S3 URL of the deployed Remotion bundle this
    -- render targets. Captured at render-start so a later iteration of
    -- the composition doesn't rewrite history.
    remotion_bundle_url TEXT NOT NULL,

    -- Input props passed to the composition. Verbatim JSONB.
    props_blob JSONB NOT NULL,

    -- Source asset reference. Supabase Storage path
    -- (eg. 'prospect-video-sources/demo-fmcsa-query.mp4'). Resolved to
    -- a signed URL at render time.
    source_asset_path TEXT,

    -- Render lifecycle.
    render_status TEXT NOT NULL DEFAULT 'queued'
        CHECK (render_status IN ('queued', 'rendering', 'succeeded', 'failed')),
    output_mp4_url TEXT,                                 -- CloudFront/S3 URL of the rendered MP4
    output_mp4_size_bytes BIGINT,
    duration_seconds NUMERIC(8,3),
    cost_cents NUMERIC(10,4),                            -- Remotion Lambda usage cost; precision needed for sub-cent renders

    -- Dub link pointing at the (frontend-served) player URL. The
    -- player URL pattern is decided by the frontend directive; backend
    -- mints with destination = '<player_base_url>/v/<render_id>' from
    -- config.
    dub_link_id TEXT,
    dub_short_url TEXT,

    -- Lambda lifecycle metadata.
    lambda_render_id TEXT,                               -- Remotion Lambda's render id
    lambda_function_name TEXT,
    lambda_region TEXT,

    error_blob JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    created_by_user_id UUID
);

CREATE INDEX idx_rvr_target_account ON business.recipient_video_renders (target_account_id, started_at DESC);
CREATE INDEX idx_rvr_status ON business.recipient_video_renders (render_status, started_at DESC);
CREATE INDEX idx_rvr_composition_slug ON business.recipient_video_renders (composition_slug, started_at DESC);
```

Note: table is named `recipient_video_renders` to match the prior planning language even though the recipient here is a **target_account** (prospect), not a `business.recipients` row. The naming is intentional for forward consistency — when post-payment per-recipient video gets built (a later directive, optional), the same table can grow a `recipient_id NULL` column.

---

## 4. Remotion setup

### 4.1 Repo layout

New top-level directory:

```
remotion/
  package.json              # remotion deps; separate from src/trigger/package.json
  tsconfig.json
  remotion.config.ts        # Remotion bundler config
  src/
    Root.tsx                # Composition registry — registers all available compositions
    compositions/
      ProspectOutreachStub.tsx     # Stub composition (this directive)
    types/
      props.ts              # Typed prop contracts per composition
```

The directory is independent of `src/trigger/` and `app/`. Remotion's React tree never gets imported by hq-x or Trigger.dev runtime code — it's bundled separately and served from S3.

### 4.2 Stub composition

`remotion/src/compositions/ProspectOutreachStub.tsx`:

- Composition id: `prospect-outreach-stub`
- Width × height: 1920 × 1080
- FPS: 30
- Duration cap: 120 seconds (3600 frames)
- Codec: H.264 (Lambda default for `.mp4`)

Typed input props (declared via `z.object` with `@remotion/zod-types` so the studio shows form inputs):

```typescript
type ProspectOutreachStubProps = {
  prospect_first_name: string;
  prospect_company_name: string;
  prospect_role_or_title: string | null;
  audience_query_text: string;        // "FMCSA motor carriers with 10-50 power units in TX"
  audience_count_estimate: number;    // 12,431
  earmarked_for_industry: string;     // "freight factoring"
  source_asset_url: string;           // signed Supabase URL of the screen+face demo MP4
  source_asset_duration_seconds: number;
  cta_text: string;                   // "I'd love to chat — let's set up a call"
  cta_link_text: string;              // "calendly.com/yourname"
};
```

Stub layout (placeholder, for wiring only — visual design iterates locally afterward):

- 0–3s: intro slate — "Hi {prospect_first_name}" + "{prospect_company_name}" subtitle
- 3s–end-3s: pass-through of `<Video src={source_asset_url} />`
- last 3s: outro slate — `cta_text` + `cta_link_text`

Don't try to make this look good. The contract is the prop shape and the rendering loop succeeding end-to-end. Visual design is a follow-up iteration in the Remotion local studio.

### 4.3 Remotion Lambda deployment

One-time setup (operator runs once per environment):

```bash
# From the remotion/ directory:
npx remotion lambda functions deploy --memory=2048 --timeout=240
npx remotion lambda sites create src/Root.tsx --site-name=prospect-outreach-stub-v1
```

Captured outputs (stored in Doppler, see §5):
- `REMOTION_LAMBDA_FUNCTION_NAME` — eg. `remotion-render-4-0-0-mem2048mb-disk2048mb-240sec`
- `REMOTION_LAMBDA_REGION` — eg. `us-east-1`
- `REMOTION_LAMBDA_BUNDLE_URL` — the deployed site URL on S3, eg. `https://remotionlambda-useast1-xyz.s3.us-east-1.amazonaws.com/sites/prospect-outreach-stub-v1/index.html`

When the composition iterates, operator re-runs `sites create` with a new `--site-name=prospect-outreach-stub-vN`, captures the new bundle URL, updates `REMOTION_LAMBDA_BUNDLE_URL`. Old bundle URLs survive; renders pinned to old URLs continue to work for replay/debugging.

This directive includes a setup script `scripts/setup_remotion_lambda.sh` documenting the commands but does NOT run them automatically — Lambda deployment is an operator action with cost implications.

---

## 5. Config / secrets — Doppler additions

New env vars in hq-x Doppler (`dev` + `prd`):

| Name | Purpose |
|---|---|
| `REMOTION_LAMBDA_FUNCTION_NAME` | The deployed Lambda function name |
| `REMOTION_LAMBDA_REGION` | AWS region (eg. `us-east-1`) |
| `REMOTION_LAMBDA_BUNDLE_URL` | S3 URL of the deployed composition bundle |
| `AWS_ACCESS_KEY_ID` | IAM credentials for Lambda invoke (scope: invoke + S3 read on the renders bucket only) |
| `AWS_SECRET_ACCESS_KEY` | Same |
| `REMOTION_OUTPUT_S3_BUCKET` | S3 bucket Lambda writes rendered MP4s into |
| `REMOTION_OUTPUT_CLOUDFRONT_DOMAIN` | CloudFront domain in front of the output bucket; used to construct the public MP4 URL |
| `SUPABASE_VIDEO_SOURCES_BUCKET` | Supabase Storage bucket name for source assets (eg. `prospect-video-sources`) |
| `SUPABASE_SERVICE_ROLE_KEY` | If not already in hq-x — needed for backend signed-URL minting on the source asset |
| `PROSPECT_VIDEO_PLAYER_BASE_URL` | Base URL the Dub link points at, eg. `https://acq-eng.com`. Backend constructs full destination as `<base>/v/<render_id>`. The frontend directive owns the actual route at that path; backend just hands Dub the URL. |

Read each via `app.config.require(...)` at usage time, not at boot. Render endpoint fails clean with a configuration error if any are missing.

---

## 6. Service layer

### 6.1 New module: `app/services/remotion_lambda.py`

Thin async client wrapping `@remotion/lambda-client` calls. Since Remotion's lambda client is a TS package, hq-x does NOT call it directly — instead, the Trigger.dev task layer wraps the JS client. hq-x's Python service module wraps the AWS Lambda Invoke API directly via boto3 (or aiobotocore for async), passing the Remotion-Lambda-shaped event payload.

Methods:

```python
async def trigger_render(
    bundle_url: str,
    composition_id: str,
    input_props: dict,
    output_bucket: str,
    function_name: str,
    region: str,
    privacy: str = "public",  # public, since we serve via CloudFront
) -> dict:
    """
    Invokes the Remotion Lambda function with action=launch.
    Returns: {render_id, bucket, output_key} — the Lambda response shape.
    """

async def get_render_status(
    render_id: str,
    bucket: str,
    function_name: str,
    region: str,
) -> dict:
    """
    Invokes Remotion Lambda with action=progress.
    Returns: {done, overallProgress, errors[], outputFile, costs, ...}
    """

async def construct_output_url(
    bucket: str,
    output_key: str,
    cloudfront_domain: str,
) -> str:
    """Builds the public CloudFront URL for the rendered MP4."""
```

Reference: Remotion Lambda's invoke contract is documented at remotion.dev/docs/lambda. Verify the exact event payload shape against the docs at implementation time — do not infer.

### 6.2 New module: `app/services/prospect_videos.py`

```python
async def kickoff_render(
    target_account_id: UUID,
    composition_slug: str,
    props_blob: dict,
    source_asset_path: str | None,
    created_by_user_id: UUID | None,
) -> dict:
    """
    1. INSERT recipient_video_renders row (status='queued', captures bundle_url from config snapshot)
    2. Mint Dub short link: destination = f"{PROSPECT_VIDEO_PLAYER_BASE_URL}/v/{render_id}"
       - tags: ['prospect-video', f'composition:{composition_slug}', f'render:{render_id}']
       - folder: 'prospect-outreach' (or per-target_account folder, your call — favor flat folder for v0)
    3. Persist dub_link_id + dub_short_url on the row
    4. Fire Trigger.dev task 'video.render-prospect-outreach' with {render_id}
    5. Return: {render_id, dub_short_url, render_status: 'queued'}
    """

async def execute_render(render_id: UUID) -> None:
    """
    Called by /internal/* callback. Reads row → signs source asset URL →
    invokes Remotion Lambda → updates row to status='rendering' with
    lambda_render_id + lambda_function_name + lambda_region. Does NOT
    poll — polling is the Trigger task's loop.
    """

async def complete_render(
    render_id: UUID,
    output_mp4_url: str,
    output_mp4_size_bytes: int,
    duration_seconds: float,
    cost_cents: float,
) -> None:
    """status='succeeded', completed_at=now."""

async def fail_render(render_id: UUID, error_blob: dict) -> None:
    """status='failed', error_blob populated."""

async def get_render(render_id: UUID) -> dict
async def list_renders(target_account_id: UUID | None, composition_slug: str | None, limit: int, offset: int) -> list[dict]
```

### 6.3 Source-asset signed-URL helper

`app/services/supabase_storage.py` (new or extend existing if there's already a supabase client):

```python
async def sign_video_source_url(path: str, ttl_seconds: int = 7200) -> str:
    """
    Mint a Supabase Storage signed URL for the source MP4.
    TTL must comfortably exceed Lambda render duration (Lambda fetches
    the source at render start; signed URL needs to be valid for that
    fetch). 2 hours is generous for a 120s video render.
    """
```

Render kickoff signs the URL fresh per render and passes the signed URL as `source_asset_url` in the props blob.

---

## 7. Routers

### 7.1 New file: `app/routers/admin_prospect_videos.py`

Mounted at `/api/v1/admin/prospect-videos`. All endpoints under `verify_supabase_jwt` + `require_platform_operator`.

```
POST   /api/v1/admin/prospect-videos
       body: {target_account_id, composition_slug, props_blob, source_asset_path?}
       returns: {render_id, dub_short_url, render_status: 'queued'}

GET    /api/v1/admin/prospect-videos/{render_id}
       returns: full render row including output_mp4_url when succeeded

GET    /api/v1/admin/prospect-videos
       query: target_account_id?, composition_slug?, status?, limit, offset
       returns: paginated list

POST   /api/v1/admin/prospect-videos/{render_id}/rerender
       returns: new render_id (creates a new row; original stays as historical record)
```

`rerender` semantics: takes the same target_account_id + composition_slug + props_blob + source_asset_path, fires a fresh render. Useful when iterating compositions and wanting to re-render historical inputs against a new bundle URL. The original row is preserved.

### 7.2 New file: `app/routers/internal/prospect_videos.py`

Mounted at `/internal/prospect-videos`. `TRIGGER_SHARED_SECRET` auth, mirror existing `app/routers/internal/exa_jobs.py`.

```
POST   /internal/prospect-videos/{render_id}/start         # Trigger.dev fires Lambda
POST   /internal/prospect-videos/{render_id}/poll-status   # Trigger.dev polls Lambda; updates DB
POST   /internal/prospect-videos/{render_id}/complete      # final state write (succeeded)
POST   /internal/prospect-videos/{render_id}/fail          # final state write (failed)
```

The poll-status endpoint reads Lambda's progress and returns it to Trigger.dev. Trigger.dev decides when to call complete or fail based on the response. This keeps Lambda invocation logic in Python (where the Doppler creds live) and orchestration logic in TS.

---

## 8. Trigger.dev task

New file: `src/trigger/render-prospect-video.ts`. Mirror `src/trigger/exa-process-research-job.ts`.

```typescript
export const renderProspectVideo = task({
  id: "video.render-prospect-outreach",
  run: async ({ renderId }: { renderId: string }) => {
    // 1. Tell hq-x to start the Lambda render
    await hqxPost(`/internal/prospect-videos/${renderId}/start`, {});

    // 2. Poll until terminal — Lambda renders 60s of 1080p in ~30-60s wall clock
    const POLL_INTERVAL_MS = 5000;
    const MAX_WAIT_MS = 10 * 60 * 1000;  // 10 min hard cap
    const startedAt = Date.now();

    while (Date.now() - startedAt < MAX_WAIT_MS) {
      const status = await hqxPost<{ done: boolean; failed: boolean; output_url?: string; error?: any }>(
        `/internal/prospect-videos/${renderId}/poll-status`,
        {},
      );
      if (status.done) {
        await hqxPost(`/internal/prospect-videos/${renderId}/complete`, {
          output_mp4_url: status.output_url,
          // duration + size + cost captured server-side from the Lambda response
        });
        return;
      }
      if (status.failed) {
        await hqxPost(`/internal/prospect-videos/${renderId}/fail`, { error: status.error });
        throw new Error(`render ${renderId} failed`);
      }
      await new Promise(r => setTimeout(r, POLL_INTERVAL_MS));
    }

    await hqxPost(`/internal/prospect-videos/${renderId}/fail`, { error: { reason: "render_timeout" } });
    throw new Error(`render ${renderId} timed out`);
  },
});
```

Polling vs `wait.for(...)`: polling is simpler and the wait window is short (under 5 minutes typical for a 120s render). Trigger.dev's task durability covers the polling loop — if the task crashes, Trigger.dev retries.

---

## 9. Dub integration

Mint via `app/providers/dub/client.py` (existing). One link per render row.

- Destination URL: `f"{PROSPECT_VIDEO_PLAYER_BASE_URL}/v/{render_id}"`
- Tags: `['prospect-video', f'composition:{composition_slug}', f'render:{render_id}']`
- Folder: `prospect-outreach`
- External id: `f"prospect-video:{render_id}"` (idempotency)

Webhooks: existing Dub webhook ingestion (`app/webhooks/dub_processor.py`) projects clicks into `dmaas_dub_events`. **Do not** add a new event-projection table for prospect-video clicks — they share infrastructure. Filtering happens on `dub_link_id` join through `dmaas_dub_links` (today's table for DM short-links). For prospect-videos, a separate join table is justified:

- New table `business.prospect_video_dub_links(id, render_id, dub_link_id, dub_short_url, created_at)` so prospect-video click lookups are clean and don't collide with the DM-side `dmaas_dub_links.channel_campaign_step_id NOT NULL` constraint.

Add this in the same migration as §3.1 or as a sibling migration; keep the directive's migration count to 1-2 files.

---

## 10. End-to-end smoke

New script: `scripts/seed_prospect_video_demo.py`. Runs against dev DB + AWS + Supabase + Trigger.dev.

Sequence:

1. Verify Doppler env vars are populated (fail-fast list).
2. Verify `REMOTION_LAMBDA_BUNDLE_URL` resolves (HEAD request returns 200).
3. Resolve or create a fixture target_account_id (UUID; if `business.target_accounts` doesn't exist, generate a UUID and skip the FK assertion).
4. Verify a fixture source asset exists in Supabase Storage at `prospect-video-sources/demo-fmcsa-stub.mp4`. If absent, exit with a clear "upload a sample MP4 to <bucket>/demo-fmcsa-stub.mp4 first" message.
5. POST `/api/v1/admin/prospect-videos` with composition_slug=`prospect-outreach-stub`, sample props (hardcoded JSON), source_asset_path pointing at the fixture.
6. Capture render_id + dub_short_url. Print the short URL so the operator can manually click and verify the Dub redirect resolves to `<player_base>/v/<render_id>` (which 404s — the frontend directive provides the player route).
7. Poll `GET /api/v1/admin/prospect-videos/{render_id}` until terminal state (or 10-min timeout).
8. On succeeded: HEAD the output_mp4_url, assert 200 + content-type=`video/mp4`. Print the URL.
9. Exit 0 on succeeded; exit non-zero on failed/timeout.

```
doppler --project hq-x --config dev run -- uv run python -m scripts.seed_prospect_video_demo
```

The script's job is to prove the loop end-to-end: rows persist, Lambda renders, MP4 resolves, Dub link is minted. The MP4 will look like a placeholder — that's expected — composition design is iterated separately.

---

## 11. Out of scope (defer)

- All frontend (player page, admin UI, source-asset upload UX) — separate hq-command directive
- Composition visual design beyond the placeholder stub — iterate locally in `npx remotion studio`
- TTS / programmatic voiceover generation (ElevenLabs or similar)
- Per-prospect props authoring via LLM (a future agent that reads `target_account.research_blob` + earmarked audience and produces the props JSON)
- Vertical-mobile composition variant (1080×1920) — second composition slug whenever needed
- Cost per-render aggregation / monthly billing reports
- Render quota / rate limiting per operator
- Backfill renders for past prospects
- Post-payment per-recipient video (would land in `recipient_video_renders` as a sibling rendering use case, with `recipient_id NOT NULL` instead of `target_account_id`)
- Migrating prospect-video click events into a wide-events ClickHouse table (still no cluster)

---

## 12. Tests / acceptance

### 12.1 Pytest

- `tests/test_recipient_video_renders_migration.py` — table + indexes land cleanly.
- `tests/test_prospect_videos_service.py` — kickoff creates row + mints Dub + fires Trigger; complete/fail update row; rerender creates a new row; full coverage of the four lookup helpers.
- `tests/test_remotion_lambda_client.py` — Lambda invoke payload shape (`action=launch`, `action=progress`); error handling on Lambda invoke errors. Mock boto3 / aiobotocore.
- `tests/test_admin_prospect_videos_router.py` — full router coverage. Mock service layer.
- `tests/test_internal_prospect_videos_router.py` — Trigger.dev callback shape; auth.

E2E test (`RUN_E2E_PROSPECT_VIDEO=1` opt-in):

- `tests/test_prospect_video_e2e.py` — runs `seed_prospect_video_demo.py` programmatically. Skip on default `pytest -q`.

### 12.2 Acceptance

- `uv run pytest -q` baseline passes (917 + new tests).
- `RUN_E2E_PROSPECT_VIDEO=1 uv run pytest tests/test_prospect_video_e2e.py -v` passes against dev DB + real AWS Lambda + Supabase.
- Manual: render a video via curl (or via the smoke script), confirm the MP4 URL resolves and contains a video. The visual quality is "stub level" — that's the point.
- Manual: confirm the Dub short URL minted by kickoff appears in the Dub dashboard with the expected tags + folder.

---

## 13. Sequencing within the directive

1. Migration 3.1 + run locally.
2. Doppler env vars (§5) added.
3. Remotion subdirectory + stub composition (§4.1, §4.2) committed.
4. Operator runs `scripts/setup_remotion_lambda.sh` once to deploy Lambda function + bundle. Captures outputs into Doppler.
5. `app/services/remotion_lambda.py` + `app/services/supabase_storage.py` + tests (Lambda + Supabase mocked).
6. `app/services/prospect_videos.py` + tests.
7. Routers (admin + internal) + tests.
8. Trigger.dev task + deploy.
9. `scripts/seed_prospect_video_demo.py` + iterate until smoke passes.
10. PR. Title: `feat(prospect-video): backend rendering pipeline via Remotion Lambda`.

PR description: confirms scope is backend-only, frontend directive lands separately. Notes that the smoke script's MP4 is a stub composition and visual design iteration happens in local `npx remotion studio`, not in this PR.

---

## 14. Notes on what this enables

After this ships:

1. The frontend directive in hq-command can build the `/v/<short_code>` player page consuming `GET /api/v1/admin/prospect-videos/{render_id}` and the `output_mp4_url` field. Zero blockers from backend.
2. Composition iteration: open `remotion/` locally, `npx remotion studio`, edit `ProspectOutreachStub.tsx` against fixture props, see live preview. When happy, re-deploy via `npx remotion lambda sites create --site-name=prospect-outreach-stub-vN`, update Doppler `REMOTION_LAMBDA_BUNDLE_URL`, fire a rerender via the API. New renders use new bundle; historical renders remain pinned to the old bundle.
3. New scenarios (eg. `prospect-brand-walkthrough`) ship as new composition files in `remotion/src/compositions/`, register in `Root.tsx`, deploy with a new site name, choose at kickoff time via `composition_slug`.
4. Per-prospect prop authoring (LLM-generated copy from research_blob) is a follow-up agent. It produces the `props_blob` and calls the existing kickoff endpoint. No backend change here.
