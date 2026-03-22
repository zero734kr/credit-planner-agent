"""
Spending Pattern Predictor
- Forecast future spending based on 3-6+ months of category-wise monthly spending data
- Uses weighted moving average + trend line method to capture seasonality
- Alternative: Prophet/ARIMA can be adopted once sufficient data accumulates

Usage:
  from pipeline.spending_predictor.predictor import SpendingPredictor
  predictor = SpendingPredictor(db_path)
  forecast = predictor.predict_monthly(user_id, months_ahead=6)
"""

import sqlite3
import os
from datetime import datetime
from collections import defaultdict
import numpy as np


class SpendingPredictor:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _get_monthly_spending(self, user_id: str) -> dict:
        """Retrieve monthly spending by category"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        cur.execute("""
            SELECT category,
                   strftime('%Y-%m', tx_date) as month,
                   SUM(ABS(amount)) as total
            FROM transactions
            WHERE user_id = ?
              AND category NOT IN ('income', 'card_payment', 'payment', 'uncategorized')
              AND category IS NOT NULL
            GROUP BY category, month
            ORDER BY category, month
        """, (user_id,))

        data = defaultdict(dict)
        for category, month, total in cur.fetchall():
            data[category][month] = total

        conn.close()
        return dict(data)

    def _weighted_moving_average(self, values: list, weights: list = None) -> float:
        """Weighted moving average — assigns higher weight to recent data"""
        if not values:
            return 0.0
        if weights is None:
            n = len(values)
            weights = list(range(1, n + 1))  # [1, 2, 3, ...] higher weight for recent months
        total_weight = sum(weights[-len(values):])
        weighted_sum = sum(v * w for v, w in zip(values, weights[-len(values):]))
        return weighted_sum / total_weight

    def _detect_trend(self, values: list) -> float:
        """Simple linear trend detection (monthly change rate)"""
        if len(values) < 2:
            return 0.0
        x = np.arange(len(values))
        coeffs = np.polyfit(x, values, 1)
        return coeffs[0]  # slope (change per month)

    def predict_monthly(self, user_id: str, months_ahead: int = 6) -> dict:
        """
        Forecast expected spending for next N months by category
        Returns: {category: {"monthly_avg": float, "predicted_total": float, "trend": str}}
        """
        monthly_data = self._get_monthly_spending(user_id)

        if not monthly_data:
            # If no transactions, fall back to spending_pattern table
            return self._fallback_from_spending_pattern(user_id, months_ahead)

        predictions = {}

        for category, month_totals in monthly_data.items():
            months_sorted = sorted(month_totals.keys())
            values = [month_totals[m] for m in months_sorted]

            if len(values) >= 3:
                # Sufficient data → weighted moving average + trend
                avg = self._weighted_moving_average(values)
                trend = self._detect_trend(values)
                trend_label = "increasing" if trend > 50 else "decreasing" if trend < -50 else "stable"
            else:
                # Insufficient data → simple average
                avg = sum(values) / len(values)
                trend = 0
                trend_label = "insufficient data"

            predictions[category] = {
                "monthly_avg": round(avg, 2),
                "predicted_total": round(avg * months_ahead, 2),
                "trend": trend_label,
                "monthly_change": round(trend, 2),
                "data_months": len(values),
            }

        return predictions

    def _fallback_from_spending_pattern(self, user_id: str, months_ahead: int) -> dict:
        """Retrieve from spending_pattern table (when transaction data unavailable)"""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT category, monthly_avg FROM spending_pattern WHERE user_id = ?
        """, (user_id,))

        predictions = {}
        for category, monthly_avg in cur.fetchall():
            predictions[category] = {
                "monthly_avg": monthly_avg,
                "predicted_total": monthly_avg * months_ahead,
                "trend": "user input (trend analysis unavailable)",
                "monthly_change": 0,
                "data_months": 0,
            }

        conn.close()
        return predictions

    def can_meet_minimum_spend(self, user_id: str, required_amount: float,
                                months: int, extra_monthly: float = 0) -> dict:
        """
        Assess feasibility of meeting SUB minimum spend requirement
        Args:
            required_amount: Required SUB spending amount
            months: Deadline in months
            extra_monthly: Additional monthly spend possible (e.g., from redirecting fixed expenses)
        Returns: Feasibility analysis result
        """
        predictions = self.predict_monthly(user_id, months)

        total_predicted = sum(p["monthly_avg"] for p in predictions.values())
        total_with_extra = total_predicted + extra_monthly
        projected_spend = total_with_extra * months

        gap = required_amount - projected_spend
        feasible = gap <= 0

        return {
            "required": required_amount,
            "months": months,
            "monthly_natural_spend": round(total_predicted, 2),
            "monthly_with_extra": round(total_with_extra, 2),
            "projected_total": round(projected_spend, 2),
            "gap": round(max(0, gap), 2),
            "feasible": feasible,
            "daily_needed": round(required_amount / (months * 30), 2),
            "suggestion": None if feasible else
                f"Additional ${gap/months:.0f} monthly spend required. Consider redirecting fixed expenses (insurance, utilities)"
        }


if __name__ == "__main__":
    # Demo: Forecast based on spending_pattern
    import sys
    DB_PATH = "/sessions/practical-eloquent-einstein/credit_planner.db"

    # Insert demo data
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("INSERT OR REPLACE INTO user_profile VALUES ('demo', 750, 'FICO', 24, 5, 3, 3, 75000, ?)",
                (datetime.utcnow().isoformat(),))

    spending = [
        ("demo", "groceries", 680),
        ("demo", "dining", 420),
        ("demo", "gas", 180),
        ("demo", "shopping", 350),
        ("demo", "travel", 200),
        ("demo", "utilities", 180),
        ("demo", "entertainment", 50),
    ]
    cur.executemany("INSERT OR REPLACE INTO spending_pattern VALUES (?, ?, ?)", spending)
    conn.commit()
    conn.close()

    predictor = SpendingPredictor(DB_PATH)

    print("━━━━ Spending Forecast (6 months) ━━━━")
    forecast = predictor.predict_monthly("demo", 6)
    total_monthly = 0
    for cat, pred in sorted(forecast.items(), key=lambda x: -x[1]["monthly_avg"]):
        total_monthly += pred["monthly_avg"]
        print(f"  {cat:<15} ${pred['monthly_avg']:>8,.0f}/mo  →  ${pred['predicted_total']:>10,.0f} (6mo)")
    print(f"  {'TOTAL':<15} ${total_monthly:>8,.0f}/mo  →  ${total_monthly*6:>10,.0f} (6mo)")

    print("\n━━━━ Minimum Spend Feasibility ━━━━")
    result = predictor.can_meet_minimum_spend("demo", 4000, 3)
    print(f"  SUB: $4,000 / 3 months")
    print(f"  Projected natural spend: ${result['projected_total']:,.0f}")
    print(f"  Feasible: {'✅ Sufficient' if result['feasible'] else '⚠️ Shortfall'}")
    if result['gap'] > 0:
        print(f"  Gap: ${result['gap']:,.0f}")
        print(f"  {result['suggestion']}")

    print()
    result2 = predictor.can_meet_minimum_spend("demo", 6000, 6)
    print(f"  SUB: $6,000 / 6 months")
    print(f"  Projected natural spend: ${result2['projected_total']:,.0f}")
    print(f"  Feasible: {'✅ Sufficient' if result2['feasible'] else '⚠️ Shortfall'}")
