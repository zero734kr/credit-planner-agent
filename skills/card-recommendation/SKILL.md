# Card Recommendation — Card Recommendation Engine

## Purpose

Combine user profile + spending patterns + issuer rules to determine optimal new card recommendations and application sequence.

## Trigger Conditions

- User requests new card recommendations
- Called as part of timeline roadmap generation
- Profile update triggers recommendation recalculation

## Prerequisites

- User data exists in `user_profile` table (if not, run profile-intake skill first)
- Spending data exists in `spending_pattern` table (if not, ask user directly or run statement-analysis)

## Processing Flow

### Step 1: Real-Time Card Information Search

**On every recommendation request**, fetch the latest card info from the web. Never recommend based on stale data.

```
WebSearch query examples:
- "best credit cards {year} signup bonus"
- "{issuer} credit card current offers {year}"
- "Chase Sapphire Preferred current signup bonus"
- "doctor of credit best current offers"
```

**Search source priority:**
1. Doctor of Credit — Best Current Credit Card Offers
2. US Credit Card Guide
3. Official issuer websites
4. The Points Guy — current offers

Cache search results in `search_cache` table (TTL: 24 hours).

### Step 2: Hard Constraint Filtering

Remove ineligible cards via SQLite queries first:

```sql
-- If over 5/24, exclude all Chase cards
SELECT chase_524_count FROM user_profile WHERE user_id = ?;
-- chase_524_count >= 5 → remove Chase from candidates

-- Exclude already-held cards (Amex once-per-lifetime)
SELECT card_name, issuer FROM user_cards WHERE user_id = ? AND issuer = 'Amex';

-- Citi 48-month rule check
SELECT card_name, opened_date FROM user_cards
WHERE user_id = ? AND issuer = 'Citi'
AND julianday('now') - julianday(opened_date) < 48*30;

-- Velocity rule check (Chase 2/30, Amex 1/5, Amex 2/90, etc.)
SELECT issuer, opened_date FROM user_cards WHERE user_id = ?
ORDER BY opened_date DESC;
```

### Step 3: Spending Pattern Matching

Match user's top categories against each card's bonus categories:

```
User spending:
  Groceries: $680/mo → $8,160/yr
  Dining:    $420/mo → $5,040/yr
  Gas:       $180/mo → $2,160/yr

Candidate A: Amex Gold (4x groceries, 4x dining)
  → Groceries: 8,160 * 4 = 32,640 MR
  → Dining:    5,040 * 4 = 20,160 MR
  → Total annual earn: 52,800 MR ≈ $880+ (at 1.67cpp)

Candidate B: Chase Sapphire Preferred (3x dining, 2x travel)
  → Dining: 5,040 * 3 = 15,120 UR
  → Travel: 2,400 * 2 = 4,800 UR
  → Total annual earn: 19,920 UR ≈ $330+ (at 1.67cpp)
```

### Step 4: LLM Synthesis

Pass filtered candidates + user context to LLM for final recommendation:

**Context provided to LLM:**
- User profile summary (score, 5/24, goals)
- Top 5 spending categories
- Candidate card list (SUB, AF, category multipliers)
- Issuer-specific constraints
- Existing held cards (for ecosystem synergy evaluation)

**LLM must determine:**
- Card priority (which card first?)
- 5/24 slot utilization strategy (Chase first vs. other issuers first)
- Points ecosystem strategy (concentrate UR vs. MR vs. diversify)
- Minimum spend achievability (vs. natural spending)
- Value vs. annual fee assessment
- Auto-upgrade/PC (Product Change) potential (e.g., Chase Freedom Rise auto-upgrades to Chase Freedom Unlimited after 12 months; consider whether this eliminates the need for a separate card application)

### Step 5: Recommendation Output

```
━━━━ Card Recommendation ━━━━
Current status: 750 FICO | 5/24: 3 | Available slots: 2

🥇 #1: Chase Sapphire Preferred
   SUB: 60,000 UR ($4K/3mo)
   Rationale: Utilize 5/24 slots, enter UR ecosystem, natural spend covers SUB
   Timing: Apply immediately
   Est. 1st-year value: SUB $1,000 + earn $330 - AF $95 = $1,235

🥈 #2: Amex Gold
   SUB: 60,000 MR ($6K/6mo)
   Rationale: Optimal for Groceries+Dining spending pattern, highest everyday earn
   Timing: Wait 2–3 months after CSP approval (5/24: 4→5)
   ⚠️ No more new Chase cards after this (5/24 reached)

❌ On hold: Citi Premier
   Reason: 48-month rule — Citi DC opened 2024-08
━━━━━━━━━━━━━━━━━━━━
```

Report saved to `/report/card_recommendation_{date}.md`.

### Step 6: Decision Logging

```jsonl
{"ts":"...","type":"card_recommendation","input":{"score":750,"524":3,"goal":"travel"},"decision":"CSP","reasoning":"5/24 slots available, UR ecosystem entry prioritized","alternatives":["Amex Gold","Citi Premier"],"confidence":"high"}
```

→ Append to `logs/decision_log.jsonl`.

## Important Notes

- Card info must always be verified via real-time search. Re-search if cache is older than 24 hours
- SUB amounts may vary by targeted offer — recommend user verification
- Recommendations are informational, not financial advice — state this clearly
