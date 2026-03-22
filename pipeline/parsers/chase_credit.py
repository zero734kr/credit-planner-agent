"""
Chase credit card statement parser (Freedom Rise, Flex, Unlimited, Sapphire, Ink)
Format: "ACCOUNT ACTIVITY" section with MM/DD Description Amount lines
"""

import os
import re
from datetime import datetime
from typing import Dict

from pipeline.parsers.helpers import extract_year, extract_year_from_period, parse_mmdd


def parse_chase_credit(text: str, filepath: str) -> Dict:
    result = {
        "card_name": "Chase Freedom Rise",
        "card_type": "credit",
        "issuer": "Chase",
        "account_last4": "",
        "statement_period": {"start": "", "end": ""},
        "format": "chase_credit",
        "transactions": [],
        "source_file": os.path.basename(filepath),
    }

    # Extract card name
    card_names = {
        "FREEDOM RISE": "Chase Freedom Rise",
        "FREEDOM FLEX": "Chase Freedom Flex",
        "FREEDOM UNLIMITED": "Chase Freedom Unlimited",
        "SAPPHIRE PREFERRED": "Chase Sapphire Preferred",
        "SAPPHIRE RESERVE": "Chase Sapphire Reserve",
        "INK BUSINESS": "Chase Ink Business",
    }
    text_upper = text.upper()
    for key, name in card_names.items():
        if key in text_upper:
            result["card_name"] = name
            break

    # Extract account number
    acct_match = re.search(r"Account\s+Number:?\s*(?:XXXX\s*){0,3}(\d{4})", text, re.I)
    if acct_match:
        result["account_last4"] = acct_match.group(1)

    # Extract statement period
    period_match = re.search(
        r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})", text,
    )
    if period_match:
        try:
            start = datetime.strptime(period_match.group(1), "%m/%d/%y")
            end = datetime.strptime(period_match.group(2), "%m/%d/%y")
            result["statement_period"]["start"] = start.strftime("%Y-%m-%d")
            result["statement_period"]["end"] = end.strftime("%Y-%m-%d")
        except ValueError:
            pass

    year = extract_year_from_period(result["statement_period"]) or extract_year(text)

    # Find ACCOUNT ACTIVITY section
    activity_match = re.search(
        r"AACCCCOOUUNNTT\s+AACCTTIIVVIITTYY|ACCOUNT\s+ACTIVITY", text, re.I,
    )
    activity_text = text[activity_match.start():] if activity_match else text

    current_section = "purchase"
    lines = activity_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        line_upper = line.upper()

        if "PAYMENT" in line_upper and ("CREDIT" in line_upper or "OTHER" in line_upper):
            current_section = "payment"
            i += 1
            continue
        elif "PURCHASE" in line_upper and len(line) < 30:
            current_section = "purchase"
            i += 1
            continue
        elif "FEE" in line_upper and "CHARGED" in line_upper:
            current_section = "fee"
            i += 1
            continue
        elif "INTEREST" in line_upper and "CHARGE" in line_upper:
            current_section = "interest"
            i += 1
            continue
        elif "TOTAL" in line_upper and ("PERIOD" in line_upper or "FEE" in line_upper):
            i += 1
            continue

        tx_match = re.match(r"(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})$", line)
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

            if current_section == "payment" and amount > 0:
                amount = -amount

            tx_type = "purchase" if current_section == "purchase" else current_section

            if any(skip in description.upper() for skip in [
                "TOTAL", "YEAR-TO-DATE", "ANNUAL", "PERCENTAGE", "BALANCE TYPE",
                "INTEREST RATE", "BILLING", "SUBJECT TO",
            ]):
                i += 1
                continue

            result["transactions"].append({
                "date": full_date,
                "description": description,
                "amount": amount,
                "tx_type": tx_type,
            })

            i += 1
            continue

        i += 1

    return result
