# pipeline/ — Data Processing + Classification Layer

## Overview

LLM handles decision-making and merchant classification. Deterministic rules handle income/P2P/keyword detection. The `merchant_aliases` SQLite table acts as a learning cache populated by LLM results.

## Module Structure

```
pipeline/
├── spending_analyzer.py               # Orchestrator — runs the full pipeline
├── resolution.py                      # P2P/LLM pending resolution logic
├── exclusions.py                      # Transaction exclusion rule CRUD + matching
├── preferences.py                     # User preference CRUD (key-value store)
├── db_writer.py                       # DB ingestion, loading, recurring detection, aggregation
├── report_writer.py                   # Report generation + file saving
├── statement_parser.py                # Dispatcher — routes files to format parsers
├── parsers/
│   ├── helpers.py                     # Shared date parsing utilities
│   ├── capital_one.py                 # Capital One statement parser
│   ├── chase_credit.py                # Chase credit card statement parser
│   ├── chase_checking.py              # Chase checking account statement parser
│   └── csv_parser.py                  # Generic CSV parser (auto-detect columns)
├── category_classifier/
│   ├── patterns.py                    # All constants: categories, regex, keyword shortcuts
│   └── classifier.py                  # 5-layer deterministic classification pipeline
└── spending_predictor/
    └── predictor.py                   # Weighted moving avg + trend forecasting
```

---

## spending_analyzer.py — Pipeline Orchestrator

### Pipeline (7 steps)
1. Load user exclusion rules (`transaction_exclusions` table)
2. Statement parsing (PDF/CSV via StatementParser)
3. Transaction classification (deterministic rules + LLM fallback)
4. Apply user exclusion rules (transaction level)
5. Resolve pending P2P/LLM classifications
6. SQLite ingestion + Recurring detection
7. Aggregation + Report generation

### Usage
```python
from pipeline.spending_analyzer import SpendingAnalyzer
analyzer = SpendingAnalyzer(db_path, user_id="user001")
report = analyzer.run(pdf_files=[...], require_resolution=True)
if report.get("status") == "needs_resolution":
    analyzer.resolve_pending(p2p_answers={...}, llm_answers={...})
    report = analyzer.finalize_after_resolution()
saved_files = analyzer.save_report(report, output_dir="report/")
```

---

## exclusions.py — Transaction Exclusion System

**Transaction-level** exclusion, not category-wide. Agent registers rules on user request.

```python
from pipeline.exclusions import add_exclusion_rule
add_exclusion_rule(db_path, "user001", "contains", "NELNET", reason="Exclude tuition")
```

Rule types: `contains`, `exact`, `regex`, `amount_gt`, `amount_lt`

---

## preferences.py — User Preferences

```python
from pipeline.preferences import set_preference, get_preferences
set_preference(db_path, "user001", "preferred_alliance", "skyteam", "Prefer SkyTeam")
prefs = get_preferences(db_path, "user001")
```

---

## category_classifier/ — Classification Pipeline

### Architecture: Deterministic Rules + LLM + Learning Cache

### Classification Order (patterns.py → classifier.py)
1. **Income Detection** (regex): Salary, refunds, card payments, cashback → excluded
2. **P2P Detection** (regex): Zelle/Venmo/PayPal → check `p2p_history` → ask user
3. **Keyword Shortcuts** (regex): AIRLINES→travel, PHARMACY→health, MORTGAGE→housing
4. **Merchant Alias Lookup** (SQLite): Normalized description → `merchant_aliases` table
5. **Ambiguous Merchant Rules**: Amount-based for Walmart/Costco/Target/Amazon
6. **LLM Fallback** (agent layer): Unknown → Haiku/Sonnet → saved via `distill_from_llm()`

### 14 Categories
groceries, dining, gas, travel, entertainment, utilities, insurance, shopping, transportation, health, education, subscriptions, housing, fees

---

## spending_predictor/ — Spending Prediction

### Data Sources (priority order)
1. **`transactions` table** — actual monthly totals grouped by category. Used when transaction history exists.
2. **`spending_pattern` table** — fallback. Pre-aggregated monthly averages (populated by `_aggregate_spending()`). Used when no individual transaction rows are available.

Income, card payments, and uncategorized transactions are excluded from all forecasts.

### Forecasting Method

**With 3+ months of data** (`predict_monthly`):
- **Weighted moving average**: Recent months weighted higher (`weights = [1, 2, 3, ...]`). Captures recency bias — if you started eating out more recently, the forecast reflects that.
- **Linear trend detection**: `numpy.polyfit` degree-1 on monthly totals. Slope = monthly change rate. Labeled as `increasing` (>$50/mo), `decreasing` (<-$50/mo), or `stable`.
- Forecast = WMA projected forward, not WMA + trend extrapolation. The trend label is informational — it doesn't inflate the prediction.

**With <3 months of data**: Simple arithmetic mean. Trend labeled `insufficient data`.

### Known Limitations
- **Partial months**: If the current month is mid-cycle, its partial total pulls the average down. No partial-month normalization yet.
- **No seasonality**: Holiday spending spikes (Nov-Dec) and summer travel aren't modeled. Would need 12+ months of data and a seasonal decomposition approach.
- **Trend is linear**: A sudden spending change (e.g., new subscription) registers as a trend, but the magnitude takes months to stabilize.
- **Category-level only**: No merchant-level prediction. "Groceries went up" — but not "Costco vs Whole Foods."

### Minimum Spend Feasibility (`can_meet_minimum_spend`)

Assesses whether natural spending can cover a signup bonus requirement:
- Sums all category monthly averages → projected total over N months
- Accepts `extra_monthly` parameter for redirectable fixed expenses (insurance, utilities onto new card)
- Returns: `feasible` (bool), `gap` (shortfall amount), `daily_needed`, and a suggestion string if infeasible

### Usage
```python
from pipeline.spending_predictor.predictor import SpendingPredictor
predictor = SpendingPredictor(db_path)

# Category-level forecast
forecast = predictor.predict_monthly("user001", months_ahead=6)
# → {"groceries": {"monthly_avg": 650.0, "predicted_total": 3900.0, "trend": "stable", ...}, ...}

# SUB feasibility check
result = predictor.can_meet_minimum_spend("user001", required_amount=4000, months=3)
# → {"feasible": True, "projected_total": 4650.0, "gap": 0, ...}

# With redirected spend (e.g., moving insurance onto new card)
result = predictor.can_meet_minimum_spend("user001", 6000, 3, extra_monthly=500)
```
