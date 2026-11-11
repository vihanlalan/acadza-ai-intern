"""Vercel Python function. All routes share one handler; see engine/api.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.api import make_handler  # noqa: E402

handler = make_handler()
