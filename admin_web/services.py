"""Bounded, parameterized read models for the administrator console."""
import json
from pathlib import Path
import sqlite3

from nutrition.review.policy import DEFAULT_POLICY
from nutrition.review.workflow import evaluate_staging_subject, history
from scripts.review_usda_staging import open_staging_readonly, review_database

MAX_SEARCH = 100
PAGE_SIZE = 20
DASHBOARD_STATISTICS_LIMIT = 5000


def configured(path):
    return bool(path and Path(path).is_file())


def list_foods(staging_path, review_path=None, *, search="", release="", decision="", evaluation="", page=1):
    if not configured(staging_path):
        return {"configured": False, "items": [], "total": 0, "page": 1, "pages": 0}
    search = (search or "").strip()[:MAX_SEARCH]
    release = (release or "").strip()[:128]
    evaluation_filter = (evaluation or "").strip()
    page = max(1, min(int(page), 10000))
    connection = open_staging_readonly(staging_path)
    try:
        clauses, params = [], []
        if search:
            clauses.append("record_json LIKE ? ESCAPE '\\'")
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if release:
            clauses.append("release_key=?")
            params.append(release)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = connection.execute("SELECT count(*) FROM staging_records" + where, params).fetchone()[0]
        rows = connection.execute(
            "SELECT release_key,fdc_id,source_locator,record_json,record_sha256 FROM staging_records" + where +
            " ORDER BY release_key,fdc_id LIMIT ? OFFSET ?", params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
    finally:
        connection.close()
    items = []
    for release_key, fdc_id, locator, record_json, record_sha in rows:
        record = json.loads(record_json)
        evaluation_result = evaluate_staging_subject(staging_path, release_key, fdc_id)["evaluation"]
        current = current_decision(review_path, release_key, fdc_id)
        item = {"release_key": release_key, "fdc_id": fdc_id,
                "name": record.get("food_name") or record.get("food_name_en") or record.get("description") or "未命名食品",
                "source_locator": locator, "record_sha256": record_sha,
                "evaluation": evaluation_result["decision"], "manual_decision": current}
        if ((not decision or current == decision) and
                (not evaluation_filter or item["evaluation"] == evaluation_filter)):
            items.append(item)
    return {"configured": True, "items": items, "total": total, "page": page,
            "pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
            "filtered_page_only": bool(decision or evaluation_filter)}


def current_decision(review_path, release_key, fdc_id):
    if not configured(review_path):
        return None
    try:
        connection = sqlite3.connect(Path(review_path).resolve().as_uri() + "?mode=ro", uri=True)
        row = connection.execute(
            "SELECT effective_decision FROM current_decisions WHERE release_key=? AND fdc_id=?",
            (release_key, fdc_id)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        if "connection" in locals(): connection.close()


def food_detail(staging_path, review_path, release_key, fdc_id):
    connection = open_staging_readonly(staging_path)
    try:
        row = connection.execute(
            "SELECT source_locator,record_json,record_sha256 FROM staging_records WHERE release_key=? AND fdc_id=?",
            (release_key, fdc_id)).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    record = json.loads(row[1])
    evaluation = evaluate_staging_subject(staging_path, release_key, fdc_id)
    events = history(review_path, release_key, fdc_id) if configured(review_path) else []
    return {"release_key": release_key, "fdc_id": fdc_id, "source_locator": row[0],
            "record_sha256": row[2], "record": record, "evaluation": evaluation,
            "history": events, "source_verified": False}


def dashboard(staging_path, review_path):
    if not configured(staging_path):
        return {"configured": False}
    connection = open_staging_readonly(staging_path)
    try:
        total = connection.execute("SELECT count(*) FROM staging_records").fetchone()[0]
    finally:
        connection.close()
    # The complete scanner reuses one validated, read-only connection.  Never
    # enumerate identities or evaluate records until the count proves bounded.
    exact = total <= DASHBOARD_STATISTICS_LIMIT
    if exact:
        counts = review_database(staging_path)["decision_counts"]
        statistics_status = "computed"
    else:
        counts = {"ready_for_manual_review": None, "needs_review": None,
                  "rejected_by_policy": None}
        statistics_status = "skipped_limit"
    handled, recent = 0, []
    if configured(review_path):
        try:
            connection = sqlite3.connect(Path(review_path).resolve().as_uri() + "?mode=ro", uri=True)
            handled = connection.execute("SELECT count(*) FROM review_events").fetchone()[0]
            recent = connection.execute(
                "SELECT release_key,fdc_id,effective_decision,decided_at_utc FROM review_events "
                "ORDER BY event_sequence DESC LIMIT 8").fetchall()
        except sqlite3.Error:
            pass
        finally:
            if "connection" in locals(): connection.close()
    return {"configured": True, "total": total, "pending": total, "counts": counts,
            "counts_exact": exact, "statistics_status": statistics_status,
            "statistics_limit": DASHBOARD_STATISTICS_LIMIT,
            "handled": handled, "recent": recent}
