---
schema_version: 2
initiative_id: bbd9d9c3-c48e-4373-91f4-721775dca54e
generated_at: 2026-05-01T01:07:19.062598+00:00
model: claude-opus-4-7

headline_offer: If you're running 10 to 50 power units on net-30 to net-60 broker terms, we know which transportation factor funds your situation — and we make the warm intro.

per_touch_direction:
  - touch_number: 1
    channel: direct_mail
    kind: postcard
    day_offset: 0
    role: name the situation
    headline_focus: Name the cash-flow gap between dispatch and broker payment in 6-10 declarative words; reference fleet-size band where data permits.
    body_focus: One sentence names the net-30/60 gap. One sentence says cold-shopping factors wastes weeks. One sentence says we already know which transportation factor funds 10-50 power-unit fleets. One sentence: scan the QR.
    primary_capital_type: factoring
  - touch_number: 2
    channel: direct_mail
    kind: letter
    day_offset: 14
    role: deepen — show the matching logic
    headline_focus: Frame why one transportation factor is right for this fleet and most are not; lead with specificity, not speed.
    body_focus: Walk through the underwriting fit logic for a 10-50 power-unit carrier (broker mix, AR aging, same-day funding criteria). Contrast with the generic "shop it to ten desks" path. Reinforce no spread, no kickback. CTA: same QR or call.
    primary_capital_type: factoring
  - touch_number: 3
    channel: direct_mail
    kind: postcard
    day_offset: 28
    role: close — loss-aversion / last call
    headline_focus: Name what staying on the wrong product costs over the next quarter given diesel and rate volatility.
    body_focus: One sentence: diesel cost surged and broker terms didn't shorten. One sentence: the wrong factor (or no factor) compounds the gap weekly. One sentence: we still have the match queued. Final CTA.
    primary_capital_type: factoring
  - touch_number: 4
    channel: email
    kind: email
    day_offset: 3
    role: name the situation (email echo of touch 1)
    headline_focus: Subject 4-8 words naming the net-60 gap or fleet-size band.
    body_focus: Reference the postcard. Longer version of why a transportation factor — not a bank LOC, not a generalist factor — fits a 10-50 power-unit carrier. One ask.
    primary_capital_type: factoring
  - touch_number: 5
    channel: email
    kind: email
    day_offset: 17
    role: deepen — operator-language scenario
    headline_focus: Subject names a concrete operator scenario (e.g., payroll Friday, broker on net-60).
    body_focus: One paragraph in operator voice walking the situation. Note the brand earns nothing on the spread. One ask.
    primary_capital_type: factoring
  - touch_number: 6
    channel: email
    kind: email
    day_offset: 35
    role: close — last touch, deprioritize offer
    headline_focus: Subject signals last email; offer to stop sending.
    body_focus: Plainly say this is the last note unless the operator says otherwise. Restate the match is queued. One ask.
    primary_capital_type: factoring
  - touch_number: 7
    channel: voice_inbound
    role: Inbound handler for operators calling the number on the mailers — greet as Capital Expansion, ask for DOT#, confirm the fleet-size and broker-terms situation referenced on the piece, validate qualification (10-50 power units), live-transfer to the partner with context if qualified and in-hours, schedule callback if out-of-hours, decline plainly if unqualified.

hook_bank:
  audience_pain_phrases:
    - "Cash flow is slower until brokers start paying."
    - "Shippers and brokers often pay on 30–60 day terms, so even if you're booked solid, you'll need reserves for expenses."
    - "Long Payment Cycles - Most brokers and shippers pay invoices on 30, 45, or even 60-day terms... this gap puts tremendous strain on cash flow."
    - "Load prices simply haven't gone up while all other expenses have."
    - "Fuel costs me more than $1200 a week. Insurance breaks down to about $350 a week a truck."
    - "Insurance down payments and setup costs under your own authority are a big hit early."
    - "Rates also swing a lot, so one month can feel great, and the next feels like survival mode."
    - "Factoring companies want nothing more than to get their hooks into a company... it is very difficult to divorce yourself from one."
    - "You're running an office on wheels."
    - "You need a minimum of 3–6 months of expenses banked."
  why_now_hooks:
    - Diesel prices surged in early 2026 with regional diesel reported above $5/gal, widening the gap between dispatch and broker payment.
    - Spot and contract rates have been volatile and largely flat-to-modest growth through 2025–2026, leaving carriers with limited revenue upside to absorb cost shocks.
    - FMCSA compliance and ELD enforcement attention increased in the last 12 months, raising audit and paperwork burden on small carriers.
    - Double brokering and ghost-broker fraud rose visibly in 2026, increasing carrier appetite for vetted partners over cold load-board shopping.
    - Industry guides confirm broker/shipper terms run 30, 45, or 60 days as the current baseline, not the exception.
  partner_proof_atoms:
    - DAT operates the largest on-demand freight marketplace in North America with 1 million daily load posts and 258 million annual load posts (Freight Focus 2024).
    - 110,000+ carrier subscribers active on DAT's network (Freight Focus 2024).
    - $869 billion in real freight transactions routed through DAT's marketplace since 2012 (Freight Focus 2024).
    - DAT has been a freight-data provider since 1978, headquartered in Portland, Oregon.
    - DAT introduced the Carrier Management Suite on October 16, 2025, integrating carrier vetting (authority, insurance, safety) directly into DAT One.
    - DAT announced acquisition of the Convoy Platform from Flexport on July 28, 2025, adding automated freight-matching, QuickPay, and ML-driven fraud prevention.
    - DAT One carrier plans start at $54/month (Standard) and tier up through Enhanced $119, Pro $169, Select $239, Office $329.

anti_framings:
  - Do not use "solutions," "best-in-class," "world-class," "industry-leading," "innovative," "cutting-edge," "frictionless," "seamless," "concierge," "empower," "unlock potential," or "leverage" as a verb.
  - No exclamation points, no emojis, no "reach out" — say "tell us."
  - Do not claim a Capital Expansion track record, customer count, dollar volume, time-to-close average, or close rate — none exist yet.
  - Do not name a specific lender or factoring partner in copy; partner relationships are private.
  - Do not pitch more than one capital type per piece; this is a factoring sequence.
  - Do not promise speed alone ("get paid fast," "quick pay") as the lead — operators treat that as table-stakes and discount it.
  - Do not use cold-call or phishing-adjacent patterns: no unsolicited link-only emails, no urgency-trap subject lines.
  - Do not frame the operator as struggling or failing; they are navigating a fragmented capital market.
  - Do not cite DAT pricing, scale claims, or Convoy/Carrier Management Suite events without grounding to the specific numbers and dates in the proof atoms.
  - Do not imply Capital Expansion earns on the spread or steers to higher-cost product — the matchmaker stance is load-bearing.

capital_outlay_plan:
  total_estimated_cents: 900000
  per_recipient_estimated_cents: 1800
  notes: Assumes ~500 recipient fleets in the 10-50 power-unit band; three direct-mail touches per recipient (postcard ~$0.50, letter ~$0.80, postcard ~$0.50 fully loaded) ≈ $1.80/recipient; total fits under the $10,000 partner cap.
---