# ml/ — Data Preprocessing + ML Layer

## Overview

LLM handles decision-making. ML specializes in data preprocessing stages (transaction classification, spending prediction).

## Module Structure

```
ml/
├── statement_parser.py         # PDF/CSV → transaction extraction
├── spending_analyzer.py        # Integrated spending analysis pipeline
├── category_classifier/        # Transaction category classification (TF-IDF + LogReg)
│   ├── classifier.py
│   ├── training_data.json      # Training data (seed 235 + LLM distillation)
│   └── model.pkl / vectorizer.pkl
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
from ml.statement_parser import StatementParser
parser = StatementParser()
results = parser.parse_multiple(["stmt1.pdf", "stmt2.pdf"])
all_txs = parser.get_all_transactions(results)
```

---

## spending_analyzer.py — Integrated Spending Analysis

### Pipeline (6 stages)
1. Load user exclusion rules (`transaction_exclusions` table)
2. Statement parsing (PDF/CSV via StatementParser)
3. Transaction classification (5-layer pipeline)
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

## category_classifier/ — 5-Layer Classification Pipeline

### Classification Order
1. **Income Detection**: Salary/refunds/interest/Zelle received → excluded from spending
2. **P2P Handling**: Zelle/Venmo/PayPal → check `p2p_history` → ask user if unknown
3. **Merchant Alias Normalization**: `merchant_aliases` table + amount-based inference for ambiguous merchants (Walmart, etc.)
4. **ML Classification**: TF-IDF + Logistic Regression (adopt if confidence >= 15%)
5. **LLM Fallback + Auto-Distillation**: ML uncertain → LLM inference → auto-add to training data → model retraining

### 12 Categories
groceries, dining, gas, travel, entertainment, utilities, insurance, shopping, transportation, health, education, subscriptions

### LLM Distillation
Seed 235 → 350+ after distillation. LLM fallback ratio: 68% → 8% decrease.

---

## spending_predictor/ — Spending Prediction

### Method
- 3+ months of data: weighted moving average + linear trend detection
- Insufficient data: simple average from spending_pattern table

### Key Functions
- `predict_monthly(user_id, months_ahead)`: Forecast category-level spending for N months
- `can_meet_minimum_spend(user_id, required, months)`: Assess SUB minimum spend achievability

```python
from ml.spending_predictor.predictor import SpendingPredictor
predictor = SpendingPredictor(db_path)
forecast = predictor.predict_monthly("hajin", months_ahead=6)
result = predictor.can_meet_minimum_spend("hajin", 4000, 3)
```
