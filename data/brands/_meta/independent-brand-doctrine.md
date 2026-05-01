# Independent-brand doctrine

This doc encodes the rules every owned-brand outreach surface must follow.
The brand presents as an **independent operator providing the value
directly** — never as an aggregator, marketplace, broker-of-brokers, or
partner-finder.

It is read by every creative-authoring subagent (master strategist,
per-recipient creative author, landing-page author, voice-agent
configurator). Mirrored into `business.brand_content` with
`brand_id IS NULL` and `content_key='_meta:independent-brand-doctrine'`
so MAGS agents without filesystem access can still load it.

---

## 1. The illusion contract

The brand acts as the operator, not the matchmaker. Specifically:

- The brand presents as an **independent operator** providing the
  value (cash advance / financing / capital / equipment / etc.)
  directly to the recipient.
- The brand **never** presents as an aggregator, marketplace,
  broker-of-brokers, partner-finder, comparison engine, or
  "we connect you with the right people" service on its primary
  surfaces.
- The brand's **persona is the offering**. When a recipient lands on
  the postcard, opens the email, or calls the voice agent's number,
  what they encounter looks like the company that does the thing.

The actual fulfillment routes through the demand-side partner. That
implementation detail is not relevant to the recipient at the surfaces
where the illusion is binding (postcard, email subject/preview,
landing-page hero).

---

## 2. Channel-tier framing rules

Each surface has a different dwell time, different attention budget,
and different latitude for partner-bridge language. Authors **must**
respect these tiers.

### 2.1 Postcard (shallowest, lowest dwell time)

**The brand IS the offering.** The headline is the value prop.

- ✅ "Need cash fast?"
- ✅ "Same-day funding for established carriers."
- ✅ "Capital for fleets that just hit 90-day authority."
- ❌ "We connect you with our network of factoring partners."
- ❌ "Let us match you with the right capital provider."
- ❌ "Compare offers from leading lenders."

No partner talk. No brand-bridge language. No mention of marketplaces,
networks, or matching. The headline IS the value-prop.

### 2.2 Letter (medium dwell, per-recipient leverage allowed)

The brand still speaks as the operator, **with recipient-specific framing**
derived from Exa / Claygent / DEX research signals.

- ✅ "We noticed your authority just hit 90 days active — congrats. Most
  carriers at your stage need working capital before they need
  equipment finance."
- ✅ "Saw your fleet grew from 8 to 12 power units this quarter."
- ✅ "12 power units in a 50-mile radius means net-60 broker terms are
  about to become a problem."
- ❌ Anything that breaks the operator persona ("our partners say...",
  "we'll get you in front of...").

The letter can lean hard on per-recipient signals because the recipient
is **already** committed enough to read past line one. But the brand is
still the operator.

### 2.3 Landing page (deeper surface, partner-bridge language permitted as secondary CTA)

Primary copy still acts as the offering. **Secondary** CTA can include
partner-bridge framing.

- ✅ Primary hero: "Funding for carriers that grew faster than their
  cash flow."
- ✅ Below-fold: "Want us to connect you with a specialist who can
  quote this today?"
- ✅ Footer or detail sections: "We partner with vetted operators in
  [vertical]."
- ❌ Hero copy talking about partners or networks.

The landing page is allowed to bridge **after** it has the recipient's
attention. Not before.

### 2.4 Voice agent (deepest, conversational, partner-bridge explicit)

The voice agent is the only surface where partner-bridge language is
**default**, because by the time someone called, they've crossed the
trust threshold and want to be routed.

- ✅ "We work with partners who specialize in [vertical_from_exa].
  They have experience with [pain_signal_from_exa]. Want me to
  connect you?"
- ✅ "Let me get you on with someone who can quote this today —
  what's the best number?"
- ✅ "Based on what you've told me, I'd route you to our team that
  handles [specific situation]."

The voice agent can **explicitly** acknowledge the partner network
because that is the moment of routing.

### 2.5 Email (between letter and landing)

- Subject + preview line: brand acts as the offering. NO partner talk.
- Body: per-recipient lean OK (like the letter). Partner-bridge mention
  permitted as a soft body CTA only ("happy to connect you with a
  specialist if useful — reply 'yes'").
- ❌ Subject lines that say "match," "network," "marketplace," or any
  variant.

---

## 3. Anti-rules (literal prohibitions across all surfaces)

These never appear — postcard through voice agent — without explicit
operator override on the initiative:

- No "**marketplace**," "**network of partners**," "**we connect
  you with...**," or similar meta-positioning on **print or email
  subject lines**.
- No claims of **brand age**, **founding year**, "**since 19XX**,"
  "**established**," or any patina-related claim. The brand is new
  and openly so. Patina is not the wedge.
- No claims of **partner-side capability the brand can't fulfill in
  persona**. The brand should always sound like it CAN do the thing —
  even if the actual fulfillment routes through the partner.
- No comparison-shopping language: "**get quotes from**," "**compare
  offers**," "**see what you qualify for from multiple lenders**."
  This breaks the illusion that the brand is the operator.

If a draft pulls from these patterns, the verdict subagent must
return `ship: false` with `area: "doctrine_violation"` and a concrete
quote.

---

## 4. Per-recipient detail provenance

Personalization comes from:

- **Exa / Claygent research output** stored against the audience or
  the partner's research blob. This is the source of "what's hurting
  this audience right now" framings.
- **DEX recipient data points** (DOT#, power_units, MC#,
  authority_granted_at, BBL, state of operation, etc., depending on
  the audience's underlying dataset). This is the source of "facts
  specific to this recipient."

Personalization does **not** come from:

- Partner-supplied per-recipient text. The partner provides only
  routing config (phone, hours, qualification rules, intro email).
- Generic templates filled with the recipient's name. Per-recipient
  bespoke is the wedge — name-mail-merge is the segmented direct-mail
  shop, which is what we're explicitly not.

If any creative step would require partner input per-recipient, the
throughput goal fails. The pipeline assumes the partner is hands-off
post-payment except for the voice-agent live-transfer on the inbound
side.

---

## 5. Voice-agent partner-bridge wording template

This is the canonical template the voice-agent-configurator (and any
agent that emits voice-agent system prompts in the future) renders
into the assistant prompt:

> "We work with partners who specialize in `<vertical_from_exa>`.
> They have experience with `<pain_signal_from_exa>`. Want me to
> connect you?"

Variables sourced from:

- `vertical_from_exa` — primary vertical descriptor from the
  partner-research Exa run.
- `pain_signal_from_exa` — the specific pain framing the
  strategic-context Exa run identified for this audience.

The voice agent **may** vary the wording. It **must not** drop the
"partners who specialize" framing on the voice surface — that's the
explicit-bridge moment.

---

## 6. Mode of failure — what a violation looks like

A doctrine violation looks like one of:

- A postcard headline that says "we connect you with" or "match you with."
- A letter that drops a partner name in the salutation ("Hi from
  Acme Capital and our partner Specialty Funding Inc...").
- A landing-page hero that names the partner before the offering.
- An email subject line: "Compare 3 funding options."
- A voice-agent opening that immediately lists multiple partners.

The verdict subagents must catch these. The operator doctrine in
`data/orgs/acq-eng/doctrine.md` adds margin / outlay / model-tier
constraints on top of these illusion-contract constraints.
