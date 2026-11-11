"""Stage 1 - deterministic scanners. Zero cost, zero latency, no LLM.

They produce *signals*, not verdicts. Signals are handed to the auditor as hard
facts ("option C is blank", "comment contains an instruction override") and are
re-run by the guard after an edit is applied to prove the edit did not break
anything.
"""
from __future__ import annotations

import html
import re
from datetime import datetime

from .data import to_text

LETTERS = "ABCD"

# ---------------------------------------------------------------- options ---

def parse_options(question_html: str) -> dict[str, str | None]:
    """Find (A)..(D) inside the question HTML.

    Uses the LAST (D), then the last (C) before it, etc., so that stems like
    "Assertion (A) : ..." are not mistaken for option A.
    """
    text = to_text(question_html)
    found: dict[str, int] = {}
    end = len(text)
    for letter in reversed(LETTERS):
        pos = text.rfind(f"({letter})", 0, end)
        if pos != -1:
            found[letter] = pos
            end = pos
    opts: dict[str, str | None] = {}
    ordered = sorted(found.items(), key=lambda kv: kv[1])
    for i, (letter, pos) in enumerate(ordered):
        stop = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        opts[letter] = text[pos + 3:stop].strip()
    for letter in LETTERS:
        opts.setdefault(letter, None)  # None = marker absent
    return opts


def _norm(s: str) -> str:
    # whitespace only - stripping '.' once made "1.1 uA" equal "11 uA"
    return re.sub(r"\s+", "", s or "").lower()


def option_signals(v: dict) -> list[dict]:
    if v["questionType"] == "integerQuestion":
        return []
    out = []
    opts = parse_options(v["question"])
    present = {k: t for k, t in opts.items() if t is not None}
    if len(present) < 2:
        return out  # options not inline; nothing to say
    for k, t in opts.items():
        if t is None:
            out.append({"code": "option_missing", "field": "question", "detail": f"Option ({k}) is absent from the question HTML", "option": k})
        elif t.strip() == "":
            out.append({"code": "option_blank", "field": "question", "detail": f"Option ({k}) has no text", "option": k})
    seen: dict[str, str] = {}
    for k, t in present.items():
        if not t:
            continue
        n = _norm(t)
        if n in seen:
            out.append({"code": "option_duplicate", "field": "question", "detail": f"Options ({seen[n]}) and ({k}) are identical: '{t}'", "options": [seen[n], k]})
        else:
            seen[n] = k
    keyed = answer_letters(v["answer"])
    for k in keyed:
        if k in opts and not (opts[k] or "").strip():
            out.append({"code": "key_points_to_empty_option", "field": "answer", "detail": f"The answer key is ({k}) but option ({k}) is blank or missing"})
    return out


# ----------------------------------------------------------------- answer ---

def answer_letters(ans) -> list[str]:
    if isinstance(ans, list):
        flat = []
        for a in ans:
            flat += [x for x in re.split(r"[\s,]+", str(a)) if x]
        return [x for x in flat if x in LETTERS]
    if isinstance(ans, str) and ans in LETTERS:
        return [ans]
    return []


def answer_signals(v: dict) -> list[dict]:
    t, ans = v["questionType"], v["answer"]
    out = []
    if t == "scq":
        if not (isinstance(ans, str) and ans in LETTERS):
            out.append({"code": "answer_malformed", "field": "answer", "detail": f"SCQ answer should be one letter, got {ans!r}"})
    elif t == "mcq":
        if not isinstance(ans, list) or not ans:
            out.append({"code": "answer_malformed", "field": "answer", "detail": f"MCQ answer should be a non-empty list, got {ans!r}"})
        else:
            if any(a == "" for a in ans):
                out.append({"code": "answer_malformed", "field": "answer", "detail": f"MCQ answer contains an empty string: {ans!r}"})
            if any(isinstance(a, str) and "," in a for a in ans):
                out.append({"code": "answer_malformed", "field": "answer", "detail": f"MCQ answer packs several letters into one string: {ans!r} (graders compare element-wise)"})
            if len(answer_letters(ans)) == 1:
                out.append({"code": "mcq_single_key", "field": "questionType", "detail": "Stored as multi-correct (mcq) but only one option is keyed - possible type mismatch"})
    elif t == "integerQuestion":
        if not re.fullmatch(r"-?\d+(\.\d+)?", str(ans).strip()):
            out.append({"code": "answer_malformed", "field": "answer", "detail": f"Integer answer is not numeric: {ans!r}"})
    return out


# ----------------------------------------------------------------- markup ---

_LATEX = re.compile(r"\\(frac|sqrt|AA|times|alpha|beta|theta|pi|mu|lambda|left|right|text|begin|end|cdot)\b")
_EXAM = re.compile(r"\(\s*(NEET|JEE|AIPMT|AIIMS|MHT[- ]?CET|MHCET|BITSAT|KCET|EAMCET)[^)]{0,30}\d{4}\s*\)", re.I)
# "in the circuit" alone is NOT a figure reference ("power consumed in the circuit")
_FIG = re.compile(r"(as shown|shown (in|below)|in the (given |adjacent )?(figure|diagram|graph)|(given|following|adjacent) (figure|diagram|graph|circuit))", re.I)


def markup_signals(v: dict) -> list[dict]:
    out = []
    for field in ("question", "solution"):
        raw = v[field]
        for m in _LATEX.finditer(raw):
            out.append({"code": "latex_leak", "field": field, "detail": f"Raw LaTeX '{m.group(0)}' will show as text", "match": m.group(0)})
        txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
        if re.search(r"(^|[\s>(])&(?=\s*[A-Za-z=])", txt):
            out.append({"code": "align_amp_leak", "field": field, "detail": "Stray '&' (LaTeX alignment marker) visible in rendered text"})
        if re.search(r"<mspace[^>]*linebreak", raw) and field == "question":
            stack_ok = raw.count("<math") == raw.count("</math>")
            out.append({"code": "mspace_outside_math", "field": field, "detail": "MathML <mspace linebreak> used as an HTML line break around options" + ("" if stack_ok else " (math tags unbalanced)")})
        if raw.count("<math") != raw.count("</math>"):
            out.append({"code": "math_unbalanced", "field": field, "detail": f"{raw.count('<math')} <math> open vs {raw.count('</math>')} close"})
    m = _EXAM.search(to_text(v["question"]))
    if m:
        out.append({"code": "exam_reference", "field": "question", "detail": f"Exam tag '{m.group(0)}' inside question text", "match": m.group(0)})
    if _FIG.search(to_text(v["question"])) and not v["quesImages"]:
        out.append({"code": "figure_missing", "field": "question", "detail": "Stem refers to a figure but quesImages is empty"})
    if not to_text(v["solution"]).strip() and not v["solutionImages"]:
        out.append({"code": "solution_empty", "field": "solution", "detail": "No solution text or images"})
    return out


def question_signals(v: dict) -> list[dict]:
    return option_signals(v) + answer_signals(v) + markup_signals(v)


# ------------------------------------------------------------- the comment ---

_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|rules)|you are (now )?(the )?(admin|system|developer)"
    r"|system prompt|mark (this|the) report as|change the (answer|key) to|as an ai|disregard)", re.I)
_KEYBOARD_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")


def comment_signals(report: dict, v: dict) -> list[dict]:
    c = (report.get("comment") or "").strip()
    out = []
    if _INJECTION.search(c):
        out.append({"code": "prompt_injection", "detail": "Comment tries to instruct the system. It is quarantined: never shown to the solver, never a reason to write."})
    low = re.sub(r"[^a-z0-9]", "", c.lower())
    if not low or len(low) <= 1 or (len(low) >= 4 and any(low in row for row in _KEYBOARD_ROWS)):
        out.append({"code": "gibberish", "detail": f"Comment '{c}' has no content (keyboard mash / empty)"})
    # already-fixed: a faculty edit logged AFTER the report that names the thing the student quoted
    quoted = re.findall(r"['\"\u2018\u2019]([^'\"\u2018\u2019]{2,40})['\"\u2018\u2019]", c)
    live = (to_text(v["question"]) + " " + to_text(v["solution"])).lower()
    rep_at = _ts(report.get("reportedAt"))
    for h in v["errorHistory"]:
        if rep_at and _ts(h.get("at")) and _ts(h["at"]) > rep_at:
            note = (h.get("note") or "").lower()
            for word in quoted:
                w = word.lower()
                if w in note and not re.search(rf"\b{re.escape(w)}\b", live):
                    out.append({"code": "fixed_after_report", "detail": f"Faculty log {h['at']}: \"{h.get('note')}\" - '{word}' is no longer in the live record", "history": h})
    return out


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None
