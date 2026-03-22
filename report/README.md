# report/ — Generated Analysis Reports

## Overview

Folder for all output artifacts (spending analysis, roadmaps, CLI strategy, etc.).

## Structure

```
report/
├── spending_analysis_YYYYMMDD.md   # Comprehensive spending analysis report
├── monthly/                         # Monthly detailed reports
│   ├── 2025-09.md
│   ├── 2025-10.md
│   └── ...
└── README.md
```

## Comprehensive Report (spending_analysis_YYYYMMDD.md)

Contents:
- Analysis period and transaction count
- Summary of excluded transactions (user exclusion rules applied)
- Spending by card
- Category-level monthly average (bar charts)
- Monthly detailed breakdown
- Recurring transaction detection results
- P2P recipient list requiring user confirmation
- LLM classification required list
- Classification method statistics

## Monthly Detailed Report (monthly/YYYY-MM.md)

Contents:
- Total spending for the month
- Category-level spending (bar charts)
- Detailed transaction list (date, category, amount, description)
- Excluded transaction list (if any)

## Generation Method

```python
from pipeline.spending_analyzer import SpendingAnalyzer
analyzer = SpendingAnalyzer(db_path="db/credit_planner.db", user_id="user001")
report = analyzer.run(pdf_files=[...])
saved_files = analyzer.save_report(report)  # Auto-saves to report/
```
