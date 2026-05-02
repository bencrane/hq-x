# DIRECTIVE — EmailBison API ↔ MCP Coverage Investigation

**Status:** Read-only investigation. No code changes, no schema changes, no destructive API calls.
**Target repo:** `hq-x`
**Output:** A single canonical reference doc at `hq-x/docs/emailbison-api-mcp-coverage.md` (lives in this repo alongside the other `docs/*.md` references).

---

## Context

hq-x will wire **EmailBison as the email provider** after the in-flight
`campaigns → channel_campaigns` refactor lands. hq-x already accepts EmailBison
webhook payloads but does not yet **project** them into local state, and has no
provider client / send pipeline / sequence / lead model wired up.

Before we write any of that, we need a single canonical reference that answers:

> For every operation hq-x will need to wire EmailBison as an email provider,
> what is the API endpoint, is it covered by the connected EmailBison MCP, and
> what's the gap?

This directive produces that reference. It is **strictly read-only** and
**strictly EmailBison-focused** — do not use this investigation as an excuse to
audit hq-x, design schemas, or recommend ports.

---

## Scope

**In scope.**

- The EmailBison HTTP API surface (canonical OpenAPI + per-resource guides).
- The connected `emailbison` MCP server's tools and how they map to API endpoints.
- EmailBison's webhook event taxonomy and which API endpoints return matching state.

**Out of scope — do not investigate, cite, or read.**

- `outbound-engine-x` and any of its sub-repos / worktrees / archives. We are
  **not** porting from OEX. Do not open any file under
  `/Users/benjamincrane/outbound-engine-x*`,
  `/Users/benjamincrane/conductor/repos/outbound-engine-x*`, or any path
  containing `outbound-engine-x`. If a file references OEX, ignore the OEX-side
  details.
- Smartlead, Instantly, HeyReach, Twilio, Vapi, Lob, or any non-EmailBison
  provider.
- Multi-channel orchestration, GTM theme logic, inbox-agent design.
- hq-x schema design — that comes after this investigation, not in it.

---

## Inputs (read in this order)

1. **`/Users/benjamincrane/api-reference-docs-new/emailbison/api-1.json`** —
   canonical OpenAPI 3.0.3 spec. Source of truth for endpoints.
2. **`/Users/benjamincrane/api-reference-docs-new/emailbison/01-account-management/`
   through `17-scheduled-emails/`** — per-resource Markdown reference.
3. **`/Users/benjamincrane/api-reference-docs-new/emailbison/guides/`** — for
   intent / "how it's meant to be used."
4. **`/Users/benjamincrane/api-reference-docs-new/emailbison-data-model-investigation.md`**
   — upstream entity hierarchy, identifier strategy, multi-tenancy model.
5. **The connected `emailbison` MCP server's tool list** — visible in this
   session as deferred tools under `mcp__emailbison__*`. Use `discover_tools`
   and `search_api_spec` to enumerate the surface; do not assume the static
   list is exhaustive.

### Using the MCP as an investigation tool (encouraged)

The agent **should** actively use the EmailBison MCP throughout the
investigation, not just in §6. Treat it as a live oracle for any question
the static docs don't answer crisply. Specifically:

- Call `discover_tools` and `search_api_spec` freely while building §1 (the
  endpoint inventory) and §2 (the MCP coverage map). The OpenAPI file is
  authoritative for *shape*; the MCP is authoritative for *what's actually
  reachable from this client*.
- Sample real response bodies with read-only typed tools (`list_*`,
  `get_*`, `get_*_analytics`, `get_*_stats`, `get_account_details`,
  `get_active_workspace_info`, `validate_workspace_key`,
  `get_leads_analytics`, `get_replies_analytics`, `bulk_count`,
  `search_replies`) to confirm the spec matches reality, surface paging
  shapes, and capture error shapes.
- Use `call_api` (read-only HTTP verbs only — `GET`) to hit endpoints that
  have no typed wrapper, so §3.B and §3.C can be answered with evidence
  rather than inference.
- For §5, sample at least one real `Reply` and one real `CampaignEvent` /
  campaign analytics row and walk the chain: webhook payload shape →
  matching API endpoint response shape → fields available for
  reconciliation. Quote actual JSON.
- For §4b's reconciliation question, *probe EB itself* via the MCP for
  evidence of replay / redelivery / idempotency-key support
  (`search_api_spec` for "redeliver", "replay", "idempotency"; inspect any
  webhook-management endpoints under `09-webhooks/`). Recommendation must
  cite what was actually found, not assumed.

**Hard rules on MCP use** (apply everywhere, including §6):

- **Read-only verbs only.** No `create_*`, `update_*`, `delete_*`,
  `send_reply`, `export_leads_csv`, `export_replies_csv`, `bulk_export`,
  or any `run_subroutine_*` that mutates state. `call_api` is allowed
  with `GET` only.
- **Do not switch workspaces.** No `set_active_workspace`,
  no `reset_to_primary_workspace` unless the active workspace is
  unexpectedly *not* the one the user was previously on, in which case
  surface it and stop. Use whatever `get_active_workspace_info` returns at
  the start of the investigation for the entire run.
- **Small page sizes.** Cap `list_*` calls at the smallest page the API
  allows (typically 10–25 rows). The goal is shape, not data extraction.
- **Cite every live call.** When a finding rests on a live MCP call,
  reference the exact tool name and the relevant response keys, not the
  full response body. Do not paste large response payloads into the
  deliverable; quote only the fields that prove the point.

---

## Tasks

### 1. Endpoint inventory

From `api-1.json`, produce a flat table of every endpoint:

| Resource folder | Method | Path | Summary | Request body shape | Response shape | Auth scope |

Group by the 17 numbered resource folders so the table aligns with the docs
layout. Capture path-vs-query parameters distinctly.

### 2. MCP coverage map

For each tool under `mcp__emailbison__*`:

- Classify it as **typed wrapper** (e.g. `create_campaign`, `list_leads`,
  `get_reply`) or **generic** (`call_api`, `search_api_spec`,
  `discover_tools`, `bulk_count`, `bulk_export`).
- For typed wrappers, map the tool back to the underlying API endpoint(s).
  Note any tool that fans out to multiple endpoints or that wraps a paginated
  endpoint with auto-paging.
- Note workspace-scope behavior (`set_active_workspace`,
  `get_active_workspace_info`, `reset_to_primary_workspace`,
  `validate_workspace_key`) — these are session-state, not endpoint wrappers.

### 3. Gap analysis

Produce three lists derived from §1 and §2:

- **A. Endpoints with a typed MCP wrapper.** Prefer the wrapper at call sites.
- **B. Endpoints reachable only via `call_api`.** No typed wrapper; we'd hit
  the MCP generic or call HTTP directly.
- **C. Endpoints with no MCP path at all** (if any). Verify against
  `discover_tools` output, not just the static deferred-tool list.

### 4. Day-one wiring shortlist

Based on §1–§3 alone (no OEX references), recommend the **minimum endpoint
set** hq-x needs on day one to:

- Create / list / pause / resume campaigns.
- Attach and detach leads on a campaign.
- Attach and detach sender emails on a campaign.
- Fetch a campaign's sequence and schedule.
- List replies and fetch a single reply.
- Pull campaign analytics / stats.
- Project the webhook event types in §5 into local state and (later) reconcile
  drift via API pull.

For each entry on the shortlist, recommend **MCP-typed-wrapper vs `call_api`
vs direct HTTP** for the hq-x implementation, with a one-line rationale.

### 4b. Where hq-x needs its *own* endpoints (not just MCP passthrough)

The MCP and the EmailBison HTTP API are sufficient for **calling** EmailBison.
They are **not** sufficient for everything hq-x will need. Take a position on
each of the following and recommend whether hq-x should expose its own
endpoint / job / projection layer, with rationale:

- **Tracking / event capture.** Does hq-x need to record every send, open,
  reply, bounce, unsubscribe in its own store for analytics, attribution,
  and channel_campaign-level rollups? If yes — the source is webhooks, not
  polling. Confirm webhook coverage is complete enough to be the system of
  record, or name the gaps that force us to also pull from the API.
- **Reconciliation strategy.** Decide between three options and recommend one,
  citing EB behavior:
  1. **Pure webhook projection.** Trust webhooks, no backfill. Risk: lost
     events on receiver downtime → permanent state drift.
  2. **Webhook + periodic reconciliation pull.** Webhooks drive the live
     projection; a scheduled job re-reads `list_campaigns` /
     `list_leads` / `list_replies` / per-campaign analytics and reconciles
     drift. This is the §5 alignment table's reason for existing.
  3. **Polling-only.** Ignore webhooks entirely. Identify whether EB's API
     supports incremental pulls (cursors, `updated_after`, etc.) at all — if
     not, this is a non-starter.
  Confirm whether EB itself offers any **replay / redelivery** or
  **idempotency-key** mechanism on webhooks. If EB does not, hq-x must own
  idempotency at the receiver and own reconciliation as the recovery path.
- **Send-time tracking we cannot get from EB.** Identify any signal hq-x
  needs but EB doesn't emit / expose (e.g. lead-stage transitions, GTM-level
  attribution, channel_campaign-level rollups, cross-provider unified
  inbox). These force hq-x-side endpoints / tables regardless of MCP
  coverage.
- **Internal hq-x endpoints.** Recommend the shortlist of `app/routers/`
  endpoints hq-x will need: webhook intake (already exists), webhook
  projector trigger, reconciliation trigger, lead-attach proxy (if we want
  hq-x to own validation/idempotency above raw MCP), analytics readback. For
  each, justify why it must exist in hq-x rather than being a thin MCP
  passthrough at the call site.
- **MCP-as-runtime-dependency risk.** Note any operational concerns with
  treating the EmailBison MCP as a production runtime dependency
  (auth-token model, rate limits, workspace-state side effects from
  `set_active_workspace`, error-shape stability). If the MCP is best
  treated as a *developer tool* and production traffic should hit EB HTTP
  directly, say so.

The output of this section is a clear recommendation, not a survey. The
expected shape is: "hq-x should own X, Y, Z internally; the rest can be
direct MCP/HTTP calls; reconciliation strategy is option N because…"

### 5. Webhook event ↔ API state alignment

Enumerate every EmailBison webhook event type from the spec (`09-webhooks/`).
For each event, name the API endpoint that returns the same canonical
post-event state — so a projector and a reconciliation backfill can share
types and a future hq-x sync layer can self-heal from drift.

Format:

| Event type | Triggering action | Canonical state endpoint | Notes |

### 6. MCP ergonomics check (read-only)

Live-call **read-only** tools, one from each typed family, and record actual
response shapes. Note divergence from the OpenAPI spec.

Allowed calls:

- `get_active_workspace_info`
- `validate_workspace_key`
- `list_campaigns` (small page)
- `get_campaign` (one id from §6.list_campaigns)
- `get_campaign_analytics` / `get_campaign_stats` for that campaign
- `list_leads` (small page)
- `get_lead` (one id)
- `list_replies` (small page)
- `get_reply` (one id)
- `get_account_details`
- `search_api_spec` (smoke check)
- `discover_tools`

**Disallowed.** Any `create_*`, `update_*`, `delete_*`, `send_reply`,
`set_active_workspace`, `bulk_export`, `export_*`, or any subroutine that
mutates state. If `get_active_workspace_info` returns an unexpected workspace,
**stop and surface it** — do not switch workspaces to "fix" it.

---

## Deliverable

A single **canonical** Markdown reference at:

```
hq-x/docs/emailbison-api-mcp-coverage.md
```

This file is the long-lived source of truth for *"how hq-x talks to
EmailBison"* — every future PR that touches the EmailBison wiring should be
able to cite it. Write it as a reference document, not as an investigation
journal:

- No "I looked at…", no "next steps", no TODOs aimed at the agent itself.
- Every claim cited (`path/file:line` for spec/docs, exact MCP tool name +
  arguments for live findings).
- Tables over prose where the data is structured.
- Stable section anchors (don't reorder once shipped — downstream docs will
  link to them).
- Date-stamped header with the EB API version / OpenAPI spec hash the doc
  was built against, so future readers know when it goes stale.

Contents, in order:

1. **Endpoint inventory** (§1 table).
2. **MCP coverage map** (§2 table + classification).
3. **Gap analysis** (§3 — three lists A / B / C).
4. **Day-one wiring shortlist** (§4 — endpoint × MCP-vs-HTTP recommendation).
5. **hq-x-owned surface recommendation** (§4b — tracking, reconciliation
   strategy, internal endpoints, MCP-as-runtime risk).
6. **Webhook ↔ API alignment** (§5 table).
7. **MCP ergonomics findings** (§6 — actual vs spec response shapes,
   surprises, paging behavior, error shapes).
8. **One-page recommendation:** *"How hq-x should call EmailBison."* Cover:
   typed wrapper vs HTTP default, where `call_api` is acceptable, how
   workspace state is managed across requests, how pagination is handled, and
   how webhook projection should interact with the reference list endpoints
   for reconciliation.

No code changes anywhere. No new files outside the deliverable path. Cite
every claim with `path/file:line` (for spec / docs) or the exact MCP tool call
(for live calls).

---

## Constraints recap

- **Read-only.** No mutating API calls, no MCP `create_*` / `update_*` /
  `delete_*` / `send_reply`, no workspace switching, no bulk exports.
- **Do not read OEX.** Anything under `outbound-engine-x*` is off limits for
  this directive.
- **Single deliverable file.** Don't sprawl into multiple reports.
- **No design proposals for hq-x schema.** That comes after this lands.
