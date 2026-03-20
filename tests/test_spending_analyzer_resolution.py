"""
Focused regression checks for the pending-resolution statement-analysis flow.

This file is intentionally executable as a plain script:
  uv run tests/test_spending_analyzer_resolution.py
"""

import os
import sqlite3
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJECT_ROOT)

from db.init_db import init_db
from ml.category_classifier.classifier import TransactionClassifier
from ml.spending_analyzer import SpendingAnalyzer
from ml.statement_parser import StatementParser


passed = 0
failed = 0


def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} — {detail}")


def build_analyzer(db_path: str):
    TransactionClassifier.load_or_train = lambda self: None

    analyzer = SpendingAnalyzer(db_path=db_path, user_id="test_user")

    transactions = [
        {
            "date": "2026-01-01",
            "description": "Zelle Payment To Friend 123",
            "amount": 25.0,
            "tx_type": "purchase",
            "card_name": "Checking",
            "source": "dummy.pdf",
        },
        {
            "date": "2026-01-02",
            "description": "VENTRA ACCOUNT CHICAGO IL",
            "amount": 2.5,
            "tx_type": "purchase",
            "card_name": "Checking",
            "source": "dummy.pdf",
        },
    ]

    analyzer.parser.parse_multiple = lambda filepaths: [{"source_file": "dummy.pdf", "transactions": transactions}]
    analyzer.parser.get_all_transactions = lambda results: list(transactions)

    def fake_classify(description, amount=None, recipient=None, user_id=None):
        if "ZELLE" in description.upper():
            return {
                "category": None,
                "confidence": 0.0,
                "method": "p2p_new",
                "needs_user_input": True,
                "user_prompt": "What was this transfer for?",
                "p2p_recipient": "FRIEND",
                "previous_category": None,
            }
        return {
            "category": None,
            "confidence": 0.1,
            "method": "needs_llm",
            "needs_user_input": False,
            "user_prompt": None,
            "p2p_recipient": None,
            "previous_category": None,
            "ml_suggestion": "transportation",
            "ml_top3": [("transportation", 0.1)],
            "description_for_llm": description,
        }

    analyzer.classifier.classify = fake_classify
    analyzer.classifier.save_p2p_category = lambda *args, **kwargs: None
    analyzer.classifier.distill_from_llm = lambda *args, **kwargs: None
    return analyzer


def build_fixed_category_analyzer(db_path: str, source: str):
    TransactionClassifier.load_or_train = lambda self: None

    analyzer = SpendingAnalyzer(db_path=db_path, user_id="test_user")

    transactions = [
        {
            "date": "2026-01-01",
            "description": "Whole Foods 123",
            "amount": 10.0,
            "tx_type": "purchase",
            "card_name": "Checking",
            "source": source,
        },
    ]

    analyzer.parser.parse_multiple = lambda filepaths: [{"source_file": source, "transactions": transactions}]
    analyzer.parser.get_all_transactions = lambda results: list(transactions)
    analyzer.classifier.classify = lambda *args, **kwargs: {
        "category": "groceries",
        "confidence": 1.0,
        "method": "ml",
        "needs_user_input": False,
        "user_prompt": None,
        "p2p_recipient": None,
        "previous_category": None,
    }
    return analyzer


def main():
    print("━━━━ Spending Analyzer Resolution Tests ━━━━\n")

    with tempfile.TemporaryDirectory() as td:
        csv_path = os.path.join(td, "sample.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("date,description,amount\n2026-01-01,Test Merchant,12.34\n")

        parser = StatementParser()
        csv_results = parser.parse_multiple([csv_path])
        csv_transactions = parser.get_all_transactions(csv_results)
        test("CSV file parsed", len(csv_transactions) == 1, f"rows={csv_transactions}")
        test("CSV format detected", csv_results[0].get("format") == "csv", f"format={csv_results[0].get('format')}")

        db_path = os.path.join(td, "resolution_test.db")
        init_db(db_path)

        analyzer = build_analyzer(db_path)
        result = analyzer.run(pdf_files=["dummy.pdf"], require_resolution=True)

        test("Run returns needs_resolution", result.get("status") == "needs_resolution")
        test("P2P question count", len(result.get("p2p_questions", [])) == 1)
        test("LLM pending count", len(result.get("llm_needed", [])) == 1)

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM transactions WHERE user_id = 'test_user'")
        rows_before = cur.fetchone()[0]
        conn.close()
        test("No DB writes before resolution", rows_before == 0, f"rows={rows_before}")

        save_blocked = False
        try:
            analyzer.save_report(result, output_dir=os.path.join(td, "report"))
        except ValueError:
            save_blocked = True
        test("save_report blocks unresolved result", save_blocked)

        p2p_key = result["p2p_questions"][0]["resolution_key"]
        llm_key = result["llm_needed"][0]["resolution_key"]

        resolved = analyzer.resolve_pending(
            p2p_answers={p2p_key: "dining"},
            llm_answers={llm_key: "transportation"},
        )
        test("All pending P2P resolved", resolved["remaining_p2p"] == 0)
        test("All pending LLM items resolved", resolved["remaining_llm"] == 0)

        report = analyzer.finalize_after_resolution()
        test("Final report generated", report.get("status") != "needs_resolution")
        test("Final insert count", report.get("total_inserted") == 2, f"inserted={report.get('total_inserted')}")
        test("Dining category present", "dining" in report.get("category_summary", {}))
        test("Transportation category present", "transportation" in report.get("category_summary", {}))

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT description, category FROM transactions WHERE user_id = 'test_user' ORDER BY tx_date"
        )
        rows_after = cur.fetchall()
        conn.close()
        test(
            "Resolved categories persisted",
            rows_after == [
                ("Zelle Payment To Friend 123", "dining"),
                ("VENTRA ACCOUNT CHICAGO IL", "transportation"),
            ],
            f"rows={rows_after}",
        )

        dedup_db = os.path.join(td, "dedup_test.db")
        init_db(dedup_db)
        build_fixed_category_analyzer(dedup_db, "stmt_a.pdf").run(pdf_files=["stmt_a.pdf"], require_resolution=True)
        build_fixed_category_analyzer(dedup_db, "stmt_b.pdf").run(pdf_files=["stmt_b.pdf"], require_resolution=True)

        conn = sqlite3.connect(dedup_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), SUM(amount) FROM transactions WHERE user_id = 'test_user'")
        dedup_row = cur.fetchone()
        conn.close()
        test("Cross-source duplicate upload stays deduplicated", dedup_row == (1, 10.0), f"row={dedup_row}")

    print(f"\nResults: {passed} passed / {failed} failed / {passed + failed} total")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
