# CreditPlanner Guide for Humans

> This guide is for those new to using the CreditPlanner AI agent.
> CreditPlanner is an interactive AI agent that analyzes your credit profile and provides recommendations based on natural language.
> No commands to memorize; just talk to it naturally.

---

## 1. What is CreditPlanner?

CreditPlanner is an AI agent that builds credit card strategies for you. Specifically, it:

- Analyzes your spending patterns and recommends which cards offer the best value
- Creates a roadmap showing when and in what order to apply for new cards
- Identifies the best timing to request Credit Limit Increases (CLI)
- Helps you decide whether to keep, downgrade, or close cards when annual fees arrive
- Predicts whether you can hit signup bonus minimum spend based on your natural spending

**Important**: The recommendations provided by this system are for informational purposes only and are not financial advice.

---

## 2. Getting Started: Profile Registration

When you first use the agent, it will collect your profile information through conversation. Here's what to prepare:

**Basic Information**
- Credit score (FICO or VantageScore — approximate figures are fine)
- Annual income (a rough range is acceptable)

**Current Cards**
- For each card: issuer, card name, and opening date
- Current credit limit
- Annual fee amount and when it's due
- If you recently opened a card, your progress toward the signup bonus minimum spend

**Credit History**
- Number of hard pulls in the last 24 months (if unsure, the agent can calculate from your card list)
- Your Chase 5/24 count (if unsure, the agent can calculate this)

**Goals**
- Do you prefer cashback, travel points, hotel rewards, or airline miles?
- Any preferred airline or hotel chains?
- Any travel plans coming up soon?

### Example prompts

> "I want to register my profile. My FICO is 740 and I make $80k a year. I opened Chase Freedom Flex in January last year with a $5,000 limit, and Discover it in June 2023 with an $8,000 limit. I have 2 hard pulls."

If you don't know exact figures, you can just say "I'm not sure." The agent can estimate from other information or you can update it later. The agent will never ask for sensitive information like your SSN or account numbers.

---

## 3. Spending Analysis

### Uploading Statements

Upload your credit card or bank statements (PDF or CSV format), and the agent will automatically extract transactions and categorize them.

Supported PDF formats:
- Capital One (Savor, etc.)
- Chase Credit (Freedom Rise, etc.)
- Chase Checking (College Checking, etc.)
- CSV is universally supported (columns detected automatically)

### Example prompts

> "Analyze these statements for me" (upload files)

> "Show me my spending patterns for the last 6 months"

### Category Classification

Transactions are classified into 12 categories: groceries, dining, gas, travel, entertainment, utilities, insurance, shopping, transportation, health, education, subscriptions.

Classification is handled automatically through a 5-step pipeline. Most transactions are categorized automatically, but the agent may ask you about:

- **P2P transfers (Zelle, Venmo, etc.)**: "What was the $30 payment to John for?" — Once you answer, future transfers to the same recipient are automatically categorized the same way.
- **Amount-based inference at merchants**: A $150 Walmart transaction is inferred as groceries, while a $15 one is shopping. Similarly, Wawa/Sheetz transactions of $20+ are inferred as gas, under $20 as dining.

### Excluding Specific Transactions

If you want certain transactions excluded from spending analysis (for example, tuition, one-time large purchases):

> "Exclude transactions containing NELNET. Those are student loan payments."

> "Exclude transactions over $5,000"

> "Exclude only transactions that exactly match STEVENS INST"

You can specify exclusion rules in these ways:

| Method    | Description                           | Example                 |
| --------- | ------------------------------------- | ----------------------- |
| contains  | Excludes if description contains text | "NELNET" → excluded     |
| exact     | Excludes if description matches exact | "STEVENS INST OF TECH"  |
| regex     | Excludes matching a pattern           | "UNIV.*TUITION" pattern |
| amount_gt | Excludes if amount exceeds threshold  | Over $5,000 excluded    |
| amount_lt | Excludes if amount is below threshold | Under $1 excluded       |

Once registered, rules apply automatically to future analyses.

> "Show me my current exclusion rules"

You can see the complete list of registered rules with this command.

### Viewing Reports

After analysis, two types of reports are saved in the `report/` folder:

1. **Summary Report** (`report/spending_analysis_YYYYMMDD.md`) — category breakdown across the full period, recurring charges, classification stats, etc.
2. **Monthly Details** (`report/monthly/YYYY-MM.md`) — all transactions for that month, breakdown by category, excluded items, etc.

---

## 4. Card Recommendations

Once your profile and spending patterns are on file, you can get card recommendations.

### Example prompts

> "Recommend a new card for me"

> "What card should I apply for next?"

> "Recommend cashback cards" / "Suggest travel points cards"

### What the AI considers

- **Issuer rules**: Chase 5/24, Amex Once Per Lifetime, Citi 48-month rule, etc. — cards you wouldn't be approved for are automatically filtered out
- **Velocity rules**: Chase 2/30, Amex 1/5, Amex 2/90, etc. — prevents applying for too many cards in quick succession
- **Spending match**: Prioritizes cards with high multipliers in your top spending categories
- **SUB value**: Signup bonus size and achievability based on your natural spending
- **Ecosystem synergy**: Synergy with your existing cards in the UR, MR, or other points ecosystems
- **Value vs. annual fee**: Net benefit after subtracting the annual fee from estimated first-year value

**Important**: Card information (SUB amounts, annual fees, etc.) is fetched in real time from the web every time. Recommendations are never based on stale data.

Each recommendation includes the rationale, optimal application timing, and estimated first-year value.

---

## 5. CLI (Credit Limit Increase) Strategy

### Example prompts

> "Give me a CLI strategy"

> "Which cards can I request a limit increase on?"

> "Should I request a CLI on my Amex Gold?"

### What the AI evaluates

- **Soft pull vs. hard pull**: Each issuer handles CLI requests differently. Hard pull CLIs (Citi, Barclays, etc.) are coordinated so they don't conflict with upcoming card application plans.
- **Cooldown period**: Minimum waiting period since the last CLI request
- **3x rule (Amex)**: Amex allows soft pull CLIs up to 3x your starting limit
- **Utilization improvement**: Calculates how much a CLI would improve your overall utilization ratio

After you request a CLI, just tell the AI the result and it will be recorded:

> "Got approved for Amex Gold CLI, went from $6K to $15K"

---

## 6. Annual Fee Management

The AI will alert you 60 days before an annual fee is due on any of your cards.

### Example prompts

> "Do I have any annual fees coming up?"

> "My CSP annual fee is due—should I keep it or not?"

> "Give me a retention call guide"

### Decision criteria

The AI compares the value you get from the card over a year (points earned, statement credits, perks) against the annual fee.

- **Value > AF x 1.2**: Recommend keeping (comfortable margin)
- **Value > AF**: Recommend trying a retention call (tight margin)
- **Value < AF**: Retention call first — if no offer, downgrade (PC) or cancel

The AI provides a call script and tips before you make a retention call. Just share the result afterward:

> "Called about my CSP, got a $50 statement credit offer"

> "No offer, so I product changed to CFF"

---

## 7. Roadmap (Timeline)

### Example prompts

> "Build me a 24-month roadmap"

> "Create a card strategy plan for the next year"

### What's included

The roadmap is a comprehensive plan that places all strategies (card recommendations + CLI + annual fees) on a timeline. You can see at a glance what actions to take each month:

- When and in what order to apply for new cards
- CLI request timing
- Minimum spend completion schedule
- Upcoming annual fees and recommended actions
- 5/24 count changes over time

The roadmap is a living document. You can request updates whenever something changes—a new card is opened, a CLI result comes in, or circumstances shift:

> "I got approved for CSP, update my roadmap"

---

## 8. Preference Settings

Save your personal preferences once, and they'll be applied automatically going forward—no need to repeat yourself.

### Example prompts

> "Exclude Citi cards from recommendations"

> "Focus on travel points over cashback"

> "Always exclude tuition-related transactions from spending analysis"

> "Show me my preferences"

---

## 9. FAQ

**Q: What is 5/24?**
It's a Chase rule. If you've opened 5 or more new credit cards (from any issuer) in the past 24 months, Chase will automatically deny new card applications. This is why application order matters—the common strategy is to get Chase cards first, then move on to other issuers.

**Q: Does a CLI affect my credit score?**
Soft pull CLIs have no impact on your score. Hard pull CLIs (Citi, Barclays, etc.) add a hard inquiry, which may cause a small, temporary score dip. The AI will always warn you before recommending a hard pull CLI.

**Q: What is a retention offer?**
When you call to cancel a card with an annual fee, the issuer may offer incentives to keep you—statement credits, bonus points, or an annual fee waiver. Even if the first representative says no offer is available, HUCA (Hang Up, Call Again) often works.

**Q: What is a Product Change (PC)?**
It's converting one card to another within the same issuer. For example, you can convert a Chase Sapphire Preferred ($95 AF) to a no-annual-fee Chase Freedom Flex. The advantage is that your account history is preserved, so it doesn't affect your AAoA (Average Age of Accounts).

**Q: Will re-uploading a statement cause duplicates?**
No. Transactions with the same date, amount, and description are automatically deduplicated.

**Q: What if my profile information changes?**
Just tell the AI:

> "My credit score went up to 760"

> "I opened a new card — Amex Gold, opened March 15, no preset limit (charge card)"

All changes are recorded automatically.

---

## 10. File Structure (Reference)

You don't need to know this for everyday use, but if you're curious about what files are generated:

```
CreditPlanner/
├── db/credit_planner.db       ← Main database storing all data
├── report/                    ← Generated reports
│   ├── spending_analysis_*.md ← Comprehensive spending analysis
│   ├── monthly/YYYY-MM.md    ← Monthly detail reports
│   ├── card_recommendation_*.md
│   ├── cli_strategy_*.md
│   ├── retention_*.md
│   └── roadmap_*.md
└── logs/                      ← Decision history (auto-generated)
    ├── decision_log.jsonl
    └── profile_log.jsonl
```

---

## 11. Quick Start Checklist

For the most efficient onboarding, follow these steps in order:

1. **Register your profile** — Share your credit score, current cards, and goals
2. **Upload statements** — Upload your last 3–6 months of statements
3. **Review spending analysis** — Answer any questions the AI asks (e.g., about P2P transfers)
4. **Get card recommendations** — "Recommend a new card for me"
5. **Generate a roadmap** — "Build me a 24-month roadmap"

After that, just update whenever things change. Let the AI know about new cards opened, CLI results, credit score changes, etc., and it will recalibrate your roadmap accordingly.
