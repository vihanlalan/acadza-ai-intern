"""Review items and the patched question bank.

An item is one thing a faculty member decides on: either a group of reports on one question that got the
same verdict and edit, or one finding nobody reported. Item ids stay stable for a given data + cache build.

The bank is what students should see after review. Per question it applies:
  - every auto-applied fix nobody rolled back, and
  - every fix faculty approved (their edited version if they changed it).
Edits are applied with the same exact-once rule as the gate. If two approved fixes collide, the second one
is reported as a conflict and not applied, so a bad combination never reaches students silently.

    python -m engine.bank --decisions audit-log.json --out questions.patched.json
"""
from __future__ import annotations

import argparse
import copy
import json
from collections import defaultdict
from datetime import datetime, timezone

from .data import load_all, view
from .guards import apply_edits, listify

ACTIONS = ("approved", "edited", "rejected")


def build_items(qid: str, case_reports: list, secondary: list) -> list:
    groups: dict[str, list] = {}
    for r in case_reports:
        k = json.dumps([r["verdict"], r["repair"], r["route"], r["edit"]], ensure_ascii=False, sort_keys=True)
        groups.setdefault(k, []).append(r)
    items = []
    for i, reps in enumerate(groups.values()):
        r = reps[0]
        items.append({"id": f"{qid}:R{i}", "kind": "reported", "questionId": qid,
                      "reportIds": [x["reportId"] for x in reps], "verdict": r["verdict"], "repair": r["repair"],
                      "route": r["route"], "edit": r["edit"], "suggestion": r["suggestion"],
                      "confidence": r["confidence"], "evidence": r["evidence"], "checks": r["checks"]})
    for f in secondary:
        items.append({"id": f["findingId"], "kind": "finding", "questionId": qid, "reportIds": [],
                      "verdict": "defect", "repair": f["repair"], "route": f["route"], "edit": f["edit"],
                      "suggestion": f["suggestion"], "confidence": f["confidence"], "evidence": f["evidence"],
                      "checks": f["checks"]})
    return items


def outcome(item: dict, dec: dict | None) -> tuple[str, list]:
    """What this item does to the bank: (status, edits)."""
    if item["verdict"] != "defect":
        return "no_change", []
    if dec is None:
        if item["kind"] == "reported" and item["route"] == "auto_apply" and item["edit"]:
            return "auto", listify(item["edit"])
        return "pending", []
    if dec["action"] == "rejected":
        return "rejected", []
    edits = listify(dec["edits"]) if dec.get("edits") is not None else listify(item["edit"] or item["suggestion"])
    # A confirmed human-only repair (e.g. a missing diagram) has no text edit: it stays open for the content team.
    return ("approved", edits) if edits else ("confirmed_needs_content", [])


def build_bank(questions: dict, items: list, decisions: dict) -> dict:
    todo, open_items, counts = defaultdict(list), [], defaultdict(int)
    for it in items:
        dec = decisions.get(it["id"])
        status, edits = outcome(it, dec)
        counts[status] += 1
        if edits:
            todo[it["questionId"]].append((it, status, edits, dec))
        elif status in ("pending", "confirmed_needs_content"):
            open_items.append({"itemId": it["id"], "questionId": it["questionId"], "repair": it["repair"], "status": status})

    changed, changes, conflicts = [], [], []
    for qid, entries in todo.items():
        q = questions[qid]
        v = view(q)
        # auto fixes first (they were live before anyone reviewed), then faculty decisions in time order
        entries.sort(key=lambda e: (e[1] != "auto", (e[3] or {}).get("at", "")))
        history = []
        for it, status, edits, dec in entries:
            try:
                v = apply_edits(v, edits)
            except ValueError as e:
                conflicts.append({"itemId": it["id"], "questionId": qid, "reason": str(e)})
                continue
            at = (dec or {}).get("at") or q.get("updatedAt")
            history.append({"type": (it["repair"] or "fix").replace("_", " ").capitalize(),
                            "actor": "faculty" if dec else "auto", "by": (dec or {}).get("by", "triage engine"),
                            "at": at, "itemId": it["id"], "reportIds": it["reportIds"],
                            "note": it["evidence"][:240]})
            changes.append({"itemId": it["id"], "questionId": qid, "status": status, "edits": edits,
                            "by": (dec or {}).get("by"), "at": at})
        if history:
            changed.append(_write_back(q, v, history))
    return {"generatedAt": _now(), "counts": dict(counts), "changed": changed, "changes": changes,
            "conflicts": conflicts, "open": open_items}


def _write_back(q: dict, v: dict, history: list) -> dict:
    """Put a patched flat view back into the production document shape."""
    nq = copy.deepcopy(q)
    old_t, new_t = q["questionType"], v["questionType"]
    sub = nq.pop(old_t) if new_t != old_t else nq.get(old_t)
    sub = sub or {}
    sub["question"], sub["solution"], sub["answer"] = v["question"], v["solution"], v["answer"]
    nq[new_t] = sub
    nq["questionType"] = new_t
    nq["errorHistory"] = list(q.get("errorHistory") or []) + history
    nq["updatedAt"] = max([h["at"] for h in history if h["at"]] + [q.get("updatedAt") or ""])
    return nq


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def all_items() -> list:
    from .pipeline import build
    return [it for c in build()["cases"] for it in c["items"]]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Write the question bank with reviewed fixes applied.")
    ap.add_argument("--decisions", help="console audit-log export (JSON with a 'decisions' object); omit for auto fixes only")
    ap.add_argument("--out", default="questions.patched.json")
    a = ap.parse_args()
    decisions = json.load(open(a.decisions, encoding="utf-8"))["decisions"] if a.decisions else {}
    questions, _, _ = load_all()
    bank = build_bank(questions, all_items(), decisions)
    patched = {q["_id"]: q for q in bank["changed"]}
    full = [patched.get(qid, q) for qid, q in questions.items()]
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False, indent=1)
    print(f"wrote {a.out}: {len(bank['changed'])} questions changed, {len(bank['conflicts'])} conflicts, "
          f"{len(bank['open'])} items still open; {bank['counts']}")
