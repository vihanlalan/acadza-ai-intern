"""Shared queue: store compare-and-swap, API validation, patched bank.  python -m unittest engine.test_shared"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from . import api
from .bank import build_bank
from .store import MemoryStore, SqliteStore

try:
    import sqlite3  # noqa: F401
    HAVE_SQLITE = True
except ImportError:  # e.g. some serverless build images
    HAVE_SQLITE = False


def call(method, route, body=None, headers=None):
    status, _, payload = api.handle(method, f"/api/{route}", {}, headers or {},
                                    json.dumps(body).encode() if body is not None else b"")
    return status, payload


@unittest.skipUnless(HAVE_SQLITE, "sqlite3 not available")
class Sqlite(unittest.TestCase):
    def test_compare_and_swap(self):
        with tempfile.TemporaryDirectory() as d:
            st = SqliteStore(Path(d) / "t.db")
            dec = {"action": "approved", "at": "t1", "by": "A"}
            self.assertEqual(st.swap("x", "", dec, {"a": 1}), (True, dec))
            self.assertEqual(st.swap("x", "", {"action": "rejected", "at": "t2", "by": "B"}, {"a": 2}), (False, dec))
            self.assertTrue(st.swap("x", "t1", None, {"a": 3})[0])
            self.assertEqual(st.snapshot(), {"decisions": {}, "log": [{"a": 1}, {"a": 3}]})


class SharedQueue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        api.STORE = MemoryStore()
        self.items = api.ctx()["items"]
        self.auto = next(i for i in self.items.values() if i["route"] == "auto_apply" and i["kind"] == "reported"
                         and isinstance(i["edit"], dict) and i["edit"]["field"] in ("question", "solution"))
        self.review = next(i for i in self.items.values() if i["route"] == "faculty_review" and (i["edit"] or i["suggestion"]))

    def tearDown(self):
        api.STORE = None
        os.environ.pop("RF_FACULTY_KEY", None)
        self.tmp.cleanup()

    def test_decide_then_second_reviewer_conflicts(self):
        s, p = call("POST", "decide", {"itemId": self.auto["id"], "action": "approved", "by": "Asha"})
        self.assertEqual(s, 200)
        s, p = call("POST", "decide", {"itemId": self.auto["id"], "action": "rejected", "by": "Ravi"})
        self.assertEqual(s, 409)
        self.assertEqual(p["current"]["by"], "Asha")
        state = call("GET", "state")[1]
        self.assertEqual(state["decisions"][self.auto["id"]]["action"], "approved")
        self.assertEqual(len(state["log"]), 1)

    def test_undo_needs_the_current_decision(self):
        _, p = call("POST", "decide", {"itemId": self.auto["id"], "action": "approved"})
        self.assertEqual(call("POST", "undo", {"itemId": self.auto["id"], "expect": "2000-01-01T00:00:00.000Z"})[0], 409)
        self.assertEqual(call("POST", "undo", {"itemId": self.auto["id"], "expect": p["decision"]["at"]})[0], 200)
        state = call("GET", "state")[1]
        self.assertNotIn(self.auto["id"], state["decisions"])
        self.assertEqual([e["action"] for e in state["log"]], ["approved", "undo"])  # append-only

    def test_server_rejects_bad_edits(self):
        it = self.auto
        e = dict(it["edit"])
        bad = [({**e, "find": e["find"] + "zz-not-there"}, 422),   # find must match exactly once
               ({**e, "replace": '<img src=x onerror="alert(1)">'}, 422),  # no handlers
               ({**e, "field": "questionType"}, 422)]             # outside the repair's fields
        for edit, code in bad:
            self.assertEqual(call("POST", "decide", {"itemId": it["id"], "action": "edited", "edits": [edit]})[0], code, edit)
        self.assertEqual(call("POST", "decide", {"itemId": "nope", "action": "approved"})[0], 404)
        self.assertEqual(call("POST", "decide", {"itemId": it["id"], "action": "delete"})[0], 400)

    def test_faculty_key(self):
        os.environ["RF_FACULTY_KEY"] = "k1"
        body = {"itemId": self.auto["id"], "action": "approved"}
        self.assertEqual(call("POST", "decide", body)[0], 401)
        self.assertEqual(call("POST", "decide", body, {"X-Faculty-Key": "wrong"})[0], 401)
        self.assertEqual(call("POST", "decide", body, {"X-Faculty-Key": "k1"})[0], 200)

    def test_bank_applies_auto_and_approved_only(self):
        qs = api.ctx()["questions"]
        items = list(self.items.values())
        base = build_bank(qs, items, {})
        ids = {c["itemId"] for c in base["changes"]}
        self.assertIn(self.auto["id"], ids)               # auto fix live by default
        self.assertNotIn(self.review["id"], ids)          # human item waits
        call("POST", "decide", {"itemId": self.auto["id"], "action": "rejected"})       # roll back
        call("POST", "decide", {"itemId": self.review["id"], "action": "approved"})
        bank = call("GET", "bank")[1]
        ids = {c["itemId"] for c in bank["changes"]}
        self.assertNotIn(self.auto["id"], ids)
        self.assertIn(self.review["id"], ids)
        q = next(q for q in bank["changed"] if q["_id"] == self.review["questionId"])
        self.assertEqual(q["errorHistory"][-1]["actor"], "faculty")
        self.assertIn(q["questionType"], q)               # production shape kept

    def test_no_database_means_local_mode(self):
        api.STORE = None
        os.environ["VERCEL"] = "1"
        try:
            for k in ("UPSTASH_REDIS_REST_URL", "KV_REST_API_URL"):
                os.environ.pop(k, None)
            s, p = call("GET", "state")
            self.assertEqual((s, p["shared"]), (503, False))
        finally:
            os.environ.pop("VERCEL")


if __name__ == "__main__":
    unittest.main()
