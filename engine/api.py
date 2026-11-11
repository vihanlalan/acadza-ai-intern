"""HTTP API for the shared faculty queue. Used by the Vercel functions in api/ and by engine/server.py.

  GET  /api/state    decisions + recent audit log (503 if no database is configured: the console then
                     falls back to saving in the browser)
  POST /api/decide   {itemId, action: approved|edited|rejected, by, edits?, replies?}
  POST /api/undo     {itemId, by, expect: <at of the decision being undone>}
  GET  /api/bank     questions changed by the reviewed fixes, in production document shape (?download=1)

The browser is not trusted. The server rebuilds every log entry itself, checks the item exists, keeps a
faculty edit inside the repair's allowed fields, re-applies it exactly-once against the stored record, and
refuses scripts. If RF_FACULTY_KEY is set, writes need it in the X-Faculty-Key header.
"""
from __future__ import annotations

import hmac
import json
import os
import re
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from . import store as store_mod
from .bank import ACTIONS, _now, build_bank
from .data import load_all, view
from .guards import apply_edits, listify

MAX_BODY = 64 * 1024
UNSAFE = re.compile(r"<\s*(script|iframe|object|embed)|\son\w+\s*=|javascript:", re.I)

_CTX = None
STORE = None  # tests inject one


def ctx():
    global _CTX
    if _CTX is None:
        from .pipeline import build
        questions, _, taxonomy = load_all()
        items = {it["id"]: it for c in build()["cases"] for it in c["items"]}
        _CTX = {"questions": questions, "taxonomy": taxonomy, "items": items}
    return _CTX


def _store():
    return STORE if STORE is not None else store_mod.get_store()


class ApiError(Exception):
    def __init__(self, status, msg, **extra):
        super().__init__(msg)
        self.status, self.body = status, {"error": msg, **extra}


def handle(method: str, path: str, query: dict, headers, body: bytes):
    """-> (status, extra_headers, payload). payload is JSON-serialisable."""
    try:
        route = path.rstrip("/").rsplit("/api/", 1)[-1]
        st = _store()
        if st is None:
            raise ApiError(503, "no database configured", shared=False)
        if route == "state" and method == "GET":
            snap = st.snapshot()
            return 200, {}, {"shared": True, "backend": st.name, "writeKey": bool(os.environ.get("RF_FACULTY_KEY")), **snap}
        if route == "bank" and method == "GET":
            bank = build_bank(ctx()["questions"], list(ctx()["items"].values()), st.snapshot()["decisions"])
            extra = {"Content-Disposition": f'attachment; filename="questions-patched-{bank["generatedAt"][:10]}.json"'} \
                if query.get("download") else {}
            return 200, extra, bank
        if route in ("decide", "undo") and method == "POST":
            _auth(headers)
            if len(body) > MAX_BODY:
                raise ApiError(413, "request too large")
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                raise ApiError(400, "body is not JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "body must be an object")
            return (_decide if route == "decide" else _undo)(st, data)
        raise ApiError(404, f"no route {method} /api/{route}")
    except ApiError as e:
        return e.status, {}, e.body


def _auth(headers):
    key = os.environ.get("RF_FACULTY_KEY")
    if key and not hmac.compare_digest((headers.get("X-Faculty-Key") or "").encode(), key.encode()):
        raise ApiError(401, "faculty key required", needKey=True)


def _item(data):
    it = ctx()["items"].get(str(data.get("itemId")))
    if not it:
        raise ApiError(404, "unknown item")
    return it


def _by(data):
    return (str(data.get("by") or "").strip() or "Faculty")[:40]


def _decide(st, data):
    it = _item(data)
    action = data.get("action")
    if action not in ACTIONS:
        raise ApiError(400, f"action must be one of {', '.join(ACTIONS)}")
    edits = _check_edits(it, data.get("edits")) if action == "edited" else None
    replies = _check_replies(it, data.get("replies")) if action == "edited" else None
    at, by = _now(), _by(data)
    dec = {"action": action, "at": at, "by": by}
    if edits is not None:
        dec["edits"] = edits
    if replies:
        dec["replies"] = replies
    entry = {"at": at, "actor": by, "action": action, "questionId": it["questionId"], "itemId": it["id"],
             "reportIds": it["reportIds"], "verdict": it["verdict"], "repair": it["repair"], "aiRoute": it["route"],
             "edit": edits if edits is not None else it["edit"]}
    if replies:
        entry["replies"] = replies
    if action == "rejected":
        entry["note"] = "Rolled back" if it["route"] == "auto_apply" and it["kind"] == "reported" else "Rejected"
    ok, cur = st.swap(it["id"], "", dec, entry)
    if not ok:
        raise ApiError(409, f"{cur['by']} already decided this", current=cur)
    return 200, {}, {"decision": dec, "entry": entry}


def _undo(st, data):
    it = _item(data)
    expect = str(data.get("expect") or "")
    if not expect:
        raise ApiError(400, "expect (the decision's time) is required")
    by = _by(data)
    cur = st.snapshot()["decisions"].get(it["id"])
    entry = {"at": _now(), "actor": by, "action": "undo", "questionId": it["questionId"], "itemId": it["id"],
             "undid": (cur or {}).get("action")}
    ok, cur = st.swap(it["id"], expect, None, entry)
    if not ok:
        raise ApiError(409, "this decision changed since you loaded it", current=cur)
    return 200, {}, {"entry": entry}


def _check_edits(it, edits):
    if edits is None:
        return None
    edits = listify(edits)
    if not all(isinstance(e, dict) and set(e) == {"field", "find", "replace"} for e in edits):
        raise ApiError(422, "each edit must be {field, find, replace}")
    tax = ctx()["taxonomy"]["repairs"].get(it["repair"] or "")
    for e in edits:
        if not tax or e["field"] not in tax["fields"]:
            raise ApiError(422, f"'{e['field']}' can't be changed by a {it['repair']} repair",
                           allowed=tax["fields"] if tax else [])
        if e["field"] in ("question", "solution"):
            if not isinstance(e["find"], str) or not isinstance(e["replace"], str) or not e["find"]:
                raise ApiError(422, "find and replace must be non-empty text")
            if UNSAFE.search(e["replace"]):
                raise ApiError(422, "scripts and event handlers are not allowed")
    try:
        apply_edits(view(ctx()["questions"][it["questionId"]]), edits)
    except ValueError as e:
        raise ApiError(422, str(e))
    return edits


def _check_replies(it, replies):
    if not replies:
        return None
    if not isinstance(replies, dict) or not set(replies) <= set(it["reportIds"]):
        raise ApiError(422, "replies must be keyed by this item's report ids")
    if not all(isinstance(v, str) and len(v) <= 2000 for v in replies.values()):
        raise ApiError(422, "replies must be text under 2000 characters")
    return replies


class ApiHandler(BaseHTTPRequestHandler):
    """Request handler for Vercel's Python runtime (api/*.py subclass it as `handler`) and the local server."""

    def do_GET(self):
        self._api("GET")

    def do_POST(self):
        self._api("POST")

    def _api(self, method):
        u = urlsplit(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, MAX_BODY + 1)) if n else b""
        try:
            status, extra, payload = handle(method, u.path, parse_qs(u.query), self.headers, body)
        except Exception as e:  # never leak a stack trace to the browser
            status, extra, payload = 500, {}, {"error": f"server error: {type(e).__name__}"}
        out = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def make_handler():
    return ApiHandler
