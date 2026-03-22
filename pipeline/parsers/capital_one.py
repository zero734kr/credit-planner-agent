"""
Capital One statement parser (Savor, Venture, Quicksilver, etc.)
Format: "Trans Date | Post Date | Description | Amount"
"""

import os
import re
from datetime import datetime
from typing import Dict

from pipeline.parsers.helpers import parse_month_day, extract_year


def parse_capital_one(text: str, filepath: str) -> Dict:
    result = {
        "card_name": "Capital One Savor",
        "card_type": "credit",
        "issuer": "Capital One",
        "account_last4": "",
        "statement_period": {"start": "", "end": ""},
        "format": "capital_one",
        "transactions": [],
        "source_file": os.path.basename(filepath),
    }

    # Extract card name
    card_match = re.search(
        r"(Savor|Venture|Quicksilver|SavorOne|VentureOne|Platinum)\s*(One)?\s*Credit Card",
        text, re.I,
    )
    if card_match:
        result["card_name"] = f"Capital One {card_match.group(0).replace('Credit Card', '').strip()}"

    # Extract account number
    acct_match = re.search(r"ending in (\d{4})", text)
    if acct_match:
        result["account_last4"] = acct_match.group(1)

    # Extract statement period
    period_match = re.search(
        r"(\w{3}\s+\d{1,2},?\s+\d{4})\s*-\s*(\w{3}\s+\d{1,2},?\s+\d{4})", text,
    )
    if period_match:
        try:
            start_str = period_match.group(1).replace(",", "")
            end_str = period_match.group(2).replace(",", "")
            start = datetime.strptime(start_str, "%b %d %Y")
            end = datetime.strptime(end_str, "%b %d %Y")
            result["statement_period"]["start"] = start.strftime("%Y-%m-%d")
            result["statement_period"]["end"] = end.strftime("%Y-%m-%d")
        except ValueError:
            pass

    year = extract_year(text)

    pattern = re.compile(
        r"(\w{3})\s+(\d{1,2})\s+\w{3}\s+\d{1,2}\s+(.+?)\$([0-9,]+\.?\d*)",
        re.M,
    )

    is_payment_section = False
    lines = text.split("\n")

    for line in lines:
        line = line.strip()

        if "Payments, Credits" in line or "Payments,Credits" in line:
            is_payment_section = True
            continue
        elif "Transactions" in line and "Total" not in line and "#" in line:
            is_payment_section = False
            continue

        m = pattern.match(line)
        if m:
            trans_month = m.group(1)
            trans_day = m.group(2)
            description = m.group(3).strip()
            amount_str = m.group(4).replace(",", "")

            if "Description" in description or "Amount" in description:
                continue
            if "Total" in description:
                continue

            try:
                amount = float(amount_str)
            except ValueError:
                continue

            date_str = parse_month_day(trans_month, int(trans_day), year)

            if is_payment_section:
                result["transactions"].append({
                    "date": date_str,
                    "description": description,
                    "amount": -amount,
                    "tx_type": "payment",
                })
            else:
                result["transactions"].append({
                    "date": date_str,
                    "description": description,
                    "amount": amount,
                    "tx_type": "purchase",
                })

    return result
