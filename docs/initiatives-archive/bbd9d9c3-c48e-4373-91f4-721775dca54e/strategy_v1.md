---
schema_version: 1
initiative_id: bbd9d9c3-c48e-4373-91f4-721775dca54e
generated_at: 2026-04-30T12:00:00Z
model: claude-sonnet-4-5

headline_offer: A warm intro to one transportation factor whose criteria already match a 90-day-old authority running 1–20 trucks.
core_thesis: Newly authorized for-hire carriers running 1 to 20 power units are in the exact stretch where net-30 to net-60 broker terms collide with diesel above five dollars, first-year insurance down payments, and zero patience from anyone who decides whether they get paid this Friday. They don't need a load board pitch and they don't need ten factors cold-calling them off the FMCSA feed — they need one introduction to a transportation factor whose criteria already match an authority this new and a fleet this size. Capital Expansion is a matchmaker, not a lender, so we earn nothing on the spread and have no reason to steer them toward a stickier contract than the one their situation actually calls for.
narrative_beats:
  - Your authority is 90 days old; brokers still pay on 30 to 60.
  - Most factors won't fit a fleet this new at this size — we know which ones do.
  - One warm intro to one transportation factor, not a list.
  - We don't earn on the spread, so we have no reason to push a stickier contract.
  - If we don't have a fit, we'll say so before we waste your Friday.
channel_mix:
  direct_mail:
    enabled: true
    touches:
      - touch_number: 1
        kind: postcard
        day_offset: 0
      - touch_number: 2
        kind: letter
        day_offset: 14
      - touch_number: 3
        kind: postcard
        day_offset: 28
  email:
    enabled: false
    touches: []
  voice_inbound:
    enabled: true
capital_outlay_plan:
  total_estimated_cents: 900000
  per_recipient_estimated_cents: 9000
personalization_variables:
  - name: legal_name
    how_to_pull: FMCSA new-carrier feed → carrier.legal_name
  - name: dba_name
    how_to_pull: FMCSA new-carrier feed → carrier.dba_name (fallback to legal_name)
  - name: usdot_number
    how_to_pull: FMCSA new-carrier feed → carrier.usdot
  - name: power_units
    how_to_pull: FMCSA new-carrier feed → carrier.power_units
  - name: physical_state
    how_to_pull: FMCSA new-carrier feed → carrier.physical_state
  - name: authority_grant_date
    how_to_pull: FMCSA new-carrier feed → carrier.mc_grant_date (used to compute days_since_authority)
  - name: days_since_authority
    how_to_pull: derived = today - authority_grant_date
  - name: recipient_match_url
    how_to_pull: /match/{usdot_number}
anti_framings:
  - Do not say "solutions," "frictionless," "seamless," or "concierge."
  - Do not say "unlock," "empower," "best-in-class," or "industry-leading."
  - Do not promise "fast pay" or "low fees" as the lead claim — operators treat these as table stakes and discount them.
  - Do not name a specific factor or partner; warm intros are private until the recipient asks for one.
  - Do not include clickable links or attachments in the mail piece beyond the personalized URL/QR — the audience is currently being phished by parties impersonating regulators.
  - Do not frame the operator as struggling, broke, or failing; they just got their authority and they're navigating a fragmented market.
---

# Strategy: warm intros for newly authorized carriers, 1–20 power units

## Why this audience, why this partner, why now

This audience is the cleanest cut of the trucking pain we already understand. Every recipient on the list has held for-hire authority for fewer than 90 days and operates between 1 and 20 power units. That single fact carries the entire pitch: brokers and shippers pay on 30 to 60 day terms, and a carrier this new does not have the receivables history a bank wants to see for a working-capital line. The math is already determined. They will either sit on invoices and bleed, or they will work with a transportation factor.

The trade-press and the operator forums are saying the same thing in plainer language. From an owner-operator thread this spring: "Cash flow is slower until brokers start paying." From another: "You'll want a cash flow buffer: Shippers and brokers often pay on 30–60 day terms, so even if you're booked solid, you'll need reserves for expenses." And from a carrier breaking down weekly fixed costs: "Fuel costs me more than $1200 a week. Insurance breaks down to about $350 a week a truck." None of that is a marketing fiction. It is the operator's own description of the gap between a delivered load and a deposited check.

DAT is the right partner because their existing customer base is exactly this carrier — small fleets, owner-operators, brokers' counterparties — and their own product family already includes a factoring offering inside the same workflow. The partner contract gives us 90 days and a per-recipient capital outlay ceiling that fits a three-touch direct-mail program against a 100-recipient prototype list. The why-now is concrete: diesel is high enough that the 30-to-60 day broker payment gap hurts more this quarter than it did last quarter, and a 90-day-old authority cannot wait it out.

## The narrative beats expanded

**Beat 1 — name the situation.** The recipient already knows their authority is new and their fleet is small. We say it back to them in one sentence so the rest of the piece earns the read.

**Beat 2 — name the mismatch.** Most factors won't fit an authority this new at this size. The operator has probably already gotten on the phone with one or two and been told to come back in six months, or been quoted terms that read like a 24% APR once you do the arithmetic. The forum language is direct: "Factoring companies want nothing more than to get their hooks into a company... it is very difficult to divorce yourself from one." We acknowledge that fear because dismissing it loses the reader.

**Beat 3 — the matchmaker stance.** We are not a lender. We are not a marketplace. We have done the work of mapping which transportation factors actually take 90-day-old authorities running 1–20 trucks, and we make one warm intro to one of them. Not a list of ten. Not a portal. One introduction with the recipient's situation already in the partner's inbox.

**Beat 4 — incentive transparency.** "We don't make money on the spread." That sentence is load-bearing because every other party in this market — brokers chasing factoring referral fees, factors offering kickbacks to load boards — has the opposite incentive. We say it plainly because the operator is already counting incentives in their head.

**Beat 5 — the out.** "If we don't have a fit, we'll say so." This is the operator-first pillar in one line. It also defuses the "lead-gen farm" suspicion that any unsolicited piece of mail will attract from this audience.

## Per-touch creative direction

**Touch 1 — postcard, day 0.** Headline names the situation in 6–10 words: state, fleet size, days since authority. Body is 60–100 words: name the broker-payment gap, name factoring as the right tool for the situation (not a line of credit, not an SBA), introduce Capital Expansion as a matchmaker, single CTA to the personalized URL or QR code. Imagery is a clean visual reference to the carrier's situation — not a stock smiling driver. The recommended capital type for every piece in this initiative is `factoring`. No second option. No mention of equipment finance even where adjacent.

**Touch 2 — letter, day 14.** Show the work. Explain in operator-to-operator language why a transportation factor is the right tool for a 90-day authority running this many trucks, why a bank line is not, and why a generic SMB factor that does not specialize in transportation will probably decline them on industry alone. This is the piece where specificity substitutes for track record — we have no volume to claim, so we earn the read by demonstrating the matching logic.

**Touch 3 — postcard, day 28.** Loss-aversion frame. The operator who waits another 60 days on broker payments and does not solve this is the operator who burns through their own savings paying drivers and fuel cards. Final CTA, same URL.

**Voice inbound.** When the recipient calls the number, the voice agent greets with "Capital Expansion," asks for the USDOT number to pull the situation already on file, confirms power units and authority date, validates the qualification rules from the partner contract, and either schedules the warm intro or says plainly that this isn't the right fit.

## What we explicitly avoid

We do not pitch a load board. DAT sells that; we do not. We do not promise "fast pay" or "low fees" as the lead claim — those are table stakes and the audience discounts them on sight. We do not name the specific factor partner in the mail piece; partner names are private until the warm intro is made by mutual agreement. We do not include any link or attachment beyond the single personalized match URL, because this audience is currently being targeted by phishing campaigns impersonating regulators, and any mail piece that looks like a sales funnel disguised as an FMCSA notice will be filed in the trash. We do not claim a track record we do not have. We are a new brand. We say so if asked. The targeting precision and the matching logic are what the partner is paying for, and they are what the recipient is reading the piece for.