# db/ — Database Layer

## Overview

SQLite WAL mode-based primary data repository. SQLite was chosen over JSON due to concurrency safety and SQL query flexibility.

## DB Path

- During work: created in session directory then copied to mount folder (SQLite disk I/O constraints)
- Final: `db/credit_planner.db`

## Table List

| Table | Purpose |
|-------|---------|
| `user_profile` | User credit profile (score, AAoA, hard pulls, income, etc.) |
| `user_cards` | List of owned cards (limits, SUB progress, CLI history, retention history) |
| `spending_pattern` | Category-level monthly average spending aggregates |
| `transactions` | Raw transaction data (parsed from statements) |
| `issuer_churning_rules` | Churning rules by issuer (5/24, Once Per Lifetime, etc.) |
| `issuer_cli_policy` | CLI policies by issuer (soft/hard pull, cooldown, etc.) |
| `search_cache` | Real-time web search results cache |
| `user_preferences` | User preferences (general-purpose key-value store) |
| `transaction_exclusions` | Transaction exclusion rules (per-user) |
| `p2p_history` | P2P recipient category history |
| `merchant_aliases` | Merchant description normalization mapping |
| `recurring_transactions` | Recurring transaction detection results |

## Schema Details

### user_profile
```sql
CREATE TABLE user_profile (
    user_id TEXT PRIMARY KEY,
    credit_score INTEGER,
    score_type TEXT,             -- FICO / VantageScore
    aaoa_months INTEGER,        -- Average age of accounts (months)
    total_accounts INTEGER,
    hard_pull_count_24mo INTEGER,
    chase_524_count INTEGER,
    annual_income INTEGER,
    updated_at TIMESTAMP
);
```

### user_cards
```sql
CREATE TABLE user_cards (
    card_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    issuer TEXT,
    card_name TEXT,
    card_type TEXT,              -- credit / charge
    opened_date DATE,
    credit_limit INTEGER,
    starting_limit INTEGER,     -- For Amex 3x rule calculation
    annual_fee INTEGER,
    af_due_date DATE,
    signup_bonus_met BOOLEAN DEFAULT 0,
    signup_bonus_deadline DATE,
    signup_bonus_spend_req INTEGER,
    signup_bonus_progress INTEGER DEFAULT 0,
    product_changed_from TEXT,
    last_cli_request DATE,
    last_cli_result TEXT,
    last_retention_call DATE,
    retention_offer TEXT,
    status TEXT DEFAULT 'active',
    FOREIGN KEY (user_id) REFERENCES user_profile(user_id)
);
```

### user_preferences
```sql
CREATE TABLE user_preferences (
    user_id TEXT,
    pref_key TEXT,               -- Examples: "exclude_tuition", "avoid_issuer", "goal"
    pref_value TEXT,
    description TEXT,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, pref_key)
);
```

### transaction_exclusions
```sql
CREATE TABLE transaction_exclusions (
    exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    rule_type TEXT,              -- contains / exact / regex / amount_gt / amount_lt
    pattern TEXT,                -- Pattern to match (examples: "NELNET", "STEVENS INST")
    match_field TEXT DEFAULT 'description',  -- description / category / card_name
    reason TEXT,                 -- Reason user wants exclusion
    active BOOLEAN DEFAULT 1,
    created_at TIMESTAMP
);
```

## Initialization

```python
from db.init_db import init_db
init_db()  # Create tables + seed churning rules/CLI policies
init_db(reset=True)  # Recreate DB and clear decision/profile logs
```

## Churning Rules Seed (14 issuers)

Chase 5/24, One Sapphire Rule, 2/30 Rule | Amex Once Per Lifetime, 1/5, 2/90, Max 5 Credit Cards | Citi 48 Month, 1/8, 2/65 | Barclays 6/24 | US Bank Conservative | Capital One One Card Per Product | Wells Fargo Cell Phone Rule

## CLI Policy Seed (10 issuers)

Chase (soft, 180 days), Amex credit (soft, 61 days, 3x), Amex charge, Citi (hard!), Discover, Capital One, Barclays (hard!), US Bank, Wells Fargo, Bank of America
