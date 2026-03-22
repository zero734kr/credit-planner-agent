"""
Pending resolution logic — handles P2P and LLM classification resolution.

Transactions that can't be classified deterministically are queued as
"pending" (P2P questions or needs_llm). This module resolves them via
user-provided answers or callback functions.
"""

from collections import defaultdict
from typing import Callable, Dict, List, Optional

from pipeline.category_classifier.patterns import ALL_CATEGORIES


_SKIP_TOKENS = {"skip"}


def resolution_key(tx: Dict) -> str:
    """Stable key for matching pending resolutions back to a transaction."""
    return "|".join([
        tx.get("date", ""),
        tx.get("description", ""),
        f"{float(tx.get('amount', 0)):.2f}",
        tx.get("source", ""),
        tx.get("card_name", ""),
    ])


def normalize_resolution_category(category: str | None) -> str | None:
    if not category:
        return None
    normalized = category.strip().lower()
    return normalized if normalized in ALL_CATEGORIES else None


def is_skip_resolution(category: str | None) -> bool:
    if not category:
        return False
    return category.strip().lower() in _SKIP_TOKENS


def get_pending_resolutions(
    p2p_questions: List[Dict],
    llm_needed: List[Dict],
) -> Dict[str, List[Dict]]:
    """Return unresolved P2P and LLM classifications with stable keys."""
    p2p_out = []
    for q in p2p_questions:
        item = dict(q)
        item["resolution_key"] = item.get("resolution_key") or resolution_key(item["transaction"])
        p2p_out.append(item)

    llm_out = []
    for tx in llm_needed:
        llm_out.append({
            "resolution_key": tx.get("resolution_key") or resolution_key(tx),
            "date": tx.get("date"),
            "description": tx.get("description"),
            "amount": tx.get("amount"),
        })

    return {"p2p_questions": p2p_out, "llm_needed": llm_out}


def build_pending_result(
    all_transactions: List[Dict],
    classified_transactions: List[Dict],
    excluded_transactions: List[Dict],
    p2p_questions: List[Dict],
    llm_needed: List[Dict],
) -> Dict:
    """Return a non-final result that tells the caller more input is needed."""
    pending = get_pending_resolutions(p2p_questions, llm_needed)
    return {
        "status": "needs_resolution",
        "resolution_required": True,
        "total_parsed": len(all_transactions),
        "total_classified": len(classified_transactions),
        "total_excluded": len(excluded_transactions),
        "p2p_questions": pending["p2p_questions"],
        "llm_needed": pending["llm_needed"],
        "message": "Resolve pending P2P and/or LLM classifications before generating the final report.",
    }


def resolve_pending(
    p2p_questions: List[Dict],
    llm_needed: List[Dict],
    classified_transactions: List[Dict],
    classifier,
    user_id: str,
    clean_description_fn: Callable[[str], str],
    p2p_answers: Dict[str, str] | None = None,
    llm_answers: Dict[str, str] | None = None,
    llm_resolver: Callable[[Dict], Optional[str]] | None = None,
    p2p_resolver: Callable[[Dict], Optional[str]] | None = None,
) -> tuple[List[Dict], List[Dict], List[Dict], Dict[str, int]]:
    """
    Apply answers/callbacks to unresolved transactions.

    Returns: (remaining_p2p, remaining_llm, classified_transactions, stats)
    """
    p2p_answers = dict(p2p_answers or {})
    llm_answers = dict(llm_answers or {})

    if p2p_resolver:
        for q in p2p_questions:
            key = q.get("resolution_key") or resolution_key(q["transaction"])
            if key not in p2p_answers:
                p2p_answers[key] = p2p_resolver(dict(q))

    if llm_resolver:
        for tx in llm_needed:
            key = tx.get("resolution_key") or resolution_key(tx)
            if key not in llm_answers:
                llm_answers[key] = llm_resolver(dict(tx))

    resolved_p2p = 0
    skipped_p2p = 0
    excluded_from_resolution = []
    remaining_p2p = []
    p2p_history_updates = defaultdict(set)

    for q in p2p_questions:
        key = q.get("resolution_key") or resolution_key(q["transaction"])
        answer = p2p_answers.get(key)

        if is_skip_resolution(answer):
            tx = q["transaction"]
            tx["excluded"] = True
            tx["exclusion_reason"] = "user_marked_skip"
            tx["classify_method"] = "user_excluded"
            excluded_from_resolution.append(tx)
            skipped_p2p += 1
            continue

        category = normalize_resolution_category(answer)
        if not category:
            remaining_p2p.append(q)
            continue

        tx = q["transaction"]
        tx["category"] = category
        tx["confidence"] = 1.0
        tx["classify_method"] = "p2p_resolved"
        recipient = q.get("recipient")
        if recipient:
            p2p_history_updates[recipient].add(category)
        resolved_p2p += 1

    resolved_llm = 0
    skipped_llm = 0
    remaining_llm = []

    for tx in llm_needed:
        key = tx.get("resolution_key") or resolution_key(tx)
        answer = llm_answers.get(key)

        if is_skip_resolution(answer):
            tx["excluded"] = True
            tx["exclusion_reason"] = "user_marked_skip"
            tx["classify_method"] = "user_excluded"
            excluded_from_resolution.append(tx)
            skipped_llm += 1
            continue

        category = normalize_resolution_category(answer)
        if not category:
            remaining_llm.append(tx)
            continue

        tx["category"] = category
        tx["confidence"] = 1.0
        tx["classify_method"] = "llm_resolved"
        classifier.distill_from_llm(
            clean_description_fn(tx.get("description", "")),
            category,
        )
        resolved_llm += 1

    # Remove excluded transactions from classified list
    updated_classified = [tx for tx in classified_transactions if not tx.get("excluded")]

    # Persist consistent P2P categorizations
    for recipient, categories in p2p_history_updates.items():
        if len(categories) == 1:
            classifier.save_p2p_category(user_id, recipient, next(iter(categories)))

    stats = {
        "resolved_p2p": resolved_p2p,
        "resolved_llm": resolved_llm,
        "skipped_p2p": skipped_p2p,
        "skipped_llm": skipped_llm,
        "remaining_p2p": len(remaining_p2p),
        "remaining_llm": len(remaining_llm),
    }

    return remaining_p2p, remaining_llm, updated_classified, stats
