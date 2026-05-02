# Claygent — target account research prompt (pre-outreach, lightweight)

Reusable Claygent prompt for researching a prospective demand-side partner **before** they pay. Output is markdown-structured prose, not deep JSON, so Claygent doesn't choke on schema overhead. Output gets stored as `business.target_accounts.research_blob` (markdown string + a small parsed-out keys object) and is consumed by the audience-derivation agent (primary) and the brand factory (light read of vertical / core need / voice register).

When reusing this prompt for a different prospect, replace `<<TARGET_URL>>` on the first line of the prompt block with the company's homepage URL. Nothing else needs to change.

---

## Prompt

```
Research the company at <<TARGET_URL>>. This is light pre-outreach research — we are about to email this company to propose a partnership and we need enough of a profile to slice a targeted audience for them. We are NOT doing a deep dive; budget your time and keep the output compact. Use the company's website (especially their pages aimed at customers, e.g. "For Operators" / "For Carriers" / "Solutions" / "Pricing"), their LinkedIn, and any recent news or press. Cite a source URL inline next to any non-obvious claim. If you genuinely can't find something, say so — do not invent.

Return your output as a single markdown document with the six section headings below, in this order. Use short paragraphs and bullet lists. Keep the whole document under roughly 600 words. The downstream consumer is another LLM agent that will use this to slice a structured database — so prefer concrete, specific values (numbers, ranges, named segments, named industries) over vague descriptors ("small to mid" → "5–50 employees" or "fleet of 5–50 trucks"; "established carriers" → "MC# active >12 months").

Sections required:

## Who they are
One short paragraph: company name, what they do, who their customers are at the archetype level. End with the vertical they live in (e.g. transportation/freight, real-estate, manufacturing, healthcare, staffing, construction).

## Their ideal customer
A bullet list capturing the customer profile they most want — size band (with concrete numbers if available), geographic footprint (US-wide / specific states / global), industry sub-segment, and any operator-level attributes you can pin down (years in business, regulatory licenses held, fleet size, building portfolio size, revenue band, etc.). Each bullet should be one specific attribute with a concrete value or range. Include a one-line "primary archetype" at the top of the list summarizing the rest.

## Qualification minimums and disqualifiers
Two short bullet lists. First list: the concrete thresholds that define a real prospect for them (e.g. "annual revenue >$500K", "MC# active >6 months", "owns 3+ rental units"). Second list: who they explicitly do NOT serve (too small, too large, wrong sub-segment, wrong geography, wrong stage). Be concrete — these become hard filters on our database slice.

## Pain signals and observable proxies
A short bullet list. Each bullet has two halves separated by a hyphen: the operational pain their customers experience, and an observable data-side proxy that detects it. Examples of the shape: "fleet aging out of useful life — average truck age >8 years"; "behind on receivables — DSO trending up"; "distressed property — recent HPD violations + zero water usage". The downstream agent uses these to add filters to the database query.

## Where they're growing
One short paragraph or 2-3 bullets: which sub-segments they appear to be actively pushing into vs maintaining. This is sharper than current-customer descriptions because it tells us what KIND of prospect they would value MORE of right now. Cite source URLs (recent blog posts, press releases, leadership statements, product launches).

## Quick keys (for our internal classification)
A short bullet list with exactly these three keys, one bullet each:
- vertical: <one of transportation/freight | real-estate | manufacturing | healthcare | staffing | construction | financial-services | SaaS-operations | other>
- core_need_category: <short phrase describing the customer-side need this company addresses, e.g. "factoring/working-capital", "load-matching/freight-marketplace", "equipment-finance", "insurance", "labor-staffing", "property-disposition">
- voice_register: <one of operator-to-operator | institutional | empathetic/disclosure-forward | transactional | premium/concierge | self-serve/SaaS>

End the document with a one-line "Confidence: high | medium | low" indicating how well-sourced this profile is. Do not add any commentary outside these sections.
```

---

## What downstream agents read

**Audience-derivation agent** parses the markdown and treats these sections as load-bearing:
- *Their ideal customer* — size band, geography, archetype attributes → maps to dataset filters
- *Qualification minimums and disqualifiers* → hard filters
- *Pain signals and observable proxies* → additional filter hints, mapped to data-side columns
- *Where they're growing* → overweight signals when ranking the audience

**Brand factory** parses only:
- *Quick keys* — the three classification fields drive pairing-shape derivation
- *Who they are* — vertical context for the gestalt prompt

Sections like *Pain signals* and *Qualification minimums* are not consumed by the brand factory at this stage; they exist for the audience-derivation agent.

---

## Format note

If Claygent ever prefers a single block of text over markdown (some agent runtimes do), you can ask it to return all six sections concatenated with `---` separators between them and no `##` headings. The downstream parsers split on either delimiter.
