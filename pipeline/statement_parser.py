"""
Statement Parser — Extracts transactions from supported statement files.

Supported formats (auto-detected):
  1. Capital One (Savor, etc.)
  2. Chase Credit Card (Freedom Rise, etc.)
  3. Chase Checking (College Checking, etc.)
  4. Generic CSV exports

Usage:
  from pipeline.statement_parser import StatementParser
  parser = StatementParser()
  results = parser.parse_multiple(["stmt1.pdf", "stmt2.pdf"])
  all_txs = parser.get_all_transactions(results)
"""

import os
import re
from typing import List, Dict

try:
    import pdfplumber
except ImportError:
    raise ImportError("pdfplumber required. Run `uv sync` to install project dependencies.")

from pipeline.parsers.capital_one import parse_capital_one
from pipeline.parsers.chase_credit import parse_chase_credit
from pipeline.parsers.chase_checking import parse_chase_checking
from pipeline.parsers.csv_parser import parse_csv


class StatementParser:
    """Dispatcher that routes statement files to format-specific parsers."""

    def __init__(self):
        self.supported_formats = ["capital_one", "chase_credit", "chase_checking", "csv"]

    def parse_file(self, filepath: str) -> Dict:
        ext = os.path.splitext(filepath)[1].lower()
        if ext == ".csv":
            return parse_csv(filepath)
        return self.parse_pdf(filepath)

    def parse_pdf(self, filepath: str) -> Dict:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"File not found: {filepath}")

        with pdfplumber.open(filepath) as pdf:
            all_text = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    all_text.append(text)

        full_text = "\n".join(all_text)
        fmt = _detect_format(full_text)

        if fmt == "capital_one":
            return parse_capital_one(full_text, filepath)
        elif fmt == "chase_credit":
            return parse_chase_credit(full_text, filepath)
        elif fmt == "chase_checking":
            return parse_chase_checking(full_text, filepath)
        else:
            raise ValueError(f"Unsupported PDF statement format: {filepath}")

    def parse_multiple(self, filepaths: List[str]) -> List[Dict]:
        results = []
        for fp in filepaths:
            try:
                result = self.parse_file(fp)
                results.append(result)
                print(f"  \u2713 {os.path.basename(fp)}: {len(result['transactions'])} extracted [{result['format']}]")
            except Exception as e:
                print(f"  \u2717 {os.path.basename(fp)}: {e}")
                results.append({
                    "source_file": os.path.basename(fp),
                    "error": str(e),
                    "transactions": [],
                })
        return results

    def get_all_transactions(self, results: List[Dict]) -> List[Dict]:
        all_tx = []
        for r in results:
            card_name = r.get("card_name", "Unknown")
            source = r.get("source_file", "Unknown")
            for tx in r.get("transactions", []):
                tx_copy = dict(tx)
                tx_copy["card_name"] = tx_copy.get("card_name") or card_name
                tx_copy["source"] = tx_copy.get("source") or source
                all_tx.append(tx_copy)
        all_tx.sort(key=lambda x: x.get("date", ""))
        return all_tx


def _detect_format(text: str) -> str:
    """Auto-detect statement format from text content.

    Order is important: Check Chase Checking first.
    Chase Checking statements may contain 'Capital One' advertisements,
    so checking Capital One first would cause false positives.
    """
    text_upper = text.upper()

    # 1. Chase Checking first (clearest signal)
    if "CHECKING SUMMARY" in text_upper or "COLLEGE CHECKING" in text_upper:
        return "chase_checking"

    # 2. Capital One
    if "CAPITALONE.COM" in text_upper or "CAPITAL ONE" in text_upper:
        if any(kw in text_upper for kw in [
            "SAVOR", "VENTURE", "QUICKSILVER", "SAVORONE", "VENTUREONE", "PLATINUM",
        ]) or "CAPITALONE.COM" in text_upper:
            return "capital_one"

    # 3. Chase Credit Card
    if any(kw in text_upper for kw in [
        "FREEDOM RISE", "FREEDOM FLEX", "FREEDOM UNLIMITED",
        "SAPPHIRE PREFERRED", "SAPPHIRE RESERVE",
        "INK BUSINESS", "AACCCCOOUUNNTT AACCTTIIVVIITTYY",
        "CHASE.COM/CARDHELP", "WWW.CHASE.COM",
    ]):
        return "chase_credit"

    # Fallback
    if "Trans Date" in text and "Post Date" in text:
        return "capital_one"
    elif "CHASE" in text_upper:
        return "chase_credit"
    return "unknown"
