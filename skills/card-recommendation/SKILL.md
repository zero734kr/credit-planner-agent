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

### Step 1: Category-Driven Real-Time Search

**On every recommendation request**, determine the user's spending profile first, then construct search queries that reflect their actual spending. Never recommend based on stale data.

#### Data Tier System

The agent must assess which data tier applies and construct search queries accordingly:

**Tier 1 — Spending analysis available** (statements were analyzed, `spending_pattern` table populated)

```sql
-- Pull top spending categories from DB
SELECT category, SUM(amount) as total
FROM spending_pattern WHERE user_id = ?
GROUP BY category ORDER BY total DESC LIMIT 5;
```

Build search queries from the user's actual top categories:

```
If top categories = housing ($2,100/mo), groceries ($680/mo), dining ($420/mo):
  → "best credit card for rent payments {year}"
  → "best credit card for groceries {year} signup bonus"
  → "best credit card dining rewards {year}"
  → "doctor of credit best current offers"
```

**Tier 2 — User-stated spending priorities** (no statement data, but user said things like "I spend a lot on groceries and dining")

Same query shape as Tier 1, just sourced from conversation instead of DB:

```
User says "groceries and dining are my biggest categories":
  → "best credit card for groceries {year} signup bonus"
  → "best credit card dining rewards {year}"
  → "doctor of credit best current offers"
```

**Tier 3 — Profile + goal only** (score, income, reward goal — no spending breakdown)

```
User wants travel rewards, has 750 FICO:
  → "best travel credit card {year} signup bonus"
  → "best credit card for beginners travel rewards {year}"
  → "doctor of credit best current offers"
```

**Tier 4 — Minimal info** (just a goal or nothing specific)

```
  → "best credit cards {year} signup bonus"
  → "doctor of credit best current offers {year}"
  → "best starter credit cards for beginners no credit history {year}"
  → Ask clarifying questions in parallel with search
```

**Search source priority:**
1. Official issuer websites
2. Doctor of Credit — Best Current Credit Card Offers
3. US Credit Card Guide
4. The Points Guy — current offers

Cache search results in `search_cache` table (TTL: 24 hours).

### Step 2: Hard Constraint Filtering

Remove ineligible cards via SQLite queries first. Apply issuer-specific rules for all relevant issuers.

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
  Housing:   $2,100/mo → $25,200/yr
  Groceries: $680/mo   → $8,160/yr
  Dining:    $420/mo   → $5,040/yr

Candidate A: Amex Gold (4x groceries, 4x dining)
  → Groceries: 8,160 * 4 = 32,640 MR
  → Dining:    5,040 * 4 = 20,160 MR
  → Total annual earn: 52,800 MR ≈ $880+ (at 1.67cpp)

Candidate B: [Card with rent/housing rewards]
  → Housing: 25,200 * Nx = significant annual earn
  → This category alone may outweigh other cards' total value

Candidate C: Chase Sapphire Preferred (3x dining, 2x travel)
  → Dining: 5,040 * 3 = 15,120 UR
  → Travel: 2,400 * 2 = 4,800 UR
  → Total annual earn: 19,920 UR ≈ $330+ (at 1.67cpp)
```

When `housing` is a top-3 spending category, the agent should note that rent-specific cards can deliver outsized value because of the sheer monthly dollar volume.

### Step 4: LLM Synthesis

Pass filtered candidates + user context to LLM for final recommendation:

**Context provided to LLM:**
- User profile summary (score, 5/24, goals)
- Top 5 spending categories with monthly amounts
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
Top spending: Housing $2,100/mo | Groceries $680/mo | Dining $420/mo

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

🥉 #3: [Rent-optimized card]
   Rationale: Housing is your single largest expense — earning rewards on $25K+/yr
   is significant value that most people leave on the table
   Timing: After evaluating whether your landlord accepts credit card payments

❌ On hold: Citi Premier
   Reason: 48-month rule — Citi DC opened 2024-08
━━━━━━━━━━━━━━━━━━━━
```

Report saved to `/report/card_recommendation_{date}.md`.

### Step 6: Decision Logging

```jsonl
{"ts":"...","type":"card_recommendation","input":{"score":750,"524":3,"goal":"travel","top_categories":["housing","groceries","dining"]},"decision":"CSP","reasoning":"5/24 slots available, UR ecosystem entry prioritized","alternatives":["Amex Gold","Citi Premier"],"confidence":"high"}
```

→ Append to `logs/decision_log.jsonl`.

## Important Notes

- Card info must always be verified via real-time search. Re-search if cache is older than 24 hours
- SUB amounts may vary by targeted offer — recommend user verification
- Recommendations are informational, not financial advice — state this clearly
- When housing appears as a top spending category, the search queries should naturally surface rent-optimized cards — do not hardcode specific card names
