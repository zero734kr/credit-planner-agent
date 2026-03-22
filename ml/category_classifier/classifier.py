"""
Transaction Category Classifier
TF-IDF + Logistic Regression + LLM Fallback + P2P Processing

Classification Pipeline:
  1. P2P Detection (Zelle, Venmo, PayPal, etc.) → Lookup recipient record in p2p_history
     → Record found: Suggest previous category → User confirms
     → No record: Ask user directly
  2. Merchant alias normalization → Unify different descriptions of same merchant
  3. ML Classification (confidence >= THRESHOLD)
  4. Low ML confidence → LLM inference fallback → Distill results into dataset
  5. LLM also uncertain → Ask user

Usage:
  from ml.category_classifier.classifier import TransactionClassifier
  clf = TransactionClassifier()
  clf.load_or_train()
  result = clf.classify("WHOLEFDS MKT 10293")
  # → {"category": "groceries", "confidence": 0.85, "method": "ml", "needs_user_input": False}
"""

import os
import re
import json
import pickle
import sqlite3
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

MODEL_DIR = os.path.dirname(__file__)
MODEL_PATH = os.path.join(MODEL_DIR, "model.pkl")
TRAINING_DATA_PATH = os.path.join(MODEL_DIR, "training_data.json")
DEFAULT_DB_PATH = os.path.join(MODEL_DIR, "..", "..", "db", "credit_planner.db")

# ML Classification confidence threshold — below this triggers LLM fallback
# With 14 classes, random baseline is ~7%, so 12%+ is meaningful classification
# Lowered from 0.15 to 0.12 after expanding training data to 1400+ samples —
# model is better calibrated, and borderline cases (airlines, etc.) fall 12-14%
CONFIDENCE_THRESHOLD = 0.12

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


# ─── Training data ───
# Seed data lives in training_data.json (same directory).
# distill_from_llm() and add_feedback() append to this file at runtime,
# so it accumulates learned examples over time.

def _load_training_data() -> dict:
    """Load training data from JSON file"""
    if os.path.exists(TRAINING_DATA_PATH):
        with open(TRAINING_DATA_PATH, "r") as f:
            return json.load(f)
    return {}

SEED_TRAINING_DATA = _load_training_data()

# Full list of categories (used in LLM prompts and validation)
ALL_CATEGORIES = list(SEED_TRAINING_DATA.keys())


class TransactionClassifier:
    def __init__(self, db_path: str):
        self.pipeline = None
        self.db_path = db_path

    def _build_training_set(self):
        """Load training data (seed + user feedback + accumulated LLM distillation)"""
        if os.path.exists(TRAINING_DATA_PATH):
            with open(TRAINING_DATA_PATH, "r") as f:
                data = json.load(f)
        else:
            data = SEED_TRAINING_DATA

        texts, labels = [], []
        for category, descriptions in data.items():
            for desc in descriptions:
                texts.append(desc.upper())
                labels.append(category)
        return texts, labels

    def train(self):
        """Train the model"""
        texts, labels = self._build_training_set()

        self.pipeline = Pipeline(
            [
                (
                    "tfidf",
                    TfidfVectorizer(
                        analyzer="char_wb",
                        ngram_range=(2, 5),
                        max_features=5000,
                        lowercase=True,
                    ),
                ),
                (
                    "clf",
                    LogisticRegression(
                        max_iter=1000,
                        C=1.0,
                        class_weight="balanced",
                    ),
                ),
            ]
        )

        self.pipeline.fit(texts, labels)
        self._save()
        return len(texts)

    def _save(self):
        with open(MODEL_PATH, "wb") as f:
            pickle.dump(self.pipeline, f)

    def load_or_train(self):
        """Load existing model, or train if not found"""
        if os.path.exists(MODEL_PATH):
            with open(MODEL_PATH, "rb") as f:
                self.pipeline = pickle.load(f)
        else:
            self.train()

    # ─── Core: Integrated classification pipeline ───

    def classify(
        self,
        description: str,
        amount: float | None = None,
        recipient: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """
        Integrated classification — processes through all logic to determine final category

        Returns:
            {
                "category": str or None,
                "confidence": float,
                "method": "ml" | "llm_distill" | "p2p_history" | "ambiguous_rule" | "income",
                "needs_user_input": bool,
                "user_prompt": str or None,   # Question to ask user
                "p2p_recipient": str or None, # P2P recipient if applicable
                "previous_category": str or None,  # Previous category if P2P history exists
            }
        """
        desc_upper = description.upper().strip()

        # ── Step 0: Income/non-expense detection → excluded from spending analysis ──
        if self._is_income(desc_upper):
            return {
                "category": "income",
                "confidence": 1.0,
                "method": "income",
                "needs_user_input": False,
                "user_prompt": None,
                "p2p_recipient": None,
                "previous_category": None,
            }

        # ── Step 1: P2P detection ──
        if self._is_p2p(desc_upper):
            return self._handle_p2p(desc_upper, amount, recipient, user_id)

        # ── Step 1.5: Keyword-based category shortcuts ──
        # Some terms are strong enough signals to skip ML entirely
        keyword_result = self._check_keyword_shortcuts(desc_upper)
        if keyword_result:
            return keyword_result

        # ── Step 2: Merchant alias normalization ──
        normalized = self._normalize_merchant(desc_upper)

        # ── Step 3: Ambiguous merchant handling ──
        ambiguous_result = self._check_ambiguous(normalized, amount)
        if ambiguous_result:
            return ambiguous_result

        # ── Step 4: ML classification ──
        if self.pipeline is None:
            self.load_or_train()

        assert self.pipeline is not None

        category = self.pipeline.predict([normalized])[0]
        proba = self.pipeline.predict_proba([normalized])[0]
        confidence = float(np.max(proba))

        if confidence >= CONFIDENCE_THRESHOLD:
            return {
                "category": category,
                "confidence": confidence,
                "method": "ml",
                "needs_user_input": False,
                "user_prompt": None,
                "p2p_recipient": None,
                "previous_category": None,
            }

        # ── Step 5: Low ML confidence → indicate LLM fallback needed ──
        # Actual LLM call performed at agent layer (this module is pure Python)
        # When LLM determines category, call distill_from_llm() to add to dataset
        return {
            "category": None,
            "confidence": confidence,
            "method": "needs_llm",
            "needs_user_input": False,
            "user_prompt": None,
            "p2p_recipient": None,
            "previous_category": None,
            "ml_suggestion": category,
            "ml_top3": self._get_top3(normalized),
            "description_for_llm": description,
        }

    # ─── P2P Processing ───

    def _is_p2p(self, desc: str) -> bool:
        return any(re.search(p, desc) for p in P2P_PATTERNS)

    def _is_income(self, desc: str) -> bool:
        return any(re.search(p, desc) for p in INCOME_PATTERNS)

    def _extract_p2p_recipient(self, desc: str) -> str:
        """Extract recipient name from P2P description"""
        # "ZELLE TO JOHN DOE" → "JOHN DOE"
        # "VENMO PAYMENT JANE" → "JANE"
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
        # fallback: text after P2P keyword
        for keyword in ["ZELLE", "VENMO", "PAYPAL", "CASHAPP", "CASH APP"]:
            if keyword in desc:
                after = desc.split(keyword, 1)[1].strip()
                # Remove "TO "
                after = re.sub(r"^(TO|PAYMENT|SEND)\s+", "", after).strip()
                # Until digits/special characters
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

        # Lookup previous P2P record in DB
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
        """Lookup previous category of P2P recipient from DB"""
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

    # ─── Keyword-based category shortcuts ───
    # Strong keyword signals that reliably indicate a category without ML
    KEYWORD_SHORTCUTS = [
        # Travel — airlines, hotels, rental cars
        (r"\bAIRLINES?\b", "travel"),
        (r"\bAIRWAYS?\b", "travel"),
        (r"\bAIRLINE\b", "travel"),
        (r"\bAIR LINES?\b", "travel"),
        # Pharmacy / health — explicit pharmacy mentions
        (r"\bPHARMACY\b", "health"),
        (r"\bPHARMA\b", "health"),
    ]

    def _check_keyword_shortcuts(self, desc: str) -> dict | None:
        """Check for strong keyword signals that bypass ML classification"""
        for pattern, category in self.KEYWORD_SHORTCUTS:
            if re.search(pattern, desc):
                return {
                    "category": category,
                    "confidence": 0.75,
                    "method": "keyword_shortcut",
                    "needs_user_input": False,
                    "user_prompt": None,
                    "p2p_recipient": None,
                    "previous_category": None,
                }
        return None

    # ─── Merchant alias normalization ───

    def _normalize_merchant(self, desc: str) -> str:
        """Normalize various descriptions of the same merchant"""
        if self.db_path:
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT canonical_name FROM merchant_aliases
                    WHERE UPPER(?) LIKE '%' || UPPER(alias_pattern) || '%'
                    ORDER BY LENGTH(alias_pattern) DESC LIMIT 1
                """,
                    (desc,),
                )
                row = cur.fetchone()
                conn.close()
                if row:
                    return row[0]
            except Exception:
                pass

        # Strip POS system prefixes (TST* = Toast, SQ * = Square, etc.)
        POS_PREFIXES = [
            r"^TST\*\s*",       # Toast POS
            r"^SQ\s*\*\s*",     # Square POS
            r"^SP\s*\*\s*",     # Shopify POS
            r"^IN\s*\*\s*",     # Invoice / misc POS
            r"^CKE\s*\*\s*",    # CKE POS
            r"^PP\*\s*",        # PayPal commerce
            r"^CLOVER\s*\*\s*", # Clover POS
        ]
        stripped = desc
        for prefix in POS_PREFIXES:
            stripped = re.sub(prefix, "", stripped)

        # Strip city/state suffixes: "HOBOKENNJ", "NEW YORKNY", "JERSEY CITYNJ"
        # Pattern: word characters ending with 2-letter state code at end of string
        stripped = re.sub(r"[A-Z]{2,}\s*[A-Z]{2}\s*$", "", stripped).strip()

        # Basic normalization: remove numbers/special chars, trim end
        normalized = re.sub(
            r"\s*#?\d{3,}.*$", "", stripped
        )  # "WHOLEFDS MKT 10293" → "WHOLEFDS MKT"
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized or desc

    def save_merchant_alias(
        self, alias_pattern: str, canonical_name: str, category: str
    ):
        """Register new merchant alias"""
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

    # ─── Ambiguous merchant ───

    def _check_ambiguous(self, desc: str, amount: float | None) -> dict | None:
        """Amount-based inference for multi-purpose merchants like Walmart/Costco"""
        # membership/annual keywords → go straight to subscriptions (COSTCO ANNUAL MEMBERSHIP, etc.)
        MEMBERSHIP_KEYWORDS = [
            "MEMBERSHIP",
            "ANNUAL FEE",
            "MEMBER FEE",
            "ANNUAL RENEWAL",
        ]
        if any(kw in desc for kw in MEMBERSHIP_KEYWORDS):
            return {
                "category": "subscriptions",
                "confidence": 0.8,
                "method": "membership_keyword",
                "needs_user_input": False,
                "user_prompt": None,
                "p2p_recipient": None,
                "previous_category": None,
            }

        # If explicit fuel/gas keywords present, skip ambiguous handling and go straight to gas
        FUEL_KEYWORDS = ["FUEL", "GAS ", "GAS$", "GASOLINE", "PETRO"]
        if any(kw in desc for kw in FUEL_KEYWORDS):
            return {
                "category": "gas",
                "confidence": 0.8,
                "method": "fuel_keyword",
                "needs_user_input": False,
                "user_prompt": None,
                "p2p_recipient": None,
                "previous_category": None,
            }

        for merchant, rules in AMBIGUOUS_MERCHANTS.items():
            if merchant in desc:
                threshold = rules["high_threshold"]
                if threshold is None or amount is None:
                    # Cannot determine by amount (e.g., Amazon) → delegate to ML
                    return None
                if amount >= threshold:
                    cat = rules["high_category"]
                else:
                    cat = rules["low_category"]
                return {
                    "category": cat,
                    "confidence": 0.6,
                    "method": "ambiguous_rule",
                    "needs_user_input": False,
                    "user_prompt": None,
                    "p2p_recipient": None,
                    "previous_category": None,
                }
        return None

    # ─── ML helpers ───

    def _get_top3(self, desc: str) -> list:
        """Get top 3 categories + probabilities"""
        if self.pipeline is None:
            self.load_or_train()
        assert self.pipeline is not None
        proba = self.pipeline.predict_proba([desc])[0]
        classes = self.pipeline.classes_
        top_idx = np.argsort(proba)[-3:][::-1]
        return [(classes[i], round(float(proba[i]), 3)) for i in top_idx]

    def predict(self, description: str) -> str:
        """Backward compatible — simple category return"""
        result = self.classify(description)
        return result["category"] or "unknown"

    def predict_with_confidence(self, description: str) -> tuple:
        """Backward compatible — category + confidence"""
        if self.pipeline is None:
            self.load_or_train()

        assert self.pipeline is not None

        desc_upper = description.upper()
        category = self.pipeline.predict([desc_upper])[0]
        proba = self.pipeline.predict_proba([desc_upper])[0]
        confidence = float(np.max(proba))
        return category, confidence

    def predict_batch(self, descriptions: list) -> list:
        """Batch prediction"""
        if self.pipeline is None:
            self.load_or_train()

        assert self.pipeline is not None

        return list(self.pipeline.predict([d.upper() for d in descriptions]))

    # ─── LLM Distillation: Improve model by adding LLM-classified results to dataset ───

    def distill_from_llm(self, description: str, llm_category: str):
        """
        Add category determined by LLM to training data and retrain model.
        Next time similar description arrives, ML can handle it directly.
        """
        if llm_category not in ALL_CATEGORIES:
            # Ignore invalid category
            return

        if os.path.exists(TRAINING_DATA_PATH):
            with open(TRAINING_DATA_PATH, "r") as f:
                data = json.load(f)
        else:
            data = {k: list(v) for k, v in SEED_TRAINING_DATA.items()}

        if llm_category not in data:
            data[llm_category] = []

        desc_upper = description.upper().strip()
        # Prevent duplicates
        if desc_upper not in [d.upper() for d in data[llm_category]]:
            data[llm_category].append(desc_upper)

            with open(TRAINING_DATA_PATH, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            # Retrain model
            self.train()

    def add_feedback(self, description: str, correct_category: str):
        """Add training data from user feedback (same logic as distill_from_llm)"""
        self.distill_from_llm(description, correct_category)


if __name__ == "__main__":
    clf = TransactionClassifier(db_path=DEFAULT_DB_PATH)
    n = clf.train()
    print(f"✓ Model training complete: {n} samples ({len(ALL_CATEGORIES)} categories)")

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

    print("\n━━━━ Classification Test ━━━━")
    for desc, amount in test_cases:
        result = clf.classify(desc, amount=amount)
        method = result["method"]
        cat = result["category"] or "(?)"
        conf = result["confidence"]
        suffix = f" [${amount:.0f}]" if amount else ""
        print(f"  {desc:<25}{suffix:<8} → {cat:<15} ({conf:.0%}) [{method}]")

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
            print(f"  {desc:<30} → Question: {result['user_prompt']}")
        else:
            print(f"  {desc:<30} → {result['category']} [{result['method']}]")

    # Income detection test
    print("\n━━━━ Income Detection ━━━━")
    income_cases = ["PAYROLL DIRECT DEP", "TAX REFUND IRS", "REFUND AMAZON"]
    for desc in income_cases:
        result = clf.classify(desc)
        print(f"  {desc:<25} → {result['category']} [{result['method']}]")
