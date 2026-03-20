# Retention Strategy — Annual Fee Management + Retention

## Purpose

Support keep/downgrade/cancel decisions for cards approaching annual fee renewal, and provide retention offer strategies.

## Trigger Conditions

- 60 days before annual fee due date (based on af_due_date)
- User requests card keep/cancel consultation
- Timeline roadmap includes annual fee events

## Processing Flow

### Step 1: Scan Cards Approaching Annual Fee

```sql
SELECT card_name, issuer, annual_fee, af_due_date, credit_limit,
       opened_date, last_retention_call, retention_offer, status
FROM user_cards
WHERE user_id = ? AND annual_fee > 0 AND status = 'active'
AND julianday(af_due_date) - julianday('now') <= 60
AND julianday(af_due_date) - julianday('now') > 0
ORDER BY af_due_date ASC;
```

### Step 2: Calculate Annual Value Per Card

**Value components:**
- Spending-based point earnings (spending_pattern × category multipliers)
- Built-in card credits (Amex Gold: $120 dining + $120 Uber = $240)
- Indirect value: free checked bags, lounge access, etc.
- Ancillary benefits: travel insurance, purchase protection, etc.

**Cost components:**
- Annual fee
- Opportunity cost of switching to an alternative card

```
Example: Amex Gold
  Annual fee: $250
  Value:
    - Dining credit: $120
    - Uber credit: $120
    - Groceries 4x MR: $680/mo × 12 × 4 × $0.0167 = $544
    - Dining 4x MR: $420/mo × 12 × 4 × $0.0167 = $336
  Total value: $1,120
  Net benefit: $1,120 - $250 = $870 → ✅ Recommend keeping
```

### Step 3: Decision Tree

```
60 days before annual fee
│
├─ Annual value > AF × 1.2?
│   └─ YES → Recommend keeping (comfortable margin)
│
├─ Annual value > AF?
│   └─ YES (but thin margin) → Recommend trying a retention call
│       ├─ Offer received → Evaluate offer value → keep/decline
│       └─ No offer → Re-evaluate usage patterns
│
└─ Annual value < AF?
    └─ Try retention call
        ├─ Offer worth accepting? → Keep
        └─ No offer / insufficient
            ├─ No-AF downgrade path available?
            │   ├─ YES → Recommend PC (preserves AAoA)
            │   │   e.g.: CSP → CFF, CSR → CFF
            │   │   e.g.: Amex Gold → Amex Green ($150 AF) or cancel
            │   └─ NO → Calculate AAoA impact
            │       ├─ Significant AAoA impact → Consider keeping (AF as AAoA cost)
            │       └─ Minimal AAoA impact → Cancel
            └─ Cancellation precautions
```

### Step 4: Retention Call Guide

Provide script/tips before user makes retention call:

```
━━━━ Retention Call Guide ━━━━
Card: Chase Sapphire Preferred | AF: $95

📞 Call: Number on back of card → say "account retention" or "cancel card"

💬 Script points:
  1. "I noticed my annual fee is coming up and I'm reconsidering keeping this card"
  2. "I'd like to continue using this card but I'm having trouble justifying the fee"
  3. "Are there any offers available to help me keep the card?"

⚠️ Tips:
  - If first agent says no offer, HUCA (Hang Up Call Again)
  - Be careful with cancel threats — they may actually close the account
  - Amex has a dedicated retention department (provide number)
━━━━━━━━━━━━━━━━━━━━
```

### Step 5: PC (Product Change) Recommendations

Available downgrade paths:
```
Chase:
  CSR → CSP → CFF / CFU (no AF)
  CSP → CFF / CFU (no AF)

Amex:
  Amex Gold → Cancel only (no no-AF downgrade path)
  Amex Platinum → Amex Green ($150 AF) or cancel
  Amex Blue Cash Preferred → Blue Cash Everyday (no AF)

Citi:
  Citi Premier → Citi Double Cash (no AF)
  Citi Custom Cash → Keep (no AF)

Capital One:
  Venture X → Venture One (no AF) / VentureOne
```

### Step 6: Result Recording

```sql
UPDATE user_cards
SET last_retention_call = ?, retention_offer = ?, status = ?
WHERE card_id = ?;
-- status: 'active' / 'downgraded' / 'closed'
-- product_changed_from: Record original card name on PC
```

Append to `logs/decision_log.jsonl`:
```jsonl
{"ts":"...","type":"retention","card":"CSP","action":"downgrade_to_CFF","reasoning":"AF $95 vs annual value $60, no retention offer, PC to CFF preserves AAoA","confidence":"high"}
```

Report saved to `/report/retention_{card}_{date}.md`.

## Important Notes

- Amex once-per-lifetime rule applies after cancellation — always verify before canceling
- Chase Sapphire 48-month cooldown resets on cancellation/PC — be aware
- Closed cards remain on credit report for ~10 years when calculating AAoA
- Annual fee refund: Most issuers offer full refund if canceled within 30–41 days of AF posting
