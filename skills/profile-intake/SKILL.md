# Profile Intake — User Profile Collection

## Purpose

Collect user's credit profile interactively and save it to SQLite DB.

## Trigger Conditions

- First-time user visits
- Existing user requests profile update
- When profile is missing or outdated during card recommendation/CLI strategy request

## Conversation Flow

### Step 1: Check Existing Profile

```sql
SELECT * FROM user_profile WHERE user_id = ?;
```

- **Profile exists**: "Last update: {updated_at}. Any changes?" → Collect only changes
- **No profile**: Start full intake

### Step 2: Collect Required Information

Ask questions naturally and sequentially. Group 2–3 questions together to avoid overwhelming.

**Group A — Basic Profile:**
- Current credit score + type (FICO or VantageScore?)
- Annual income (approximate range is acceptable)

**Group B — Card Accounts:**
- Card list: issuer, card name, open date, current limit
- Annual fee for each card + fee due date
- If recently opened card exists: SUB achievement status

**Group C — Credit History:**
- Hard pulls in last 24 months
- 5/24 count (calculate from card list if user unsure)
- AAoA (calculate from open dates if user unsure)

**Group D — Goals:**
- Primary goal: Cashback / Travel / Hotel / Airline / General
- Preferred airline/hotel chain (if applicable)
- Near-term travel plans (if applicable — relevant for SUB timing)

### Step 3: Auto-Calculate Fields

Derive automatically from user input:
- `aaoa_months`: Average of all card open dates → difference from today (months)
- `chase_524_count`: Count of cards opened in last 24 months
- `total_accounts`: Total number of cards held

### Step 4: Save to DB + Log

```sql
INSERT OR REPLACE INTO user_profile (...) VALUES (...);
INSERT INTO user_cards (...) VALUES (...);  -- repeat per card
```

Append to `logs/profile_log.jsonl`:
```jsonl
{"ts":"...","action":"profile_created","data":{...}}
```

### Step 5: Profile Summary Output

After collection, show user a organized profile:

```
━━━━ Profile Summary ━━━━
Score: 750 (FICO)
5/24: 3/5  |  Hard pulls: 4 (24mo)  |  AAoA: 2 years 3 months
Income: $75,000

Cards Held:
  1. Chase Freedom Flex (2024-01) — $5,000 / AF $0
  2. Amex Gold (2025-03) — Charge / AF $250
  3. Discover it (2023-06) — $8,000 / AF $0

Goal: Travel Points (United preferred)
━━━━━━━━━━━━━━━━━━━━
```

After confirmation, handoff to next skill (card-recommendation or cli-strategy).

## Important Notes

- Allow estimates if user doesn't know exact numbers, but mark as estimated in DB
- Never request sensitive information (SSN, account numbers, etc.)
