"""Code provenance and measurable acceptance facts from committed checkpoints."""

import difflib
import hashlib
from datetime import datetime

from .state import timestamp


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_revision(state, node, summary, *, origin="agent"):
    state.revisions.append({"index": len(state.revisions), "cycle": state.cycle,
        "node": node, "time": timestamp(), "origin": origin, "summary": summary,
        "source": state.artifacts["solution.py"], "code_sha256": digest(state.artifacts["solution.py"]),
        "tests_sha256": digest(state.baseline_tests), "message_seq": len(state.messages)})


def build_evidence(state):
    rows = []
    for index, revision in enumerate(state.revisions):
        previous = state.revisions[index - 1] if index else None
        reviews = [review for review in state.reviews if review.get("revision") == revision["index"]]
        before = previous["source"].splitlines(keepends=True) if previous else []
        after = revision["source"].splitlines(keepends=True)
        patch = "".join(difflib.unified_diff(before, after, fromfile=f"revision-{index-1}/solution.py" if previous else "/dev/null",
                                           tofile=f"revision-{index}/solution.py"))
        changed = list(difflib.ndiff(before, after)) if previous else []
        rows.append({**revision, "diff": patch, "added": sum(line.startswith('+ ') for line in changed),
                     "removed": sum(line.startswith('- ') for line in changed),
                     "changed": previous is None or previous["code_sha256"] != revision["code_sha256"],
                     "reviews": [{"cycle": review["cycle"], "approved": review["approved"],
                                  "issues": len(review["issues"]), "test_status": review["tests"]["status"],
                                  "tests_run": review["tests"]["tests_run"]} for review in reviews]})
    latest = state.reviews[-1] if state.reviews else None
    current_hash = digest(state.artifacts.get("solution.py", ""))
    reviewed_current = bool(latest and latest.get("code_sha256") == current_hash)
    # Older checkpoints have no revision association: report what is known, never reconstruct lost versions.
    if latest and "code_sha256" not in latest:
        reviewed_current = state.status == "completed"
    frozen = bool(state.baseline_tests and state.baseline_tests == state.artifacts.get("test_solution.py"))
    verified = bool(state.status == "completed" and reviewed_current and frozen and latest["approved"]
                    and latest["tests"]["passed"] is True)
    node_seconds = {}
    starts = {}
    for event in state.events:
        node = event.get("agent")
        try:
            instant = datetime.fromisoformat(event["time"])
        except (ValueError, KeyError, TypeError):
            continue
        if event["kind"] == "node_start":
            starts[node] = instant
        elif event["kind"] in {"node_done", "workflow_error", "limit"} and node in starts:
            node_seconds[node] = round(node_seconds.get(node, 0) + max(0, (instant - starts.pop(node)).total_seconds()), 3)
    return {"run_id": state.id, "status": state.status, "mode": state.mode,
        "acceptance": "verified" if verified else "unverified" if state.status == "completed" else "unfinished",
        "final_tests": latest["tests"]["status"] if latest else "not_started", "tests_run": latest["tests"]["tests_run"] if latest else None,
        "tests_frozen": frozen, "tests_sha256": digest(state.baseline_tests) if frozen else None,
        "code_sha256": current_hash if state.artifacts else None,
        "repairs": state.cycle, "messages": len(state.messages), "reviews": len(state.reviews),
        "node_seconds": node_seconds, "revisions": rows,
        "review_history": [{"cycle": r["cycle"], "issues": len(r["issues"]),
                            "tests": r["tests"]["status"], "tests_run": r["tests"]["tests_run"]} for r in state.reviews]}
