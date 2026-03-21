"""
Spending Analyzer — Integrated spending analysis pipeline

Pipeline:
  1. Extract transactions from PDF/CSV via StatementParser
  2. Classify categories via TransactionClassifier (5-layer pipeline)
  3. Ingest transactions into SQLite (deduplication)
  4. Aggregate spending patterns (update spending_pattern table)
  5. Detect recurring transactions
  6. Generate analysis report

Usage:
  from ml.spending_analyzer import SpendingAnalyzer
  analyzer = SpendingAnalyzer(db_path, user_id="hajin")
  report = analyzer.run(pdf_files=[...])
"""

import os
import sys
import re
import json
import sqlite3
from datetime import datetime
from collections import defaultdict, Counter
from typing import List, Dict, Optional, Callable

# Module path setup
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from ml.statement_parser import StatementParser
from ml.category_classifier.classifier import TransactionClassifier, ALL_CATEGORIES


class SpendingAnalyzer:
    """Integrated spending analysis pipeline"""

    def __init__(self, db_path: str, user_id: str = "default"):
        """
        Args:
            db_path: SQLite DB path
            user_id: User identifier
        """
        self.db_path = db_path
        self.user_id = user_id
        self.parser = StatementParser()
        self.classifier = TransactionClassifier(db_path=db_path)
        self.classifier.load_or_train()

        # Analysis results storage
        self.parsed_results = []
        self.all_transactions = []
        self.classified_transactions = []
        self.excluded_transactions = []  # Transactions excluded by user preferences
        self.p2p_questions = []  # P2P transactions requiring user input
        self.llm_needed = []  # Transactions requiring LLM fallback
        self.stats = {}
        self.exclusion_rules = []  # Exclusion rules loaded from DB

    _SKIP_TOKENS = {"skip"}

    def run(
        self,
        pdf_files: List[str] | None = None,
        csv_files: List[str] | None = None,
        llm_resolver: Callable[[Dict], Optional[str]] | None = None,
        p2p_resolver: Callable[[Dict], Optional[str]] | None = None,
        require_resolution: bool = False,
    ) -> Dict:
        """
        Execute the full pipeline

        Returns:
            {
                "total_parsed": int,
                "total_classified": int,
                "total_inserted": int,
                "p2p_questions": [...],
                "llm_needed": [...],
                "category_summary": {...},
                "monthly_summary": {...},
                "recurring": [...],
                "card_breakdown": {...},
                "report_text": str,
            }
        """
        files = (pdf_files or []) + (csv_files or [])
        if not files:
            return {"error": "No files provided."}

        print(f"\n{'━'*60}")
        print(f"  Spending Analysis Pipeline Started")
        print(f"  User: {self.user_id} | Files: {len(files)}")
        print(f"{'━'*60}\n")

        # ── Step 0: Load user exclusion rules ──
        self._load_exclusion_rules()
        if self.exclusion_rules:
            print(f"[0/6] Loaded {len(self.exclusion_rules)} user exclusion rules")
            for rule in self.exclusion_rules:
                print(f"      → {rule['rule_type']}: \"{rule['pattern']}\" ({rule['reason']})")
            print()

        # ── Step 1: Parse PDFs ──
        print("[1/6] Parsing PDFs...")
        parsed_results = self.parser.parse_multiple(files)
        self.parsed_results = parsed_results
        self.all_transactions = self.parser.get_all_transactions(parsed_results)
        print(f"      → {len(self.all_transactions)} transactions extracted\n")

        # ── Step 2: Classification ──
        print("[2/6] Classifying transactions...")
        self._classify_all()
        print(f"      → Classification complete (P2P questions: {len(self.p2p_questions)}, LLM needed: {len(self.llm_needed)})\n")

        # ── Step 3: Apply transaction exclusions ──
        print("[3/6] Applying user exclusion rules...")
        self._apply_exclusions()
        print(f"      → {len(self.excluded_transactions)} transactions excluded\n")

        # ── Step 4: Resolve pending classifications (optional) ──
        print("[4/7] Resolving pending classifications...")
        self.resolve_pending(llm_resolver=llm_resolver, p2p_resolver=p2p_resolver)
        pending = self.get_pending_resolutions()
        pending_count = len(pending["p2p_questions"]) + len(pending["llm_needed"])
        print(f"      → {pending_count} unresolved after resolver pass\n")

        if require_resolution and pending_count:
            print("[stop] Resolution required before DB ingestion/report generation\n")
            return self._build_pending_resolution_result()

        return self.finalize_after_resolution()

    def _resolution_key(self, tx: Dict) -> str:
        """Stable key for matching pending resolutions back to a transaction."""
        return "|".join([
            tx.get("date", ""),
            tx.get("description", ""),
            f"{float(tx.get('amount', 0)):.2f}",
            tx.get("source", ""),
            tx.get("card_name", ""),
        ])

    def _normalize_resolution_category(self, category: str | None) -> str | None:
        if not category:
            return None
        normalized = category.strip().lower()
        return normalized if normalized in ALL_CATEGORIES else None

    def _is_skip_resolution(self, category: str | None) -> bool:
        if not category:
            return False
        return category.strip().lower() in self._SKIP_TOKENS

    def _exclude_transaction(self, tx: Dict, reason: str) -> None:
        tx["excluded"] = True
        tx["exclusion_reason"] = reason
        tx["classify_method"] = "user_excluded"
        if tx not in self.excluded_transactions:
            self.excluded_transactions.append(tx)

    def get_pending_resolutions(self) -> Dict[str, List[Dict]]:
        """Return unresolved P2P and LLM classifications with stable keys."""
        p2p_questions = []
        for q in self.p2p_questions:
            item = dict(q)
            item["resolution_key"] = item.get("resolution_key") or self._resolution_key(item["transaction"])
            p2p_questions.append(item)

        llm_needed = []
        for tx in self.llm_needed:
            llm_needed.append({
                "resolution_key": tx.get("resolution_key") or self._resolution_key(tx),
                "date": tx.get("date"),
                "description": tx.get("description"),
                "amount": tx.get("amount"),
                "ml_suggestion": tx.get("ml_suggestion"),
                "ml_top3": tx.get("ml_top3"),
            })

        return {
            "p2p_questions": p2p_questions,
            "llm_needed": llm_needed,
        }

    def resolve_pending(
        self,
        p2p_answers: Dict[str, str] | None = None,
        llm_answers: Dict[str, str] | None = None,
        llm_resolver: Callable[[Dict], Optional[str]] | None = None,
        p2p_resolver: Callable[[Dict], Optional[str]] | None = None,
    ) -> Dict[str, int]:
        """
        Apply provided answers and/or callback resolvers to unresolved transactions.

        Args:
            p2p_answers: mapping of resolution_key -> category
            llm_answers: mapping of resolution_key -> category
            llm_resolver: callback invoked for each pending LLM item
            p2p_resolver: callback invoked for each pending P2P question
        """
        p2p_answers = dict(p2p_answers or {})
        llm_answers = dict(llm_answers or {})

        if p2p_resolver:
            for q in self.p2p_questions:
                key = q.get("resolution_key") or self._resolution_key(q["transaction"])
                if key not in p2p_answers:
                    p2p_answers[key] = p2p_resolver(dict(q))

        if llm_resolver:
            for tx in self.llm_needed:
                key = tx.get("resolution_key") or self._resolution_key(tx)
                if key not in llm_answers:
                    llm_answers[key] = llm_resolver(dict(tx))

        resolved_p2p = 0
        skipped_p2p = 0
        remaining_p2p = []
        p2p_history_updates = defaultdict(set)
        for q in self.p2p_questions:
            key = q.get("resolution_key") or self._resolution_key(q["transaction"])
            answer = p2p_answers.get(key)
            if self._is_skip_resolution(answer):
                self._exclude_transaction(q["transaction"], "user_marked_skip")
                skipped_p2p += 1
                continue

            category = self._normalize_resolution_category(answer)
            if not category:
                remaining_p2p.append(q)
                continue

            tx = q["transaction"]
            tx["category"] = category
            tx["confidence"] = 1.0
            tx["classify_method"] = "p2p_resolved"
            recipient = q.get("recipient")
            if recipient:
                p2p_history_updates[recipient].add(category)
            resolved_p2p += 1

        resolved_llm = 0
        skipped_llm = 0
        remaining_llm = []
        for tx in self.llm_needed:
            key = tx.get("resolution_key") or self._resolution_key(tx)
            answer = llm_answers.get(key)
            if self._is_skip_resolution(answer):
                self._exclude_transaction(tx, "user_marked_skip")
                skipped_llm += 1
                continue

            category = self._normalize_resolution_category(answer)
            if not category:
                remaining_llm.append(tx)
                continue

            tx["category"] = category
            tx["confidence"] = 1.0
            tx["classify_method"] = "llm_resolved"
            self.classifier.distill_from_llm(
                self._clean_description(tx.get("description", "")),
                category,
            )
            resolved_llm += 1

        self.p2p_questions = remaining_p2p
        self.llm_needed = remaining_llm
        self.classified_transactions = [
            tx for tx in self.classified_transactions
            if not tx.get("excluded")
        ]

        for recipient, categories in p2p_history_updates.items():
            # Persist a default only when the recipient was consistently categorized.
            if len(categories) == 1:
                self.classifier.save_p2p_category(
                    self.user_id,
                    recipient,
                    next(iter(categories)),
                )

        return {
            "resolved_p2p": resolved_p2p,
            "resolved_llm": resolved_llm,
            "skipped_p2p": skipped_p2p,
            "skipped_llm": skipped_llm,
            "remaining_p2p": len(self.p2p_questions),
            "remaining_llm": len(self.llm_needed),
        }

    def _build_pending_resolution_result(self) -> Dict:
        """Return a non-final result that tells the caller more input is needed."""
        pending = self.get_pending_resolutions()
        return {
            "status": "needs_resolution",
            "resolution_required": True,
            "total_parsed": len(self.all_transactions),
            "total_classified": len(self.classified_transactions),
            "total_excluded": len(self.excluded_transactions),
            "p2p_questions": pending["p2p_questions"],
            "llm_needed": pending["llm_needed"],
            "message": "Resolve pending P2P and/or LLM classifications before generating the final report.",
        }

    def finalize_after_resolution(self) -> Dict:
        """Continue the pipeline after pending classifications have been resolved."""
        pending = self.get_pending_resolutions()
        if pending["p2p_questions"] or pending["llm_needed"]:
            return self._build_pending_resolution_result()

        # ── Step 5: DB ingestion ──
        print("[5/7] Ingesting into SQLite...")
        inserted = self._insert_to_db()
        print(f"      → {inserted} rows inserted (duplicates excluded)\n")

        # ── Step 6: Recurring detection ──
        print("[6/7] Detecting recurring payments...")
        recurring = self._detect_recurring()
        print(f"      → {len(recurring)} recurring items detected\n")

        # ── Step 7: Aggregate + Report ──
        print("[7/7] Generating analysis report...")
        self._aggregate_spending()
        report = self._generate_report(self.parsed_results, recurring)
        report["total_inserted"] = inserted
        print(f"      → Report generation complete\n")

        return report

    # ──────────────────────────────────────────────────
    # User preferences / Exclusion rules
    # ──────────────────────────────────────────────────

    def _load_exclusion_rules(self):
        """Load active transaction exclusion rules from DB"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # Check if table exists
        cur.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='transaction_exclusions'
        """)
        if not cur.fetchone():
            conn.close()
            self.exclusion_rules = []
            return

        cur.execute("""
            SELECT exclusion_id, rule_type, pattern, match_field, reason
            FROM transaction_exclusions
            WHERE user_id = ? AND active = 1
        """, (self.user_id,))

        self.exclusion_rules = []
        for row in cur.fetchall():
            self.exclusion_rules.append({
                "id": row[0],
                "rule_type": row[1],    # contains, exact, regex, amount_gt, amount_lt
                "pattern": row[2],
                "match_field": row[3],  # description, category, card_name
                "reason": row[4],
            })
        conn.close()

    def _apply_exclusions(self):
        """Apply user exclusion rules to classified transactions"""
        if not self.exclusion_rules:
            self.excluded_transactions = []
            return

        included = []
        excluded = []

        for tx in self.classified_transactions:
            matched_rule = self._match_exclusion(tx)
            if matched_rule:
                tx["excluded"] = True
                tx["exclusion_reason"] = matched_rule["reason"]
                tx["exclusion_rule_id"] = matched_rule["id"]
                excluded.append(tx)
            else:
                included.append(tx)

        self.excluded_transactions = excluded
        self.classified_transactions = included

    def _match_exclusion(self, tx: Dict) -> Optional[Dict]:
        """Check if a transaction matches any exclusion rule"""
        for rule in self.exclusion_rules:
            field_value = ""
            if rule["match_field"] == "description":
                field_value = tx.get("description", "")
            elif rule["match_field"] == "category":
                field_value = tx.get("category", "")
            elif rule["match_field"] == "card_name":
                field_value = tx.get("card_name", "")

            matched = False
            if rule["rule_type"] == "contains":
                matched = rule["pattern"].upper() in field_value.upper()
            elif rule["rule_type"] == "exact":
                matched = field_value.upper() == rule["pattern"].upper()
            elif rule["rule_type"] == "regex":
                matched = bool(re.search(rule["pattern"], field_value, re.I))
            elif rule["rule_type"] == "amount_gt":
                try:
                    matched = abs(tx.get("amount", 0)) > float(rule["pattern"])
                except ValueError:
                    pass
            elif rule["rule_type"] == "amount_lt":
                try:
                    matched = abs(tx.get("amount", 0)) < float(rule["pattern"])
                except ValueError:
                    pass

            if matched:
                return rule
        return None

    @staticmethod
    def add_exclusion_rule(db_path: str, user_id: str, rule_type: str,
                           pattern: str, match_field: str = "description",
                           reason: str = "") -> int | None:
        """
        Add a transaction exclusion rule (called by agent on user request)

        Args:
            rule_type: "contains" | "exact" | "regex" | "amount_gt" | "amount_lt"
            pattern: Pattern to match (e.g., "NELNET", "STEVENS INST")
            match_field: "description" | "category" | "card_name"
            reason: User-provided reason for exclusion

        Returns: exclusion_id
        """
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        now = datetime.utcnow().isoformat()
        cur.execute("""
            INSERT INTO transaction_exclusions
            (user_id, rule_type, pattern, match_field, reason, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (user_id, rule_type, pattern, match_field, reason, now))
        exclusion_id = cur.lastrowid
        conn.commit()
        conn.close()
        return exclusion_id

    @staticmethod
    def set_preference(db_path: str, user_id: str, key: str, value: str,
                       description: str = ""):
        """
        Save a general-purpose user preference

        Examples:
            set_preference(db, "hajin", "exclude_tuition", "true", "Exclude tuition from spending analysis")
            set_preference(db, "hajin", "avoid_issuer", "citi", "Exclude Citi card recommendations")
            set_preference(db, "hajin", "goal", "travel", "Travel points priority")
        """
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        now = datetime.utcnow().isoformat()
        cur.execute("""
            INSERT INTO user_preferences (user_id, pref_key, pref_value, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, pref_key) DO UPDATE SET
                pref_value = excluded.pref_value,
                description = excluded.description,
                updated_at = excluded.updated_at
        """, (user_id, key, value, description, now, now))
        conn.commit()
        conn.close()

    @staticmethod
    def get_preferences(db_path: str, user_id: str) -> Dict[str, Dict]:
        """Retrieve all preferences for a user"""
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Check if table exists
        cur.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='user_preferences'
        """)
        if not cur.fetchone():
            conn.close()
            return {}

        cur.execute("""
            SELECT pref_key, pref_value, description, updated_at
            FROM user_preferences WHERE user_id = ?
        """, (user_id,))

        prefs = {}
        for key, value, desc, updated in cur.fetchall():
            prefs[key] = {"value": value, "description": desc, "updated_at": updated}
        conn.close()
        return prefs

    @staticmethod
    def get_exclusion_rules(db_path: str, user_id: str) -> List[Dict]:
        """Retrieve all exclusion rules for a user"""
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        cur.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='transaction_exclusions'
        """)
        if not cur.fetchone():
            conn.close()
            return []

        cur.execute("""
            SELECT exclusion_id, rule_type, pattern, match_field, reason, active, created_at
            FROM transaction_exclusions WHERE user_id = ?
        """, (user_id,))

        rules = []
        for row in cur.fetchall():
            rules.append({
                "id": row[0], "rule_type": row[1], "pattern": row[2],
                "match_field": row[3], "reason": row[4],
                "active": bool(row[5]), "created_at": row[6],
            })
        conn.close()
        return rules

    # ──────────────────────────────────────────────────
    # Step 2: Classification
    # ──────────────────────────────────────────────────

    def _classify_all(self):
        """Classify all transactions"""
        self.classified_transactions = []
        self.p2p_questions = []
        self.llm_needed = []

        # Chase credit card payment patterns (not spending)
        PAYMENT_PATTERNS = [
            r"PAYMENT THANK YOU",
            r"AUTOPAY PAYMENT",
            r"CAPITAL ONE MOBILE PMT",
            r"PAYMENT TO CHASE CARD",
            r"AUTOPAY ENROLL",
        ]

        for tx in self.all_transactions:
            raw_desc_upper = tx.get("description", "").upper()

            # card_payment (card balance transfers) excluded from spending analysis
            if tx.get("tx_type") == "card_payment":
                tx["category"] = "card_payment"
                tx["classify_method"] = "type_filter"
                self.classified_transactions.append(tx)
                continue

            # Additional payment pattern detection (missed by parser)
            if any(re.search(p, raw_desc_upper) for p in PAYMENT_PATTERNS):
                tx["category"] = "card_payment"
                tx["classify_method"] = "payment_pattern"
                self.classified_transactions.append(tx)
                continue

            # income already detected by parser
            if tx.get("tx_type") == "income":
                tx["category"] = "income"
                tx["classify_method"] = "type_filter"
                self.classified_transactions.append(tx)
                continue

            # P2P detection checks raw description first (before clean, to keep Zelle prefix)
            raw_desc = tx["description"]
            if self.classifier._is_p2p(raw_desc.upper()):
                result = self.classifier.classify(
                    raw_desc,  # Use original (includes Zelle/Venmo prefix)
                    amount=tx.get("amount"),
                    user_id=self.user_id,
                )
            else:
                # Clean description (remove Chase checking prefixes)
                desc = self._clean_description(raw_desc)
                result = self.classifier.classify(
                    desc,
                    amount=tx.get("amount"),
                    user_id=self.user_id,
                )

            tx["category"] = result["category"]
            tx["confidence"] = result.get("confidence", 0)
            tx["classify_method"] = result["method"]
            tx["resolution_key"] = self._resolution_key(tx)

            if result.get("needs_user_input"):
                self.p2p_questions.append({
                    "resolution_key": tx["resolution_key"],
                    "transaction": tx,
                    "prompt": result["user_prompt"],
                    "recipient": result.get("p2p_recipient"),
                    "previous_category": result.get("previous_category"),
                })
            elif result["method"] == "needs_llm":
                tx["ml_suggestion"] = result.get("ml_suggestion")
                tx["ml_top3"] = result.get("ml_top3")
                self.llm_needed.append(tx)

            self.classified_transactions.append(tx)

    def _clean_description(self, desc: str) -> str:
        """Remove prefixes from Chase checking transaction descriptions"""
        # "Card Purchase 10/27 Popeyes 14156 New York NY Card 5839" → "Popeyes 14156 New York NY"
        # "Recurring Card Purchase 02/15 Apple.Com/Bill ..." → "Apple.Com/Bill ..."
        # "Card Purchase With Pin 09/02 7-Eleven Hoboken NJ ..." → "7-Eleven Hoboken NJ"

        patterns = [
            r"^(?:Recurring\s+)?Card Purchase(?:\s+(?:With Pin|Return))?\s+\d{2}/\d{2}\s+",
            r"^ATM Cash (?:Deposit|Withdrawal)\s+\d{2}/\d{2}\s+",
            r"^(?:Zelle Payment (?:To|From))\s+",
            r"^Online (?:Transfer|Payment)\s+",
            r"^(?:\d{2}/\d{2}\s+)?Payment To\s+",
        ]
        cleaned = desc
        for p in patterns:
            cleaned = re.sub(p, "", cleaned, flags=re.I)

        # Remove "Card 5839" suffix
        cleaned = re.sub(r"\s*Card\s+\d{4}\s*$", "", cleaned, flags=re.I)
        # Remove trailing whitespace
        cleaned = cleaned.strip()

        return cleaned if cleaned else desc

    # ──────────────────────────────────────────────────
    # Step 3: DB ingestion
    # ──────────────────────────────────────────────────

    def _insert_to_db(self) -> int:
        """Refresh imported statement sources in SQLite using the latest classifications."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        sources = sorted({tx.get("source", "") for tx in self.classified_transactions if tx.get("source")})
        if sources:
            placeholders = ",".join("?" for _ in sources)
            cur.execute(
                f"DELETE FROM transactions WHERE user_id = ? AND source IN ({placeholders})",
                (self.user_id, *sources),
            )

        inserted = 0
        seen = set()
        for tx in self.classified_transactions:
            if tx.get("excluded"):
                continue

            key = (
                tx.get("date", ""),
                tx.get("description", "")[:50],
                round(tx.get("amount", 0), 2),
                tx.get("card_name", ""),
            )
            if key in seen:
                continue

            category = tx.get("category") or "uncategorized"

            # Replace any previously imported identical transaction regardless of source.
            cur.execute("""
                DELETE FROM transactions
                WHERE user_id = ?
                  AND tx_date = ?
                  AND description = ?
                  AND amount = ?
                  AND IFNULL(card_name, '') = IFNULL(?, '')
            """, (
                self.user_id,
                tx.get("date"),
                tx.get("description"),
                tx.get("amount", 0),
                tx.get("card_name", ""),
            ))

            cur.execute("""
                INSERT INTO transactions (user_id, tx_date, description, amount, category, source, card_name)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                self.user_id,
                tx.get("date"),
                tx.get("description"),
                tx.get("amount", 0),
                category,
                tx.get("source", ""),
                tx.get("card_name", ""),
            ))
            seen.add(key)
            inserted += 1

        conn.commit()
        conn.close()
        return inserted

    # ──────────────────────────────────────────────────
    # Step 4: Recurring detection
    # ──────────────────────────────────────────────────

    # Categories treated as subscription/fixed costs (lower threshold)
    _SUBSCRIPTION_CATEGORIES = {"subscriptions", "insurance", "utilities", "education", "housing"}

    # Merchant keywords likely to be subscriptions
    _SUBSCRIPTION_KEYWORDS = [
        "NETFLIX", "SPOTIFY", "HULU", "DISNEY", "HBO", "APPLE.COM/BILL",
        "YOUTUBE PREMIUM", "AMAZON PRIME", "ADOBE", "MICROSOFT 365",
        "GOOGLE STORAGE", "ICLOUD", "DROPBOX", "CHATGPT", "ANTHROPIC",
        "GEICO", "STATE FARM", "PROGRESSIVE", "ALLSTATE", "INSURANCE",
        "MEMBERSHIP", "SUBSCRIPTION", "CLOUDFLARE", "GITHUB",
    ]

    def _detect_recurring(self) -> List[Dict]:
        """
        Recurring payment detection — filters for genuine fixed costs/subscriptions only.

        Criteria:
        1. Amount consistency: within ±10% (or absolute diff < $2)
        2. Minimum count: 3+ for general merchants, 2+ for subscription/fixed cost categories
        3. Time regularity: avg gap 20-40d (monthly) or 80-100d (quarterly)
           - Subscription keyword matches bypass time regularity check
        """
        merchant_history = defaultdict(list)

        for tx in self.classified_transactions:
            if tx.get("tx_type") in ("income", "card_payment", "payment"):
                continue

            desc_clean = self._clean_description(tx.get("description", ""))
            key = re.sub(r"\s*#?\d{3,}.*$", "", desc_clean.upper()).strip()
            key = re.sub(r"\s+", " ", key)
            if len(key) < 3:
                continue

            merchant_history[key].append({
                "amount": tx.get("amount", 0),
                "date": tx.get("date", ""),
                "category": tx.get("category", ""),
            })

        recurring = []
        for merchant, entries in merchant_history.items():
            # ── Determine category ──
            categories = [e["category"] for e in entries if e["category"]]
            most_common_cat = Counter(categories).most_common(1)
            category = most_common_cat[0][0] if most_common_cat else "uncategorized"

            is_subscription_cat = category in self._SUBSCRIPTION_CATEGORIES
            is_subscription_kw = any(kw in merchant for kw in self._SUBSCRIPTION_KEYWORDS)

            # ── Minimum count ──
            min_count = 2 if (is_subscription_cat or is_subscription_kw) else 3
            if len(entries) < min_count:
                continue

            # ── Amount consistency (±10% or ±$2) ──
            amounts = [e["amount"] for e in entries]
            avg_amount = sum(amounts) / len(amounts)

            if avg_amount > 0:
                similar = all(
                    abs(a - avg_amount) / avg_amount < 0.10 or abs(a - avg_amount) < 2.0
                    for a in amounts
                )
            else:
                similar = True

            if not similar:
                continue

            # ── Time regularity (waived for subscription keywords) ──
            if not is_subscription_kw:
                dates_sorted = sorted(e["date"] for e in entries if e["date"])
                if len(dates_sorted) >= 2:
                    try:
                        from datetime import datetime as _dt
                        parsed = [_dt.strptime(d, "%Y-%m-%d") for d in dates_sorted]
                        gaps = [(parsed[i+1] - parsed[i]).days for i in range(len(parsed)-1)]
                        avg_gap = sum(gaps) / len(gaps)
                        # Monthly (20-40d) or quarterly (80-100d) or semi-annual (160-200d)
                        is_regular = (
                            (20 <= avg_gap <= 40) or
                            (80 <= avg_gap <= 100) or
                            (160 <= avg_gap <= 200)
                        )
                        if not is_regular:
                            continue
                        # Gap variance check — irregular if stddev > 40% of mean
                        if len(gaps) >= 2 and avg_gap > 0:
                            variance = sum((g - avg_gap) ** 2 for g in gaps) / len(gaps)
                            stddev = variance ** 0.5
                            if stddev / avg_gap > 0.40:
                                continue
                    except (ValueError, TypeError):
                        pass  # Skip on date parse failure (insufficient data)

            # ── Frequency determination ──
            dates_sorted = sorted(e["date"] for e in entries if e["date"])
            frequency_label = "monthly"
            if len(dates_sorted) >= 2:
                try:
                    from datetime import datetime as _dt
                    parsed = [_dt.strptime(d, "%Y-%m-%d") for d in dates_sorted]
                    gaps = [(parsed[i+1] - parsed[i]).days for i in range(len(parsed)-1)]
                    avg_gap = sum(gaps) / len(gaps)
                    if avg_gap > 100:
                        frequency_label = "semi-annual"
                    elif avg_gap > 60:
                        frequency_label = "quarterly"
                    else:
                        frequency_label = "monthly"
                except (ValueError, TypeError):
                    pass

            recurring.append({
                "merchant": merchant,
                "typical_amount": round(avg_amount, 2),
                "frequency": len(entries),
                "frequency_label": frequency_label,
                "category": category,
                "dates": dates_sorted,
            })

        # Sort by amount descending (fixed cost importance)
        recurring.sort(key=lambda x: -x["typical_amount"])

        self._save_recurring(recurring)
        return recurring

    def _save_recurring(self, recurring: List[Dict]):
        """Save to recurring_transactions table"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        cur.execute("DELETE FROM recurring_transactions WHERE user_id = ?", (self.user_id,))

        for r in recurring:
            cur.execute("""
                INSERT OR REPLACE INTO recurring_transactions
                (user_id, merchant_pattern, typical_amount, category, frequency, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                self.user_id,
                r["merchant"],
                r["typical_amount"],
                r["category"],
                str(r["frequency"]) + "x",
                r["dates"][-1] if r["dates"] else "",
            ))

        conn.commit()
        conn.close()

    # ──────────────────────────────────────────────────
    # Step 5: Aggregate + Report
    # ──────────────────────────────────────────────────

    def _aggregate_spending(self):
        """Aggregate monthly spending by category"""
        monthly = defaultdict(lambda: defaultdict(float))
        category_total = defaultdict(float)
        card_total = defaultdict(float)

        for tx in self.classified_transactions:
            cat = tx.get("category") or "uncategorized"
            if cat in ("income", "card_payment", "payment"):
                continue

            month = tx.get("date", "")[:7]  # YYYY-MM
            amount = abs(tx.get("amount", 0))

            monthly[month][cat] += amount
            category_total[cat] += amount
            card_total[tx.get("card_name", "Unknown")] += amount

        # Update spending_pattern table
        self._update_spending_pattern(monthly)

        self.stats = {
            "monthly": dict(monthly),
            "category_total": dict(category_total),
            "card_total": dict(card_total),
        }

    def _update_spending_pattern(self, monthly: dict):
        """UPSERT spending_pattern table"""
        if not monthly:
            return

        # Calculate monthly average per category
        cat_months = defaultdict(list)
        for month, cats in monthly.items():
            for cat, total in cats.items():
                cat_months[cat].append(total)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # Rebuild the user's aggregate view from current transaction data.
        cur.execute("DELETE FROM spending_pattern WHERE user_id = ?", (self.user_id,))

        for cat, totals in cat_months.items():
            avg = round(sum(totals) / len(totals))
            cur.execute("""
                INSERT INTO spending_pattern (user_id, category, monthly_avg)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, category) DO UPDATE SET monthly_avg = excluded.monthly_avg
            """, (self.user_id, cat, avg))

        conn.commit()
        conn.close()

    def _generate_report(self, parsed_results: List[Dict], recurring: List[Dict]) -> Dict:
        """Generate analysis report"""
        # Calculate period
        dates = [tx.get("date", "") for tx in self.classified_transactions if tx.get("date")]
        dates = [d for d in dates if d]
        min_date = min(dates) if dates else "?"
        max_date = max(dates) if dates else "?"

        # Spending transactions only (exclude income/card_payment)
        spend_txs = [tx for tx in self.classified_transactions
                     if tx.get("category") not in ("income", "card_payment", "payment", None)]

        total_spend = sum(abs(tx.get("amount", 0)) for tx in spend_txs)
        income_txs = [tx for tx in self.classified_transactions if tx.get("category") == "income"]
        total_income = sum(abs(tx.get("amount", 0)) for tx in income_txs)

        # Category-level aggregation
        cat_summary = self.stats.get("category_total", {})
        total_months = len(self.stats.get("monthly", {})) or 1

        # Monthly average
        cat_monthly_avg = {cat: round(total / total_months, 2) for cat, total in cat_summary.items()}

        # Bar chart
        max_val = max(cat_monthly_avg.values()) if cat_monthly_avg else 1
        bar_width = 24

        report_lines = []
        report_lines.append(f"━━━━ Spending Analysis Report ━━━━")
        report_lines.append(f"Period: {min_date} ~ {max_date} ({total_months} months)")
        report_lines.append(f"Total txns: {len(self.classified_transactions)} | "
                           f"Spending: {len(spend_txs)} (${total_spend:,.0f}) | "
                           f"Income: {len(income_txs)} (${total_income:,.0f})")

        # Excluded transactions summary
        if self.excluded_transactions:
            excluded_total = sum(abs(tx.get("amount", 0)) for tx in self.excluded_transactions)
            report_lines.append(f"Excluded: {len(self.excluded_transactions)} txns (${excluded_total:,.0f})")
            # Aggregate by exclusion reason
            reason_counts = Counter(tx.get("exclusion_reason", "other") for tx in self.excluded_transactions)
            for reason, count in reason_counts.most_common():
                reason_amount = sum(abs(tx.get("amount", 0)) for tx in self.excluded_transactions
                                   if tx.get("exclusion_reason") == reason)
                report_lines.append(f"  → {reason}: {count} txn(s) (${reason_amount:,.0f})")

        report_lines.append("")

        # Card-level aggregation
        report_lines.append("Spending by Card:")
        for card, total in sorted(self.stats.get("card_total", {}).items(), key=lambda x: -x[1]):
            report_lines.append(f"  {card:<30} ${total:>10,.2f}")
        report_lines.append("")

        # Category monthly average (bar chart)
        report_lines.append("Monthly Average by Category:")
        sorted_cats = sorted(
            ((cat, avg) for cat, avg in cat_monthly_avg.items() if cat and avg is not None),
            key=lambda x: -x[1]
        )
        total_cat_avg = sum(avg for _, avg in sorted_cats) if sorted_cats else 1
        for cat, avg in sorted_cats:
            pct = (avg / total_cat_avg) * 100 if total_cat_avg > 0 else 0
            filled = int((avg / max_val) * bar_width) if max_val > 0 else 0
            bar = "█" * filled + "░" * (bar_width - filled)
            report_lines.append(f"  {cat:<15} ${avg:>8,.0f}  {bar}  {pct:.0f}%")
        report_lines.append("")

        # ── Monthly detail breakdown ──
        monthly_data = self.stats.get("monthly", {})
        if monthly_data:
            report_lines.append("━━━━ Monthly Detail ━━━━")
            for month in sorted(monthly_data.keys()):
                cats = monthly_data[month]
                month_total = sum(cats.values())
                report_lines.append(f"\n  {month}  (total ${month_total:,.0f})")
                for cat, amount in sorted(cats.items(), key=lambda x: -x[1]):
                    if amount > 0 and cat is not None:
                        pct = (amount / month_total) * 100 if month_total > 0 else 0
                        report_lines.append(f"    {cat:<15} ${amount:>8,.0f}  ({pct:.0f}%)")
            report_lines.append("")

        # Recurring payments
        if recurring:
            report_lines.append("Recurring Payments (Fixed Costs / Subscriptions):")
            for r in recurring[:15]:
                freq = r.get("frequency_label", "monthly")
                report_lines.append(f"  {r['merchant']:<30} ${r['typical_amount']:>8.2f}/{freq:<10} "
                                   f"({r['category']})")
            report_lines.append("")

        # P2P questions
        if self.p2p_questions:
            report_lines.append(f"P2P transfers: {len(self.p2p_questions)} — user confirmation required:")
            for q in self.p2p_questions[:10]:
                report_lines.append(f"  → {q['prompt']}")
            report_lines.append("")

        # LLM fallback
        if self.llm_needed:
            report_lines.append(f"LLM classification needed: {len(self.llm_needed)}")
            for tx in self.llm_needed[:10]:
                desc = self._clean_description(tx.get("description", ""))
                report_lines.append(f"  → {desc[:60]} (${tx.get('amount', 0):.2f})")
            report_lines.append("")

        # Classification method statistics
        method_counts = Counter(tx.get("classify_method", "?") for tx in self.classified_transactions)
        report_lines.append("Classification Method Stats:")
        for method, count in method_counts.most_common():
            report_lines.append(f"  {method:<20} {count:>4}")
        report_lines.append("")
        report_lines.append("━" * 50)

        report_text = "\n".join(report_lines)

        return {
            "total_parsed": len(self.all_transactions),
            "total_classified": len(self.classified_transactions),
            "total_excluded": len(self.excluded_transactions),
            "total_spend": total_spend,
            "total_income": total_income,
            "period": {"start": min_date, "end": max_date, "months": total_months},
            "p2p_questions": self.p2p_questions,
            "llm_needed": self.llm_needed,
            "excluded_transactions": self.excluded_transactions,
            "category_summary": cat_summary,
            "category_monthly_avg": cat_monthly_avg,
            "monthly_summary": self.stats.get("monthly", {}),
            "recurring": recurring,
            "card_breakdown": self.stats.get("card_total", {}),
            "classification_methods": dict(method_counts),
            "report_text": report_text,
        }

    def save_report(self, report: Dict, output_dir: str | None = None) -> List[str]:
        """
        Save report to files

        Output files:
          - report/spending_analysis_YYYYMMDD.md  (comprehensive report)
          - report/monthly/YYYY-MM.md             (monthly detail report)

        Returns: list of saved file paths
        """
        if report.get("status") == "needs_resolution":
            raise ValueError("Cannot save final report while classifications still need resolution.")

        if output_dir is None:
            output_dir = os.path.join(BASE_DIR, "report")
        os.makedirs(output_dir, exist_ok=True)

        saved_files = []

        # ── Comprehensive report ──
        date_str = datetime.now().strftime("%Y%m%d")
        filepath = os.path.join(output_dir, f"spending_analysis_{date_str}.md")
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report.get("report_text", ""))
        saved_files.append(filepath)

        # ── Monthly detail reports ──
        monthly_data = report.get("monthly_summary", {})
        if monthly_data:
            monthly_dir = os.path.join(output_dir, "monthly")
            os.makedirs(monthly_dir, exist_ok=True)

            for month in sorted(monthly_data.keys()):
                cats = monthly_data[month]
                month_total = sum(cats.values())
                if month_total == 0:
                    continue

                month_lines = []
                month_lines.append(f"# {month} Spending Analysis")
                month_lines.append(f"")
                month_lines.append(f"Total Spending: ${month_total:,.0f}")
                month_lines.append(f"")

                # Category-level aggregation (bar chart)
                max_cat = max(cats.values()) if cats else 1
                month_lines.append("## Spending by Category")
                month_lines.append("")
                for cat, amount in sorted(cats.items(), key=lambda x: -x[1]):
                    if amount > 0 and cat is not None:
                        pct = (amount / month_total) * 100 if month_total > 0 else 0
                        filled = int((amount / max_cat) * 20) if max_cat > 0 else 0
                        bar = "█" * filled + "░" * (20 - filled)
                        month_lines.append(f"  {cat:<15} ${amount:>8,.0f}  {bar}  {pct:.0f}%")
                month_lines.append("")

                # Transaction detail for this month
                month_txs = [tx for tx in self.classified_transactions
                             if tx.get("date", "")[:7] == month
                             and tx.get("category") not in ("income", "card_payment", "payment", None)]
                month_txs.sort(key=lambda x: x.get("date", ""))

                if month_txs:
                    month_lines.append("## Transaction Detail")
                    month_lines.append("")
                    month_lines.append(f"{'Date':<12} {'Category':<15} {'Amount':>10}  Description")
                    month_lines.append("-" * 70)
                    for tx in month_txs:
                        desc = self._clean_description(tx.get("description", ""))[:40]
                        month_lines.append(
                            f"{tx.get('date', '?'):<12} "
                            f"{tx.get('category', '?'):<15} "
                            f"${abs(tx.get('amount', 0)):>9,.2f}  "
                            f"{desc}"
                        )
                    month_lines.append("")

                # Excluded transactions (this month)
                excluded_month = [tx for tx in self.excluded_transactions
                                  if tx.get("date", "")[:7] == month]
                if excluded_month:
                    excluded_total = sum(abs(tx.get("amount", 0)) for tx in excluded_month)
                    month_lines.append(f"## Excluded Transactions ({len(excluded_month)} txns, ${excluded_total:,.0f})")
                    month_lines.append("")
                    for tx in excluded_month:
                        desc = self._clean_description(tx.get("description", ""))[:40]
                        month_lines.append(
                            f"  {tx.get('date', '?'):<12} "
                            f"${abs(tx.get('amount', 0)):>9,.2f}  "
                            f"{desc}  [{tx.get('exclusion_reason', '')}]"
                        )
                    month_lines.append("")

                month_filepath = os.path.join(monthly_dir, f"{month}.md")
                with open(month_filepath, "w", encoding="utf-8") as f:
                    f.write("\n".join(month_lines))
                saved_files.append(month_filepath)

        return saved_files


# ──────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────

if __name__ == "__main__":
    import glob

    # DB path (working in session directory)
    SESSION_DIR = "/sessions/practical-eloquent-einstein"
    MOUNT_DB = os.path.join(SESSION_DIR, "mnt", "CreditPlanner", "db", "credit_planner.db")
    WORK_DB = os.path.join(SESSION_DIR, "credit_planner.db")

    # Copy DB to working directory
    import shutil
    if os.path.exists(MOUNT_DB):
        shutil.copy2(MOUNT_DB, WORK_DB)

    # PDF file list
    pdf_dir = os.path.join(SESSION_DIR, "mnt", "uploads")
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))

    if not pdfs:
        print("No PDF files found.")
        sys.exit(1)

    # Run analysis
    analyzer = SpendingAnalyzer(db_path=WORK_DB, user_id="hajin")
    report = analyzer.run(pdf_files=pdfs)

    # Print report
    print("\n" + report.get("report_text", ""))

    # Save report
    report_dir = os.path.join(SESSION_DIR, "mnt", "CreditPlanner", "report")
    saved_files = analyzer.save_report(report, output_dir=report_dir)
    print(f"\nReports saved:")
    for f in saved_files:
        print(f"  → {f}")

    # Copy DB back to mounted folder
    shutil.copy2(WORK_DB, MOUNT_DB)
    for ext in ["-wal", "-shm"]:
        src = WORK_DB + ext
        if os.path.exists(src):
            shutil.copy2(src, MOUNT_DB + ext)
    print(f"DB updated: {MOUNT_DB}")
