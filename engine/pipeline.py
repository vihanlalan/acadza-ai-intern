"""Report -> Fix pipeline.

    python -m engine.pipeline                 # build outputs from cached stage results (no API calls)
    python -m engine.pipeline --live          # re-run LLM stages (GROQ_API_KEY, or LLM_PROVIDER=anthropic)
    python -m engine.pipeline --live --only <questionId>

Stages
  0  cluster reports by questionId (one solve per question, not per report)
  1  deterministic scanners (question + each comment)
  2  blind solver (question only)                         - LLM
  3  auditor (record + signals + blind solve + fenced reports) - LLM
  4  safety gate (taxonomy scope, exact find, re-scan, key consensus)
Outputs: results.json, unreported_findings.json, console/data.json
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import json
import math
import sys
from collections import defaultdict

from . import llm
from .data import ROOT, load_all, to_text, view
from .guards import gate
from .prompts import SOLVER_SCHEMA, SOLVER_SYSTEM, AUDITOR_SCHEMA, auditor_system, PROMPT_VERSION
from .scanners import comment_signals, parse_options, question_signals

# RF_OUT_DIR lets an experimental run (e.g. a live Groq pass) write somewhere other than the repo.
OUT = Path(os.environ.get("RF_OUT_DIR") or ROOT)

# How much a defect of each kind hurts a student who meets it right now.
SEVERITY = {
    "answer_key": 10, "type_mismatch": 9, "unsound": 9, "stem_data": 8, "option_repair": 8, "wrong_question": 8,
    "diagram": 7, "identical_options": 7, "solution_fix": 6, "solution_missing": 5, "answer_form": 4,
    "notation": 3, "markup_leak": 3, "wrong_chapter": 3, "syllabus": 2, "spelling": 1, "noise": 1,
    "exam_reference": 1,
}


def solver_user(v: dict) -> str:
    return json.dumps({
        "subject": v["subject"], "chapter": v["chapter"], "questionType": v["questionType"],
        "question_html": v["question"], "question_text": to_text(v["question"]),
        "has_images": bool(v["quesImages"]),
    }, ensure_ascii=False, indent=1)


def auditor_user(v: dict, qsig: list, solve: dict | None, reports: list, rsig: dict) -> str:
    parts = ["RECORD:", json.dumps({
        "questionId": v["id"], "subject": v["subject"], "chapter": v["chapter"], "questionType": v["questionType"],
        "answer": v["answer"], "quesImages": v["quesImages"], "errorHistory": v["errorHistory"],
        "question_raw": v["question"], "solution_raw": v["solution"],
        "question_text": to_text(v["question"]), "solution_text": to_text(v["solution"]),
    }, ensure_ascii=False, indent=1),
        "SIGNALS:", json.dumps([{k: s[k] for k in ("code", "detail")} for s in qsig], ensure_ascii=False),
        "BLIND_SOLVE:", json.dumps(solve, ensure_ascii=False)]
    if reports:
        parts.append("REPORTS:")
        for r in reports:
            flags = [s["code"] for s in rsig[r["reportId"]]]
            parts.append(f'<untrusted_student_report id="{r["reportId"]}" source="{r["source"]}" reportedAt="{r["reportedAt"]}" scanner_flags="{",".join(flags)}">\n'
                         f'{r["comment"]}\n</untrusted_student_report>')
    else:
        parts.append("REPORTS: none - proactive audit. Return an empty `reports` object and list any defects in secondary_findings.")
    return "\n".join(parts)


def run(live: bool = False, only: str | None = None, check: bool = False) -> dict:
    questions, reports, taxonomy = load_all()
    by_q = defaultdict(list)
    for r in reports:
        by_q[r["questionId"]].append(r)
    asys = auditor_system(taxonomy)

    results, cases, unreported = [], [], []
    for qid, q in questions.items():
        if only and qid != only:
            live_here = False
        else:
            live_here = live
        v = view(q)
        qsig = question_signals(v)
        reps = sorted(by_q.get(qid, []), key=lambda r: r["reportedAt"])
        rsig = {r["reportId"]: comment_signals(r, v) for r in reps}

        # Stage 2 - blind solve. The comment is never in this prompt.
        solve = _safe(lambda: llm.run_stage("solver", qid, SOLVER_SYSTEM, solver_user(v), SOLVER_SCHEMA, live_here))
        # Stage 3 - audit.
        audit = _safe(lambda: llm.run_stage("auditor", qid, asys, auditor_user(v, qsig, solve, reps, rsig), AUDITOR_SCHEMA, live_here))
        audit = audit or {"solution_concludes": None, "reports": {}, "secondary_findings": []}

        case_reports = []
        for r in reps:
            flags = [s["code"] for s in rsig[r["reportId"]]]
            prop = audit["reports"].get(r["reportId"]) or _fallback(r, flags)
            g = gate(prop, v, taxonomy, solve, audit.get("solution_concludes"), flags)
            row = {
                "reportId": r["reportId"], "questionId": qid, "verdict": g["verdict"], "repair": g["repair"],
                "edit": g["edit"], "route": g["route"], "confidence": g["confidence"],
                "evidence": prop.get("evidence", ""), "student_reply": prop.get("student_reply", ""),
            }
            results.append(row)
            case_reports.append({**r, **row, "flags": rsig[r["reportId"]], "checks": g["checks"],
                                 "suggestion": g["suggestion"], "after": _after_fields(g["after"], v)})

        secondary = []
        for i, f in enumerate(audit.get("secondary_findings", [])):
            prop = {"verdict": "defect", "repair": f["repair"], "edit": f["edit"], "confidence": f["confidence"],
                    "route_request": "auto_apply" if taxonomy["repairs"].get(f["repair"], {}).get("auto") else "faculty_review"}
            g = gate(prop, v, taxonomy, solve, audit.get("solution_concludes"), [])
            item = {"findingId": f"F{qid[-4:]}-{i + 1}", "questionId": qid, "reported": bool(reps),
                    "repair": g["repair"], "edit": g["edit"], "route": g["route"], "confidence": g["confidence"],
                    "evidence": f["evidence"], "checks": g["checks"], "suggestion": g["suggestion"],
                    "after": _after_fields(g["after"], v)}
            secondary.append(item)
            unreported.append({k: item[k] for k in ("findingId", "questionId", "reported", "repair", "edit", "route", "confidence", "evidence")})

        cases.append(_case(v, q, qsig, solve, audit, case_reports, secondary))

    results.sort(key=lambda r: r["reportId"])
    if check:
        _check_committed(ROOT / "results.json", results)
    _write(OUT / "results.json", results)
    _write(OUT / "unreported_findings.json", unreported)
    cases.sort(key=lambda c: -c["urgency"]["score"])
    payload = {"generatedWith": {"promptVersion": PROMPT_VERSION, "provider": llm.PROVIDER,
                                 "stageMeta": _stage_meta()},
               "taxonomy": taxonomy, "cases": cases}
    _write(OUT / "console" / "data.json", payload)
    # Same payload as a script so the console also works from file:// (no fetch).
    (OUT / "console" / "data.js").write_text(
        "window.RF_DATA = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    return {"results": results, "unreported": unreported, "cases": cases}


def _case(v, q, qsig, solve, audit, case_reports, secondary):
    defect_rows = [r for r in case_reports if r["verdict"] == "defect"]
    needs_human = any(r["route"] == "faculty_review" for r in defect_rows) or any(s["route"] == "faculty_review" for s in secondary)
    if defect_rows:
        lane = "review" if any(r["route"] == "faculty_review" for r in defect_rows) else "auto"
    elif case_reports:
        lane = "found" if secondary else "closed"
    else:
        lane = "found" if secondary else "clean"
    sev = max([SEVERITY.get(r["repair"], 0) for r in defect_rows] + [SEVERITY.get(s["repair"], 0) * 0.6 for s in secondary] + [0])
    n = len(case_reports)
    score = round(sev * (1 + math.log1p(n)) + (5 if lane == "review" else 0), 1)
    return {
        "questionId": v["id"], "qid": q.get("qid"), "subject": v["subject"], "chapter": v["chapter"],
        "questionType": v["questionType"], "level": q.get("level"),
        "record": {k: v[k] for k in ("question", "solution", "answer", "questionType", "quesImages", "errorHistory")},
        "options": parse_options(v["question"]) if v["questionType"] != "integerQuestion" else None,
        "signals": qsig, "solve": solve, "solution_concludes": audit.get("solution_concludes"),
        "reports": case_reports, "secondary": secondary, "lane": lane, "needsHuman": needs_human,
        "urgency": {"score": score, "severity": sev, "reports": n},
    }


def _after_fields(after, v):
    if not after:
        return None
    return {k: after[k] for k in ("question", "solution", "answer", "questionType") if after[k] != v[k]}


def _fallback(r, flags):
    """No LLM output available: never guess. Route to a human unless the scanner is certain."""
    if "fixed_after_report" in flags:
        return {"verdict": "already_fixed", "repair": None, "edit": None, "route_request": "no_change", "confidence": 0.9,
                "evidence": "A faculty edit logged after this report removed the quoted text.",
                "student_reply": "Thanks for flagging this. A teacher already corrected it, so you should see the fixed version now."}
    if "gibberish" in flags:
        return {"verdict": "not_actionable", "repair": None, "edit": None, "route_request": "no_change", "confidence": 0.7,
                "evidence": "Comment has no content and the scanners found nothing.",
                "student_reply": "We couldn't tell what went wrong from your report. If something looks off, tell us which option or line and we'll check it."}
    return {"verdict": "defect", "repair": None, "edit": None, "route_request": "faculty_review", "confidence": 0.0,
            "evidence": "LLM stages unavailable - sent to faculty untriaged.",
            "student_reply": "Thanks for reporting this. A teacher will check the question and get back to you."}


def _safe(fn):
    try:
        return fn()
    except llm.LLMUnavailable as e:
        print(f"  ! LLM unavailable: {e}", file=sys.stderr)
        return None


def _stage_meta():
    out = {}
    for stage in ("solver", "auditor"):
        metas = [e.get("meta", {}) for e in llm.load_cache(stage).values()]
        out[stage] = sorted({f"{m.get('provider')}:{m.get('model')}" for m in metas})
    return out


def _check_committed(path, results):
    """Build-time guard: the committed results.json must be exactly what code + cache produce."""
    if not path.exists():
        raise SystemExit(f"check failed: {path.name} is missing")
    committed = {r["reportId"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    fresh = {r["reportId"]: r for r in results}
    bad = sorted(k for k in committed.keys() | fresh.keys()
                 if json.dumps(committed.get(k), sort_keys=True) != json.dumps(fresh.get(k), sort_keys=True))
    if bad:
        raise SystemExit(f"check failed: results.json is stale for {len(bad)} report(s): {', '.join(bad[:10])}"
                         " - run `python -m engine.pipeline` and commit the result")
    print(f"check ok: results.json matches the pipeline output ({len(fresh)} reports)")


def _write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--check", action="store_true", help="fail if committed results.json differs from fresh output (used by the deploy build)")
    a = ap.parse_args()
    out = run(live=a.live, only=a.only, check=a.check)
    from collections import Counter
    print("reports:", len(out["results"]), dict(Counter(r["route"] for r in out["results"])))
    print("verdicts:", dict(Counter(r["verdict"] for r in out["results"])))
    print("unreported findings:", len(out["unreported"]))
