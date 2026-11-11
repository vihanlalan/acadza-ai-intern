"""LLM access with a per-question cache.

PROVIDER NOTE
  Default provider is **Groq** (open-weight models, free tier). This is purely
  a cost decision for the assignment - I can't fund paid Claude API keys right
  now. The production target is the Claude API: set LLM_PROVIDER=anthropic and
  the same prompts, schemas and guards run on claude-opus-5-5. Nothing else
  in the pipeline changes.

CACHE
  Every stage output is cached in engine/cache/<stage>.json keyed by question
  id, together with the prompt version and the model that produced it. The
  console and results.json are built from the cache, so the live URL loads
  instantly and costs nothing per view. `--live` re-runs a stage and
  overwrites its entry.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from .prompts import PROMPT_VERSION

CACHE_DIR = Path(os.environ.get("RF_CACHE_DIR") or Path(__file__).resolve().parent / "cache")

PROVIDER = os.environ.get("LLM_PROVIDER", "groq").lower()
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# Override with GROQ_SOLVER_MODEL / GROQ_AUDITOR_MODEL if Groq's catalogue changes.
GROQ_MODELS = {
    "solver": os.environ.get("GROQ_SOLVER_MODEL", "openai/gpt-oss-120b"),
    "auditor": os.environ.get("GROQ_AUDITOR_MODEL", "openai/gpt-oss-120b"),
}
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = {"solver": "high", "auditor": "medium"}


class LLMUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ cache ---

def _cache_path(stage: str) -> Path:
    return CACHE_DIR / f"{stage}.json"


def load_cache(stage: str) -> dict:
    p = _cache_path(stage)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_cache(stage: str, data: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    _cache_path(stage).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def run_stage(stage: str, key: str, system: str, user: str, schema: dict, live: bool) -> dict | None:
    cache = load_cache(stage)
    if not live and key in cache:
        return cache[key]["output"]
    if not live:
        return None
    t0 = time.time()
    try:
        out, model = call_json(stage, system, user, schema)
    except LLMUnavailable:
        if key in cache:  # never let a failed live call wipe a good cached result
            print(f"  ! {stage}/{key}: live call failed, keeping cached output", flush=True)
            return cache[key]["output"]
        raise
    cache[key] = {"output": out, "meta": {"model": model, "provider": PROVIDER, "prompt_version": PROMPT_VERSION,
                                          "seconds": round(time.time() - t0, 1)}}
    save_cache(stage, cache)
    return out


# -------------------------------------------------------------- providers ---

def call_json(stage: str, system: str, user: str, schema: dict) -> tuple[dict, str]:
    if PROVIDER == "anthropic":
        return _call_claude(stage, system, user, schema)
    return _call_groq(stage, system, user, schema)


def _call_groq(stage, system, user, schema):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise LLMUnavailable("GROQ_API_KEY is not set")
    model = GROQ_MODELS[stage]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system + "\n\nJSON schema to follow exactly:\n" + json.dumps(schema)},
            {"role": "user", "content": user},
        ],
        # json_object works across Groq's models; we validate the shape ourselves.
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 8000,
    }
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "high" if stage == "solver" else "medium"
    req = urllib.request.Request(GROQ_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "acadza-report-fix/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
            return _parse(data["choices"][0]["message"]["content"], schema), model
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:  # free-tier rate limits
                time.sleep(float(e.headers.get("retry-after") or 2 ** (attempt + 2)))
                continue
            raise LLMUnavailable(f"Groq HTTP {e.code}: {e.read()[:300]!r}") from e
    raise LLMUnavailable("Groq retries exhausted")


def _call_claude(stage, system, user, schema):
    """Production path. Same prompts; structured outputs enforce the schema."""
    import anthropic  # imported lazily so the Groq path needs no dependencies
    client = anthropic.Anthropic()
    resp = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": CLAUDE_EFFORT[stage], "format": {"type": "json_schema", "schema": schema}},
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if resp.stop_reason == "refusal":
        raise LLMUnavailable("Claude declined the request")
    text = next(b.text for b in resp.content if b.type == "text")
    return _parse(text, schema), resp.model


def _parse(text: str, schema: dict) -> dict:
    out = json.loads(text)
    missing = [k for k in schema.get("required", []) if k not in out]
    if missing:
        raise LLMUnavailable(f"model output missing keys {missing}")
    return out
