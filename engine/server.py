"""Local server: the console plus the shared-queue API, backed by SQLite (.data/faculty.db).

    python -m engine.server            # http://localhost:8000
    python -m engine.server --port 9000

Open it in two browsers to see two reviewers sharing one queue.
"""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from . import pipeline
from .api import make_handler
from .data import ROOT

Api = make_handler()


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/"):
            return Api._api(self, "GET")
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            return Api._api(self, "POST")
        self.send_error(405)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    pipeline.run()  # make sure console/data.js exists and matches the cache
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), partial(Handler, directory=str(ROOT / "console")))
    print(f"console + shared queue on http://localhost:{a.port}  (decisions in .data/faculty.db)")
    srv.serve_forever()
