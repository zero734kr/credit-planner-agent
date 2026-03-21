# Statement Analysis — Spending Analysis + Category Classification

## Purpose

Parse user-submitted bank/card statements (PDF/CSV), classify transactions by category, analyze spending patterns, and store results in the DB.

## Trigger Conditions

- User uploads statement files (PDF/CSV)
- User requests spending pattern analysis
- Minimum spend achievability assessment is needed

## Processing Pipeline

### Step 1: File Parsing

**CSV:**
```python
import pandas as pd
df = pd.read_csv(filepath)
# Auto-detect columns: date, description/merchant, amount, category (if present)
```

**PDF:**
```python
import pdfplumber
with pdfplumber.open(filepath) as pdf:
    for page in pdf.pages:
        table = page.extract_table()
```

Normalize to common schema:
```
tx_date | description | amount | card_name | source
```

### Step 2: 5-Layer Classification Pipeline

All transactions pass through the following layers in order. If a higher layer resolves the classification, lower layers are skipped.

#### Layer 0: Income/Non-expense Detection

Salary, refunds, interest, Zelle received, etc. are not "spending" and are excluded from analysis:
```
PAYROLL → income (exclude)
TAX REFUND → income (exclude)
REFUND AMAZON → income (exclude)
ZELLE FROM EMPLOYER → income (exclude)
```

#### Layer 1: P2P Transfers (Zelle, Venmo, PayPal, CashApp)

P2P transfers have unknown purpose, so user must be asked:

```
[Auto] "ZELLE TO JOHN DOE" detected → check p2p_history
  ├─ Previous record exists (e.g., JOHN DOE → dining)
  │   → "Previously classified transfers to JOHN DOE as 'dining'. Same category this time?"
  │   → User confirms/changes → update p2p_history if consistent
  └─ No previous record
      → "You sent $30 to JOHN DOE — which spending category best fits this transfer?"
      → Agent normalizes the user's answer into one of the 12 canonical categories (or `skip`)
      → Save to p2p_history only when the recipient has a consistent category pattern
```

**Key**: If you Zelle a friend for splitting dinner → dining. If the user answers in freeform language, the agent should normalize that into the closest canonical spending category before calling the Python analyzer.

#### Layer 2: Merchant Alias Normalization + Ambiguous Merchants

**Normalization**: Unify "WHOLEFDS MKT 10293" and "WHOLE FOODS #1029" as the same merchant.
Match in `merchant_aliases` table and convert to canonical name.

**Ambiguous**: Walmart, Costco, Target, etc. use amount-based inference:
```
TARGET $150 → groceries (high probability of bulk grocery purchase)
TARGET $25  → shopping (small amount = likely general merchandise)
WALMART $200 → groceries
WALMART $15  → shopping
```
When amount alone can't decide (Amazon, etc.), pass to ML.

#### Layer 3: ML Classification (TF-IDF + Logistic Regression)

Classify using `ml/category_classifier/` model. Adopt if confidence >= 15%.

**14 categories:**
groceries, dining, gas, travel, entertainment, utilities, insurance, shopping, transportation, health, education, subscriptions, housing, fees

#### Layer 4: LLM Fallback + Auto-Distillation

ML confidence < 15% → pass description to LLM for category inference:

```
LLM Prompt:
"Classify the following transaction into a category.
 Description: {description}
 Amount: ${amount}
 Possible categories: groceries, dining, gas, travel, entertainment,
 utilities, insurance, shopping, transportation, health, education,
 subscriptions, housing, fees
 Reply with a single category word only."
```

LLM/user-resolved merchant results are **automatically added to training data** (distillation):
```python
classifier.distill_from_llm(description, llm_category)
# → append to training_data.json → retrain model
# → next time, similar descriptions are handled by ML directly
```

#### Layer 5: User Question (Last Resort)

When even LLM is uncertain:
```
"Could not classify this transaction:
 OBSCURE MERCHANT 99881 — $47.00
 What category is this?"
→ User responds → add to training data + retrain model
```

### Step 3: Recurring Transaction Detection

When the same merchant bills similar amounts monthly:
```python
# 2+ occurrences of same merchant + similar amount (±10%) → tag as recurring
INSERT INTO recurring_transactions (user_id, merchant_pattern, typical_amount, category, frequency)
```
- Auto-detect subscriptions: Netflix $15.99, Spotify $9.99, etc.
- Separate fixed expenses: insurance, utilities, rent, etc.
- Separate fixed vs. variable costs for more accurate minimum spend forecasting

### Step 4: SQLite Ingestion

Only proceed to DB ingestion after unresolved `P2P` and `needs_llm` items have been resolved for a final run. If any remain, return a `needs_resolution` result to the caller instead of ingesting and reporting prematurely.

```sql
INSERT INTO transactions (user_id, tx_date, description, amount, category, source, card_name)
VALUES (?, ?, ?, ?, ?, ?, ?);
```

### Step 5: Spending Pattern Aggregation

```sql
SELECT category, ROUND(AVG(monthly_total)) as monthly_avg
FROM (
    SELECT category, strftime('%Y-%m', tx_date) as month, SUM(amount) as monthly_total
    FROM transactions WHERE user_id = ? AND category != 'income'
    GROUP BY category, month
)
GROUP BY category;
```

Update results in `spending_pattern` table (UPSERT).

### Step 6: Analysis Report Output

```
━━━━ Spending Analysis Report ━━━━
Period: 2025-10 ~ 2026-03 (6 months)
Total transactions: 347 | Total spending: $14,832 | Excluded: 23 income

Monthly average by category:
  Groceries     $680  ████████████████████░░░░  28%
  Dining        $420  ████████████░░░░░░░░░░░░  17%
  Gas           $180  █████░░░░░░░░░░░░░░░░░░░   7%
  Shopping      $350  ██████████░░░░░░░░░░░░░░  14%
  Travel        $200  ██████░░░░░░░░░░░░░░░░░░   8%
  Education     $150  ████░░░░░░░░░░░░░░░░░░░░   6%
  Other         $492  █████████████░░░░░░░░░░░  20%

Recurring payments detected:
  Netflix       $15.99/mo   (entertainment)
  Spotify       $9.99/mo    (entertainment)
  GEICO         $145.00/mo  (insurance)

Card optimization insights:
  → Groceries $680/mo: Amex Gold 4x MR = 2,720 MR/mo
  → Dining $420/mo: Amex Gold 4x MR = 1,680 MR/mo
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Report saved to `/report/spending_analysis_{date}.md`.

Before saving the final report, resolve any pending `P2P` questions with the user and any `needs_llm` transactions through the LLM fallback path. Batch P2P prompts together, then regenerate/finalize the report only after those items are classified.

Recommended module flow:
```python
from ml.spending_analyzer import SpendingAnalyzer

analyzer = SpendingAnalyzer(db_path="db/credit_planner.db", user_id="hajin")
result = analyzer.run(pdf_files=[...], require_resolution=True)

if result.get("status") == "needs_resolution":
    # Normalize freeform user answers at the agent layer first.
    analyzer.resolve_pending(
        p2p_answers={...},   # transaction-level user answers
        llm_answers={...},   # resolved low-confidence merchants
    )
    result = analyzer.finalize_after_resolution()

saved_files = analyzer.save_report(result)
```

### Step 7: Minimum Spend Achievability Assessment

```
Card: Amex Gold | SUB: $6,000 / 6 months
Natural monthly spend: $2,472 (fixed $420 + variable $2,052)
6-month projection: $14,832 → ✅ Sufficient ($8,832 headroom)
```

## Virtuous Data Cycle

```
New statement uploaded
  → Layer 3: ML can't classify some items
  → Layer 4: LLM infers categories
  → distill_from_llm() auto-adds to training data
  → Model retrained
  → Next statement: ML handles directly
  → Over time, LLM fallback ratio decreases
```

## Important Notes

- If statements contain personally identifiable info (account numbers, etc.), mask during parsing
- Prevent duplicate uploads of same statement (dedup by source + tx_date + amount + description)
- Refunds (negative amounts) are classified as income and excluded from spending analysis
- Batch P2P questions together (don't ask 10 Zelle transfers one by one)
