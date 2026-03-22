"""
Database operations — transaction ingestion, loading, recurring detection, aggregation.
"""

import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from typing import Dict, List

from pipeline.category_classifier.patterns import SUBSCRIPTION_CATEGORIES, SUBSCRIPTION_KEYWORDS


def insert_transactions(
    db_path: str,
    user_id: str,
    classified_transactions: List[Dict],
) -> int:
    """Refresh imported statement sources using the latest classifications."""
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    sources = sorted({
        tx.get("source", "") for tx in classified_transactions if tx.get("source")
    })
    if sources:
        placeholders = ",".join("?" for _ in sources)
        cur.execute(
            f"DELETE FROM transactions WHERE user_id = ? AND source IN ({placeholders})",
            (user_id, *sources),
        )

    inserted = 0
    seen = set()
    for tx in classified_transactions:
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

        cur.execute("""
            DELETE FROM transactions
            WHERE user_id = ?
              AND tx_date = ?
              AND description = ?
              AND amount = ?
              AND IFNULL(card_name, '') = IFNULL(?, '')
        """, (
            user_id,
            tx.get("date"),
            tx.get("description"),
            tx.get("amount", 0),
            tx.get("card_name", ""),
        ))

        cur.execute("""
            INSERT INTO transactions (user_id, tx_date, description, amount, category, source, card_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
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


def load_all_transactions(db_path: str, user_id: str) -> List[Dict]:
    """Load ALL user transactions from DB for cumulative reporting."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT tx_date, description, amount, category, source, card_name
        FROM transactions
        WHERE user_id = ?
        ORDER BY tx_date
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "date": r["tx_date"],
            "description": r["description"],
            "amount": r["amount"],
            "category": r["category"],
            "source": r["source"],
            "card_name": r["card_name"],
        }
        for r in rows
    ]


def detect_recurring(
    classified_transactions: List[Dict],
    clean_description_fn,
) -> List[Dict]:
    """
    Recurring payment detection — filters for genuine fixed costs/subscriptions.

    Criteria:
    1. Amount consistency: within +/-10% (or absolute diff < $2)
    2. Minimum count: 3+ for general, 2+ for subscription categories
    3. Time regularity: avg gap 20-40d (monthly), 80-100d (quarterly), or 160-200d (semi-annual)
       Subscription keyword matches bypass time regularity check.
    """
    merchant_history = defaultdict(list)

    for tx in classified_transactions:
        if tx.get("tx_type") in ("income", "card_payment", "payment"):
            continue

        desc_clean = clean_description_fn(tx.get("description", ""))
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
        categories = [e["category"] for e in entries if e["category"]]
        most_common_cat = Counter(categories).most_common(1)
        category = most_common_cat[0][0] if most_common_cat else "uncategorized"

        is_subscription_cat = category in SUBSCRIPTION_CATEGORIES
        is_subscription_kw = any(kw in merchant for kw in SUBSCRIPTION_KEYWORDS)

        min_count = 2 if (is_subscription_cat or is_subscription_kw) else 3
        if len(entries) < min_count:
            continue

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

        # Time regularity (waived for subscription keywords)
        if not is_subscription_kw:
            dates_sorted = sorted(e["date"] for e in entries if e["date"])
            if len(dates_sorted) >= 2:
                try:
                    parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates_sorted]
                    gaps = [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]
                    avg_gap = sum(gaps) / len(gaps)
                    is_regular = (
                        (20 <= avg_gap <= 40) or
                        (80 <= avg_gap <= 100) or
                        (160 <= avg_gap <= 200)
                    )
                    if not is_regular:
                        continue
                    if len(gaps) >= 2 and avg_gap > 0:
                        variance = sum((g - avg_gap) ** 2 for g in gaps) / len(gaps)
                        stddev = variance ** 0.5
                        if stddev / avg_gap > 0.40:
                            continue
                except (ValueError, TypeError):
                    pass

        # Frequency determination
        dates_sorted = sorted(e["date"] for e in entries if e["date"])
        frequency_label = "monthly"
        if len(dates_sorted) >= 2:
            try:
                parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates_sorted]
                gaps = [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]
                avg_gap = sum(gaps) / len(gaps)
                if avg_gap > 100:
                    frequency_label = "semi-annual"
                elif avg_gap > 60:
                    frequency_label = "quarterly"
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

    recurring.sort(key=lambda x: -x["typical_amount"])
    return recurring


def save_recurring(db_path: str, user_id: str, recurring: List[Dict]):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("DELETE FROM recurring_transactions WHERE user_id = ?", (user_id,))

    for r in recurring:
        cur.execute("""
            INSERT OR REPLACE INTO recurring_transactions
            (user_id, merchant_pattern, typical_amount, category, frequency, last_seen)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            r["merchant"],
            r["typical_amount"],
            r["category"],
            str(r["frequency"]) + "x",
            r["dates"][-1] if r["dates"] else "",
        ))

    conn.commit()
    conn.close()


def aggregate_spending(
    db_path: str,
    user_id: str,
    transactions: List[Dict],
) -> Dict:
    """Aggregate monthly spending by category. Returns stats dict."""
    monthly = defaultdict(lambda: defaultdict(float))
    category_total = defaultdict(float)
    card_total = defaultdict(float)

    for tx in transactions:
        cat = tx.get("category") or "uncategorized"
        if cat in ("income", "card_payment", "payment"):
            continue

        month = tx.get("date", "")[:7]
        amount = abs(tx.get("amount", 0))

        monthly[month][cat] += amount
        category_total[cat] += amount
        card_total[tx.get("card_name", "Unknown")] += amount

    _update_spending_pattern(db_path, user_id, monthly)

    return {
        "monthly": dict(monthly),
        "category_total": dict(category_total),
        "card_total": dict(card_total),
    }


def _update_spending_pattern(db_path: str, user_id: str, monthly: dict):
    if not monthly:
        return

    cat_months = defaultdict(list)
    for month, cats in monthly.items():
        for cat, total in cats.items():
            cat_months[cat].append(total)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    cur.execute("DELETE FROM spending_pattern WHERE user_id = ?", (user_id,))

    for cat, totals in cat_months.items():
        avg = round(sum(totals) / len(totals))
        cur.execute("""
            INSERT INTO spending_pattern (user_id, category, monthly_avg)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, category) DO UPDATE SET monthly_avg = excluded.monthly_avg
        """, (user_id, cat, avg))

    conn.commit()
    conn.close()
