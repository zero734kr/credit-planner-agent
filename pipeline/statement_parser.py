"""
Statement Parser — Module that automatically extracts transactions from supported statement files

Supported formats:
  1. Capital One (Savor, etc.) — "Trans Date | Post Date | Description | Amount"
  2. Chase Credit Card (Freedom Rise, etc.) — "Date | Merchant | Amount" (PAYMENTS/PURCHASE sections)
  3. Chase Checking (College Checking, etc.) — "DATE | DESCRIPTION | AMOUNT | BALANCE"
  4. Generic CSV exports — auto-detected date/description/amount columns

Usage:
  from pipeline.statement_parser import StatementParser
  parser = StatementParser()
  transactions = parser.parse_file("/path/to/statement.pdf")
  # → [{"date": "2026-02-13", "description": "MCDONALD'S ...", "amount": 8.92, "card_name": "Capital One Savor", ...}, ...]
"""

import re
import os
from datetime import datetime
from typing import List, Dict, Optional

import pandas as pd

try:
    import pdfplumber
except ImportError:
    raise ImportError("pdfplumber required. Run `uv sync` to install project dependencies.")


class StatementParser:
    """Parser that extracts transactions from PDF statements"""

    def __init__(self):
        self.supported_formats = ["capital_one", "chase_credit", "chase_checking", "csv"]

    def parse_file(self, filepath: str) -> Dict:
        """Parse a supported statement file based on extension."""
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".csv":
            return self.parse_csv(filepath)
        return self.parse_pdf(filepath)

    def parse_pdf(self, filepath: str) -> Dict:
        """
        Parse PDF and return transaction list
        Returns:
            {
                "card_name": str,
                "card_type": "credit" | "debit",
                "issuer": str,
                "account_last4": str,
                "statement_period": {"start": str, "end": str},
                "format": str,
                "transactions": [
                    {"date": str, "description": str, "amount": float, "tx_type": "purchase"|"payment"|"fee"|"income"}
                ]
            }
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")

        with pdfplumber.open(filepath) as pdf:
            all_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    all_text.append(text)

        full_text = "\n".join(all_text)

        # Auto-detect format
        fmt = self._detect_format(full_text)

        if fmt == "capital_one":
            return self._parse_capital_one(full_text, filepath)
        elif fmt == "chase_credit":
            return self._parse_chase_credit(full_text, filepath)
        elif fmt == "chase_checking":
            return self._parse_chase_checking(full_text, filepath)
        else:
            raise ValueError(f"Unsupported PDF statement format: {filepath}")

    def parse_csv(self, filepath: str) -> Dict:
        """Parse a CSV export with auto-detected columns."""
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
        date_col = self._pick_column(
            columns,
            ["date", "transaction date", "posted date", "post date", "tx date"],
        )
        desc_col = self._pick_column(
            columns,
            ["description", "merchant", "details", "transaction", "name", "memo"],
        )
        amount_col = self._pick_column(columns, ["amount", "transaction amount", "value"])
        debit_col = self._pick_column(columns, ["debit", "withdrawal", "outflow", "charge"])
        credit_col = self._pick_column(columns, ["credit", "deposit", "inflow", "payment"])
        card_col = self._pick_column(columns, ["card", "card name", "account", "account name"])

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
                signed_amount = self._parse_csv_amount(row.get(amount_col))
            else:
                debit = self._parse_csv_amount(row.get(debit_col)) if debit_col else 0.0
                credit = self._parse_csv_amount(row.get(credit_col)) if credit_col else 0.0
                signed_amount = credit if credit else -abs(debit)

            description = str(raw_desc).strip()
            if not description:
                continue

            tx_type = self._classify_csv_tx_type(description, signed_amount)
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

    def _detect_format(self, text: str) -> str:
        """Auto-detect statement format from text content

        Order is important: Check Chase Checking first.
        Chase Checking statements may contain 'Capital One' advertisements,
        so checking Capital One first would cause false positives.
        """
        text_upper = text.upper()

        # 1. Chase Checking first (clearest signal)
        if "CHECKING SUMMARY" in text_upper or "COLLEGE CHECKING" in text_upper:
            return "chase_checking"

        # 2. Capital One — "CAPITALONE.COM" or "Capital One" in card name
        #    Note: Chase statements may also match "chase" in unexpected places,
        #    so check Capital One unique signals first
        if "CAPITALONE.COM" in text_upper or "CAPITAL ONE" in text_upper:
            # Check if Capital One card name is explicitly present
            if any(kw in text_upper for kw in [
                "SAVOR", "VENTURE", "QUICKSILVER", "SAVORONE", "VENTUREONE", "PLATINUM"
            ]) or "CAPITALONE.COM" in text_upper:
                return "capital_one"

        # 3. Chase Credit Card (only when clear Chase signals are present)
        if any(kw in text_upper for kw in [
            "FREEDOM RISE", "FREEDOM FLEX", "FREEDOM UNLIMITED",
            "SAPPHIRE PREFERRED", "SAPPHIRE RESERVE",
            "INK BUSINESS", "AACCCCOOUUNNTT AACCTTIIVVIITTYY",
            "CHASE.COM/CARDHELP", "WWW.CHASE.COM"
        ]):
            return "chase_credit"

        # fallback
        if "Trans Date" in text and "Post Date" in text:
            return "capital_one"
        elif "CHASE" in text_upper:
            return "chase_credit"
        else:
            return "unknown"

    def _pick_column(self, columns: List[str], aliases: List[str]) -> Optional[str]:
        """Find the best matching CSV column name."""
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

    def _parse_csv_amount(self, value) -> float:
        """Parse numeric amount values from common CSV exports."""
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

    def _classify_csv_tx_type(self, description: str, signed_amount: float) -> str:
        """Classify broad transaction type for CSV imports."""
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

    # ──────────────────────────────────────────────────
    # Capital One Parser
    # ──────────────────────────────────────────────────

    def _parse_capital_one(self, text: str, filepath: str) -> Dict:
        """Parse Capital One statement"""
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
        card_match = re.search(r"(Savor|Venture|Quicksilver|SavorOne|VentureOne|Platinum)\s*(One)?\s*Credit Card", text, re.I)
        if card_match:
            result["card_name"] = f"Capital One {card_match.group(0).replace('Credit Card', '').strip()}"

        # Extract account number
        acct_match = re.search(r"ending in (\d{4})", text)
        if acct_match:
            result["account_last4"] = acct_match.group(1)

        # Extract statement period
        period_match = re.search(r"(\w{3}\s+\d{1,2},?\s+\d{4})\s*-\s*(\w{3}\s+\d{1,2},?\s+\d{4})", text)
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

        # Capital One transaction: "Feb 13 Feb 14 MERCHANT NAME CITY ST $35.00"
        # or "Feb 13 Feb 16 MEXICO TACOS HOBOKENHOBOKENNJ $31.85"
        year = self._extract_year(text)

        # Capital One may have multiple transaction sections
        # Actual transactions appear below "HAJIN LIM #9521: Transactions"
        # Pattern: "Feb 13 Feb 16 MEXICO TACOS HOBOKENHOBOKENNJ $31.85"
        # City/state often appear without spaces in the description

        pattern = re.compile(
            r"(\w{3})\s+(\d{1,2})\s+\w{3}\s+\d{1,2}\s+(.+?)\$([0-9,]+\.?\d*)",
            re.M
        )

        # Track current section
        is_payment_section = False
        lines = text.split("\n")

        for line in lines:
            line = line.strip()

            # Detect section headers
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

                # Skip header-like lines
                if "Description" in description or "Amount" in description:
                    continue
                if "Total" in description:
                    continue

                try:
                    amount = float(amount_str)
                except ValueError:
                    continue

                date_str = self._parse_month_day(trans_month, int(trans_day), year)

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

    # ──────────────────────────────────────────────────
    # Chase Credit Card Parser
    # ──────────────────────────────────────────────────

    def _parse_chase_credit(self, text: str, filepath: str) -> Dict:
        """Parse Chase credit card statement (Freedom Rise, etc.)"""
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
        period_match = re.search(r"Opening/Closing Date\s+(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})", text)
        if period_match:
            try:
                start = datetime.strptime(period_match.group(1), "%m/%d/%y")
                end = datetime.strptime(period_match.group(2), "%m/%d/%y")
                result["statement_period"]["start"] = start.strftime("%Y-%m-%d")
                result["statement_period"]["end"] = end.strftime("%Y-%m-%d")
            except ValueError:
                pass

        year = self._extract_year_from_period(result["statement_period"]) or self._extract_year(text)

        # Find ACCOUNT ACTIVITY section
        activity_match = re.search(r"AACCCCOOUUNNTT\s+AACCTTIIVVIITTYY|ACCOUNT\s+ACTIVITY", text, re.I)
        if activity_match:
            activity_text = text[activity_match.start():]
        else:
            activity_text = text

        # Track current section
        current_section = "purchase"  # default

        lines = activity_text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # Detect section headers
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

            # Transaction line: "MM/DD Description Amount"
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

                # Append year to date
                full_date = self._parse_mmdd(date_str, year, result["statement_period"].get("start", ""), result["statement_period"].get("end", ""))

                # Payments/refunds are already marked as negative or are in payment section
                if current_section == "payment" and amount > 0:
                    amount = -amount

                tx_type = "purchase" if current_section == "purchase" else current_section

                # Skip non-transaction lines
                if any(skip in description.upper() for skip in [
                    "TOTAL", "YEAR-TO-DATE", "ANNUAL", "PERCENTAGE", "BALANCE TYPE",
                    "INTEREST RATE", "BILLING", "SUBJECT TO"
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

            # Foreign transaction pattern: "12/29 BRAZILIAN REAL\n7.90 X 0.181..."
            # → skip these continuation lines
            i += 1

        return result

    # ──────────────────────────────────────────────────
    # Chase Checking Parser
    # ──────────────────────────────────────────────────

    def _parse_chase_checking(self, text: str, filepath: str) -> Dict:
        """Parse Chase checking account statement"""
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

        # Extract period: "October 28, 2025throughNovember 28, 2025" (no space between)
        period_match = re.search(
            r"(\w+\s+\d{1,2},?\s+\d{4})\s*through\s*(\w+\s+\d{1,2},?\s+\d{4})", text
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

        year = self._extract_year_from_period(result["statement_period"]) or self._extract_year(text)

        # Extract TRANSACTION DETAIL section
        tx_start = text.find("TRANSACTION DETAIL")
        if tx_start == -1:
            tx_start = text.find("*start*transaction detail")
        if tx_start == -1:
            return result

        tx_text = text[tx_start:]

        # Parse each line
        lines = tx_text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # Skip header lines
            if any(skip in line for skip in [
                "TRANSACTION DETAIL", "DATE", "Beginning Balance",
                "*start*", "*end*", "DAILY ENDING", "DRE", "NNNN",
                "CUSTOMER SERVICE", "Para Espanol", "JPMorgan",
                "Columbus", "Chase.com", "Service Center",
                "International Calls", "relay calls",
            ]):
                i += 1
                continue

            # Checking transaction: "MM/DD Description Amount Balance"
            # Amount is negative (-) or positive, Balance is always positive
            tx_match = re.match(
                r"(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})\s+[\d,]+\.\d{2}$",
                line
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

                full_date = self._parse_mmdd(date_str, year, result["statement_period"].get("start", ""), result["statement_period"].get("end", ""))

                # Determine transaction type
                tx_type = self._classify_checking_tx_type(description, amount)

                # Check if next line is a continuation (Card 5839, etc.)
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if re.match(r"^(Card \d+|5839)$", next_line):
                        i += 1  # skip continuation

                result["transactions"].append({
                    "date": full_date,
                    "description": description,
                    "amount": abs(amount),  # store absolute value, distinguish by tx_type
                    "tx_type": tx_type,
                    "original_amount": amount,  # preserve original sign
                })

                i += 1
                continue

            # Continuation line with amount only (part of previous transaction)
            # "Card 5839", etc.
            if re.match(r"^(Card \d+|\d{4})$", line):
                i += 1
                continue

            # Amount without balance (some edge cases)
            tx_match2 = re.match(
                r"(\d{2}/\d{2})\s+(.+?)\s+(-?[\d,]+\.\d{2})$",
                line
            )
            if tx_match2 and len(tx_match2.group(2)) > 5:
                date_str = tx_match2.group(1)
                description = tx_match2.group(2).strip()
                amount_str = tx_match2.group(3).replace(",", "")

                # Check if next line has balance
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    balance_match = re.match(r"^(-?[\d,]+\.\d{2})$", next_line)
                    # or balance after "Card 5839"
                    if balance_match or re.match(r"^Card \d+", next_line):
                        try:
                            amount = float(amount_str)
                            full_date = self._parse_mmdd(date_str, year, result["statement_period"].get("start", ""), result["statement_period"].get("end", ""))
                            tx_type = self._classify_checking_tx_type(description, amount)
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

    def _classify_checking_tx_type(self, description: str, amount: float) -> str:
        """Classify checking account transaction type"""
        desc_upper = description.upper()
        if amount > 0 and any(kw in desc_upper for kw in [
            "DEPOSIT", "DIRECT DEP", "PAYROLL", "ZELLE PAYMENT FROM", "ACH CREDIT"
        ]):
            return "income"
        elif "PAYMENT TO" in desc_upper and "CHASE CARD" in desc_upper:
            return "card_payment"  # card payment transfer — exclude from spending analysis (avoid duplication)
        elif amount < 0:
            return "purchase"
        elif amount > 0:
            return "income"
        return "purchase"

    # ──────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────

    def _extract_year(self, text: str) -> int:
        """Extract most common year from text"""
        years = re.findall(r"20[2-3]\d", text)
        if years:
            from collections import Counter
            return int(Counter(years).most_common(1)[0][0])
        return datetime.now().year

    def _extract_year_from_period(self, period: dict) -> Optional[int]:
        """Extract year from statement period"""
        if period.get("end"):
            return int(period["end"][:4])
        if period.get("start"):
            return int(period["start"][:4])
        return None

    def _parse_month_day(self, month_abbr: str, day: int, year: int) -> str:
        """'Feb', 13, 2026 → '2026-02-13'"""
        month_map = {
            "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
            "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
        }
        month_num = month_map.get(month_abbr[:3].title(), 1)
        return f"{year}-{month_num:02d}-{day:02d}"

    def _parse_mmdd(self, mmdd: str, year: int, period_start: str = "", period_end: str = "") -> str:
        """'12/25' + 2026 → '2025-12-25' (auto-handle year-end crossover)

        year is based on statement end year.
        When statement spans year-end to year-start (e.g., 12/03/25 ~ 01/02/26),
        December transactions should use the previous year (2025).
        """
        parts = mmdd.split("/")
        month = int(parts[0])
        day = int(parts[1])

        # Handle year-end crossover
        if period_start and period_end:
            start_month = int(period_start[5:7]) if len(period_start) >= 7 else 0
            end_month = int(period_end[5:7]) if len(period_end) >= 7 else 0
            start_year = int(period_start[:4]) if len(period_start) >= 4 else year

            # If statement spans year-end→year-start and transaction is in later month, use previous year
            if start_month > end_month and month >= start_month:
                return f"{start_year}-{month:02d}-{day:02d}"
        elif month >= 10 and year > 2025:
            # Heuristic when period info missing: if transaction is Oct-Dec but year is next year, use previous year
            # (but apply conservatively as statement may start in October)
            pass

        return f"{year}-{month:02d}-{day:02d}"

    # ──────────────────────────────────────────────────
    # Batch Processing
    # ──────────────────────────────────────────────────

    def parse_multiple(self, filepaths: List[str]) -> List[Dict]:
        """Parse multiple statement files at once."""
        results = []
        for fp in filepaths:
            try:
                result = self.parse_file(fp)
                results.append(result)
                print(f"  ✓ {os.path.basename(fp)}: {len(result['transactions'])} extracted [{result['format']}]")
            except Exception as e:
                print(f"  ✗ {os.path.basename(fp)}: {e}")
                results.append({
                    "source_file": os.path.basename(fp),
                    "error": str(e),
                    "transactions": [],
                })
        return results

    def get_all_transactions(self, results: List[Dict]) -> List[Dict]:
        """Combine all transactions from parsing results into a single list"""
        all_tx = []
        for r in results:
            card_name = r.get("card_name", "Unknown")
            source = r.get("source_file", "Unknown")
            for tx in r.get("transactions", []):
                tx_copy = dict(tx)
                tx_copy["card_name"] = tx_copy.get("card_name") or card_name
                tx_copy["source"] = tx_copy.get("source") or source
                all_tx.append(tx_copy)

        # Sort by date
        all_tx.sort(key=lambda x: x.get("date", ""))
        return all_tx


if __name__ == "__main__":
    import glob

    parser = StatementParser()
    pdf_dir = "/sessions/practical-eloquent-einstein/mnt/uploads"
    pdfs = sorted(glob.glob(os.path.join(pdf_dir, "*.pdf")))

    print(f"━━━━ Statement Parser Test ━━━━")
    print(f"PDF files: {len(pdfs)}\n")

    results = parser.parse_multiple(pdfs)

    all_tx = parser.get_all_transactions(results)
    print(f"\nTotal transactions: {len(all_tx)}")

    # Count by card
    from collections import Counter
    card_counts = Counter(tx["card_name"] for tx in all_tx)
    for card, cnt in card_counts.most_common():
        print(f"  {card}: {cnt}")

    # Sample transactions
    print(f"\n━━━━ Sample Transactions (first 15) ━━━━")
    for tx in all_tx[:15]:
        amt = tx.get("original_amount", tx["amount"])
        sign = "" if amt >= 0 else "-"
        print(f"  {tx['date']}  {sign}${abs(tx['amount']):>8.2f}  {tx['tx_type']:<12}  {tx['description'][:50]}")
