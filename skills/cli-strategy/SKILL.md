# CLI Strategy — Credit Limit Increase Strategy

## Purpose

Evaluate optimal timing for CLI (Credit Limit Increase) requests across held cards, score each candidate by priority, and provide a comprehensive CLI strategy.

## Trigger Conditions

- User requests CLI strategy
- Called as part of timeline roadmap generation
- CLI timing guidance needed after opening a new card
- High utilization requires score improvement

## Prerequisites

- Card data exists in `user_cards` table
- Policy seeds in `issuer_cli_policy` table

## Processing Flow

### Step 1: Scan CLI-Eligible Cards

```sql
SELECT
    uc.card_name, uc.issuer, uc.card_type, uc.opened_date,
    uc.credit_limit, uc.starting_limit,
    uc.last_cli_request, uc.last_cli_result,
    icp.pull_type, icp.min_wait_days, icp.cooldown_days,
    icp.max_multiplier, icp.request_method
FROM user_cards uc
JOIN issuer_cli_policy icp
    ON uc.issuer = icp.issuer AND uc.card_type = icp.card_type
WHERE uc.user_id = ? AND uc.status = 'active';
```

### Step 2: Eligibility Assessment (Per Card)

Check the following conditions for each card:

**A. Minimum Wait Period:**
```python
days_since_open = (today - opened_date).days
eligible = days_since_open >= min_wait_days
```

**B. Cooldown Check (if previous request exists):**
```python
if last_cli_request:
    days_since_last = (today - last_cli_request).days
    cooldown_ok = days_since_last >= cooldown_days
    # Be more conservative after a denial
    if last_cli_result == 'denied':
        cooldown_ok = days_since_last >= cooldown_days * 1.5
```

**C. 3x Rule Check (Amex, etc.):**
```python
if max_multiplier and starting_limit:
    target_limit = starting_limit * max_multiplier
    room = target_limit - credit_limit
    has_room = room > 0
```

**D. Utilization Impact:**
```python
total_credit = sum(all_card_limits)
current_util = total_balance / total_credit
# Impact of this card's CLI on overall utilization
new_util = total_balance / (total_credit + expected_increase)
util_improvement = current_util - new_util
```

### Step 3: Priority Scoring

Assign a score to each CLI candidate:

```
score = base_score + soft_pull_bonus + util_impact + timing_bonus

base_score:
  - All conditions met: 100
  - Cooldown not elapsed: 0 (exclude)

soft_pull_bonus:
  - Soft pull: +50
  - Hard pull: +0 (must check for conflicts with new card plans)

util_impact:
  - Per 1%p utilization improvement: +20

timing_bonus:
  - 6+ months since opening: +10
  - 12+ months since opening: +20
  - Amex 3x not yet reached: +30
```

**Hard Pull CLI Special Handling:**
HP CLIs (Citi, Barclays, etc.) must be coordinated with new card application plans:
- New card application planned within 3 months → defer HP CLI
- No new card plans → HP CLI may proceed

### Step 4: Strategy Output

```
━━━━ CLI Strategy ━━━━
Total held credit: $25,000 | Current utilization: 32%

✅ Ready to request now:
  1. Amex Gold → CLI (soft pull, online)
     Current $6,000 | Starting $6,000 | 3x target: $18,000
     Expected limit: $12,000–$18,000
     Utilization improvement: 32% → 24–21%

  2. Chase Freedom Flex → CLI (soft pull, SM)
     Current $5,000 | Opened 14 months ago
     Expected limit: $8,000–$12,000
     Combined utilization improvement: 18–15%

⏳ Wait required:
  3. Discover it → Available in 47 days (cooldown in progress)

⚠️ Caution:
  4. Citi Double Cash → CLI is a HARD PULL
     Current $7,000 | CSP application planned this month — recommend deferring
━━━━━━━━━━━━━━━━━━━━
```

Report saved to `/report/cli_strategy_{date}.md`.

### Step 5: Result Recording

After CLI request, confirm results with user:
```sql
UPDATE user_cards
SET last_cli_request = ?, last_cli_result = ?, credit_limit = ?
WHERE card_id = ?;
```

Append to `logs/decision_log.jsonl`:
```jsonl
{"ts":"...","type":"cli_recommendation","card":"Amex Gold","action":"request_cli","reasoning":"61 days elapsed, 3x not reached, soft pull","priority_score":180}
```

Append to `logs/profile_log.jsonl` (after result):
```jsonl
{"ts":"...","action":"cli_result","data":{"card":"Amex Gold","old_limit":6000,"new_limit":15000,"pull_type":"soft"}}
```

## Important Notes

- Hard pull CLI must always be disclosed to user beforehand (score impact)
- Amex Financial Review (FR) risk: aggressive CLI + high spending may trigger FR
- CLI denial itself doesn't directly affect score, but HP CLI adds a hard inquiry
