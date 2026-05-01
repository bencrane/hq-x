# Capital Expansion — brand content index

Brand owned by Ben. Domain: `capitalexpansion.com`.

Brand row in `business.brands` provides the identity (id / name / domain / Twilio creds / Trust Hub). The rich content the per-recipient creative author consumes lives here.

## Files

| File | Purpose |
|---|---|
| [brand.json](brand.json) | Structured metadata an agent can read without parsing markdown. Slug, domain, paths to other files, partner/audience taxonomy. |
| [positioning.md](positioning.md) | What the brand is, what it isn't, the wedge, the matchmaker-not-lender stance. |
| [voice.md](voice.md) | Tone, register, reusable phrases, words to avoid, sentence shapes, surface-specific calibration. |
| [audience-pain.md](audience-pain.md) | Operator pain framings per industry; "before" feeling per persona; what NOT to frame as pain. |
| [value-props.md](value-props.md) | The four pillars: no origination conflicts, operator-first, across capital types, warm intros. |
| [capital-types.md](capital-types.md) | Six capital types — definition, right fit, wrong fit, partner archetype, operator language. |
| [industries.md](industries.md) | Industries served, primary capital fit per industry, pain language per industry. |
| [proof-and-credibility.md](proof-and-credibility.md) | What we can claim, what we cannot (it's a new brand), how to substitute specificity for missing track record. |
| [creative-directives.md](creative-directives.md) | How a copywriter / LLM authors per-recipient creative under this brand. Decision tree per piece, headline/body rules, sequence variations, output format. |

## Read order for a copywriting agent

For per-recipient creative authoring, the read order is:

1. `brand.json` — pick up metadata + paths
2. `positioning.md` — understand what the brand is and isn't
3. `voice.md` — internalize tone and reusable language
4. `audience-pain.md` (filtered to the recipient's industry) — pick up the operator pain framing
5. `industries.md` (one row, by industry) — pick the primary capital type
6. `capital-types.md` (one section, by capital type) — pick up the operator language for that type
7. `value-props.md` — pick which pillar to lead with for this piece
8. `proof-and-credibility.md` — confirm the proof claims the piece is allowed to make
9. `creative-directives.md` — author the piece following the decision tree + output format

## Read order for the voice agent persona shell

For the inbound voice agent:

1. `brand.json` (`voice_agent_persona_notes`)
2. `positioning.md` ("matchmaker, not lender" stance)
3. `voice.md` ("Voice in different surfaces" → voice agent)
4. `creative-directives.md` ("Voice agent inbound")

## Status

Brand-new (2026-04-30). No track record. See `proof-and-credibility.md` for the what-we-can-and-cannot-claim line.
