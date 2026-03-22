# pipeline/ — Data Processing + Classification Layer

## Overview

LLM handles decision-making and merchant classification. Deterministic rules handle income/P2P/keyword detection. The `merchant_aliases` SQLite table acts as a learning cache populated by LLM results.

## Module Structure

```
pipeline/
├── statement_parser.py         # PDF/CSV → transaction extraction
├── spending_analyzer.py        # Integrated spending analysis pipeline
├── category_classifier/        # Deterministic rules + LLM-backed merchant cache
│   ├── classifier.py           # Classification pipeline
└── spending_predictor/         # Spending pattern prediction (weighted moving avg + trend)
    └── predictor.py
```

---

## statement_parser.py — Statement Parser

### Supported Formats (auto-detected)
1. **Capital One** (Savor, etc.) — MM/DD format in "New Charges" section
2. **Chase Credit** (Freedom Rise, etc.) — "ACCOUNT ACTIVITY" section
3. **Chase Checking** (College Checking, etc.) — "TRANSACTION DETAIL" section

### Format Detection Priority (Important!)
Chase Checking → Capital One → Chase Credit (order matters to avoid misdetection)

### Year-End Crossover Handling
For statements covering 12/03/25~01/02/26, correctly assign year to December transactions.

### Usage
```python
from pipeline.statement_parser import StatementParser
parser = StatementParser()
results = parser.parse_multiple(["stmt1.pdf", "stmt2.pdf"])
all_txs = parser.get_all_transactions(results)
```

---

## spending_analyzer.py — Integrated Spending Analysis

### Pipeline (6 stages)
1. Load user exclusion rules (`transaction_exclusions` table)
2. Statement parsing (PDF/CSV via StatementParser)
3. Transaction classification (deterministic rules + LLM fallback)
4. Apply user exclusion rules (transaction level)
5. SQLite ingestion + Recurring detection
6. Aggregation + Report generation

### Transaction Exclusion System
**Transaction-level** exclusion, not category-wide. When user requests, agent registers rule.

```python
# Agent calls this when user requests
SpendingAnalyzer.add_exclusion_rule(
    db_path, user_id="hajin",
    rule_type="contains",           # contains / exact / regex / amount_gt / amount_lt
    pattern="NELNET",               # Match in description
    match_field="description",
    reason="Exclude tuition payments"
)
```

### User Preference System
General-purpose key-value store for all user preferences.

```python
SpendingAnalyzer.set_preference(db, "hajin", "exclude_tuition", "true", "Tuition exclusion preference")
SpendingAnalyzer.set_preference(db, "hajin", "avoid_issuer", "citi", "Exclude Citi card recommendations")
prefs = SpendingAnalyzer.get_preferences(db, "hajin")
```

### Report Saving
```python
report = analyzer.run(pdf_files=[...], require_resolution=True)
if report.get("status") == "needs_resolution":
    # Normalize freeform answers at the agent layer first, then resolve.
    analyzer.resolve_pending(
        p2p_answers={...},
        llm_answers={...},
    )
    report = analyzer.finalize_after_resolution()

saved_files = analyzer.save_report(report, output_dir="report/")
# → report/spending_analysis_YYYYMMDD.md  (comprehensive)
# → report/monthly/YYYY-MM.md             (monthly detail, includes transaction list)
```

---

## category_classifier/ — Classification Pipeline

### Architecture: Deterministic Rules + LLM + Learning Cache

The classifier uses deterministic rules for clear-cut cases and delegates unknowns to LLM at the agent layer. LLM results are auto-saved to the `merchant_aliases` table (SQLite) for instant future lookups.

### Classification Order
1. **Income Detection** (regex): Salary, refunds, card payments, cashback → excluded from spending
2. **P2P Detection** (regex): Zelle/Venmo/PayPal → check `p2p_history` → ask user if unknown
3. **Keyword Shortcuts** (regex): Strong signals like AIRLINES→travel, PHARMACY→health, MORTGAGE→housing, etc.
4. **Merchant Alias Lookup** (SQLite): Normalized description matched against `merchant_aliases` table (learning cache)
5. **Ambiguous Merchant Rules**: Amount-based inference for Walmart/Costco/Target/Amazon/etc.
6. **LLM Fallback** (agent layer): Unknown merchants → Haiku/Sonnet classifies → result saved to `merchant_aliases` via `distill_from_llm()`

### 14 Categories
groceries, dining, gas, travel, entertainment, utilities, insurance, shopping, transportation, health, education, subscriptions, housing, fees

---

## spending_predictor/ — Spending Prediction

### Method
- 3+ months of data: weighted moving average + linear trend detection
- Insufficient data: simple average from spending_pattern table

### Key Functions
- `predict_monthly(user_id, months_ahead)`: Forecast category-level spending for N months
- `can_meet_minimum_spend(user_id, required, months)`: Assess SUB minimum spend achievability

```python
from pipeline.spending_predictor.predictor import SpendingPredictor
predictor = SpendingPredictor(db_path)
forecast = predictor.predict_monthly("hajin", months_ahead=6)
result = predictor.can_meet_minimum_spend("hajin", 4000, 3)
```
