"""
Chase checking account statement parser (College Checking, etc.)
Format: "TRANSACTION DETAIL" section with MM/DD Description Amount Balance lines
"""

import os
import re
from datetime import datetime
from typing import Dict

from pipeline.parsers.helpers import extract_year, extract_year_from_period, parse_mmdd


def parse_chase_checking(text: str, filepath: str) -> Dict:
    result = {
        "card_name": "Chase College Checking",
        "card_type": "debit",
        "issuer": "Chase",
        "account_last4": "",
        "statement_period": {"start": "", "end": ""},
        "format": "chase_checking",
        "transactions": [],
        "source_file": os.path.basename(filepath),
    }

    # Extract period: "October 28, 2025throughNovember 28, 2025"
    period_match = re.search(
        r"(\w+\s+\d{1,2},?\s+\d{4})\s*through\s*(\w+\s+\d{1,2},?\s+\d{4})", text,
    )
    if period_match:
        try:
            start_str = period_match.group(1).replace(",", "")
            end_str = period_match.group(2).replace(",", "")
            start = datetime.strptime(start_str, "%B %d %Y")
            end = datetime.strptime(end_str, "%B %d %Y")
            result["statement_period"]["start"] = start.strftime("%Y-%m-%d")
            result["statement_period"]["end"] = end.strftime("%Y-%m-%d")
        except ValueError:
            pass

    year = extract_year_from_period(result["statement_period"]) or extract_year(text)

    # Find TRANSACTION DETAIL section
    tx_start = text.find("TRANSACTION DETAIL")
    if tx_start == -1:
        tx_start = text.find("*start*transaction detail")
    if tx_start == -1:
        return result

    tx_text = text[tx_start:]
    lines = tx_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if any(skip in line for skip in [
            "TRANSACTION DETAIL", "DATE", "Beginning Balance",
            "*start*", "*end*", "DAILY ENDING", "DRE", "NNNN",
            "CUSTOMER SERVICE", "Para Espanol", "JPMorgan",
            "Columbus", "Chase.com", "Service Center",
            "International Calls", "relay calls",
        ]):
            i += 1
            continue

        # Primary pattern: MM/DD Description Amount Balance
        tx_match = re.match(
            r"(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})\s+[\d,]+\.\d{2}$",
            line,
        )
        if tx_match:
            date_str = tx_match.group(1)
            description = tx_match.group(2).strip()
            amount_str = tx_match.group(3).replace(",", "")

            try:
                amount = float(amount_str)
            except ValueError:
                i += 1
                continue

            full_date = parse_mmdd(
                date_str, year,
                result["statement_period"].get("start", ""),
                result["statement_period"].get("end", ""),
            )
            tx_type = _classify_checking_tx_type(description, amount)

            # Skip continuation lines like "Card 5839"
            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                if re.match(r"^(Card \d+|5839)$", next_line):
                    i += 1

            result["transactions"].append({
                "date": full_date,
                "description": description,
                "amount": abs(amount),
                "tx_type": tx_type,
                "original_amount": amount,
            })

            i += 1
            continue

        # Skip card continuation lines
        if re.match(r"^(Card \d+|\d{4})$", line):
            i += 1
            continue

        # Fallback: MM/DD Description Amount (no balance column)
        tx_match2 = re.match(
            r"(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})$",
            line,
        )
        if tx_match2 and len(tx_match2.group(2)) > 5:
            date_str = tx_match2.group(1)
            description = tx_match2.group(2).strip()
            amount_str = tx_match2.group(3).replace(",", "")

            if i + 1 < len(lines):
                next_line = lines[i + 1].strip()
                balance_match = re.match(r"^(-?[\d,]+\.\d{2})$", next_line)
                if balance_match or re.match(r"^Card \d+", next_line):
                    try:
                        amount = float(amount_str)
                        full_date = parse_mmdd(
                            date_str, year,
                            result["statement_period"].get("start", ""),
                            result["statement_period"].get("end", ""),
                        )
                        tx_type = _classify_checking_tx_type(description, amount)
                        result["transactions"].append({
                            "date": full_date,
                            "description": description,
                            "amount": abs(amount),
                            "tx_type": tx_type,
                            "original_amount": amount,
                        })
                    except ValueError:
                        pass

        i += 1

    return result


def _classify_checking_tx_type(description: str, amount: float) -> str:
    desc_upper = description.upper()
    if amount > 0 and any(kw in desc_upper for kw in [
        "DEPOSIT", "DIRECT DEP", "PAYROLL", "ZELLE PAYMENT FROM", "ACH CREDIT",
    ]):
        return "income"
    elif "PAYMENT TO" in desc_upper and "CHASE CARD" in desc_upper:
        return "card_payment"
    elif amount < 0:
        return "purchase"
    elif amount > 0:
        return "income"
    return "purchase"
