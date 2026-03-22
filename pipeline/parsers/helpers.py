"""
Shared helpers for statement parsers — date parsing utilities.
"""

import re
from collections import Counter
from datetime import datetime
from typing import Optional


def extract_year(text: str) -> int:
    years = re.findall(r"20[2-3]\d", text)
    if years:
        return int(Counter(years).most_common(1)[0][0])
    return datetime.now().year


def extract_year_from_period(period: dict) -> Optional[int]:
    if period.get("end"):
        return int(period["end"][:4])
    if period.get("start"):
        return int(period["start"][:4])
    return None


def parse_month_day(month_abbr: str, day: int, year: int) -> str:
    """'Feb', 13, 2026 → '2026-02-13'"""
    month_map = {
        "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
        "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
    }
    month_num = month_map.get(month_abbr[:3].title(), 1)
    return f"{year}-{month_num:02d}-{day:02d}"


def parse_mmdd(mmdd: str, year: int, period_start: str = "", period_end: str = "") -> str:
    """'12/25' + 2026 → '2025-12-25' (auto-handle year-end crossover)

    year is based on statement end year.
    When statement spans year-end to year-start (e.g., 12/03/25 ~ 01/02/26),
    December transactions should use the previous year (2025).
    """
    parts = mmdd.split("/")
    month = int(parts[0])
    day = int(parts[1])

    if period_start and period_end:
        start_month = int(period_start[5:7]) if len(period_start) >= 7 else 0
        end_month = int(period_end[5:7]) if len(period_end) >= 7 else 0
        start_year = int(period_start[:4]) if len(period_start) >= 4 else year

        if start_month > end_month and month >= start_month:
            return f"{start_year}-{month:02d}-{day:02d}"

    return f"{year}-{month:02d}-{day:02d}"
