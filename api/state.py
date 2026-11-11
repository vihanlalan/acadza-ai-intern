"""Vercel Python function. All routes share one handler; see engine/api.py.

Vercel only deploys a file as a function if it defines `handler` (or `app`) itself, so the class is
declared here rather than assigned from a factory.
"""
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.api import ApiHandler  # noqa: E402


class handler(ApiHandler, BaseHTTPRequestHandler):
    pass
