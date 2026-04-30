# Strategic direction — owned-brand lead-gen

Date: 2026-04-30
Author: Ben Crane
Status: Active direction. Supersedes the self-serve DMaaS positioning implied throughout
the directive history and the existing `STATE_OF_HQ_X.md` snapshot.

---

## 1. The shift

The hq-x platform substrate is not changing. What's changing is **who the customer is.**

Old framing: external companies (factoring co's, insurance agencies, wholesale RE, etc.)
sign up to run their own direct-mail / multi-channel outreach via hq-x as a self-serve
DMaaS API.

New framing: **Ben operates the platform internally.** External companies become
*demand-side partners* who pay for 90-day exclusive flow of qualified leads produced by
hq-x running outreach **under brands Ben owns**, against audiences Ben slices from
data-engine-x.

The platform substrate (campaigns hierarchy, multi-step scheduler, analytics emit, Lob /
EmailBison / Vapi adapters, hosted landing pages, customer webhooks, reconciliation
crons) is now **internal tooling for the lead-gen business**, not a SaaS product surface
sold to external orgs.

---

## 2. The model

### 2.1 The offering

A demand-side partner pays Ben to reserve an audience spec for 90 days. During that
window, hq-x runs multi-channel outreach (direct mail + email; voice-agent inbound) to
the audience under one of Ben's brands. Inbound qualified leads are live-transferred to
the partner's phone during their hours of operation, with scheduled-callback fallback
outside hours.

Pricing structure: roughly **$25K up front per audience spec for 90 days**, plus either
a per-lead-transfer fee or a percentage of residuals, depending on the partner vertical.
Specifics vary per market — the price is elastic; sharper specs command more. Floor is
"covers cost + margin."

After 90 days, Ben decides whether to offer the partner a re-reservation. Re-reservation
is not the partner's right.

### 2.2 What the partner pays for

A reservation against an **audience spec**. A spec is a set of attributes + values
(plus optional descriptive text) defining the audience: e.g. "FMCSA motor carriers
with 10–20 power units, USA-wide, as of data date X." All members of the audience
who match those attributes count toward the partner's reservation. Leads outside the
spec are not the partner's leads (and not their cost).

Pricing is **per spec, not per lead.** Lead-quality disputes are resolved by spec
adherence: every lead must meet the declared attribute values as of the stated data date.

### 2.3 What the partner provides

- Phone number for live transfer
- Hours of operation
- Email address for written intros / scheduled-callback follow-ups
- Qualification minimums (which become attribute thresholds on the spec)

That's it. They do not provide creative, brand, copy, audience selection, or run any
part of the outreach.

### 2.4 What Ben provides

- The brand under which outreach runs
- The audience spec (selected, sliced, locked at reservation)
- The full multi-channel sequence
- Per-recipient creative for every direct-mail piece in the sequence
- Email copy
- Landing page on the brand's domain
- A Vapi voice agent that handles inbound calls and routes leads by code
  (e.g. DOT# for FMCSA carriers, BBL for NYC RE) to the partner during their hours

---

## 3. The independent brand

Each gtm initiative runs under a brand Ben owns. Brand examples Ben has used in
discussion: **LicensedToHaul** (FMCSA truckers), **CapitalRequests**, **CapitalExpansion**.

The brand is **fungible / swappable**. It is an *asset* in the offering, not a constraint.
The partner is reaching audience members *through* an apparently-independent brand they
do not operate.

The illusion of independence is the offering — the partner should feel like they wish
they ran this brand themselves. Brand age / patina is **not relevant**: a brand-new
brand is fine, and Ben can openly say the brand is new. What is relevant is targeting
precision and routing — that is what the partner is paying for.

A brand carries:
- A domain (registered + DNS) — Vercel-hosted marketing site
- Brand-context `.md` files (positioning, voice, audience-pain framing) used by LLMs
  authoring per-recipient creative
- A `landing_page_domain_config` (Entri Power → hq-x backend)
- A `dub_domain_config` (Dub-managed link host)
- An email-from identity
- A voice-agent persona

Brand bootstrap is an operational pipeline. It runs fast enough to instantiate on-close.
There is no business decision to make on "speculative pre-staging vs on-close" — the
question collapses to "make the pipeline fast."

---

## 4. The design choice: per-recipient bespoke creative

Each member of the audience spec receives **bespoke creative on every direct-mail
piece in the sequence** — postcard at touch 1, letter at touch 2, postcard at touch
3, etc. Each piece is generated specifically for that recipient using their data
points (e.g. for an FMCSA carrier: power-unit count, fleet age, MC# active-since,
state of operation, whatever the spec attributes expose).

This is **not** the universal-scaffold + token-substitution pattern. Cluster-based
segmentation is not a fallback to per-recipient. They are different products with
different value propositions:

| dimension | per-recipient (chosen) | cluster (not the model) |
|---|---|---|
| value prop | written for THIS recipient | written for people like them — same as the scaffold model already rejected |
| LLM contract | bespoke gen per recipient, must succeed N times | small N gens, runtime substitution |
| QA model | automated invariants + sampling | human review of each variant once |
| cost curve | linear in recipient count | fixed in N |
| failure radius | bad gen affects 1 | bad gen pollutes a whole cluster |

The pitch differential — "we write a postcard specifically for the recipient" — is the
part that makes this not just another segmented direct-mail shop. Cluster IS the
segmented direct-mail shop.

Quality is enforced by **automated invariant checks** (extending the dmaas_designs /
zone-binding / MediaBox work already shipped) plus **sampled audit**. Pixel-by-pixel
human review of N=5,000 pieces is not the model.

Email copy is **more standardized** — lower stakes per recipient, cheaper iteration.
Per-recipient bespoke applies primarily to direct mail.

### 4.1 Provider implications

Lob does not charge per campaign. Per-recipient creative implies one Lob campaign per
recipient (audience size 1) — at scale, ~5,000 campaigns per gtm initiative.

Open question (with Lob): can their API and rate limits accommodate that pattern. If
yes, build proceeds against Lob. If no, **the answer is to evaluate other direct-mail
providers** (e.g. PostGrid) until one is found that can. Per-recipient is the architecture;
the provider is a knob underneath it. Provider abstraction across direct-mail providers
is a build target either way.

The same per-recipient pattern applies to EmailBison.

---

## 5. The full lifecycle

### Phase 0 — pre-outreach (Ben's own outreach to demand-side partners)

This is **separate from the gtm-initiative outreach** structurally. Ben's own outreach
is its own organization → brand → campaign tree in the platform — i.e. one tenant among
many. It shares infrastructure with the lead-gen pipeline; it is not the lead-gen
pipeline.

Inputs: companies in data-engine-x (factoring, insurance, wholesale RE, etc.). Some
tagged with vertical/specialty. Most tagged only with industry.

Process:
1. Select target companies + people at those companies.
2. LLM consults the dex manifest (or queries dex via MCP) and proposes an **audience
   spec relevant to that demand-side prospect.** Each prospect can get a *unique* spec —
   the underlying data is sliceable infinitely. Specs are *earmarked* (soft hold)
   against the prospect for the duration of the conversation.
3. Outreach to the prospect: email + optionally a Remotion-rendered personalized video
   ("loom-style"). The exact spec may or may not be mentioned in the email — could
   surface as a statistic only. CTA: book a meeting.

For Ben's own prospect outreach, copy can use a single emailbison campaign with
template variables. The per-recipient bespoke pattern applies to *audience-side*
outreach, not to prospect-side outreach.

### Phase 1 — meeting booked

Ben sends the prospect either a video or a link to a presentation of the earmarked
audience spec. Spec presentation: count, sample rows, demographic summary. Brand-neutral.

### Phase 2 — sales call

Live work, LLM/MCP-assisted:
- Pull up the earmarked spec.
- Prospect can modify — exclude geo, raise/lower thresholds, add filters, "build a new
  one" entirely. Modifications resolve through an MCP-driven session against dex.
- Already-reserved specs are not shown.
- If a spec is too large, it is spliced down. If a spec covers some members already
  partially contacted under a different reservation, the offering is constrained to
  the still-available slice.
- Pricing: flat $25K per 90-day reservation for full coverage of the spec's members
  (subject to internal cap on capital outlay per initiative).

### Phase 3 — payment

The partner pays. The outputs of payment:
- A `demand_side_partners` record
- A `partner_contracts` record with terms, qualification rules, hours, phone, intro
  email, commercial structure
- An `audience_spec_reservations` record locking the spec to the partner for 90 days

At this point, the four input materials for instantiation are all available:

#### Input #1 — Brand
The brand under which outreach will run, either pre-existing or just-created. Either
way it has a domain, marketing site, brand-context `.md` files, theme config, etc.
The website can be iterated between reservation lockdown and campaign launch.

#### Input #2 — Audience spec + member data points
The locked spec, plus per-member data points pulled from dex (manufacturing co ≠
logistics co; details per recipient that feed creative authoring). Plus member count
(may be hard-capped per initiative).

#### Input #3 — Partner research
Descriptive material on the demand-side partner: what they do, what they offer, how
they position themselves. Sourced from their website + exa, optionally validated by
the partner post-payment. Surfaces in creative as e.g. "we have a partner with deep
expertise in X who has done Y before…"

#### Input #4 — Pricing terms / amount / duration
The contract structure. Influences # of mailers in the sequence (capital outlay
budget per initiative). Internal guardrails cap maximum spend per initiative.

### Phase 4 — instantiation (post-payment)

A managed agent — pattern-precedented by `managed-agents-x`'s `dmaas-scaffold-author` —
takes the four inputs and instantiates the full multi-channel campaign tree.

**Order of operations** (Ben specified, dependency-correct):
1. **Strategy**: channels (direct_mail, email), # of touches, mailer type per touch
   (postcard / letter / self-mailer), step delays, capital outlay plan within
   guardrails.
2. **Step sequence** materialized: `business.channel_campaigns` per channel,
   `business.channel_campaign_steps` per touch.
3. **Audience materialization**: `business.recipients` upserted from spec, memberships
   created on each step.
4. **Per-recipient creative**: copy + design generated for each recipient × each
   direct-mail step. Validated against zone-binding / MediaBox invariants. (Email
   copy authored at template level with per-recipient substitution map.)
5. **Landing pages**: per-step landing_page_config on the brand's domain, paired to
   the mailer that drives traffic to it.
6. **Voice agent**: a Vapi assistant instantiated per gtm initiative (preferred when
   economical; per-brand fallback otherwise — Ben has stated per-initiative preference).
   Persona inherits the brand voice authored in step 4. Routing manifest inherits the
   partner contract: qualification rules, hours of operation, partner phone for live
   transfer, partner email for callbacks. Recipient lookup keyed by code (DOT# for
   FMCSA, BBL for NYC RE, etc.) so an inbound caller is identified back to the
   member of the spec.

After instantiation: campaign tree exists in `draft` / `pending` status. No outreach
has fired. Voice agent exists but is not yet receiving calls.

#### Launch factor vs instantiation factor

Some inputs gate the **launch** of the campaign, not its **instantiation**:
- Partner phone number (must be live to live-transfer to)
- Partner intro email (must be valid)
- Partner hours-of-operation config
- Brand domain DNS + TLS verification (Entri)
- Voice agent assistant healthy

These are *launch factors*. The campaign tree can be fully built without them. They
gate the flip from "ready to launch" to "active." When the predicate is satisfied,
launch fires the existing async activation pipeline.

### Phase 5 — engine running (90-day window)

Existing platform substrate carries it:
- Lob mints pieces per-recipient; webhooks land per piece event; analytics six-tuple
  rolls up.
- Dub records clicks; landing pages render with brand theme + recipient personalization;
  page submissions hit `landing_page_submissions`.
- EmailBison runs the email sequence.
- Multi-step scheduler with durable `wait.for(delay_days)` advances to step N+1 when
  step N completes.
- Reconciliation crons (stale jobs, Lob piece reconciliation, Dub click drift, webhook
  replay backstop, customer webhook delivery sweep) run nightly / sub-hourly.
- Voice agent receives inbound calls. Identifies the caller's spec membership by code.
  Validates against partner qualification rules. If qualified + in-hours: live transfer
  to partner phone. If qualified + out-of-hours: scheduled callback. If unqualified:
  handled per partner-contract policy.
- Customer-facing analytics endpoints surface the funnel internally to Ben (the
  customer-of-the-platform here is Ben, not the partner). Partner-facing reporting is
  a digest email or internal-portal page (lowest-friction shape).

### Phase 6 — 90-day close

At end of window, Ben evaluates whether to offer the partner a re-reservation. The
audience spec returns to the available pool unless re-reserved.

---

## 6. Decision discipline (this is not optional)

**Not pitch-testing.** Pitch responses from prospective partners are low-signal because
prospects conditionalize ("sure, if you can also do X and Y"). Price is elastic; if
the engine produces, the price covers cost. Different markets pay different amounts
based on spec sharpness.

**Not running a self-funded pilot.** There is no internal demand-side. Leads have
nowhere to live-transfer to without a contracted partner. The voice-agent transfer
destination is the partner's phone; there is no internal substitute.

**Build the engine optimized for throughput.** Selling and engine-readiness are
coupled. Once the engine is ready, the pitch is the engine.

"Throughput" = number of `(audience-spec → brand → multichannel sequence → live
transfer)` initiatives Ben can run in parallel with minimal per-initiative manual
lift. The build target is to drive the per-initiative operator surface as close to a
single command as possible: "given audience spec ID + partner contract ID, instantiate
everything and launch."

---

## 7. What this changes about platform priorities

### Drops (no longer priorities)

From `STATE_OF_HQ_X.md` §5 (gaps), the following are now non-priorities:

- §5.1 — Customer-facing frontend (Ben is the customer; operator UI only)
- §5.4 — Customer self-serve onboarding (no external customers signing up)
- §5.3 — Billing / metering / usage tracking for external orgs (Ben's own
  cost accounting is operational, not customer-billing)
- Customer webhook subscriptions for outside partners (internal use is fine)
- §5.5 — Voice/SMS step+recipient wiring is not a near-term priority unless
  voice/SMS becomes part of an initiative sequence (current model uses voice
  for *inbound* via the per-initiative agent, not for outbound steps)
- §5.8 — Email pipeline as a *DMaaS-customer* surface (email is internal to Ben's
  own initiatives)

### Adds (new priorities)

The build now centers on:

- **Per-recipient creative generator** with invariant checks (extends dmaas_designs /
  zone-binding work)
- **Brand bootstrap pipeline** (brand `.md` → brand row + domain + Vercel site +
  email-from + voice-agent persona shell)
- **Strategic prompt-pack**, versioned, with eval cases — the audience-pain →
  brand-promise → CTA framework
- **Demand-side partner contract integration** (qualification rules per partner,
  voice-agent live-transfer to partner phone during hours, scheduled-callback fallback)
- **Provider abstraction** across direct-mail providers (Lob primary; PostGrid eval in
  parallel pending Lob's per-campaign-volume answer)
- **Operator-launch surface** — internal CLI or admin panel; one command takes
  audience-spec + partner-contract config and kicks off instantiation
- **Per-initiative observability** — operator dashboard showing all in-flight
  initiatives' funnels at-a-glance, QA-by-exception
- **Managed agent for instantiation** — wraps strategy / creative / landing-page /
  voice-agent authoring; subagents per concern
- **dex manifest** — what's available to slice, what's earmarked, what's reserved;
  the seam between data-engine-x and the platform

---

## 8. New objects / concepts in the data model

These do not exist today and must be designed:

- `demand_side_partners` — partner record (id, name, primary_contact, primary_phone,
  hours_of_operation_config, intro_email)
- `partner_contracts` — pricing_model, amount_cents, duration_days, max_capital_outlay_cents,
  terms_blob, qualification_rules
- `audience_spec_reservations` — contract_id, audience_spec_id, reserved_member_cap,
  exclusivity_window_start/end, status
- `audience_spec_earmarks` — soft holds during prospecting (prospect_id, audience_spec_id,
  earmarked_at, expires_at)
- `voice_agent_instances` — campaign_id, vapi_assistant_id, persona_config,
  routing_manifest
- `partner_research` — partner_id, source ('exa', 'website', 'manual', 'client_validated'),
  descriptive_blob, validated_at
- `step_sequence_templates` — versioned, per-vertical
- `prompt_pack_versions` — versioned, with eval cases
- Brand-context storage — markdown blobs accessible to the managed agent (location TBD:
  column on `business.brands`, separate `brand_content` table, supabase storage, etc.)
- New campaign status `ready_to_launch` between `draft` and `active`
- Launch-readiness predicate

And on the surface side:

- `POST /api/v1/initiatives` (instantiation, async, returns 202 + job_id)
- `POST /api/v1/initiatives/{campaign_id}/launch` (validates predicate, fires existing
  activation pipeline)
- `hq-x-mcp` — wraps the service layer for managed-agent consumption (does not exist)

---

## 9. Open architectural decisions (Ben's call, not recommended on)

These are real forks the build hits and that Ben decides.

### 9.1 Creative-in-data-model shape

Per-recipient bespoke creative can live in the data model two ways:

(A) **Step audience size 1, N steps per recipient.** 5K recipients × 2 mailers = 10K
direct-mail steps in one channel_campaign. Each step → its own Lob campaign. Membership
always 1.

(B) **Step audience size N, `creative_ref` moves to membership.** 1 step per touch
(postcard touch at day 0, letter touch at day 14). Step has 5K members. Each *member*
carries its own `creative_ref`. Lob handles per-piece creative variation via the
upload payload.

Differences that bite:
- Multi-step scheduler: (A) makes step ordering per-recipient; existing scheduler
  assumes step N+1 runs after step N at channel_campaign level — breaks. (B) unaffected.
- Activation API surface: (A) = touches × recipients Lob calls; (B) = touches Lob calls
  with per-recipient creative payloads in the upload.
- Audience edits: (A) edit = step add/delete; (B) edit = membership add/delete (existing
  mechanic).
- Step-level analytics: (A) rollups degenerate (count=1); (B) rollups aggregate naturally.
- Migration cost: (A) reshape multi-step scheduler; (B) one column add + generator
  writes per-membership rows + Lob upload reads per-membership creative.

### 9.2 Voice agent scope

Per-initiative (Ben's stated preference) vs per-brand. Per-initiative makes the routing
manifest scope smaller, isolates contract terms cleanly, but creates more Vapi assistant
instances. Per-brand reuses one agent across initiatives sharing the same brand at the
cost of more complex routing logic per call.

### 9.3 Earmark expiry / reservation conflict

When prospect A is earmarked an audience and prospect B comes through 3 days later
for an overlapping spec, what is the policy? Earmark expiry duration (probably tied
to prospect stage). What happens when overlapping earmarks resolve to conflicting
reservations.

### 9.4 Brand context storage location

Where do brand `.md` files live: column on `business.brands`, separate `brand_content`
table, supabase storage objects, repo files synced to the managed-agent runtime, etc.

### 9.5 Per-recipient creative generation: sync vs async during instantiation

Generation of K bespoke creatives is the longest pole in instantiation. Whether
instantiation returns once the structural tree exists (with creative jobs queued) or
waits for all creative to complete invariant checks. Affects the `draft` →
`ready_to_launch` flip semantics.

---

## 10. Bottom line

The platform is the same. The customer is different. The customer is **Ben**,
operating brands he owns, selling 90-day audience-spec reservations to demand-side
partners who pay for qualified live-transferred leads.

The engine is what's being built, optimized for throughput. There is no pre-engine
validation step — selling and engine-readiness are coupled. When the engine is ready,
the pitch is the engine.
