"""
CreditPlanner SQLite DB initialization script
- Create all tables
- Configure WAL mode
- Seed issuer-specific churning rules
- Seed CLI policies
"""

import sqlite3
import os
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "db", "credit_planner.db")
LOGS_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILES = [
    os.path.join(LOGS_DIR, "decision_log.jsonl"),
    os.path.join(LOGS_DIR, "profile_log.jsonl"),
]

def _reset_log_files() -> None:
    """Clear append-only logs as part of an explicit environment reset."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    for path in LOG_FILES:
        with open(path, "w", encoding="utf-8"):
            pass
    print("✓ Reset decision/profile logs")


def init_db(db_path: str | None = None, reset: bool = False) -> str:
    """
    Initialize DB (create tables + seed data)

    Args:
        db_path: SQLite DB path. If None, use default path in project.
                 When creating a working copy in session, pass session directory path.
        reset: If True, delete the existing DB file and clear decision/profile logs
               before reinitializing the environment.

    Returns: Actual DB path used
    """
    db_path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    if reset:
        for suffix in ("", "-wal", "-shm"):
            candidate = db_path + suffix
            if os.path.exists(candidate):
                os.remove(candidate)
        _reset_log_files()

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Configure WAL mode (concurrent read/write stability)
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA foreign_keys=ON;")

    # ─── 1. User base profile ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_profile (
        user_id TEXT PRIMARY KEY,
        credit_score INTEGER,
        score_type TEXT,
        aaoa_months INTEGER,
        total_accounts INTEGER,
        hard_pull_count_24mo INTEGER,
        chase_524_count INTEGER,
        annual_income INTEGER,
        updated_at TIMESTAMP
    );
    """)

    # ─── 2. User cards held ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_cards (
        card_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        issuer TEXT,
        card_name TEXT,
        card_type TEXT,
        opened_date DATE,
        credit_limit INTEGER,
        starting_limit INTEGER,
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
    """)

    # ─── 3. Monthly spending patterns ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS spending_pattern (
        user_id TEXT,
        category TEXT,
        monthly_avg INTEGER,
        FOREIGN KEY (user_id) REFERENCES user_profile(user_id),
        PRIMARY KEY (user_id, category)
    );
    """)

    # ─── 4. Transaction raw data ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        tx_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        tx_date DATE,
        description TEXT,
        amount REAL,
        category TEXT,
        source TEXT,
        card_name TEXT,
        FOREIGN KEY (user_id) REFERENCES user_profile(user_id)
    );
    """)

    # ─── 5. Issuer-specific churning rules ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issuer_churning_rules (
        rule_id INTEGER PRIMARY KEY AUTOINCREMENT,
        issuer TEXT,
        rule_name TEXT,
        rule_type TEXT,
        description TEXT,
        cooldown_months INTEGER,
        exception_note TEXT,
        updated_at TIMESTAMP
    );
    """)

    # ─── 6. CLI policy table ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS issuer_cli_policy (
        issuer TEXT,
        card_type TEXT,
        pull_type TEXT,
        min_wait_days INTEGER,
        cooldown_days INTEGER,
        request_method TEXT,
        max_multiplier REAL,
        notes TEXT,
        PRIMARY KEY (issuer, card_type)
    );
    """)

    # ─── 7. Search cache (temporary storage for real-time search results) ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS search_cache (
        cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
        query TEXT,
        source_url TEXT,
        card_name TEXT,
        issuer TEXT,
        signup_bonus TEXT,
        annual_fee INTEGER,
        category_multipliers TEXT,
        fetched_at TIMESTAMP,
        expires_at TIMESTAMP
    );
    """)

    # ─── 8. User preferences (generic key-value store) ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_preferences (
        user_id TEXT,
        pref_key TEXT,
        pref_value TEXT,
        description TEXT,
        created_at TIMESTAMP,
        updated_at TIMESTAMP,
        PRIMARY KEY (user_id, pref_key),
        FOREIGN KEY (user_id) REFERENCES user_profile(user_id)
    );
    """)

    # ─── 9. Transaction exclusion rules ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS transaction_exclusions (
        exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        rule_type TEXT,
        pattern TEXT,
        match_field TEXT DEFAULT 'description',
        reason TEXT,
        active BOOLEAN DEFAULT 1,
        created_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES user_profile(user_id)
    );
    """)
    # ─── 10. P2P recipient category history ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS p2p_history (
        user_id TEXT,
        recipient TEXT,
        category TEXT,
        last_used TIMESTAMP,
        PRIMARY KEY (user_id, recipient)
    );
    """)

    # ─── 11. Merchant alias normalization ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS merchant_aliases (
        alias_pattern TEXT PRIMARY KEY,
        canonical_name TEXT,
        category TEXT
    );
    """)

    # ─── 12. Recurring transaction detection ───
    cur.execute("""
    CREATE TABLE IF NOT EXISTS recurring_transactions (
        user_id TEXT,
        merchant_pattern TEXT,
        typical_amount REAL,
        category TEXT,
        frequency TEXT,
        last_seen DATE,
        PRIMARY KEY (user_id, merchant_pattern)
    );
    """)

    conn.commit()

    # ─── Churning rules seed ───
    now = datetime.datetime.utcnow()

    churning_rules = [
        ("Chase", "5/24", "hard_block",
         "Chase cards automatically denied if 5+ new cards from all issuers combined held within 24 months",
         None, "Some business card exceptions (Ink family subject to 5/24, some co-brands may be excluded)", now),

        ("Chase", "One Sapphire Rule", "hard_block",
         "Can only hold one of CSP or CSR. Sapphire family SUB has 48-month cooldown",
         48, "48-month rule applies even when converting CSP→CSR or CSR→CSP", now),

        ("Chase", "2/30 Rule", "soft_block",
         "Max 2 Chase cards approved within 30 days",
         None, "Can sometimes bypass by combining business + personal applications", now),

        ("Amex", "Once Per Lifetime", "hard_block",
         "Signup bonus for same card can only be earned once per lifetime",
         None, "NLL (No Lifetime Language) targeted offers are exceptions. Different card numbers may be treated separately (DP)", now),

        ("Amex", "1/5 Rule", "hard_block",
         "Max 1 Amex credit card approved within 5 days",
         None, "Charge cards counted separately", now),

        ("Amex", "2/90 Rule", "hard_block",
         "Max 2 Amex credit cards approved within 90 days",
         None, "Charge cards not included in this limit", now),

        ("Amex", "Max 5 Credit Cards", "hard_block",
         "Max 5 Amex credit cards (charge cards excluded) held simultaneously",
         None, "New cards possible after closing existing card. Charge cards have no limit", now),

        ("Amex", "Popup Jail", "soft_block",
         "Amex may show 'you are not eligible for this welcome offer' popup during application. "
         "This is an algorithmic decision by Amex — not a hard rule with fixed thresholds. "
         "Known risk factors: opening multiple Amex cards in short succession, churning history, "
         "low spend on existing Amex cards, and closing cards shortly after earning SUB. "
         "Popup does NOT prevent card approval — only removes the signup bonus.",
         None,
         "Not a hard rule — varies by individual. Recovery strategies include: "
         "increasing organic spend on existing Amex cards, waiting 3-6+ months, "
         "adding authorized users, putting recurring bills on Amex cards. "
         "Check via 'apply if you are pre-qualified/approved' flow — if popup appears, "
         "cancel application (no hard pull incurred). "
         "Community DPs vary widely — always verify via real-time web search before advising. "
         "Do NOT make definitive claims about popup causes or timelines without current DPs.",
         now),

        ("Citi", "48 Month Rule", "hard_block",
         "Same card SUB has 48-month cooldown. Must wait 48 months after closing/PC to earn SUB again",
         48, "48-month countdown starts from card closure or product change date", now),

        ("Citi", "1/8 Rule", "soft_block",
         "Max 1 Citi card approved within 8 days",
         None, None, now),

        ("Citi", "2/65 Rule", "soft_block",
         "Max 2 Citi cards approved within 65 days",
         None, None, now),

        ("Barclays", "6/24 Sensitivity", "soft_block",
         "Barclays approval very conservative if 6+ new cards held within 24 months",
         None, "Not a hard rule but denial rate spikes if 6/24 exceeded", now),

        ("US Bank", "Conservative Approval", "soft_block",
         "US Bank very conservative with new relationships. Existing relationships (checking/savings accounts) matter",
         None, "High denial likelihood if multiple new accounts within past 12 months. Existing relationship is advantageous", now),

        ("Capital One", "One Card Per Product", "soft_block",
         "Conservative with duplicate holdings in same product family (Venture, SavorOne, etc.)",
         None, "Can leverage strategy of PC existing card then reapply", now),

        ("Wells Fargo", "Cell Phone Rule", "soft_block",
         "Phone number registered in Wells Fargo account must match application info when applying for card",
         None, "Existing relationship (checking/savings) significantly increases approval odds", now),
    ]

    cur.execute("SELECT COUNT(*) FROM issuer_churning_rules")
    if cur.fetchone()[0] == 0:
        cur.executemany("""
            INSERT INTO issuer_churning_rules
            (issuer, rule_name, rule_type, description, cooldown_months, exception_note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, churning_rules)
        print(f"✓ Seeded {len(churning_rules)} churning rules")

    # ─── CLI policies seed ───
    cli_policies = [
        ("Chase", "credit", "soft", 180, 90, "online (Secure Message)",
         None, "Recommended via SM after 6 months. Automatic CLI may occur. Phone possible but HP risk"),

        ("Amex", "credit", "soft", 61, 91, "online (account → credit limit)",
         3.0, "Available after 61 days. Up to 3x starting limit relatively easy. Exceeding 3x risks FR (Financial Review)"),

        ("Amex", "charge", "soft", 61, 91, "online",
         None, "Charge cards have NPSL so no traditional CLI. Spending power adjusts automatically"),

        ("Citi", "credit", "hard", 180, 180, "online / phone",
         None, "Citi CLI is hard pull! Must coordinate with new card plans. Available after 6 months"),

        ("Discover", "credit", "soft", 90, 90, "online",
         None, "Online requests available after 3 months. Relatively generous"),

        ("Capital One", "credit", "soft", 180, 180, "online / phone",
         None, "Capital One CLI request itself is soft pull but approval criteria conservative. Waiting 6+ months advantageous"),

        ("Barclays", "credit", "hard", 180, 180, "phone",
         None, "Barclays CLI is hard pull. Phone only. Weigh HP burden against benefit"),

        ("US Bank", "credit", "soft", 180, 180, "online / phone",
         None, "Online available after 6 months. Conservative, so usage history matters"),

        ("Wells Fargo", "credit", "soft", 180, 90, "online / phone",
         None, "Available after 6 months. Existing relationship advantageous"),

        ("Bank of America", "credit", "soft", 180, 90, "online / phone",
         None, "Results vary by BofA account relationship + Preferred Rewards tier"),
    ]

    cur.execute("SELECT COUNT(*) FROM issuer_cli_policy")
    if cur.fetchone()[0] == 0:
        cur.executemany("""
            INSERT INTO issuer_cli_policy
            (issuer, card_type, pull_type, min_wait_days, cooldown_days, request_method, max_multiplier, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, cli_policies)
        print(f"✓ Seeded {len(cli_policies)} CLI policies")

    conn.commit()
    conn.close()

    print(f"✓ DB initialization complete: {db_path}")

    return db_path


if __name__ == "__main__":
    import sys
    # CLI: python init_db.py [optional_db_path] [--reset]
    args = [arg for arg in sys.argv[1:] if arg]
    reset = False
    if "--reset" in args:
        reset = True
        args.remove("--reset")
    custom_path = args[0] if args else None
    init_db(custom_path, reset=reset)
