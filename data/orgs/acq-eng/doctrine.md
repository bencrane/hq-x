# acq-eng operator doctrine

Ben's owned-brand lead-gen operator doctrine. Read by `gtm-sequence-definer`
to make economics decisions on each new initiative. Mirrored into
`business.org_doctrine` for `acq-eng` (org id `4482eb19-f961-48e1-a957-41939d042908`).
The structured numbers live in `parameters` (see the JSONB shape
appended at the end of this doc and authored separately into the DB).

The principle behind the numbers: **margin is what funds the next
initiative.** Underwater initiatives don't fund the throughput goal,
so the floor is non-negotiable.

---

## 1. Margin floor

Target margin per initiative is **40% of partner payment after capital
outlay**. That is:

```
margin_pct = (partner_payment_cents - capital_outlay_cents) / partner_payment_cents
```

- **≥ 40%** — green-light.
- **30%–39.99%** — soft-yellow. Requires explicit operator override
  flag (`partner_contracts.metadata.operator_margin_override = true`).
- **< 30%** — hard reject. The sequence-definer must emit
  `{decision: "reject_economics", reason: "margin_below_30pct", ...}`
  and refuse to plan a sequence.

The margin floor binds even when the partner contract has high
implied LTV. We're not subsidizing initiatives speculatively at the
sequence-definer layer.

---

## 2. Capital outlay cap

Default cap is **50% of partner payment**, applied in addition to any
contract-level `max_capital_outlay_cents`. Whichever is smaller binds.

```
effective_cap_cents = min(
    partner_payment_cents * 0.50,
    contract.max_capital_outlay_cents OR float('inf'),
)
```

If the touch plan would exceed `effective_cap_cents`, sequence-definer
must drop touches (cheapest first if quality permits, else postcards
before letters before self-mailers) until the plan fits.

---

## 3. Per-piece outlay guardrails

Floor: **$1.00 per piece** ($100 cents). Sanity guardrail — no
postcards cheaper than this in practice.

Ceiling: **$8.00 per piece** ($800 cents). Booklets and heavy
self-mailers can climb here; anything above $8.00 per piece flagged
for human review (sequence-definer should emit `requires_human_review:
true` in its output rather than silently planning a $12 piece).

These are operator priors, not Lob list prices. They reflect Ben's
willingness to spend per touch on a per-recipient bespoke piece.

---

## 4. Default touch counts by audience size bucket

Heuristic priors. Sequence-definer may override with explicit
reasoning that cites the contract economics + audience-specific
signals.

| Audience size bucket | Default touch count |
|---|---|
| 0 – 500 | 4 |
| 500 – 2,500 | 3 |
| 2,500 – 10,000 | 3 |
| 10,000+ | 2 |

Smaller audiences get more touches because per-recipient cost-of-touch
is high relative to the upside of any one recipient converting.
Larger audiences get fewer touches because absolute capital outlay
matters more than incremental touch coverage.

These are **defaults** — the model may produce 5 touches for a 2,000-
recipient audience if the partner contract's economics + audience pain
signals justify it. The reasoning must be explicit in the
`justification` field of the sequence-definer output.

---

## 5. Model tier policy

**Default Opus across all subagents in the foundation build.** This
is the productized service; cost is acceptable for end-to-end output
quality validation.

After end-to-end output quality is validated, the operator dials
individual subagents down per-step (e.g. step types that don't need
voice-laden generation can drop to Sonnet to save cost). All overrides
flow through `parameters.model_tier_by_step_type` in this org's
doctrine row, which lets the operator change tier per step without a
deploy.

```yaml
model_tier_by_step_type:
  default: claude-opus-4-7
  # Future overrides go here:
  # gtm-sequence-definer: claude-sonnet-4-6
  # gtm-master-strategist-verdict: claude-sonnet-4-6
```

The model tier is read at session-create time for each subagent run.

---

## 6. Gating mode default

`auto` for v0. Straps end-to-end without manual intervention so
failure cascades surface naturally. The operator flips per-initiative
to `manual` to gate-debug specific runs (e.g. when iterating prompts
on a problem audience and wanting to inspect each step before the
next fires).

---

## 7. Anti-rules (operator-specific, on top of the brand doctrine)

These are stricter than the independent-brand doctrine because they
encode acq-eng's specific operator preferences:

- **No discount language on direct mail**, even in postcard headlines.
  Discounts read as "we're trying to close you" — they don't fit the
  operator persona we want for capital products.
- **No urgency theater** on print: "limited time," "only this week,"
  "act now." Urgency theater tells the recipient we're playing them.
  Real urgency surfaces from genuine signals (e.g. "your insurance
  policy expires in 23 days").
- **No "as seen in Forbes" / "as seen in Inc."** type credibility
  claims. The brand is new and openly so; fake credibility is worse
  than no credibility.

If the master strategist or per-recipient creative author drafts
copy violating these, the verdict layer must return `ship: false`
with `area: "operator_doctrine_violation"`.

---

## 8. The parameters JSONB

Authored alongside this markdown into `business.org_doctrine.parameters`
for `acq-eng`. The sequence-definer reads this directly via
`org_doctrine.get_for_org(organization_id)`.

```json
{
  "target_margin_pct": 0.40,
  "soft_margin_pct": 0.30,
  "max_capital_outlay_pct_of_revenue": 0.50,
  "min_per_piece_cents": 100,
  "max_per_piece_cents": 800,
  "default_touch_count_by_audience_size_bucket": {
    "0_500": 4,
    "500_2500": 3,
    "2500_10000": 3,
    "10000_plus": 2
  },
  "model_tier_by_step_type": {
    "default": "claude-opus-4-7"
  },
  "gating_mode_default": "auto"
}
```

Both the markdown and the JSON must be authored. The markdown is the
prose policy doc the operator iterates on; the JSON is what
`gtm-sequence-definer` reads at run start. Sync via
`scripts/sync_org_doctrine.py`.
