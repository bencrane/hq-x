---
schema_version: 2
initiative_id: "bbd9d9c3-c48e-4373-91f4-721775dca54e"
generated_at: "2026-05-01T01:09:09.727825+00:00"
model: "claude-opus-4-7"

headline_offer: "If brokers are paying you in 30 to 60 days while diesel hits the card today, the right factor funds the gap — and we know who funds carriers your size."

per_touch_direction:
  - touch_number: 1
    channel: "direct_mail"
    kind: "postcard"
    day_offset: 0
    role: "name the situation"
    headline_focus: "Name the 30-60 day broker payment gap for a 10-50 power-unit fleet in 6-10 declarative words."
    body_focus: "One sentence on the gap, one on why a bank LOC or one-size factor is wrong, one on transportation-specific factoring with same-day funding, one CTA to the personalized URL."
    primary_capital_type: "factoring"
  - touch_number: 2
    channel: "email"
    kind: "email"
    day_offset: 3
    role: "longer version of the postcard"
    headline_focus: "Subject line names factoring for the carrier's specific fleet-size band."
    body_focus: "Reference the postcard, then explain the matching logic: which factor archetype fits a 10-50 unit fleet running broker freight, and what we screen out (sticky contracts, all-loads requirements). One ask: tell us your situation."
    primary_capital_type: "factoring"
  - touch_number: 3
    channel: "direct_mail"
    kind: "letter"
    day_offset: 14
    role: "deepen — show the work"
    headline_focus: "Headline that contrasts a transportation factor with a generic factor or bank LOC."
    body_focus: "Walk through the matching logic for a fleet this size: net-60 broker AR, fuel and insurance outflows, why a transportation-specialty factor with broker credit checks fits and a generalist does not. Reference no sticky contracts as a screening criterion. CTA: same URL or callback."
    primary_capital_type: "factoring"
  - touch_number: 4
    channel: "email"
    kind: "email"
    day_offset: 17
    role: "operator-language scenario"
    headline_focus: "Subject line in operator voice about the cash buffer between booked freight and broker pay."
    body_focus: "One short scenario paragraph in operator voice: booked solid, fuel and insurance outflows this week, broker pays in 45. Frame the right factor as the bridge. One ask."
    primary_capital_type: "factoring"
  - touch_number: 5
    channel: "direct_mail"
    kind: "postcard"
    day_offset: 28
    role: "loss-aversion close"
    headline_focus: "Loss-aversion line about staying on the wrong product or no product while diesel and net-60 compound."
    body_focus: "Two sentences on what continues if nothing changes (cash gap widens, fuel on the card, payroll pressure). One sentence on the warm-intro path. Final CTA."
    primary_capital_type: "factoring"
  - touch_number: 6
    channel: "email"
    kind: "email"
    day_offset: 35
    role: "last call — deprioritize offer"
    headline_focus: "Subject line offers to stop sending mail unless the recipient says it is worth their time."
    body_focus: "Plain-spoken last touch. Restate the match thesis in one line. Offer to drop them from the sequence if not relevant. One reply ask."
    primary_capital_type: "factoring"
  - touch_number: 7
    channel: "voice_inbound"
    role: "inbound handler — greet by brand, confirm DOT, confirm the broker-pay-gap situation referenced on the piece, validate 10-50 power units, route to the matched transportation factor or schedule callback; if out of band, say so plainly."

hook_bank:
  audience_pain_phrases:
    - "Cash flow is slower until brokers start paying."
    - "Shippers and brokers often pay on 30-60 day terms, so even if you're booked solid, you'll need reserves for expenses."
    - "Load prices simply haven't gone up while all other expenses have."
    - "Fuel costs me more than $1200 a week. Insurance breaks down to about $350 a week a truck."
    - "Insurance down payments and setup costs under your own authority are a big hit early."
    - "Rates also swing a lot, so one month can feel great, and the next feels like survival mode."
    - "Factoring companies want nothing more than to get their hooks into a company... it is very difficult to divorce yourself from one."
    - "Factoring company taking 2% is essentially loaning you money at a 24% interest rate."
    - "You need a minimum of 3-6 months of expenses banked."
    - "You're running an office on wheels."
  why_now_hooks:
    - "Diesel prices surged sharply in early 2026, with regional diesel above $5/gal — the 30-60 day broker pay gap is harder to cover than it was last year."
    - "Spot and contract rates are flat-to-modest in 2025-2026 with persistent seasonal swings, so predictable near-term cash matters more than upside on the next load."
    - "FMCSA enforcement and ELD compliance attention have stepped up — carriers can't afford to miss premium or fuel payments while fighting an audit."
    - "Double brokering and ghost-broker fraud are rising in 2026; carriers using vetted networks and broker credit checks avoid non-payment after delivery."
    - "Spot load-to-truck ratios have fallen below 1:4 on common lanes, compressing rates and tightening cash cycles for small fleets."
  partner_proof_atoms:
    - "Transportation-specialty factors typically run same-day funding on approved invoices."
    - "Specialty factors offer broker credit checks before the load is booked, which reduces non-payment exposure."
    - "Some transportation factors do not require factoring all loads — operators can direct-bill trusted shippers and factor only brokered freight."
    - "Factoring fits net-30 to net-90 commercial AR with recurring B2B customers — the standard broker-pay carrier profile."
    - "Wrong-fit signals for factoring: high customer concentration, disputed invoices, or mostly consumer/retail revenue."
    - "Operator-language fit: 'I can't wait 60 days to get paid; I have payroll Friday.'"
  anti_framings:
    - "Do not name DAT, DAT One, DAT Outgo, Convoy Platform, OTR Solutions, Triumph, or any specific factor or lender."
    - "Do not claim a track record, customer count, dollar volume, or close rate — the brand is new and has none."
    - "Do not recommend more than one capital type per piece; factoring is the primary frame for every touch."
    - "Do not use forbidden voice words: solutions, best-in-class, world-class, industry-leading, innovative, cutting-edge, disrupting, empower, unlock, frictionless, seamless, magical, synergy, leverage (verb), concierge, reach out."
    - "No exclamation points, no emojis, no questions in headlines, no more than one em-dash per paragraph."
    - "Do not promise speed alone (\"fast pay,\" \"quick pay\") as the lead — operators treat that as table-stakes; lead with specificity and matching logic."
    - "Do not use generic stock-photo smiling-business-owner imagery; imagery must be situation-specific to a 10-50 power-unit carrier."
    - "Do not include unverified links or attachments that resemble phishing; CTA is a personalized URL or a callback number, not a generic link."
    - "Do not frame the operator as struggling or failing — they are navigating a fragmented capital market."
    - "Do not pitch as a marketplace, lead-gen list, or broker-shopping-the-deal — the stance is matchmaker, not lender, no spread."
  
capital_outlay_plan:
  total_estimated_cents: 1000000
  per_recipient_estimated_cents: 1250
  notes: "Assumes ~800 recipients across 3 direct-mail pieces (postcard/letter/postcard at ~$2.50 blended print+postage) and 3 emails (negligible variable cost), capped at the contract max_capital_outlay_cents."
---