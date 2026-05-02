---
schema_version: 2
initiative_id: "bbd9d9c3-c48e-4373-91f4-721775dca54e"
generated_at: "2026-05-01T01:11:00.005402+00:00"
model: "claude-opus-4-7"

headline_offer: "If you're running 10 to 50 power units and brokers are paying you on net-30 to net-60, we know which transportation factor funds your situation — one warm intro, no shopping the deal."

per_touch_direction:
  - touch_number: 1
    channel: "direct_mail"
    kind: "postcard"
    day_offset: 0
    role: "name the situation"
    headline_focus: "Name the net-30/60 broker-pay gap against weekly fuel and payroll for a 10-50 truck fleet."
    body_focus: "One operator-to-operator sentence that the cash gap is the situation. One sentence that we are a matchmaker, not a lender, and do not earn on the spread. One sentence that transportation factors do same-day funding. CTA: scan QR to a per-recipient match page."
    primary_capital_type: "factoring"
  - touch_number: 2
    channel: "email"
    kind: "email"
    day_offset: 3
    role: "longer version of the postcard"
    headline_focus: "Subject line names factoring for a fleet at the recipient's power-unit count."
    body_focus: "Reference the postcard. Explain why a bank LOC is the wrong first call for a carrier on net-60 broker terms and why a transportation factor is the right tool. Name one specific feature class (same-day funding, broker credit checks) without naming a partner. One ask: tell us your situation."
    primary_capital_type: "factoring"
  - touch_number: 3
    channel: "direct_mail"
    kind: "letter"
    day_offset: 14
    role: "show the work on the match"
    headline_focus: "State the matching logic for a 10-50 truck carrier on net-30 to net-60 terms."
    body_focus: "Walk through how transportation factors differ from general SMB factors and from bank lines. Address the sticky-contract fear directly: we route to partners whose terms a peer would actually accept. Address fraud/double-broker exposure as why broker credit checks matter. CTA: QR or a callback number."
    primary_capital_type: "factoring"
  - touch_number: 4
    channel: "email"
    kind: "email"
    day_offset: 17
    role: "deepen with a why-now"
    headline_focus: "Subject line ties the diesel spike to the net-60 pay gap."
    body_focus: "One paragraph: diesel jumped in 2026 while broker pay terms did not shorten; that gap got harder to cover. One paragraph: the right factor for a fleet this size is not the one a broker shops to ten desks. One ask."
    primary_capital_type: "factoring"
  - touch_number: 5
    channel: "direct_mail"
    kind: "postcard"
    day_offset: 28
    role: "loss-aversion close"
    headline_focus: "Name the cost of staying on the wrong product through another quarter of volatile rates."
    body_focus: "Short. If the operator settles for the first yes or stays on a sticky contract, they spend months unwinding it. We make one warm intro to a partner whose criteria already match. Final CTA: scan or call."
    primary_capital_type: "factoring"
  - touch_number: 6
    channel: "email"
    kind: "email"
    day_offset: 35
    role: "last-call, deprioritize"
    headline_focus: "Subject line offers to stop sending mail unless this is worth their time."
    body_focus: "Plain. We will stop unless they tell us this fits. Restate: matchmaker, not lender; no spread; one warm intro. One ask."
    primary_capital_type: "factoring"
  - touch_number: 7
    channel: "voice_inbound"
    role: "inbound handler for callers from the postcard or letter; greet as Capital Expansion, ask for DOT#, confirm power-unit count is 10-50, confirm net-30 to net-60 broker-pay situation, qualify per partner rules, live-transfer in-hours or schedule callback out-of-hours; if unqualified, say so plainly."

hook_bank:
  audience_pain_phrases:
    - "Cash flow is slower until brokers start paying."
    - "Shippers and brokers often pay on 30-60 day terms, so even if you're booked solid, you'll need reserves for expenses."
    - "Load prices simply haven't gone up while all other expenses have."
    - "Fuel costs me more than $1200 a week. Insurance breaks down to about $350 a week a truck."
    - "Insurance down payments and setup costs under your own authority are a big hit early."
    - "Rates also swing a lot, so one month can feel great, and the next feels like survival mode."
    - "Factoring companies want nothing more than to get their hooks into a company... it is very difficult to divorce yourself from one."
    - "Double brokering is a major problem."
    - "Ghost brokers and canceled loads after deadhead miles."
    - "You're running an office on wheels."
  why_now_hooks:
    - "Diesel prices surged in early 2026 with regional diesel above $5/gal, widening the gap between fuel-out and broker-pay-in."
    - "2025-2026 spot and contract rates are flat-to-modest with persistent seasonal swings, raising the value of predictable near-term cash."
    - "FMCSA enforcement and ELD/compliance scrutiny stepped up in the last 12 months, raising audit and paperwork load on small carriers."
    - "Freight fraud and double-brokering reports rose through 2026, pushing carriers toward vetted credit-check workflows."
  partner_proof_atoms:
    - "Transportation factors typically fund same-day on verified invoices."
    - "Specialty transportation factors run broker credit checks before a load is booked, not after non-payment."
    - "Some transportation factors do not require factoring all loads, leaving direct-billed freight separate."
    - "A 2 percent factoring fee on net-30 paper translates to roughly a 24 percent annualized cost — operator framing worth pricing against alternatives."
    - "Carrier-vetting and authority/insurance monitoring are now standard inside transportation-finance workflows."
    - "Net-30 to net-60 commercial AR is the canonical right-fit window for transportation factoring."

anti_framings:
  - "Do not use solutions, best-in-class, world-class, industry-leading, innovative, cutting-edge, disrupting, empower, unlock potential, frictionless, seamless, magical, synergy, leverage as a verb, or concierge."
  - "Do not say reach out; say tell us or send us a note."
  - "Do not use exclamation points or emojis in body copy."
  - "Do not claim a track record, customer count, dollar volume, time-to-close average, or close rate — the brand is new."
  - "Do not name a specific partner, lender, factor, or DAT product by name in copy."
  - "Do not pitch more than one capital type in a single piece; factoring is the primary frame."
  - "Do not promise fast pay, low fees, or easy app as the lead — operators treat those as table-stakes."
  - "Do not ask the recipient to click a link in email without a verifiable sender identity; prefer the QR/landing path or a callback."
  - "Do not frame the operator as struggling or failing; they are navigating a fragmented market."
  - "Do not use questions in headlines or em-dashes more than once per paragraph."

capital_outlay_plan:
  total_estimated_cents: 1000000
  per_recipient_estimated_cents: 2500
  notes: "Assumes the partner-contract max_capital_outlay_cents of $10,000 spread across approximately 400 qualified recipients (10-50 power units) at ~$25 per recipient covering 3 direct-mail pieces plus 3 emails plus voice-inbound handling over the 90-day window."
---