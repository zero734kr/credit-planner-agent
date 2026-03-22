# CreditPlanner — Architecture Decision Log

> Hajin (design/direction) + Claude (implementation/research)
> Append-only. New entries added when we hit a decision point during development.

---

## #1 — SQLite over JSON, Real-Time Search over Static DB

**Hajin**: Rejected JSON file storage due to concurrency concerns. Chose SQLite for transactional safety. Card data (SUBs, AFs, bonus categories) changes frequently — opted for exhaustive real-time web search on every planning request instead of maintaining a static card database.

**Tradeoff accepted**: Higher latency per request in exchange for always-current data.

---

## #2 — Three-Layer Rule Engine

**Hajin**: Designed a 3-layer decision architecture: (1) structured data queries, (2) deterministic rules, (3) LLM reasoning for user-specific preferences. Rationale: users have heterogeneous preferences (cashback vs. travel, specific issuers, specific benefits) that can't be handled by rules alone.

---

## #3 — Conversational Agent over Graphical Dashboard

**Hajin**: Output should be text-based tactical plans, not graph visualizations. The system is a conversational agent — charts and images are secondary to clear, actionable text.

---

## #4 — Explicit User Input, No Implicit Memory

**Hajin**: Since the LLM can't directly access the user's credit profile, require explicit user input each session. Bank/card statements follow the same pattern. Design choice prioritizes user consent and clean session boundaries.

---

## #5 — Spending Analysis Pipeline: Parse → Classify → Predict

**Hajin**: When statements are uploaded, the system should automatically classify transactions into spending categories, then predict future spending patterns. More data → better card recommendations. This became the foundational data flow.

---

## #6 — Lightweight ML (Later Replaced — See #12)

**Hajin**: Initially asked for a lightweight ML to help transaction category classification without being too heavy. The initial TF-IDF + LogReg classifier was built on this premise.

---

## #7 — Skill-Based Modular Routing

**Hajin**: Each major feature gets its own `SKILL.md` spec file under `/skills/`. `CLAUDE.md` acts as a routing index that points to the appropriate skill — keeps the master file lean and each skill independently maintainable.

Skills: `profile-intake`, `statement-analysis`, `card-recommendation`, `cli-strategy`, `retention-strategy`, `timeline-builder`.

---

## #8 — Modular Documentation Architecture

**Hajin**: As `CLAUDE.md` grew bloated, proposed splitting specs into per-subsystem `README.md` files (`db/README.md`, `pipeline/README.md`, `logs/README.md`, `report/README.md`). `CLAUDE.md` redirects — "for this feature, read that file."

**Benefit**: Each subsystem's spec is maintained independently without inflating the master file.

---

## #9 — Append-Only Decision & Profile Logging

**Hajin**: Requested dedicated `logs/` directory with two append-only JSONL files: `decision_log.jsonl` (agent planning decisions) and `profile_log.jsonl` (user profile changes). Append-only pattern prevents accidental overwrites and provides a full audit trail.

---

## #10 — P2P Resolution: Transaction-Level, Not Recipient-Level

**Hajin**: Zelle/Venmo transfers can't be auto-categorized because the same recipient can serve different purposes (dinner split → dining, rent split → housing, bar tab → entertainment). Must ask the user each time at the transaction level. Offer previous category as a suggestion, but never assume.

**Key insight**: Recipient identity ≠ transaction purpose.

---

## #11 — Continuous Distillation for Unknown Merchants

**Hajin**: When the classifier can't categorize a merchant, run LLM inference, then save the result back so the same merchant is instantly resolved next time. Every unknown encounter becomes a learning opportunity.

**Claude** implemented this as `distill_from_llm()` → `merchant_aliases` SQLite table. Normalization (POS prefix stripping, city/state removal, store number removal) ensures "FIVE GUYS 1234 NEW YORK NY" and "FIVE GUYS 5678" resolve to the same cache key.

---

## #12 — Transaction Exclusion System

**Hajin**: Wanted to exclude specific transactions (e.g., tuition payments) from spending analysis to see true discretionary spending. Rule types: `contains`, `exact`, `regex`, `amount_gt`, `amount_lt`. Rules persist and apply automatically to future analyses.

---

## #13 — User Preferences Persistence

**Hajin**: Expanded from exclusion rules to a general preferences system. Any user preference (preferred airline alliance, issuers to avoid, categories to focus on) should be recorded in `user_preferences` table and applied across all future analyses and recommendations.

---

## #14 — Two-Phase Report Generation (Resolution Before Report)

**Hajin**: Final spending reports must not be generated while P2P or `needs_llm` transactions remain unresolved. Implemented as a `require_resolution=True` flag — the analyzer pauses after the first pass, surfaces unresolved items for user/LLM resolution, then finalizes.

**Rationale**: Unresolved transactions distort spending baselines, which cascades into bad forecasts and bad card recommendations.

---

## #15 — Category-First Card Recommendations

**Hajin**: Noticed the card recommendation skill considered spending categories too late (Step 4). Moved it to Step 1 — spending patterns are the highest-signal input for card selection. Profile and goals are refinements, not the starting point.

---

## #16 — Denial/Block Response: Database First, Not Speculation

**Hajin**: Caught the agent speculating about why an Amex popup occurred instead of checking `churning_rules` in the database first. Redesigned the denial response flow: query issuer rules → identify exact trigger (e.g., 2 Amex cards + 2 SUBs in 60 days triggers RAT) → explain cause → provide recovery strategy.

**Principle**: Pattern recognition over speculation.

---

## #17 — ML → LLM Pivot for Transaction Classification

**Problem**: TF-IDF + LogReg memorized merchant names instead of generalizing. Expanding training data from 334 → 1,454 samples caused regressions (AIRLINES confused with INSURANCE due to shared char n-grams). Fundamentally, you can't fix a generalization problem with more memorization.

**Hajin** rejected three alternatives (keyword dict, hybrid features, embeddings) and proposed the LLM-centered approach: _"It addresses every problem we had so far. This is a LLM-centered application anyway; if this was a normal software it'd be problematic to rely entirely on LLM."_

**Result**: 6-layer deterministic pipeline handles the easy cases; LLM handles the long tail. `merchant_aliases` cache eliminates repeat LLM calls. scikit-learn removed entirely.

---

## #18 — LLM Model Selection: Flexible at Agent Layer

**Hajin** preferred Haiku (cost/speed) but raised domain knowledge concerns for niche merchants. **Claude** noted context clues in descriptions help even small models, and the cache compensates for first-encounter errors.

**Decision**: Classifier is model-agnostic — flags `needs_llm`, caller picks the model. No hard dependency on any specific LLM.

---

## #21 — This Decision Log

**Hajin**: No persistent record of design decisions across sessions. Context gets lost when conversations compact. Created this file as an append-only record of architectural decisions, maintained from the builders' perspective.

---

## #22 — Cumulative Report Generation (DB-Wide, Not Per-Run)

**Hajin**: Caught that running the analyzer with a single new statement PDF overwrote the report — only showing that card's transactions instead of merging with existing data.

**Root cause**: `_aggregate_spending()` and `_generate_report()` read from `self.classified_transactions` (in-memory, current run only). The DB already had cumulative data via `_insert_to_db()` deduplication, but reporting never used it.

**Claude's fix**: Added `_load_all_transactions_from_db()` → after inserting new transactions, reporting now queries the full `transactions` table. `_aggregate_spending()` and `_generate_report()` accept DB-wide data. `_update_spending_pattern()` rebuilds from cumulative totals.

**Principle**: The DB is the source of truth. In-memory state is ephemeral; reports must always reflect the full picture.
