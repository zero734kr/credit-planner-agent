"""
Transaction exclusion rules — loading, matching, and CRUD.

Rules are stored in the `transaction_exclusions` table and applied
at the transaction level (not category-wide).
"""

import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional


def load_exclusion_rules(db_path: str, user_id: str) -> List[Dict]:
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
        SELECT exclusion_id, rule_type, pattern, match_field, reason
        FROM transaction_exclusions
        WHERE user_id = ? AND active = 1
    """, (user_id,))

    rules = []
    for row in cur.fetchall():
        rules.append({
            "id": row[0],
            "rule_type": row[1],
            "pattern": row[2],
            "match_field": row[3],
            "reason": row[4],
        })
    conn.close()
    return rules


def apply_exclusions(
    transactions: List[Dict],
    rules: List[Dict],
) -> tuple[List[Dict], List[Dict]]:
    """Split transactions into (included, excluded) based on rules."""
    if not rules:
        return transactions, []

    included = []
    excluded = []

    for tx in transactions:
        matched_rule = match_exclusion(tx, rules)
        if matched_rule:
            tx["excluded"] = True
            tx["exclusion_reason"] = matched_rule["reason"]
            tx["exclusion_rule_id"] = matched_rule["id"]
            excluded.append(tx)
        else:
            included.append(tx)

    return included, excluded


def match_exclusion(tx: Dict, rules: List[Dict]) -> Optional[Dict]:
    for rule in rules:
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


def add_exclusion_rule(
    db_path: str, user_id: str, rule_type: str,
    pattern: str, match_field: str = "description",
    reason: str = "",
) -> int | None:
    """
    Add a transaction exclusion rule.

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


def get_exclusion_rules(db_path: str, user_id: str) -> List[Dict]:
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
