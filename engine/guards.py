"""Stage 4 - the safety gate. Plain Python, no model.

The LLM proposes; this module decides whether anything may be written.
It can only DOWNGRADE a route (auto_apply -> faculty_review), never upgrade.
Every check is recorded so the console can show the faculty member exactly
why something was (or was not) auto-applied.
"""
from __future__ import annotations

import copy
import re
from collections import Counter

from .scanners import answer_letters, parse_options, question_signals

AUTO_MIN_CONFIDENCE = 0.85
BLOCKING_NEW_SIGNALS = {"latex_leak", "math_unbalanced", "option_blank", "option_missing", "option_duplicate",
                        "answer_malformed", "align_amp_leak"}


def listify(edit):
    if edit is None:
        return []
    return edit if isinstance(edit, list) else [edit]


def apply_edits(v: dict, edits: list[dict]) -> dict:
    """Return a new question view with edits applied. Raises ValueError if a find is not exact."""
    nv = copy.deepcopy(v)
    for e in edits:
        f = e["field"]
        if f in ("question", "solution"):
            n = nv[f].count(e["find"])
            if n != 1:
                raise ValueError(f"'find' occurs {n} times in {f} (must be exactly 1)")
            nv[f] = nv[f].replace(e["find"], e["replace"], 1)
        elif f in ("answer", "questionType"):
            if nv[f] != e["find"]:
                raise ValueError(f"'find' {e['find']!r} != stored {f} {nv[f]!r}")
            nv[f] = e["replace"]
        else:
            raise ValueError(f"unknown field {f}")
    return nv


def gate(proposal: dict, v: dict, taxonomy: dict, solver: dict | None, solution_concludes,
         comment_flags: list[str]) -> dict:
    """Validate one report's proposal. Returns the final row + the checks that ran."""
    checks: list[dict] = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return ok

    verdict = proposal.get("verdict")
    repair = proposal.get("repair")
    edits = listify(proposal.get("edit"))
    conf = float(proposal.get("confidence") or 0)
    requested = proposal.get("route_request") or "faculty_review"

    if not check("verdict in taxonomy", verdict in taxonomy["verdicts"], verdict):
        verdict, repair, edits = "not_actionable", None, []

    if verdict != "defect":
        check("non-defect carries no edit", True, "repair/edit cleared, route no_change")
        return _row(verdict, None, None, None, "no_change", conf, checks, after=None)

    if not check("repair in taxonomy", repair in taxonomy["repairs"], str(repair)):
        return _row(verdict, None, None, None, "faculty_review", conf, checks, after=None)

    rule = taxonomy["repairs"][repair]
    if not rule["auto"]:
        check("auto:false repair -> human decides", True, f"'{repair}' may be suggested, never written")
        return _row(verdict, repair, None, proposal.get("edit"), "faculty_review", conf, checks, after=None)

    if not edits:
        check("an edit is proposed", False, "defect without a concrete edit")
        return _row(verdict, repair, None, None, "faculty_review", conf, checks, after=None)

    ok = True
    for e in edits:
        ok &= check(f"field '{e['field']}' allowed by '{repair}'", e["field"] in rule["fields"],
                    f"allowed: {rule['fields']}")
    after = None
    try:
        after = apply_edits(v, edits)
        check("find matches the live record exactly once", True)
    except ValueError as err:
        ok = check("find matches the live record exactly once", False, str(err)) and ok

    if after is not None:
        # compare counts, not just codes: a question that already had one LaTeX leak
        # must still reject an edit that adds a second one (caught by test_guards)
        before_n = Counter(s["code"] for s in question_signals(v))
        after_sig = question_signals(after)
        after_n = Counter(s["code"] for s in after_sig)
        new = [s for s in after_sig if s["code"] in BLOCKING_NEW_SIGNALS and after_n[s["code"]] > before_n[s["code"]]]
        ok &= check("edit introduces no new markup/option problems", not new, "; ".join(s["detail"] for s in new))
        for e in edits:
            if e["field"] in ("question", "solution"):
                size = len(e["find"]) + len(e["replace"])
                ok &= check("surgical (small relative to field)", size <= max(400, 0.6 * len(v[e["field"]])),
                            f"{size} chars changed of {len(v[e['field']])}")

    if repair == "answer_key" and after is not None:
        new_key = sorted(answer_letters(after["answer"])) or [_num(after["answer"])]
        solved = resolve_answer(solver["final_answer"], v) if solver else []
        concl = resolve_answer(solution_concludes, v)
        ok &= check("blind solver agrees with new key", solved == new_key, f"solver={solved} new={new_key}")
        if sorted(answer_letters(v["answer"])) == new_key and new_key and new_key[0] in "ABCD":
            # format-only repair (e.g. ["A, B, D"] -> ["A","B","D"]): the keyed letters do not change
            check("format-only: keyed letters unchanged", True, f"{v['answer']!r} -> {after['answer']!r}")
        else:
            ok &= check("stored solution concludes the new key", concl == new_key, f"solution ends at {solution_concludes!r}")

    if "prompt_injection" in comment_flags:
        ok &= check("comment is not adversarial", False, "instruction-style comment cannot trigger a write")

    ok &= check(f"confidence >= {AUTO_MIN_CONFIDENCE}", conf >= AUTO_MIN_CONFIDENCE, f"{conf:.2f}")
    if requested != "auto_apply":
        check("auditor asked for a human (content not recoverable / judgement call)", False, "route_request = faculty_review")
        ok = False

    route = "auto_apply" if ok else "faculty_review"
    return _row(verdict, repair, proposal.get("edit"), None, route, conf, checks, after=after)


def _num(x) -> str:
    """'8 cm' -> '8', '4800 N/m^2' -> '4800', '2' -> '2' (first number, normalised)."""
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace(",", ""))
    return str(float(m.group())).rstrip("0").rstrip(".") if m else str(x).strip().lower()


def _flat(s: str) -> str:
    return re.sub(r"[\s ]+", "", str(s)).lower()


def resolve_answer(ans, v: dict) -> list[str]:
    """Map a model's answer to comparable form.

    Models may answer with the letter ("A") or with the value ("50 V").
    Groq's gpt-oss wrote solution_concludes = "50 V" on its first run, which
    the old literal comparison treated as disagreement. A value is mapped to
    the option whose text matches it; integer questions compare numbers.
    """
    if ans is None:
        return []
    letters = sorted(answer_letters(ans))
    if letters and (isinstance(ans, list) or re.fullmatch(r"\(?[A-D]\)?", str(ans).strip())):
        return letters
    if v["questionType"] == "integerQuestion":
        return [_num(ans)]
    opts = {k: t for k, t in parse_options(v["question"]).items() if t}
    target = _flat(ans)
    hits = [k for k, t in opts.items() if _flat(t) == target]
    if not hits:  # fall back to the leading number + unit-insensitive match, unique only
        n = _num(ans)
        hits = [k for k, t in opts.items() if _num(t) == n]
    return sorted(hits) if len(hits) == 1 else (letters or [target])


def _row(verdict, repair, edit, suggestion, route, conf, checks, after):
    return {"verdict": verdict, "repair": repair, "edit": edit, "suggestion": suggestion, "route": route,
            "confidence": round(conf, 2), "checks": checks, "after": after}
