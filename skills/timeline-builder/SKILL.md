# Timeline Builder — Roadmap Generation

## Purpose

Integrate results from card-recommendation + cli-strategy + retention-strategy onto a time axis, generating short-term (6-month), medium-term (12-month), and long-term (24-month) roadmaps.

## Trigger Conditions

- User requests a roadmap/plan/timeline
- Automatically called after card recommendation completion
- Roadmap regeneration requested after profile changes

## Prerequisites

- Card recommendation results from card-recommendation skill
- CLI schedule from cli-strategy skill
- Annual fee events from retention-strategy skill
- user_profile + user_cards data

## Processing Flow

### Step 1: Event Collection

Gather all actions from skills chronologically:

**Card Application Events:**
- Recommended card, planned application month, 5/24 change, SUB requirements

**CLI Events:**
- Target card, earliest request date, pull type

**Annual Fee Events:**
- Affected card, due month, recommended action (keep/PC/cancel)

**SUB Deadline Events:**
- Card, deadline, remaining amount, achievability assessment

**Product Change (PC) / Auto-Upgrade Events:**
- Target card, PC eligibility date (e.g., 12 months after opening), conversion target card, 5/24 slot savings and benefit changes (annual fee, etc.)

### Step 2: Apply Timing Constraints

Check constraints when placing events on the timeline:

```
Constraint 1: Chase 2/30 — No more than 2 Chase cards within 30 days
Constraint 2: Amex 1/5 — No more than 1 Amex credit card within 5 days
Constraint 3: Amex 2/90 — No more than 2 Amex credit cards within 90 days
Constraint 4: HP CLI should avoid 3 months before/after card applications
Constraint 5: Stagger SUB minimum spend periods to avoid overlap
Constraint 6: 5/24 management — Prioritize Chase cards while under 5/24
```

### Step 3: Generate Optimal Schedule

LLM places monthly actions considering all events and constraints:

**Priority Principles:**
1. 5/24 related → Chase cards first (while slots remain)
2. Higher SUB value cards → before lower value cards
3. Soft pull CLI → fill into empty months (few constraints)
4. Annual fee events → fixed to their month (cannot be moved)
5. Minimum spend overlap prevention → space cards 1–2 months apart

### Step 4: Timeline Output

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 24-Month Credit Roadmap
User: {user_id} | Generated: 2026-03-19
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Month 1  (2026-04)  ▸ 🆕 Apply for Chase Sapphire Preferred
                       SUB: 60K UR / $4K spend in 3mo
                       5/24: 3 → 4
                       Est. 1st-year value: $1,235

Month 2  (2026-05)  ▸ 💳 CSP minimum spend in progress
                       Target: $4,000 | Natural spend forecast: ✅ Sufficient

Month 3  (2026-06)  ▸ 📈 Request Amex Gold CLI (soft pull)
                       Current $6K → Target $15K+ (3x rule)
                     ▸ ✅ CSP SUB completion expected

Month 5  (2026-08)  ▸ 🆕 Apply for Amex Gold
                       SUB: 60K MR / $6K spend in 6mo
                       5/24: 4 → 5
                       ⚠️ No new Chase cards possible after this

Month 6  (2026-09)  ▸ 📈 Chase Freedom Flex CLI (soft pull, SM)
                       Current $5K → Target $10K+
                     ▸ 💳 Amex Gold min spend in progress

Month 8  (2026-11)  ▸ ⚠️ Discover it annual fee due
                       AF: $0 → Keep (no cost)

Month 10 (2027-01)  ▸ ✅ Amex Gold SUB completion expected
                     ▸ 📈 Amex Gold 2nd CLI eligible

Month 13 (2027-04)  ▸ ⚠️ CSP annual fee due ($95)
                       Recommend: Retention call → PC to CFF if no offer

Month 18 (2027-09)  ▸ 🆕 Citi Premier eligible (5/24 natural decay: 5→4)
                       SUB: 60K TYP / $4K spend in 3mo

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 Roadmap Summary
  New cards: 3 | Est. total SUB value: $3,000+
  CLI requests: 3 (all soft pull)
  Annual fee events: 2
  5/24 peak: 5 (Month 5) → 4 (Month 18, natural decay)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### Step 5: Icon Legend

```
🆕 New card application    📈 CLI request
💳 Minimum spend          ✅ SUB/goal achieved
⚠️ Annual fee/caution      🔄 PC/downgrade
```

### Step 6: Save + Log

Report saved to `/report/roadmap_{user_id}_{date}.md`.

Append to `logs/decision_log.jsonl`:
```jsonl
{"ts":"...","type":"roadmap_generated","user_id":"...","months":24,"cards_planned":3,"cli_planned":3,"retention_events":2}
```

## Dynamic Updates

The roadmap is a living document:
- Regenerate whenever new card is opened / CLI result received / annual fee decision made
- Show before/after diff to clarify what changed
- Archive previous versions in `/report/`

## Important Notes

- All dates are estimates and will adjust based on actual approval outcomes
- 5/24 natural decay timing is calculated from exact card open dates
- When minimum spend periods overlap, always verify coverage by natural spending
- Include disclaimer: "This roadmap is for informational purposes only, not financial advice"
