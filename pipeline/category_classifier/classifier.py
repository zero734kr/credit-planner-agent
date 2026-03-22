"""
Transaction Category Classifier
Deterministic Rules + Merchant Cache + LLM Fallback + P2P Processing

Classification Pipeline:
  0. Income / non-expense detection (regex) → excluded from spending analysis
  1. P2P Detection (Zelle, Venmo, PayPal, etc.) → Lookup recipient in p2p_history
  2. Keyword-based category shortcuts (AIRLINES→travel, PHARMACY→health, etc.)
  3. Merchant alias lookup (SQLite cache) → instant if previously classified
  4. Ambiguous merchant handling (amount-based: Walmart, Costco, Target, etc.)
  5. Unknown → delegate to LLM (agent layer) → auto-distill result to merchant_aliases

Usage:
  from pipeline.category_classifier.classifier import TransactionClassifier
  clf = TransactionClassifier(db_path="db/credit_planner.db")
  result = clf.classify("WHOLEFDS MKT 10293")
"""

import re
import sqlite3

from pipeline.category_classifier.patterns import (
    ALL_CATEGORIES,
    AMBIGUOUS_MERCHANTS,
    INCOME_PATTERNS,
    KEYWORD_SHORTCUTS,
    P2P_PATTERNS,
    POS_PREFIXES,
)


class TransactionClassifier:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def classify(
        self,
        description: str,
        amount: float | None = None,
        recipient: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """
        Run the deterministic classification pipeline.

        Returns dict with keys:
            category, confidence, method, needs_user_input,
            user_prompt, p2p_recipient, previous_category
        """
        desc_upper = description.upper().strip()

        if self._is_income(desc_upper):
            return _result("income", 1.0, "income")

        if self._is_p2p(desc_upper):
            return self._handle_p2p(desc_upper, amount, recipient, user_id)

        keyword_result = self._check_keyword_shortcuts(desc_upper)
        if keyword_result:
            return keyword_result

        normalized = self._normalize_merchant(desc_upper)
        alias_result = self._lookup_merchant_alias(normalized)
        if alias_result:
            return alias_result

        ambiguous_result = self._check_ambiguous(normalized, amount)
        if ambiguous_result:
            return ambiguous_result

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

    # ─── Income / P2P detection ───

    def _is_income(self, desc: str) -> bool:
        return any(re.search(p, desc) for p in INCOME_PATTERNS)

    def _is_p2p(self, desc: str) -> bool:
        return any(re.search(p, desc) for p in P2P_PATTERNS)

    def _extract_p2p_recipient(self, desc: str) -> str:
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

    # ─── P2P history DB operations ───

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

    # ─── Keyword shortcuts ───

    def _check_keyword_shortcuts(self, desc: str) -> dict | None:
        for pattern, category in KEYWORD_SHORTCUTS:
            if re.search(pattern, desc):
                return _result(category, 0.85, "keyword_shortcut")
        return None

    # ─── Merchant alias (learning cache) ───

    def _normalize_merchant(self, desc: str) -> str:
        stripped = desc
        for prefix in POS_PREFIXES:
            stripped = re.sub(prefix, "", stripped)
        stripped = re.sub(r"[A-Z]{2,}\s*[A-Z]{2}\s*$", "", stripped).strip()
        normalized = re.sub(r"\s*#?\d{3,}.*$", "", stripped)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized or desc

    def _lookup_merchant_alias(self, normalized_desc: str) -> dict | None:
        if not self.db_path:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
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
                return _result(row[1], 0.9, "merchant_alias")

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
                return _result(row[1], 0.85, "merchant_alias")
        except Exception:
            pass
        return None

    # ─── LLM result distillation ───

    def distill_from_llm(self, description: str, llm_category: str):
        """Save LLM classification to merchant_aliases for instant future lookup."""
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
        self.distill_from_llm(description, correct_category)

    # ─── Ambiguous merchant ───

    def _check_ambiguous(self, desc: str, amount: float | None) -> dict | None:
        MEMBERSHIP_KEYWORDS = ["MEMBERSHIP", "ANNUAL FEE", "MEMBER FEE", "ANNUAL RENEWAL"]
        if any(kw in desc for kw in MEMBERSHIP_KEYWORDS):
            return _result("subscriptions", 0.8, "membership_keyword")

        FUEL_KEYWORDS = ["FUEL", "GAS ", "GAS$", "GASOLINE", "PETRO"]
        if any(kw in desc for kw in FUEL_KEYWORDS):
            return _result("gas", 0.8, "fuel_keyword")

        for merchant, rules in AMBIGUOUS_MERCHANTS.items():
            if merchant in desc:
                threshold = rules["high_threshold"]
                if threshold is None or amount is None:
                    return None
                cat = rules["high_category"] if amount >= threshold else rules["low_category"]
                return _result(cat, 0.6, "ambiguous_rule")
        return None


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
