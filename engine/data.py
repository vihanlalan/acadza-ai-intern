"""Loading + normalising Acadza's production question format.

A question stores its payload in a sub-document named after its type:
q["scq"]["question"], q["mcq"]["answer"], q["integerQuestion"]["solution"] ...
Everything downstream works on the flat view returned by `view()`.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_all(data_dir: Path = DATA):
    questions = json.loads((data_dir / "questions.json").read_text(encoding="utf-8"))
    reports = json.loads((data_dir / "reports.json").read_text(encoding="utf-8"))
    taxonomy = json.loads((data_dir / "taxonomy.json").read_text(encoding="utf-8"))
    return {q["_id"]: q for q in questions}, reports, taxonomy


def view(q: dict) -> dict:
    """Flat, type-agnostic view of one question."""
    qtype = q["questionType"]
    sub = q.get(qtype) or {}
    return {
        "id": q["_id"],
        "questionType": qtype,
        "subject": q.get("subject"),
        "chapter": q.get("chapter"),
        "question": sub.get("question") or "",
        "solution": sub.get("solution") or "",
        "answer": sub.get("answer"),
        "quesImages": sub.get("quesImages") or [],
        "solutionImages": sub.get("solutionImages") or [],
        "errorHistory": q.get("errorHistory") or [],
    }


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def to_text(fragment: str) -> str:
    """HTML+MathML -> readable plain text (for scanners and for the LLM)."""
    s = fragment or ""
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<mspace[^>]*linebreak[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<mfrac>", " frac( ", s)
    s = re.sub(r"<msqrt>", " sqrt( ", s)
    s = re.sub(r"</(mfrac|msqrt)>", " ) ", s)
    s = re.sub(r"<msup>", " ", s)
    s = re.sub(r"</mrow>\s*<mrow>", " | ", s)
    s = _TAG.sub("", s)
    s = html.unescape(s).replace(" ", " ").replace("​", "")
    lines = [_WS.sub(" ", line).strip() for line in s.split("\n")]
    return "\n".join(line for line in lines if line)
