# Capital Expansion — creative directives

How a copywriter (human or LLM) should author per-recipient creative under this brand. Read positioning.md, voice.md, and audience-pain.md first.

## The frame

Every piece is one operator-to-operator conversation. The copywriter is talking *to* a specific recipient about *their* specific situation. The piece is not a brochure; it is a note.

## The decision tree per piece

1. **Who is the recipient?** (industry, size, signal from audience spec)
2. **Which capital type fits their situation?** (consult industries.md → capital-types.md)
3. **What is the operator's likely "before" state?** (consult audience-pain.md for the industry-specific framing)
4. **What is the *one* sentence that names their situation?** (the headline)
5. **What is the *one* next step?** (the CTA)
6. **Which proof claim are we allowed to make?** (consult proof-and-credibility.md — usually: specificity, not volume)

## Headline rules

- Name the recipient's situation, not the brand.
- Use specifics from their data (industry, equipment type, AR cycle, season).
- 6–10 words. Declarative. No questions in headlines.
- Bad: "Need capital? We can help."
- Good: "Two new dry vans, 25% down you don't have liquid."

## Body rules

- First sentence references the situation. Second sentence references why the typical "first call" path is wrong. Third sentence references the right capital type. Fourth sentence references the next step.
- 60–120 words for direct-mail body, 80–150 for email.
- One specific concrete detail per piece (e.g. "transportation factors do same-day funding"; "SBA 7(a) closings run 60–90 days, not 30").
- One CTA. Never two.

## Direct mail piece variations across a sequence

A typical sequence for a recipient is touch-1 (postcard), touch-2 (letter), touch-3 (postcard).

- **Touch 1 (postcard, day 0):** Name the situation. Introduce Capital Expansion as a matchmaker. Single CTA: scan QR / visit personalized landing page.
- **Touch 2 (letter, day 14):** Go deeper on *why* the right capital type is the right one. Show the work — explain the matching logic. CTA: same QR/URL OR a phone callback.
- **Touch 3 (postcard, day 28):** Loss-aversion framing — what happens if the operator stays on the wrong product. Final CTA: same.

## Email sequence variations

- Email 1 (day 3 after touch-1): "We sent you a postcard about [situation]. Here's the longer version of why we think [capital type] fits."
- Email 2 (day 17 after touch-2): Operator-language scenario; one paragraph; one ask.
- Email 3 (day 35): Last-touch framing; offer to deprioritize the lead if not interested ("We're going to stop sending you mail unless you tell us this is worth your time").

## Voice agent inbound

When an operator calls the number on the piece:

- Greet by brand: "Capital Expansion."
- Ask for their code (DOT#, BBL, EIN, etc., depending on audience type).
- Confirm the situation referenced in the piece they received.
- Validate qualification (per partner contract: industry, revenue band, capital type).
- If qualified + in-hours: live transfer to partner phone with context.
- If qualified + out-of-hours: schedule callback during partner's hours.
- If unqualified: tell them plainly. Do not waste their time.

## Things the copywriter should never do under this brand

- Claim a track record that doesn't exist (see proof-and-credibility.md).
- Use marketing fluff vocabulary (see voice.md "words to avoid").
- Recommend more than one capital type per piece.
- Reference a specific named lender/partner.
- Use a generic stock-photo "smiling business owner" image. (Per-recipient creative gets situation-specific, brand-coherent imagery.)
- Send the same piece to two recipients with the same industry but different sub-segments — the per-recipient bespoke design is the entire point.

## Output format the LLM should produce per piece

For each direct-mail piece, the LLM should emit:

```yaml
recipient_id: <stable id>
piece_kind: postcard | letter | self_mailer
touch_number: 1 | 2 | 3 | ...
headline: <6-10 words>
body: <60-120 words for postcard, longer for letter>
cta_label: <"Tell us your situation" | "Call us at <#>" | etc.>
cta_url_path: /match/<recipient_code>
imagery_directive: <one-sentence description of the image / icon set the layout engine should render>
capital_type_recommended: factoring | sba | equipment_finance | abl | loc | rbf
recommendation_one_liner: <one sentence the voice agent can quote back if the recipient calls>
```

This output then feeds the zone-binding / MediaBox layout pipeline.
