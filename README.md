# Report → Fix

A triage engine and faculty console for student error reports on JEE/NEET questions. The original brief is in [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md).

- **Live console:** `[add your deployed URL]`
- **Video walkthrough (3 min):** `[add your video link]`
- **Results:** [results.json](results.json) has 44 reports. [unreported_findings.json](unreported_findings.json) has 22 defects across 18 questions that no student reported.
- **How I used AI:** [PROMPTS.md](PROMPTS.md). **Product thinking:** [PRODUCT.md](PRODUCT.md).

> **LLM provider.** The engine is set up for **Groq** (free tier, `openai/gpt-oss-120b`) only because I can't pay for Claude API keys right now. For real use it switches to the Claude API with one environment variable (`LLM_PROVIDER=anthropic`, model `claude-opus-5-5`); the prompts, schemas and safety gate stay the same. The committed results come from cached stage outputs. PROMPTS.md says exactly how those were produced.

## What it does

```
student reports ─► group by question ─► scanners (no AI) ─► blind solver ─► auditor ─► safety gate ─► results / console
```

1. **Group** 44 reports into 39 questions, so one question gets one solve, then a separate reply for each student.
2. **Scan** every question with plain code. The scanners catch blank, missing and duplicate options, malformed keys (`["", "B"]`, `["A, B, D"]`), raw LaTeX, stray `&` markers, exam tags, figure references with no image, prompt-injection and keyboard-mash comments, and reports that a faculty edit already fixed.
3. **Solve blind.** The solver sees only the question: never the key, the stored solution or the student's comment.
4. **Audit.** The auditor picks a verdict and repair from the taxonomy and proposes a surgical `{field, find, replace}`. Student comments reach it fenced off as untrusted data.
5. **Gate** (`engine/guards.py`, plain code). It enforces the repair's field scope and an exact single match for `find`. It re-scans the edited record and limits edit size. An answer-key change needs the blind solve *and* the stored solution's conclusion to agree. Adversarial comments can never trigger a write. Human-only repairs are never written. The gate can only downgrade a route.

**Current routing:** 17 auto-applied, 10 to faculty, 17 no change.

## Run it

Needs Python 3.10+ and nothing else (standard library only).

```bash
python -m engine.pipeline                 # rebuild results.json, unreported_findings.json, console data from the cache
python -m unittest engine.test_guards -v  # 16 tests: gate scope, exact-match, key consensus, injection, scanners, results schema

# re-run the LLM stages (overwrites engine/cache/*.json)
GROQ_API_KEY=... python -m engine.pipeline --live
GROQ_API_KEY=... python -m engine.pipeline --live --only 60f744b3f1b9bc7cc7b4a949
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... python -m engine.pipeline --live   # production path
```

Open the console: after running the pipeline once, `console/index.html` works straight from disk, or run `python -m http.server -d console 8000`.

## Faculty console

- **Four queues:**
  - *Needs you*: only a human can unblock these.
  - *Auto-applied*: spot-check, roll back with one key.
  - *Found by AI*: nobody reported these.
  - *Closed*: student replies are ready.
- **Order:** each queue is sorted by harm to students: severity × (1 + ln(1 + reports)). The formula is explained in the sidebar.
- **What a case shows:**
  - Questions render in native MathML, with a before/after diff highlighted in the rendered math. Press `S` for the raw source diff.
  - The safety-gate checks appear as badges.
  - A three-way check: blind solve vs stored solution vs stored key vs what the student said.
  - Each student's reply sits next to their comment, and you can edit it.
- **Keyboard:** `A` approve, `R` reject or roll back, `E` edit, `J`/`K` move between questions, `U` undo.
- **Audit log:** every decision is recorded. Undo adds an entry rather than deleting one, and the log exports as JSON. Decisions are stored in the browser for this demo.

## Layout

```
engine/   data.py · scanners.py · prompts.py · llm.py · guards.py · pipeline.py · test_guards.py · cache/
console/  index.html · style.css · app.js · data.js (generated)
data/     questions.json · reports.json · taxonomy.json (provided)
```

## Deploy

The deploy runs a build step ([vercel.json](vercel.json), [netlify.toml](netlify.toml)):

```bash
python3 -m engine.pipeline --check && python3 -m unittest engine.test_guards
```

1. The pipeline regenerates the console data (`console/data.js`, which is not committed) from `data/` and the cached stage outputs.
2. `--check` fails the deploy if the committed `results.json` differs from what the code produces. That way the scored file and the live console can never drift apart.
3. The 17 gate and scanner tests must pass.

The site is then served from `console/`. On Vercel, keep the project's Root Directory at the repo root, and the settings in `vercel.json` apply.
