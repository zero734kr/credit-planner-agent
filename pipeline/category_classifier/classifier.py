"""
Transaction Category Classifier
Deterministic Rules + Merchant Cache + LLM Fallback + P2P Processing

Classification Pipeline:
  0. Income / non-expense detection (regex) → excluded from spending analysis
  1. P2P Detection (Zelle, Venmo, PayPal, etc.) → Lookup recipient in p2p_history
     → Record found: Suggest previous category → User confirms
     → No record: Ask user directly
  2. Keyword-based category shortcuts (AIRLINES→travel, PHARMACY→health, etc.)
  3. Merchant alias lookup (SQLite cache) → instant if previously classified
  4. Ambiguous merchant handling (amount-based: Walmart, Costco, Target, etc.)
  5. Unknown → delegate to LLM (agent layer) → auto-distill result to merchant_aliases

The merchant_aliases table acts as a learning cache:
  - First time a merchant is seen, LLM classifies it at the agent layer.
  - The result is saved to merchant_aliases via distill_from_llm().
  - Next time the same merchant appears, Step 3 resolves it instantly.
  - Over time, the system gets faster as the cache grows.

Usage:
  from pipeline.category_classifier.classifier import TransactionClassifier
  clf = TransactionClassifier(db_path="db/credit_planner.db")
  result = clf.classify("WHOLEFDS MKT 10293")
  # → {"category": "groceries", "confidence": 0.9, "method": "merchant_alias", ...}
  # or → {"category": None, "method": "needs_llm", ...}  (first encounter)
"""

import os
import re
import sqlite3

MODEL_DIR = os.path.dirname(__file__)
DEFAULT_DB_PATH = os.path.join(MODEL_DIR, "..", "..", "db", "credit_planner.db")

# ─── Valid categories ───
ALL_CATEGORIES = [
    "groceries", "dining", "gas", "travel", "entertainment",
    "utilities", "insurance", "shopping", "transportation", "health",
    "education", "subscriptions", "housing", "fees",
]

# P2P service patterns
P2P_PATTERNS = [
    r"ZELLE\b",
    r"VENMO\b",
    r"PAYPAL\b",
    r"CASHAPP\b",
    r"CASH APP\b",
    r"APPLE\s*CASH",
    r"GOOGLE\s*PAY\s*(SEND|P2P)",
]

# Income/non-expense patterns (excluded from spending analysis)
INCOME_PATTERNS = [
    r"PAYROLL",
    r"DIRECT DEP",
    r"SALARY",
    r"WAGE",
    r"ACH CREDIT",
    r"TAX REFUND",
    r"INTEREST PAID",
    r"REFUND",
    r"CASHBACK REWARD",
    r"CASH BACK REWARD",
    r"STATEMENT CREDIT",
    r"ZELLE FROM\b",  # Zelle incoming is income
    # Credit card payments (not spending — money moving between accounts)
    r"MOBILE PYMT\b",
    r"ONLINE PYMT\b",
    r"AUTOPAY PAYMENT",
    r"PAYMENT THANK YOU",
    r"PAYMENT RECEIVED",
    r"AUTOMATIC PAYMENT",
    r"CREDIT CARD PAYMENT",
    r"ONLINE PAYMENT",
    r"PAYMENT -",
    # Cashback / rewards credits
    r"CREDIT-CASH",
    r"REWARDS? REDEMPTION",
    r"POINTS REDEMPTION",
]

# Ambiguous merchants — amount-based inference needed
AMBIGUOUS_MERCHANTS = {
    "WALMART": {
        "high_threshold": 80,
        "high_category": "groceries",
        "low_category": "shopping",
    },
    "COSTCO": {
        "high_threshold": 100,
        "high_category": "groceries",
        "low_category": "shopping",
    },
    "TARGET": {
        "high_threshold": 60,
        "high_category": "groceries",
        "low_category": "shopping",
    },
    "SAMS CLUB": {
        "high_threshold": 80,
        "high_category": "groceries",
        "low_category": "shopping",
    },
    "AMAZON": {
        "high_threshold": None,
        "high_category": None,
        "low_category": "shopping",
    },
    "WAWA": {"high_threshold": 20, "high_category": "gas", "low_category": "dining"},
    "SHEETZ": {"high_threshold": 20, "high_category": "gas", "low_category": "dining"},
}

# POS system prefixes to strip before classification
POS_PREFIXES = [
    r"^TST\*\s*",       # Toast POS
    r"^SQ\s*\*\s*",     # Square POS
    r"^SP\s*\*\s*",     # Shopify POS
    r"^IN\s*\*\s*",     # Invoice / misc POS
    r"^CKE\s*\*\s*",    # CKE POS
    r"^PP\*\s*",        # PayPal commerce
    r"^CLOVER\s*\*\s*", # Clover POS
]

# Keyword-based category shortcuts — strong signals that bypass ML/LLM
KEYWORD_SHORTCUTS = [
    # Travel — airlines, hotels, rental cars
    (r"\bAIRLINES?\b", "travel"),
    (r"\bAIRWAYS?\b", "travel"),
    (r"\bAIR LINES?\b", "travel"),
    # Health — explicit pharmacy mentions
    (r"\bPHARMACY\b", "health"),
    # Gas — explicit fuel mentions
    (r"\bGASOLINE\b", "gas"),
    (r"\bFUEL\s", "gas"),
    (r"\bPETROLEUM\b", "gas"),
    # Insurance
    (r"\bINSURANCE\b", "insurance"),
    (r"\bINS PREMIUM\b", "insurance"),
    # Housing
    (r"\bMORTGAGE\b", "housing"),
    (r"\bRENT PAYMENT\b", "housing"),
    (r"\bHOA\s+(FEE|DUES|PAYMENT)\b", "housing"),
    # Fees
    (r"\bATM\s*(FEE|SURCHARGE)\b", "fees"),
    (r"\bOVERDRAFT\s*(FEE|CHARGE)\b", "fees"),
    (r"\bLATE\s*(FEE|CHARGE|PAYMENT FEE)\b", "fees"),
    (r"\bSERVICE\s*(FEE|CHARGE)\b", "fees"),
    (r"\bMONTHLY\s*MAINTENANCE\b", "fees"),
    # Education
    (r"\bTUITION\b", "education"),
    (r"\bSTUDENT LOAN\b", "education"),
    # Subscriptions
    (r"\bMEMBERSHIP\b", "subscriptions"),
    (r"\bSUBSCRIPTION\b", "subscriptions"),
]


class TransactionClassifier:
    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path

    # ─── Core: Integrated classification pipeline ───

    def classify(
        self,
        description: str,
        amount: float | None = None,
        recipient: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """
        Integrated classification — processes through deterministic rules
        and merchant cache, delegates unknowns to LLM at agent layer.

        Returns:
            {
                "category": str or None,
                "confidence": float,
                "method": "income" | "keyword_shortcut" | "merchant_alias" |
                          "ambiguous_rule" | "p2p_history" | "p2p_new" | "needs_llm",
                "needs_user_input": bool,
                "user_prompt": str or None,
                "p2p_recipient": str or None,
                "previous_category": str or None,
            }
        """
        desc_upper = description.upper().strip()

        # ── Step 0: Income/non-expense detection ──
        if self._is_income(desc_upper):
            return self._result("income", 1.0, "income")

        # ── Step 1: P2P detection ──
        if self._is_p2p(desc_upper):
            return self._handle_p2p(desc_upper, amount, recipient, user_id)

        # ── Step 2: Keyword-based category shortcuts ──
        keyword_result = self._check_keyword_shortcuts(desc_upper)
        if keyword_result:
            return keyword_result

        # ── Step 3: Normalize and lookup merchant alias (learning cache) ──
        normalized = self._normalize_merchant(desc_upper)
        alias_result = self._lookup_merchant_alias(normalized)
        if alias_result:
            return alias_result

        # ── Step 4: Ambiguous merchant handling ──
        ambiguous_result = self._check_ambiguous(normalized, amount)
        if ambiguous_result:
            return ambiguous_result

        # ── Step 5: Unknown → delegate to LLM at agent layer ──
        return {
            "category": None,
            "confidence": 0.0,
            "method": "needs_llm",
            "needs_user_input": False,
            "user_prompt": None,
            "p2p_recipient": None,
            "previous_category": None,
            "normalized_description": normalized,
            "description_for_llm": description,
        }

    # ─── Result builder ───

    @staticmethod
    def _result(category: str, confidence: float, method: str) -> dict:
        return {
            "category": category,
            "confidence": confidence,
            "method": method,
            "needs_user_input": False,
            "user_prompt": None,
            "p2p_recipient": None,
            "previous_category": None,
        }

    # ─── Income / P2P detection ───

    def _is_income(self, desc: str) -> bool:
        return any(re.search(p, desc) for p in INCOME_PATTERNS)

    def _is_p2p(self, desc: str) -> bool:
        return any(re.search(p, desc) for p in P2P_PATTERNS)

    def _extract_p2p_recipient(self, desc: str) -> str:
        """Extract recipient name from P2P description"""
        patterns = [
            r"ZELLE\s+(?:TO\s+)?(.+?)(?:\s+\d|$)",
            r"VENMO\s+(?:PAYMENT\s+)?(?:TO\s+)?(.+?)(?:\s+\d|$)",
            r"PAYPAL\s+(?:TO\s+)?(.+?)(?:\s+\d|$)",
            r"CASHAPP\s+(?:TO\s+)?(.+?)(?:\s+\d|$)",
        ]
        for p in patterns:
            m = re.search(p, desc)
            if m:
                return m.group(1).strip()
        for keyword in ["ZELLE", "VENMO", "PAYPAL", "CASHAPP", "CASH APP"]:
            if keyword in desc:
                after = desc.split(keyword, 1)[1].strip()
                after = re.sub(r"^(TO|PAYMENT|SEND)\s+", "", after).strip()
                after = re.split(r"\s+\d", after)[0].strip()
                if after:
                    return after
        return "UNKNOWN"

    def _handle_p2p(
        self,
        desc: str,
        amount: float | None,
        recipient: str | None,
        user_id: str | None,
    ) -> dict:
        """P2P transaction handling — lookup previous record in DB"""
        extracted_recipient = recipient or self._extract_p2p_recipient(desc)

        previous_category = None
        if self.db_path and user_id:
            previous_category = self._lookup_p2p_history(user_id, extracted_recipient)

        if previous_category:
            return {
                "category": previous_category,
                "confidence": 0.8,
                "method": "p2p_history",
                "needs_user_input": True,
                "user_prompt": (
                    f"Previously classified transfer to {extracted_recipient} as "
                    f"'{previous_category}'. Classify the same way this time?"
                ),
                "p2p_recipient": extracted_recipient,
                "previous_category": previous_category,
            }
        else:
            return {
                "category": None,
                "confidence": 0.0,
                "method": "p2p_new",
                "needs_user_input": True,
                "user_prompt": (
                    f"Transfer to {extracted_recipient}: ${amount or '?'} — "
                    f"What is the best spending category for this transfer? "
                    f"Use one of: {', '.join(ALL_CATEGORIES)}, or 'skip' to exclude it."
                ),
                "p2p_recipient": extracted_recipient,
                "previous_category": None,
            }

    def _lookup_p2p_history(self, user_id: str, recipient: str) -> str | None:
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                """
                SELECT category FROM p2p_history
                WHERE user_id = ? AND UPPER(recipient) = UPPER(?)
                ORDER BY last_used DESC LIMIT 1
            """,
                (user_id, recipient),
            )
            row = cur.fetchone()
            conn.close()
            return row[0] if row else None
        except Exception:
            return None

    def save_p2p_category(self, user_id: str, recipient: str, category: str):
        """Save/update category for P2P recipient in DB"""
        if not self.db_path:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO p2p_history (user_id, recipient, category, last_used)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id, recipient) DO UPDATE SET
                    category = excluded.category,
                    last_used = excluded.last_used
            """,
                (user_id, recipient.upper(), category),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    # ─── Keyword-based shortcuts ───

    def _check_keyword_shortcuts(self, desc: str) -> dict | None:
        """Strong keyword signals that reliably indicate a category"""
        for pattern, category in KEYWORD_SHORTCUTS:
            if re.search(pattern, desc):
                return self._result(category, 0.85, "keyword_shortcut")
        return None

    # ─── Merchant alias (learning cache) ───

    def _normalize_merchant(self, desc: str) -> str:
        """Normalize transaction description for cache lookup"""
        # Strip POS system prefixes
        stripped = desc
        for prefix in POS_PREFIXES:
            stripped = re.sub(prefix, "", stripped)

        # Strip city/state suffixes: "HOBOKENNJ", "NEW YORKNY", "JERSEY CITYNJ"
        stripped = re.sub(r"[A-Z]{2,}\s*[A-Z]{2}\s*$", "", stripped).strip()

        # Remove store numbers and trailing noise
        normalized = re.sub(r"\s*#?\d{3,}.*$", "", stripped)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized or desc

    def _lookup_merchant_alias(self, normalized_desc: str) -> dict | None:
        """Check merchant_aliases table (the learning cache populated by LLM results)"""
        if not self.db_path:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            # Exact match on normalized description first
            cur.execute(
                """
                SELECT canonical_name, category FROM merchant_aliases
                WHERE UPPER(alias_pattern) = ?
                LIMIT 1
            """,
                (normalized_desc,),
            )
            row = cur.fetchone()
            if row:
                conn.close()
                return self._result(row[1], 0.9, "merchant_alias")

            # Substring match (longer patterns first for specificity)
            cur.execute(
                """
                SELECT canonical_name, category FROM merchant_aliases
                WHERE ? LIKE '%' || UPPER(alias_pattern) || '%'
                ORDER BY LENGTH(alias_pattern) DESC LIMIT 1
            """,
                (normalized_desc,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                return self._result(row[1], 0.85, "merchant_alias")
        except Exception:
            pass
        return None

    def distill_from_llm(self, description: str, llm_category: str):
        """
        Save LLM classification result to merchant_aliases table.
        Next time this merchant appears, it will be resolved instantly.
        """
        if llm_category not in ALL_CATEGORIES:
            return
        if not self.db_path:
            return

        normalized = self._normalize_merchant(description.upper().strip())
        if not normalized or len(normalized) < 3:
            return

        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO merchant_aliases
                (alias_pattern, canonical_name, category)
                VALUES (?, ?, ?)
            """,
                (normalized, normalized, llm_category),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def save_merchant_alias(
        self, alias_pattern: str, canonical_name: str, category: str
    ):
        """Register new merchant alias (manual or agent-driven)"""
        if not self.db_path:
            return
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO merchant_aliases
                (alias_pattern, canonical_name, category)
                VALUES (?, ?, ?)
            """,
                (alias_pattern.upper(), canonical_name.upper(), category),
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

    def add_feedback(self, description: str, correct_category: str):
        """User corrects a classification → save to merchant_aliases"""
        self.distill_from_llm(description, correct_category)

    # ─── Ambiguous merchant ───

    def _check_ambiguous(self, desc: str, amount: float | None) -> dict | None:
        """Amount-based inference for multi-purpose merchants"""
        # Membership keywords → subscriptions
        MEMBERSHIP_KEYWORDS = ["MEMBERSHIP", "ANNUAL FEE", "MEMBER FEE", "ANNUAL RENEWAL"]
        if any(kw in desc for kw in MEMBERSHIP_KEYWORDS):
            return self._result("subscriptions", 0.8, "membership_keyword")

        # Explicit fuel keywords → gas
        FUEL_KEYWORDS = ["FUEL", "GAS ", "GAS$", "GASOLINE", "PETRO"]
        if any(kw in desc for kw in FUEL_KEYWORDS):
            return self._result("gas", 0.8, "fuel_keyword")

        for merchant, rules in AMBIGUOUS_MERCHANTS.items():
            if merchant in desc:
                threshold = rules["high_threshold"]
                if threshold is None or amount is None:
                    return None  # Can't determine (e.g., Amazon) → delegate to LLM
                cat = rules["high_category"] if amount >= threshold else rules["low_category"]
                return self._result(cat, 0.6, "ambiguous_rule")
        return None

if __name__ == "__main__":
    clf = TransactionClassifier(db_path=DEFAULT_DB_PATH)
    print(f"✓ Classifier ready ({len(ALL_CATEGORIES)} categories)")
    print(f"  Pipeline: Income → P2P → Keywords → Merchant Cache → Ambiguous → LLM\n")

    # Basic classification test
    test_cases = [
        ("WHOLEFDS MKT 10293", None),
        ("SHELL OIL 57432", None),
        ("DOORDASH CHIPOTLE", None),
        ("UNITED AIRLINES", None),
        ("NETFLIX.COM", None),
        ("CVS PHARMACY 3821", None),
        ("TARGET 00029381", 45.0),
        ("TARGET 00029381", 150.0),
        ("UBER TRIP", None),
        ("COMCAST CABLE", None),
        ("GEICO AUTO INS", None),
        ("COURSERA SUBSCRIPTION", None),
        ("UNIVERSITY OF TEXAS", None),
        ("AVALON COMMUNITIES RENT PAYMENT", None),
        ("MORTGAGE PAYMENT WELLS FARGO", None),
        ("ATM FEE WITHDRAWAL", None),
        ("MONTHLY SERVICE FEE", None),
    ]

    print("━━━━ Classification Test ━━━━")
    for desc, amount in test_cases:
        result = clf.classify(desc, amount=amount)
        method = result["method"]
        cat = result["category"] or "→LLM"
        conf = result["confidence"]
        suffix = f" [${amount:.0f}]" if amount else ""
        print(f"  {desc:<40}{suffix:<8} → {cat:<15} ({conf:.0%}) [{method}]")

    # P2P test
    print("\n━━━━ P2P Test ━━━━")
    p2p_cases = [
        "ZELLE TO JOHN DOE",
        "VENMO PAYMENT JANE SMITH",
        "ZELLE FROM EMPLOYER",  # income
    ]
    for desc in p2p_cases:
        result = clf.classify(desc, amount=30.0)
        if result["needs_user_input"]:
            print(f"  {desc:<30} → P2P: {result['user_prompt'][:60]}...")
        else:
            print(f"  {desc:<30} → {result['category']} [{result['method']}]")

    # Income detection test
    print("\n━━━━ Income Detection ━━━━")
    income_cases = [
        "PAYROLL DIRECT DEP",
        "TAX REFUND IRS",
        "REFUND AMAZON",
        "CAPITAL ONE MOBILE PYMT",
        "CREDIT-CASH BACK REWARD",
    ]
    for desc in income_cases:
        result = clf.classify(desc)
        print(f"  {desc:<30} → {result['category']} [{result['method']}]")

    # Keyword shortcut test
    print("\n━━━━ Keyword Shortcuts ━━━━")
    keyword_cases = [
        "UNITED AIRLINES",
        "DELTA AIRLINES",
        "BRITISH AIRWAYS",
        "CVS PHARMACY 3821",
        "MORTGAGE PAYMENT WELLS FARGO",
        "ATM FEE WITHDRAWAL",
        "GEICO INSURANCE",
        "TUITION PAYMENT",
        "STUDENT LOAN PMT",
        "COSTCO MEMBERSHIP",
        "NETFLIX SUBSCRIPTION",
    ]
    for desc in keyword_cases:
        result = clf.classify(desc)
        cat = result["category"] or "→LLM"
        print(f"  {desc:<35} → {cat:<15} [{result['method']}]")
