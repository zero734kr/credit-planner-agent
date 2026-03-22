"""
Classification constants — categories, regex patterns, keyword shortcuts, ambiguous rules.

Used by classifier.py for the deterministic classification pipeline.
"""

# ─── Valid spending categories (14 total) ───
ALL_CATEGORIES = [
    "groceries", "dining", "gas", "travel", "entertainment",
    "utilities", "insurance", "shopping", "transportation", "health",
    "education", "subscriptions", "housing", "fees",
]

# ─── P2P service patterns ───
P2P_PATTERNS = [
    r"ZELLE\b",
    r"VENMO\b",
    r"PAYPAL\b",
    r"CASHAPP\b",
    r"CASH APP\b",
    r"APPLE\s*CASH",
    r"GOOGLE\s*PAY\s*(SEND|P2P)",
]

# ─── Income / non-expense patterns (excluded from spending analysis) ───
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
    r"ZELLE FROM\b",
    # Credit card payments (money moving between accounts)
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

# ─── Ambiguous merchants — amount-based inference ───
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

# ─── POS system prefixes to strip before classification ───
POS_PREFIXES = [
    r"^TST\*\s*",       # Toast POS
    r"^SQ\s*\*\s*",     # Square POS
    r"^SP\s*\*\s*",     # Shopify POS
    r"^IN\s*\*\s*",     # Invoice / misc POS
    r"^CKE\s*\*\s*",    # CKE POS
    r"^PP\*\s*",        # PayPal commerce
    r"^CLOVER\s*\*\s*", # Clover POS
]

# ─── Keyword-based category shortcuts — strong signals that bypass LLM ───
KEYWORD_SHORTCUTS = [
    # Travel
    (r"\bAIRLINES?\b", "travel"),
    (r"\bAIRWAYS?\b", "travel"),
    (r"\bAIR LINES?\b", "travel"),
    # Health
    (r"\bPHARMACY\b", "health"),
    # Gas
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

# ─── Payment patterns (used by spending_analyzer for card payment detection) ───
PAYMENT_PATTERNS = [
    r"PAYMENT THANK YOU",
    r"AUTOPAY PAYMENT",
    r"CAPITAL ONE MOBILE PMT",
    r"PAYMENT TO CHASE CARD",
    r"AUTOPAY ENROLL",
]

# ─── Subscription detection constants (used by recurring detection) ───
SUBSCRIPTION_CATEGORIES = {"subscriptions", "insurance", "utilities", "education", "housing"}

SUBSCRIPTION_KEYWORDS = [
    "NETFLIX", "SPOTIFY", "HULU", "DISNEY", "HBO", "APPLE.COM/BILL",
    "YOUTUBE PREMIUM", "AMAZON PRIME", "ADOBE", "MICROSOFT 365",
    "GOOGLE STORAGE", "ICLOUD", "DROPBOX", "CHATGPT", "ANTHROPIC",
    "GEICO", "STATE FARM", "PROGRESSIVE", "ALLSTATE", "INSURANCE",
    "MEMBERSHIP", "SUBSCRIPTION", "CLOUDFLARE", "GITHUB",
]
