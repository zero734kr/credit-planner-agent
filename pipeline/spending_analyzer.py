"""
Spending Analyzer — Orchestrator for the spending analysis pipeline.

Pipeline:
  1. Extract transactions from PDF/CSV via StatementParser
  2. Classify categories via TransactionClassifier (5-layer pipeline)
  3. Apply user exclusion rules
  4. Resolve pending P2P/LLM classifications
  5. Ingest transactions into SQLite
  6. Detect recurring transactions
  7. Aggregate spending + generate report

Usage:
  from pipeline.spending_analyzer import SpendingAnalyzer
  analyzer = SpendingAnalyzer(db_path, user_id="user001")
  report = analyzer.run(pdf_files=[...])
"""

import os
import re
from typing import Callable, Dict, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from pipeline.category_classifier.classifier import TransactionClassifier
from pipeline.category_classifier.patterns import ALL_CATEGORIES, PAYMENT_PATTERNS  # noqa: F401
from pipeline.statement_parser import StatementParser

from pipeline import db_writer
from pipeline import exclusions
from pipeline import preferences
from pipeline import report_writer
from pipeline import resolution


class SpendingAnalyzer:
    """Integrated spending analysis pipeline."""

    def __init__(self, db_path: str, user_id: str = "default"):
        self.db_path = db_path
        self.user_id = user_id
        self.parser = StatementParser()
        self.classifier = TransactionClassifier(db_path=db_path)

        # Analysis state
        self.parsed_results: List[Dict] = []
        self.all_transactions: List[Dict] = []
        self.classified_transactions: List[Dict] = []
        self.excluded_transactions: List[Dict] = []
        self.p2p_questions: List[Dict] = []
        self.llm_needed: List[Dict] = []
        self.stats: Dict = {}
        self.exclusion_rules: List[Dict] = []

    def run(
        self,
        pdf_files: List[str] | None = None,
        csv_files: List[str] | None = None,
        llm_resolver: Callable[[Dict], Optional[str]] | None = None,
        p2p_resolver: Callable[[Dict], Optional[str]] | None = None,
        require_resolution: bool = False,
    ) -> Dict:
        files = (pdf_files or []) + (csv_files or [])
        if not files:
            return {"error": "No files provided."}

        print(f"\n{'━' * 60}")
        print(f"  Spending Analysis Pipeline Started")
        print(f"  User: {self.user_id} | Files: {len(files)}")
        print(f"{'━' * 60}\n")

        # Step 0: Load exclusion rules
        self.exclusion_rules = exclusions.load_exclusion_rules(self.db_path, self.user_id)
        if self.exclusion_rules:
            print(f"[0/7] Loaded {len(self.exclusion_rules)} user exclusion rules")
            for rule in self.exclusion_rules:
                print(f"      → {rule['rule_type']}: \"{rule['pattern']}\" ({rule['reason']})")
            print()

        # Step 1: Parse
        print("[1/7] Parsing PDFs...")
        self.parsed_results = self.parser.parse_multiple(files)
        self.all_transactions = self.parser.get_all_transactions(self.parsed_results)
        print(f"      → {len(self.all_transactions)} transactions extracted\n")

        # Step 2: Classify
        print("[2/7] Classifying transactions...")
        self._classify_all()
        print(f"      → Classification complete (P2P questions: {len(self.p2p_questions)}, LLM needed: {len(self.llm_needed)})\n")

        # Step 3: Apply exclusions
        print("[3/7] Applying user exclusion rules...")
        self.classified_transactions, self.excluded_transactions = exclusions.apply_exclusions(
            self.classified_transactions, self.exclusion_rules,
        )
        print(f"      → {len(self.excluded_transactions)} transactions excluded\n")

        # Step 4: Resolve pending
        print("[4/7] Resolving pending classifications...")
        self.resolve_pending(llm_resolver=llm_resolver, p2p_resolver=p2p_resolver)
        pending = self.get_pending_resolutions()
        pending_count = len(pending["p2p_questions"]) + len(pending["llm_needed"])
        print(f"      → {pending_count} unresolved after resolver pass\n")

        if require_resolution and pending_count:
            print("[stop] Resolution required before DB ingestion/report generation\n")
            return resolution.build_pending_result(
                self.all_transactions, self.classified_transactions,
                self.excluded_transactions, self.p2p_questions, self.llm_needed,
            )

        return self.finalize_after_resolution()

    def get_pending_resolutions(self) -> Dict[str, List[Dict]]:
        return resolution.get_pending_resolutions(self.p2p_questions, self.llm_needed)

    def resolve_pending(
        self,
        p2p_answers: Dict[str, str] | None = None,
        llm_answers: Dict[str, str] | None = None,
        llm_resolver: Callable[[Dict], Optional[str]] | None = None,
        p2p_resolver: Callable[[Dict], Optional[str]] | None = None,
    ) -> Dict[str, int]:
        remaining_p2p, remaining_llm, updated_classified, stats = resolution.resolve_pending(
            p2p_questions=self.p2p_questions,
            llm_needed=self.llm_needed,
            classified_transactions=self.classified_transactions,
            classifier=self.classifier,
            user_id=self.user_id,
            clean_description_fn=self._clean_description,
            p2p_answers=p2p_answers,
            llm_answers=llm_answers,
            llm_resolver=llm_resolver,
            p2p_resolver=p2p_resolver,
        )
        self.p2p_questions = remaining_p2p
        self.llm_needed = remaining_llm
        self.classified_transactions = updated_classified
        return stats

    def finalize_after_resolution(self) -> Dict:
        pending = self.get_pending_resolutions()
        if pending["p2p_questions"] or pending["llm_needed"]:
            return resolution.build_pending_result(
                self.all_transactions, self.classified_transactions,
                self.excluded_transactions, self.p2p_questions, self.llm_needed,
            )

        # Step 5: DB ingestion
        print("[5/7] Ingesting into SQLite...")
        inserted = db_writer.insert_transactions(
            self.db_path, self.user_id, self.classified_transactions,
        )
        print(f"      → {inserted} rows inserted (duplicates excluded)\n")

        # Step 6: Recurring detection
        print("[6/7] Detecting recurring payments...")
        recurring = db_writer.detect_recurring(
            self.classified_transactions, self._clean_description,
        )
        db_writer.save_recurring(self.db_path, self.user_id, recurring)
        print(f"      → {len(recurring)} recurring items detected\n")

        # Step 7: Aggregate + Report
        print("[7/7] Generating analysis report (cumulative)...")
        all_db_txs = db_writer.load_all_transactions(self.db_path, self.user_id)
        self.stats = db_writer.aggregate_spending(self.db_path, self.user_id, all_db_txs)

        report = report_writer.generate_report(
            report_txs=all_db_txs,
            classified_transactions=self.classified_transactions,
            excluded_transactions=self.excluded_transactions,
            all_transactions=self.all_transactions,
            p2p_questions=self.p2p_questions,
            llm_needed=self.llm_needed,
            stats=self.stats,
            clean_description_fn=self._clean_description,
            recurring=recurring,
        )
        report["total_inserted"] = inserted
        print(f"      → Report covers {len(all_db_txs)} cumulative transactions\n")

        return report

    def save_report(self, report: Dict, output_dir: str | None = None) -> List[str]:
        if output_dir is None:
            output_dir = os.path.join(BASE_DIR, "report")
        return report_writer.save_report(
            report=report,
            classified_transactions=self.classified_transactions,
            excluded_transactions=self.excluded_transactions,
            clean_description_fn=self._clean_description,
            output_dir=output_dir,
        )

    # ─── Static API (backwards-compatible) ───

    @staticmethod
    def add_exclusion_rule(db_path: str, user_id: str, rule_type: str,
                           pattern: str, match_field: str = "description",
                           reason: str = "") -> int | None:
        return exclusions.add_exclusion_rule(db_path, user_id, rule_type, pattern, match_field, reason)

    @staticmethod
    def set_preference(db_path: str, user_id: str, key: str, value: str,
                       description: str = ""):
        preferences.set_preference(db_path, user_id, key, value, description)

    @staticmethod
    def get_preferences(db_path: str, user_id: str) -> Dict[str, Dict]:
        return preferences.get_preferences(db_path, user_id)

    @staticmethod
    def get_exclusion_rules(db_path: str, user_id: str) -> List[Dict]:
        return exclusions.get_exclusion_rules(db_path, user_id)

    # ─── Classification ───

    def _classify_all(self):
        self.classified_transactions = []
        self.p2p_questions = []
        self.llm_needed = []

        for tx in self.all_transactions:
            raw_desc_upper = tx.get("description", "").upper()

            if tx.get("tx_type") == "card_payment":
                tx["category"] = "card_payment"
                tx["classify_method"] = "type_filter"
                self.classified_transactions.append(tx)
                continue

            if any(re.search(p, raw_desc_upper) for p in PAYMENT_PATTERNS):
                tx["category"] = "card_payment"
                tx["classify_method"] = "payment_pattern"
                self.classified_transactions.append(tx)
                continue

            if tx.get("tx_type") == "income":
                tx["category"] = "income"
                tx["classify_method"] = "type_filter"
                self.classified_transactions.append(tx)
                continue

            raw_desc = tx["description"]
            if self.classifier._is_p2p(raw_desc.upper()):
                result = self.classifier.classify(
                    raw_desc, amount=tx.get("amount"), user_id=self.user_id,
                )
            else:
                desc = self._clean_description(raw_desc)
                result = self.classifier.classify(
                    desc, amount=tx.get("amount"), user_id=self.user_id,
                )

            tx["category"] = result["category"]
            tx["confidence"] = result.get("confidence", 0)
            tx["classify_method"] = result["method"]
            tx["resolution_key"] = resolution.resolution_key(tx)

            if result.get("needs_user_input"):
                self.p2p_questions.append({
                    "resolution_key": tx["resolution_key"],
                    "transaction": tx,
                    "prompt": result["user_prompt"],
                    "recipient": result.get("p2p_recipient"),
                    "previous_category": result.get("previous_category"),
                })
            elif result["method"] == "needs_llm":
                self.llm_needed.append(tx)

            self.classified_transactions.append(tx)

    def _clean_description(self, desc: str) -> str:
        patterns = [
            r"^(?:Recurring\s+)?Card Purchase(?:\s+(?:With Pin|Return))?\s+\d{2}/\d{2}\s+",
            r"^ATM Cash (?:Deposit|Withdrawal)\s+\d{2}/\d{2}\s+",
            r"^(?:Zelle Payment (?:To|From))\s+",
            r"^Online (?:Transfer|Payment)\s+",
            r"^(?:\d{2}/\d{2}\s+)?Payment To\s+",
        ]
        cleaned = desc
        for p in patterns:
            cleaned = re.sub(p, "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*Card\s+\d{4}\s*$", "", cleaned, flags=re.I)
        cleaned = cleaned.strip()
        return cleaned if cleaned else desc
