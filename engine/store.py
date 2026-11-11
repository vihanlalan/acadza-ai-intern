"""Shared decision store.

Two backends behind one interface:
  - RedisRestStore: Upstash Redis over its REST API (what Vercel's marketplace provisions). stdlib only.
  - SqliteStore: a local file, for `python -m engine.server`.

Every write is a compare-and-swap on one item: a decision only lands if the item is in the state the
reviewer saw (undecided, or decided at a specific time for an undo). Two faculty acting on the same item
at once can't overwrite each other; the second one gets the first one's decision back. The log is
append-only and written in the same transaction as the decision.
"""
from __future__ import annotations

import json
import os
import urllib.request
from contextlib import closing
from pathlib import Path

LOG_LIMIT = 500  # the console shows recent history; the full log stays in the store


def get_store():
    url = os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN")
    if url and token:
        return RedisRestStore(url, token, os.environ.get("RF_DB_PREFIX", "rf:"))
    if os.environ.get("VERCEL"):
        return None  # serverless disk is wiped between requests: no database configured
    from .data import ROOT
    return SqliteStore(os.environ.get("RF_DB") or ROOT / ".data" / "faculty.db")


class SqliteStore:
    name = "sqlite"

    def __init__(self, path):
        import sqlite3  # imported here: some serverless Python builds ship without it
        self._sqlite = sqlite3
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db:
            db.executescript("""
                create table if not exists decisions (item_id text primary key, at text not null, body text not null);
                create table if not exists log (seq integer primary key autoincrement, body text not null);""")

    def _db(self):
        return self._sqlite.connect(self.path, timeout=10, isolation_level=None)

    def snapshot(self) -> dict:
        with closing(self._db()) as db:
            decisions = {k: json.loads(b) for k, b in db.execute("select item_id, body from decisions")}
            log = [json.loads(b) for (b,) in db.execute(
                "select body from (select seq, body from log order by seq desc limit ?) order by seq", (LOG_LIMIT,))]
        return {"decisions": decisions, "log": log}

    def swap(self, item_id: str, expect_at: str, new: dict | None, entry: dict):
        with closing(self._db()) as db:
            db.execute("begin immediate")
            try:
                row = db.execute("select at, body from decisions where item_id = ?", (item_id,)).fetchone()
                if (row[0] if row else "") != (expect_at or ""):
                    db.execute("rollback")
                    return False, json.loads(row[1]) if row else None
                if new is None:
                    db.execute("delete from decisions where item_id = ?", (item_id,))
                else:
                    db.execute("insert or replace into decisions values (?, ?, ?)",
                               (item_id, new["at"], json.dumps(new, ensure_ascii=False)))
                db.execute("insert into log (body) values (?)", (json.dumps(entry, ensure_ascii=False),))
                db.execute("commit")
                return True, new
            except BaseException:
                if db.in_transaction:
                    db.execute("rollback")
                raise


class MemoryStore:
    """Same contract, in process memory. Used by the tests (and handy for a throwaway demo)."""
    name = "memory"

    def __init__(self):
        self.decisions, self.log = {}, []

    def snapshot(self) -> dict:
        return {"decisions": json.loads(json.dumps(self.decisions)), "log": json.loads(json.dumps(self.log[-LOG_LIMIT:]))}

    def swap(self, item_id: str, expect_at: str, new: dict | None, entry: dict):
        cur = self.decisions.get(item_id)
        if (cur["at"] if cur else "") != (expect_at or ""):
            return False, cur
        if new is None:
            self.decisions.pop(item_id, None)
        else:
            self.decisions[item_id] = new
        self.log.append(entry)
        return True, new


class RedisRestStore:
    name = "redis"
    # KEYS: at-hash, decision-hash, log list. ARGV: item, expected at, new json ('' = delete), new at, log entry
    SWAP = """
local cur = redis.call('HGET', KEYS[1], ARGV[1]) or ''
if cur ~= ARGV[2] then return {0, redis.call('HGET', KEYS[2], ARGV[1]) or ''} end
if ARGV[3] == '' then
  redis.call('HDEL', KEYS[1], ARGV[1]); redis.call('HDEL', KEYS[2], ARGV[1])
else
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[4]); redis.call('HSET', KEYS[2], ARGV[1], ARGV[3])
end
redis.call('RPUSH', KEYS[3], ARGV[5])
return {1, ''}"""

    def __init__(self, url, token, prefix="rf:"):
        self.url, self.token = url.rstrip("/"), token
        self.k_at, self.k_dec, self.k_log = prefix + "at", prefix + "dec", prefix + "log"

    def _post(self, path, payload):
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def _cmd(self, *args):
        out = self._post("", list(args))
        if out.get("error"):
            raise RuntimeError(out["error"])
        return out["result"]

    def snapshot(self) -> dict:
        res = self._post("/pipeline", [["HGETALL", self.k_dec], ["LRANGE", self.k_log, -LOG_LIMIT, -1]])
        for r in res:
            if r.get("error"):
                raise RuntimeError(r["error"])
        flat = res[0]["result"] or []
        decisions = {flat[i]: json.loads(flat[i + 1]) for i in range(0, len(flat), 2)}
        return {"decisions": decisions, "log": [json.loads(x) for x in res[1]["result"] or []]}

    def swap(self, item_id: str, expect_at: str, new: dict | None, entry: dict):
        ok, cur = self._cmd("EVAL", self.SWAP, "3", self.k_at, self.k_dec, self.k_log, item_id, expect_at or "",
                            json.dumps(new, ensure_ascii=False) if new else "", new["at"] if new else "",
                            json.dumps(entry, ensure_ascii=False))
        if int(ok) == 1:
            return True, new
        return False, json.loads(cur) if cur else None
