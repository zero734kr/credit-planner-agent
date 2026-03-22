"""
Generic CSV statement parser — auto-detects date/description/amount columns.
Handles debit/credit split columns and signed amounts.
"""

import os
import re
from typing import Dict, List, Optional

import pandas as pd


def parse_csv(filepath: str) -> Dict:
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File not found: {filepath}")

    df = pd.read_csv(filepath)
    if df.empty:
        return {
            "card_name": os.path.splitext(os.path.basename(filepath))[0],
            "card_type": "unknown",
            "issuer": "CSV Import",
            "account_last4": "",
            "statement_period": {"start": "", "end": ""},
            "format": "csv",
            "transactions": [],
            "source_file": os.path.basename(filepath),
        }

    columns = list(df.columns)
    date_col = _pick_column(columns, ["date", "transaction date", "posted date", "post date", "tx date"])
    desc_col = _pick_column(columns, ["description", "merchant", "details", "transaction", "name", "memo"])
    amount_col = _pick_column(columns, ["amount", "transaction amount", "value"])
    debit_col = _pick_column(columns, ["debit", "withdrawal", "outflow", "charge"])
    credit_col = _pick_column(columns, ["credit", "deposit", "inflow", "payment"])
    card_col = _pick_column(columns, ["card", "card name", "account", "account name"])

    if not date_col or not desc_col or (not amount_col and not (debit_col or credit_col)):
        raise ValueError(
            "CSV missing required columns. Need date + description + amount, "
            "or date + description + debit/credit."
        )

    result = {
        "card_name": os.path.splitext(os.path.basename(filepath))[0],
        "card_type": "unknown",
        "issuer": "CSV Import",
        "account_last4": "",
        "statement_period": {"start": "", "end": ""},
        "format": "csv",
        "transactions": [],
        "source_file": os.path.basename(filepath),
    }

    dates_seen = []
    for _, row in df.iterrows():
        raw_date = row.get(date_col)
        raw_desc = row.get(desc_col)
        if pd.isna(raw_date) or pd.isna(raw_desc):
            continue

        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        if pd.isna(parsed_date):
            continue

        if amount_col:
            signed_amount = _parse_csv_amount(row.get(amount_col))
        else:
            debit = _parse_csv_amount(row.get(debit_col)) if debit_col else 0.0
            credit = _parse_csv_amount(row.get(credit_col)) if credit_col else 0.0
            signed_amount = credit if credit else -abs(debit)

        description = str(raw_desc).strip()
        if not description:
            continue

        tx_type = _classify_csv_tx_type(description, signed_amount)
        card_name = (
            str(row.get(card_col)).strip()
            if card_col and not pd.isna(row.get(card_col))
            else result["card_name"]
        )

        tx_date = parsed_date.strftime("%Y-%m-%d")
        dates_seen.append(tx_date)
        result["transactions"].append({
            "date": tx_date,
            "description": description,
            "amount": abs(signed_amount),
            "tx_type": tx_type,
            "original_amount": signed_amount,
            "card_name": card_name,
        })

    if dates_seen:
        result["statement_period"]["start"] = min(dates_seen)
        result["statement_period"]["end"] = max(dates_seen)

    return result


def _pick_column(columns: List[str], aliases: List[str]) -> Optional[str]:
    normalized = {col: re.sub(r"[^a-z0-9]+", " ", str(col).strip().lower()).strip() for col in columns}
    for alias in aliases:
        for original, cleaned in normalized.items():
            if cleaned == alias:
                return original
    for alias in aliases:
        for original, cleaned in normalized.items():
            if alias in cleaned:
                return original
    return None


def _parse_csv_amount(value) -> float:
    if value is None or pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return 0.0

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    text = text.replace("$", "").replace(",", "").strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1]

    try:
        amount = float(text)
    except ValueError:
        return 0.0

    return -abs(amount) if negative or amount < 0 else amount


def _classify_csv_tx_type(description: str, signed_amount: float) -> str:
    desc_upper = description.upper()
    income_keywords = [
        "PAYROLL", "DIRECT DEP", "SALARY", "ACH CREDIT", "DEPOSIT", "REFUND",
        "INTEREST", "STATEMENT CREDIT", "CASHBACK REWARD",
    ]
    payment_keywords = [
        "PAYMENT THANK YOU", "AUTOPAY PAYMENT", "PAYMENT TO CHASE CARD",
        "PAYMENT TO", "ONLINE PAYMENT",
    ]

    if any(keyword in desc_upper for keyword in payment_keywords):
        return "card_payment"
    if any(keyword in desc_upper for keyword in income_keywords):
        return "income"
    if signed_amount > 0 and any(keyword in desc_upper for keyword in ["ZELLE FROM", "VENMO CASHOUT"]):
        return "income"
    return "purchase"
