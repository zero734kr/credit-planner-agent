"""
Report generation and file saving — comprehensive + monthly detail reports.
"""

import os
import re
from collections import Counter
from datetime import datetime
from typing import Dict, List


def generate_report(
    report_txs: List[Dict],
    classified_transactions: List[Dict],
    excluded_transactions: List[Dict],
    all_transactions: List[Dict],
    p2p_questions: List[Dict],
    llm_needed: List[Dict],
    stats: Dict,
    clean_description_fn,
    recurring: List[Dict],
) -> Dict:
    """Generate analysis report from cumulative DB data."""
    dates = [tx.get("date", "") for tx in report_txs if tx.get("date")]
    dates = [d for d in dates if d]
    min_date = min(dates) if dates else "?"
    max_date = max(dates) if dates else "?"

    spend_txs = [
        tx for tx in report_txs
        if tx.get("category") not in ("income", "card_payment", "payment", None)
    ]

    total_spend = sum(abs(tx.get("amount", 0)) for tx in spend_txs)
    income_txs = [tx for tx in report_txs if tx.get("category") == "income"]
    total_income = sum(abs(tx.get("amount", 0)) for tx in income_txs)

    cat_summary = stats.get("category_total", {})
    total_months = len(stats.get("monthly", {})) or 1
    cat_monthly_avg = {cat: round(total / total_months, 2) for cat, total in cat_summary.items()}

    max_val = max(cat_monthly_avg.values()) if cat_monthly_avg else 1
    bar_width = 24

    report_lines = []
    report_lines.append("━━━━ Spending Analysis Report ━━━━")
    report_lines.append(f"Period: {min_date} ~ {max_date} ({total_months} months)")
    report_lines.append(
        f"Total txns: {len(report_txs)} | "
        f"Spending: {len(spend_txs)} (${total_spend:,.0f}) | "
        f"Income: {len(income_txs)} (${total_income:,.0f})"
    )

    if excluded_transactions:
        excluded_total = sum(abs(tx.get("amount", 0)) for tx in excluded_transactions)
        report_lines.append(f"Excluded: {len(excluded_transactions)} txns (${excluded_total:,.0f})")
        reason_counts = Counter(tx.get("exclusion_reason", "other") for tx in excluded_transactions)
        for reason, count in reason_counts.most_common():
            reason_amount = sum(
                abs(tx.get("amount", 0)) for tx in excluded_transactions
                if tx.get("exclusion_reason") == reason
            )
            report_lines.append(f"  → {reason}: {count} txn(s) (${reason_amount:,.0f})")

    report_lines.append("")

    report_lines.append("Spending by Card:")
    for card, total in sorted(stats.get("card_total", {}).items(), key=lambda x: -x[1]):
        report_lines.append(f"  {card:<30} ${total:>10,.2f}")
    report_lines.append("")

    report_lines.append("Monthly Average by Category:")
    sorted_cats = sorted(
        ((cat, avg) for cat, avg in cat_monthly_avg.items() if cat and avg is not None),
        key=lambda x: -x[1],
    )
    total_cat_avg = sum(avg for _, avg in sorted_cats) if sorted_cats else 1
    for cat, avg in sorted_cats:
        pct = (avg / total_cat_avg) * 100 if total_cat_avg > 0 else 0
        filled = int((avg / max_val) * bar_width) if max_val > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (bar_width - filled)
        report_lines.append(f"  {cat:<15} ${avg:>8,.0f}  {bar}  {pct:.0f}%")
    report_lines.append("")

    monthly_data = stats.get("monthly", {})
    if monthly_data:
        report_lines.append("━━━━ Monthly Detail ━━━━")
        for month in sorted(monthly_data.keys()):
            cats = monthly_data[month]
            month_total = sum(cats.values())
            report_lines.append(f"\n  {month}  (total ${month_total:,.0f})")
            for cat, amount in sorted(cats.items(), key=lambda x: -x[1]):
                if amount > 0 and cat is not None:
                    pct = (amount / month_total) * 100 if month_total > 0 else 0
                    report_lines.append(f"    {cat:<15} ${amount:>8,.0f}  ({pct:.0f}%)")
        report_lines.append("")

    if recurring:
        report_lines.append("Recurring Payments (Fixed Costs / Subscriptions):")
        for r in recurring[:15]:
            freq = r.get("frequency_label", "monthly")
            report_lines.append(
                f"  {r['merchant']:<30} ${r['typical_amount']:>8.2f}/{freq:<10} "
                f"({r['category']})"
            )
        report_lines.append("")

    if p2p_questions:
        report_lines.append(f"P2P transfers: {len(p2p_questions)} — user confirmation required:")
        for q in p2p_questions[:10]:
            report_lines.append(f"  → {q['prompt']}")
        report_lines.append("")

    if llm_needed:
        report_lines.append(f"LLM classification needed: {len(llm_needed)}")
        for tx in llm_needed[:10]:
            desc = clean_description_fn(tx.get("description", ""))
            report_lines.append(f"  → {desc[:60]} (${tx.get('amount', 0):.2f})")
        report_lines.append("")

    method_counts = Counter(tx.get("classify_method", "?") for tx in classified_transactions)
    report_lines.append("Classification Method Stats:")
    for method, count in method_counts.most_common():
        report_lines.append(f"  {method:<20} {count:>4}")
    report_lines.append("")
    report_lines.append("━" * 50)

    report_text = "\n".join(report_lines)

    return {
        "total_parsed": len(all_transactions),
        "total_classified": len(classified_transactions),
        "total_cumulative": len(report_txs),
        "total_excluded": len(excluded_transactions),
        "total_spend": total_spend,
        "total_income": total_income,
        "period": {"start": min_date, "end": max_date, "months": total_months},
        "p2p_questions": p2p_questions,
        "llm_needed": llm_needed,
        "excluded_transactions": excluded_transactions,
        "category_summary": cat_summary,
        "category_monthly_avg": cat_monthly_avg,
        "monthly_summary": stats.get("monthly", {}),
        "recurring": recurring,
        "card_breakdown": stats.get("card_total", {}),
        "classification_methods": dict(method_counts),
        "report_text": report_text,
    }


def save_report(
    report: Dict,
    classified_transactions: List[Dict],
    excluded_transactions: List[Dict],
    clean_description_fn,
    output_dir: str,
) -> List[str]:
    """
    Save report to files.

    Output files:
      - report/spending_analysis_YYYYMMDD.md  (comprehensive report)
      - report/monthly/YYYY-MM.md             (monthly detail report)
    """
    if report.get("status") == "needs_resolution":
        raise ValueError("Cannot save final report while classifications still need resolution.")

    os.makedirs(output_dir, exist_ok=True)
    saved_files = []

    date_str = datetime.now().strftime("%Y%m%d")
    filepath = os.path.join(output_dir, f"spending_analysis_{date_str}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(report.get("report_text", ""))
    saved_files.append(filepath)

    monthly_data = report.get("monthly_summary", {})
    if monthly_data:
        monthly_dir = os.path.join(output_dir, "monthly")
        os.makedirs(monthly_dir, exist_ok=True)

        for month in sorted(monthly_data.keys()):
            cats = monthly_data[month]
            month_total = sum(cats.values())
            if month_total == 0:
                continue

            month_lines = _build_monthly_report(
                month, cats, month_total,
                classified_transactions, excluded_transactions,
                clean_description_fn,
            )

            month_filepath = os.path.join(monthly_dir, f"{month}.md")
            with open(month_filepath, "w", encoding="utf-8") as f:
                f.write("\n".join(month_lines))
            saved_files.append(month_filepath)

    return saved_files


def _build_monthly_report(
    month: str,
    cats: Dict,
    month_total: float,
    classified_transactions: List[Dict],
    excluded_transactions: List[Dict],
    clean_description_fn,
) -> List[str]:
    max_cat = max(cats.values()) if cats else 1
    lines = []
    lines.append(f"# {month} Spending Analysis")
    lines.append("")
    lines.append(f"Total Spending: ${month_total:,.0f}")
    lines.append("")

    lines.append("## Spending by Category")
    lines.append("")
    for cat, amount in sorted(cats.items(), key=lambda x: -x[1]):
        if amount > 0 and cat is not None:
            pct = (amount / month_total) * 100 if month_total > 0 else 0
            filled = int((amount / max_cat) * 20) if max_cat > 0 else 0
            bar = "\u2588" * filled + "\u2591" * (20 - filled)
            lines.append(f"  {cat:<15} ${amount:>8,.0f}  {bar}  {pct:.0f}%")
    lines.append("")

    month_txs = [
        tx for tx in classified_transactions
        if tx.get("date", "")[:7] == month
        and tx.get("category") not in ("income", "card_payment", "payment", None)
    ]
    month_txs.sort(key=lambda x: x.get("date", ""))

    if month_txs:
        lines.append("## Transaction Detail")
        lines.append("")
        lines.append(f"{'Date':<12} {'Category':<15} {'Amount':>10}  Description")
        lines.append("-" * 70)
        for tx in month_txs:
            desc = clean_description_fn(tx.get("description", ""))[:40]
            lines.append(
                f"{tx.get('date', '?'):<12} "
                f"{tx.get('category', '?'):<15} "
                f"${abs(tx.get('amount', 0)):>9,.2f}  "
                f"{desc}"
            )
        lines.append("")

    excluded_month = [tx for tx in excluded_transactions if tx.get("date", "")[:7] == month]
    if excluded_month:
        excluded_total = sum(abs(tx.get("amount", 0)) for tx in excluded_month)
        lines.append(f"## Excluded Transactions ({len(excluded_month)} txns, ${excluded_total:,.0f})")
        lines.append("")
        for tx in excluded_month:
            desc = clean_description_fn(tx.get("description", ""))[:40]
            lines.append(
                f"  {tx.get('date', '?'):<12} "
                f"${abs(tx.get('amount', 0)):>9,.2f}  "
                f"{desc}  [{tx.get('exclusion_reason', '')}]"
            )
        lines.append("")

    return lines
