# Handoff: GTM-initiative pipeline state — 2026-05-01

This document orients an AI agent picking up the GTM-initiative pipeline mid-build. Read [docs/strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md) first for the business model, then this for the engineering state.

---

## TL;DR

We are building the post-payment pipeline that converts a paid demand-side-partner audience reservation into a fully-instantiated multi-channel outreach campaign with per-recipient bespoke creative, run under a Ben-owned brand.

**Slice 1 has shipped.** Subagents 1 (strategic-context Exa research) and 2 (strategy synthesizer) are live in main. End-to-end exercised against the DAT fixture; an initiative reaches `strategy_ready` with a `campaign_strategy.md` artifact on disk.

**The V1 strategy doc shape was wrong.** Slice 1 produced strategy artifacts that were too memo-like, conflated literal-copy directives with conceptual framing, and skipped economic + audience-shape reasoning. We iterated to V2 in the sandbox; V2 is closer but still incomplete. **V3 is the next concrete piece of work** — see §6.

**Subagents 3–7 are not built yet.** Channel & step materializer, audience materializer, per-recipient creative author, landing-page author, voice-agent configurator. Slice 1 stopped at `strategy_ready`. Per-recipient bespoke creative is the heaviest piece downstream and depends on V3+ of the strategy artifact.

---

## 1. Business model recap (one paragraph)

hq-x is internal tooling for Ben's owned-brand lead-gen business. Demand-side partners (factoring companies, lenders, software vendors that serve operator audiences, etc.) pay roughly $25K to reserve an audience for 90 days. Ben runs multi-channel outreach (direct mail + email + inbound voice) to that audience under a Ben-owned brand. Inbound qualified leads are live-transferred to the partner during their hours of operation. The platform's customer is **Ben**, not the demand-side partner. Brand-side outreach uses **per-recipient bespoke creative** — every direct-mail piece is generated specifically for the recipient using their data points from data-engine-x.

---

## 2. What's built (in main, hq-x)

| Capability | Lives at | Shipped in |
|---|---|---|
| Strategic-direction reference doc | [docs/strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md) | [PR #70](https://github.com/bencrane/hq-x/pull/70) |
| Audience reservations (org ↔ DEX audience spec) | `business.org_audience_reservations`, `app/services/dex_client.py`, `app/routers/audience_reservations.py` | [PR #68](https://github.com/bencrane/hq-x/pull/68) |
| Exa research orchestration (destination-per-run) | `business.exa_research_jobs`, `exa.exa_calls`, `app/services/exa_client.py`, `app/routers/exa_jobs.py`, `src/trigger/exa-process-research-job.ts` | [PR #69](https://github.com/bencrane/hq-x/pull/69) |
| Exa raw-archive write surface in DEX | data-engine-x: `exa.exa_calls`, `POST /api/internal/exa/calls` | [data-engine-x PR #134](https://github.com/bencrane/data-engine-x/pull/134) |
| Capital Expansion brand (first owned brand) | `business.brands` row id `1c570a63-eac3-436a-8b52-bf0a2e1818e4` parented to `acq-eng` org; rich content at [data/brands/capital-expansion/](../data/brands/capital-expansion/) | manual + [PR #72](https://github.com/bencrane/hq-x/pull/72) |
| Brand-content DB mirror (`business.brand_content`) | Migration `20260501T010000_brand_content.sql`, `scripts/sync_brand_content.py` | [PR #75](https://github.com/bencrane/hq-x/pull/75) |
| GTM-initiative pipeline slice 1 (subagents 1 + 2) | `business.gtm_initiatives` + `demand_side_partners` + `partner_contracts`; `app/services/strategic_context_researcher.py`, `app/services/strategy_synthesizer.py`, `app/services/anthropic_client.py`, `app/routers/gtm_initiatives.py`, `app/routers/internal/gtm_initiatives.py`, `src/trigger/gtm-synthesize-initiative-strategy.ts`, `scripts/seed_dat_gtm_initiative.py` | [PR #72](https://github.com/bencrane/hq-x/pull/72) |
| Sandbox runner for synthesizer prompt iteration | `scripts/sandbox_synthesize.py` (NOT in main yet — see §7) | local |
| Directives archive | [docs/directives/exa-research-prototype.md](directives/exa-research-prototype.md), [docs/directives/gtm-initiative-strategy-pipeline.md](directives/gtm-initiative-strategy-pipeline.md) | [PR #74](https://github.com/bencrane/hq-x/pull/74) |

### Live state on dev DB (as of 2026-05-01)

- Org `acq-eng` (id `4482eb19-f961-48e1-a957-41939d042908`) — Ben's operator workspace, parent of all owned brands.
- Org `dat` (id `bf6590d4-4607-4b97-8e4b-3f66f2372879`) — DAT, treated as a demand-side-partner-as-org for the audience reservation prototype.
- Brand `Capital Expansion` (id `1c570a63-eac3-436a-8b52-bf0a2e1818e4`) — full brand content synced into `business.brand_content` (10 rows).
- Audience spec in DEX `ops.audience_specs` named "DAT — fast-growing carriers (prototype)" — fast-growing FMCSA motor carriers.
- Demand-side partner row for DAT: id `e5f43866-48a0-4e5a-950a-1cc169666aea`. Domain `dat.com`.
- Partner contract for DAT: $25K flat / 90 days / qualification rules `power_units_min: 10, power_units_max: 50`.
- GTM initiative for DAT: id `bbd9d9c3-c48e-4373-91f4-721775dca54e`. Status `strategy_ready`. Tied to Capital Expansion brand + DAT partner + the DAT audience spec.
- Two `exa.exa_calls` rows for this initiative: partner research (`75c4c7d6-0772-46fa-ab44-44418814ce49`, $0.49) + strategic-context research (`c1e37ee5-10f3-4701-a954-1cc78fb6d96b`, $0.46).

---

## 3. Outputs from the DAT run — archived in this repo

All artifacts from the DAT initiative are checked in at [docs/initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/):

| File | What it is |
|---|---|
| [partner_research.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/partner_research.md) | Subagent-equivalent Exa research run on dat.com — descriptive partner profile (target market, products, proof points, momentum, pricing tiers). 50 citations. ~36 KB. |
| [strategic_context_research.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/strategic_context_research.md) | Subagent 1 output — audience-scoped, operator-voice-sourced research. Operator pain framings, market context, "why now" hooks. ~47 KB. |
| [strategy_v1.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/strategy_v1.md) | Subagent 2 V1 synthesis — first cut. **Wrong shape.** Includes thesis paragraphs, "why this audience / partner / now" justification, and `personalization_variables` (a leak from the wrong layer). |
| [strategy_v2_iter1_yaml_failed.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/strategy_v2_iter1_yaml_failed.md) | V2 iteration 1 — leaner schema, but YAML parse failed (unquoted strings containing `:`). |
| [strategy_v2_iter2_validation_failed.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/strategy_v2_iter2_validation_failed.md) | V2 iteration 2 — fixed YAML quoting, but model nested `anti_framings` under `hook_bank` so top-level validator failed. |
| [strategy_v2_iter3_validated.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/strategy_v2_iter3_validated.md) | V2 iteration 3 — passes YAML + schema validation. Closer to the right shape; **still wrong on the conceptual-vs-literal-copy distinction** — see §6. |

The archive is the canonical place to find these. The on-disk paths under `data/initiatives/<id>/` are gitignored (production runtime data, not for source control).

---

## 4. Key code paths an agent must read

In rough order of importance:

1. [docs/strategic-direction-owned-brand-leadgen.md](strategic-direction-owned-brand-leadgen.md) — the model, the lifecycle, §8 data-model gaps, §9 open architectural decisions.
2. [docs/directives/gtm-initiative-strategy-pipeline.md](directives/gtm-initiative-strategy-pipeline.md) — the directive that produced PR #72; reflects the slice-1 design intent.
3. [app/services/strategy_synthesizer.py](../app/services/strategy_synthesizer.py) — V1 synthesizer. Note: `_SYSTEM_PROMPT_V1` is the active prompt; the file is structured so V2/V3 become new constants with `_ACTIVE_SYSTEM_PROMPT` at the bottom selecting which is live.
4. [app/services/strategic_context_researcher.py](../app/services/strategic_context_researcher.py) — subagent 1. Reuses the existing `exa_research_jobs` pipeline; does not call Exa directly.
5. [app/services/anthropic_client.py](../app/services/anthropic_client.py) — thin async wrapper. Prompt caching applied via `cache_control: {"type": "ephemeral"}` on system blocks.
6. [app/services/exa_client.py](../app/services/exa_client.py) — five-method Exa wrapper (search / contents / find_similar / answer / research). Auth header is `x-api-key`.
7. [app/services/gtm_initiatives.py](../app/services/gtm_initiatives.py) — initiative state machine.
8. [app/routers/internal/exa_jobs.py](../app/routers/internal/exa_jobs.py) — note the `_post_process_by_objective` dispatcher: when an exa_research_jobs row succeeds with `objective='strategic_context_research'`, it stamps the result_ref onto the matching gtm_initiative and transitions status. Inline, not polling.
9. [data/brands/capital-expansion/](../data/brands/capital-expansion/) — brand content the synthesizer reads. Read all 8 .md files plus `brand.json` and `README.md`. Voice-loyalty + anti-fabrication are non-negotiable rules in any synthesizer prompt.
10. [scripts/seed_dat_gtm_initiative.py](../scripts/seed_dat_gtm_initiative.py) — end-to-end exercise; the canonical reference for how an initiative gets stood up.

---

## 5. What's NOT built (subagents 3–7, plus open architectural calls)

### Subagents

3. **Channel & step materializer** — deterministic. Reads the strategy artifact's channel-mix + per-touch shape, INSERTs `business.channel_campaigns` + `business.channel_campaign_steps` rows. No LLM.
4. **Audience materializer** — pulls audience members from DEX (via `dex_client` or dex-mcp), upserts `business.recipients`, creates step memberships.
5. **Per-recipient creative author** — the heaviest. For each `(recipient × DM step)`, reads recipient data points + strategy frames + brand .md, generates copy + design directive per piece, validates against zone-binding / MediaBox invariants. Per-recipient bespoke is the brand's wedge — see §4 of the strategic-direction doc.
6. **Landing-page author** — per-step (or per-recipient if §9.5 resolves that way) landing-page configs on the brand's domain.
7. **Voice-agent configurator** — Vapi assistant per initiative. Persona inherits brand voice; routing manifest inherits partner contract (qualification rules, hours, partner phone for live transfer, partner email for callbacks).

### Open architectural decisions (Ben's calls)

From §9 of the strategic-direction doc — these affect downstream subagents:

- §9.1 Creative-in-data-model shape: per-recipient as N steps audience-size-1, vs. one step audience-size-N with `creative_ref` on each membership.
- §9.2 Voice-agent scope: per-initiative (Ben's stated preference) vs. per-brand.
- §9.3 Earmark expiry / reservation conflict policy (pre-payment soft holds).
- §9.4 Brand-context storage location: column on `business.brands` vs. separate table vs. supabase storage vs. repo files. **Currently we have repo files mirrored to `business.brand_content` — both, deliberately, for now.**
- §9.5 Per-recipient creative generation sync vs async during instantiation.

---

## 6. Where the loop is right now — V3 is the next concrete piece

The latest sandbox V2c output ([strategy_v2_iter3_validated.md](initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/strategy_v2_iter3_validated.md)) validates against the V2 schema but is **still semantically wrong** in three load-bearing ways:

### Issue 1 — V2 leaks literal-copy directives into a doc the per-recipient creative author should be writing

V2's `per_touch_direction[].headline_focus` and `body_focus` fields look like this:

> `headline_focus: "Name the 30-60 day broker payment gap for a 10-50 power-unit fleet in 6-10 declarative words."`

That reads like draft copy directives. It should be a *conceptual frame* — a theme/angle the per-recipient author renders into actual copy using the recipient's specific data points (DOT#, power_units, authority_granted_at, state, etc.). The literal-copy directive is the per-recipient author's job, not the strategist's.

### Issue 2 — V2 hardcodes touch counts + shape from brand-template defaults

The brand's `creative-directives.md` has a default sequence: postcard / letter / postcard at days 0/14/28 + 3 emails at days 3/17/35. V2 just copies that shape regardless of partner spend, audience size, or capital outlay cap.

The strategist should **reason about economics**: given audience_size, amount_paid_cents, max_capital_outlay_cents, and rough per-piece costs (postcard ~$1.50, letter ~$3.00, self-mailer ~$2.50, email negligible), choose the touch count and channel mix that fits. # of touches should be a model decision derived from `$ ÷ audience_size ÷ per-piece-cost`, not a hardcoded default.

### Issue 3 — V2 doesn't see actual audience members

The strategist gets the audience descriptor (the "true for everyone" summary) but no concrete member rows. It should see 5–10 sample members so it can ground its decisions in real attribute distributions.

### V3 plan (Ben has signed off on this direction)

One synthesizer (not split). Four upgrades:

1. **Pre-fetch audience member sample (5–10 rows)** via `dex_client` and pass into the user message.
2. **Force economic reasoning.** Add `audience_size`, `amount_paid_cents`, `max_capital_outlay_cents`, per-piece cost table. Force model to justify touch count + channel mix from these.
3. **Strip literal-copy fields.** Replace `headline_focus` / `body_focus` / `primary_capital_type` with `frame` (conceptual angle) + `assets_referenced` (which member attributes the per-recipient author should pull from).
4. **Allow segment splits.** Optional `audience_segments` array if model decides differential sequences fit (e.g. ≥20 power units gets 3 DM, <20 gets 2 DM). Default: no split.

The "warm intros, no shopping the deal" kind of brand-website prose belongs on the **landing page** and the **voice-agent system prompt** — long-form surfaces. A postcard headline + 60-word body cannot carry it. V3 frames will say things like `frame: "name the cash-flow situation in operator terms"` and let the per-recipient author render that with the recipient's specific data.

### Sandbox-driven workflow (already set up)

`scripts/sandbox_synthesize.py` (about to land in this commit; see §7) is the iteration loop. It loads the same six inputs the production synthesizer loads but swaps in an arbitrary system prompt + schema validator and writes the output to a timestamped sandbox path. Real inputs, swappable prompt. ~$0.30 per re-run on `claude-opus-4-7`. When V3 is dialed, bake `_SYSTEM_PROMPT_V3` into [app/services/strategy_synthesizer.py](../app/services/strategy_synthesizer.py), bump `_ACTIVE_SYSTEM_PROMPT`, update `_REQUIRED_FRONT_MATTER_KEYS`, ship as a PR.

### After V3 lands cleanly

The next directive is **subagents 3 + 4 (deterministic materializers)**. They're cheap to build: read the strategy artifact, INSERT rows. No LLM. After that, subagent 5 (per-recipient creative author) is the next big slice and depends on the §9.1 architectural call (creative-in-data-model shape).

---

## 7. Local-only state being committed in this PR

To make this handoff durable, the following are being added in the same commit as this doc:

- `docs/initiatives-archive/bbd9d9c3-c48e-4373-91f4-721775dca54e/*.md` — the archive of all artifacts from the DAT initiative run + V1 + V2 iterations.
- `scripts/sandbox_synthesize.py` — the sandbox runner used to iterate the synthesizer prompt without re-deploying.

After this commit lands, all of the work referenced in this doc is in main. No more "exists only in /tmp" or "exists only in another worktree."

---

## 8. Operational notes for the next agent

- **Doppler:** all scripts run via `doppler --project hq-x --config dev run -- ...`. There is no `DATABASE_URL`; use `HQX_DB_URL_POOLED`.
- **Anthropic billing:** synthesis calls are ~$0.30 each on `claude-opus-4-7`. Exa research calls are $0.45–$0.65 depending on instruction length. Sandbox iteration is cheap; iterate freely.
- **Auth between hq-x → DEX:** super-admin API key, sent as `Authorization: Bearer <key>`. Same header pattern as super-admin JWT. See [data-engine-x/app/auth/super_admin.py](../../data-engine-x/app/auth/super_admin.py).
- **Migrations:** UTC-timestamp prefix `YYYYMMDDTHHMMSS_<slug>.sql`. Apply via `doppler run -- psql "$HQX_DB_URL_POOLED" -f migrations/<file>.sql` for ad-hoc dev application. There is no automated migration runner in dev as of this writing.
- **DEX has its own Doppler project** (`data-engine-x`). Not the same as `hq-x`. Don't conflate.
- **The `data/initiatives/` path is gitignored** under `data/initiatives/*/`. Production runtime artifacts land there. To preserve specific artifacts in the repo, copy them into `docs/initiatives-archive/` (NOT gitignored) and reference from there. This is the pattern this doc is establishing.

---

## 9. Bottom line

**Done:** strategic-direction commit, audience reservations, Exa orchestration (both DBs), Capital Expansion brand + DB mirror, GTM-initiative slice 1 (subagents 1 + 2), DAT end-to-end exercise. All in main.

**Next:** V3 of the strategy synthesizer per §6 — one focused synthesizer prompt revision + a sandbox re-run + (assuming dial-in) a small PR baking V3 into production.

**After that:** subagents 3 + 4 (mechanical materializers), then subagent 5 (per-recipient creative author — the heaviest piece, depends on §9.1).

The platform substrate that already shipped (campaigns hierarchy, multi-step scheduler, Lob/EmailBison/Vapi adapters, hosted landing pages, customer webhooks, reconciliation crons) carries the campaign once it's instantiated. The work that remains is upstream of that — the synthesis + materialization pipeline that turns one paid contract into a fully-staged, per-recipient-bespoke campaign tree ready to fire.
