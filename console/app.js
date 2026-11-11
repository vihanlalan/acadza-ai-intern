/* Report → Fix — faculty console. No build step, no framework.
 * Data: window.RF_DATA (console/data.js, written by `python -m engine.pipeline`).
 * State (decisions + append-only audit log) lives in localStorage.
 */
(() => {
  "use strict";
  const D = window.RF_DATA;
  const TAX = D.taxonomy;
  const LS_KEY = "rf.console.v1";

  // ------------------------------------------------------------ state ---
  const store = loadState();
  const ui = { lane: "review", subject: "All", q: "", current: null, focus: null, view: "rendered", editing: null, undo: [] };

  function loadState() {
    try {
      const s = JSON.parse(localStorage.getItem(LS_KEY) || "null");
      if (s && s.decisions && s.log) return s;
    } catch (_) { /* private mode etc. */ }
    return { decisions: {}, log: [], reviewer: "Faculty", theme: null };
  }
  function saveState() { try { localStorage.setItem(LS_KEY, JSON.stringify(store)); } catch (_) { /* ignore */ } }

  // ------------------------------------------------------------ model ---
  const listify = (e) => (e == null ? [] : Array.isArray(e) ? e : [e]);

  const CASES = D.cases.map((c) => {
    const items = [];
    const groups = new Map();
    for (const r of c.reports) {
      const k = [r.verdict, r.repair, r.route, JSON.stringify(r.edit)].join("|");
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    }
    let i = 0;
    for (const reps of groups.values()) {
      const r = reps[0];
      items.push({ id: `${c.questionId}:R${i++}`, kind: "reported", reports: reps, verdict: r.verdict, repair: r.repair,
        route: r.route, edit: r.edit, suggestion: r.suggestion, confidence: r.confidence, evidence: r.evidence, checks: r.checks });
    }
    for (const f of c.secondary) {
      items.push({ id: f.findingId, kind: "finding", reports: [], verdict: "defect", repair: f.repair, route: f.route,
        edit: f.edit, suggestion: f.suggestion, confidence: f.confidence, evidence: f.evidence, checks: f.checks });
    }
    const reported = items.filter((x) => x.kind === "reported");
    let lane = "closed";
    if (reported.some((x) => x.verdict === "defect" && x.route === "faculty_review")) lane = "review";
    else if (reported.some((x) => x.verdict === "defect")) lane = "auto";
    else if (items.some((x) => x.kind === "finding")) lane = "found";
    return { ...c, items, lane };
  });
  const BY_ID = Object.fromEntries(CASES.map((c) => [c.questionId, c]));
  const ITEMS = Object.fromEntries(CASES.flatMap((c) => c.items.map((it) => [it.id, { item: it, c }])));

  const LANES = [
    { id: "review", label: "Needs you", long: "Needs your decision", tip: "Confirmed defects a human must decide: no-auto repairs, low-consensus fixes, unrecoverable content." },
    { id: "auto", label: "Auto-applied", long: "Auto-applied · spot check", tip: "Fixes that passed every gate and were applied. Spot-check and roll back in one key." },
    { id: "found", label: "Found by AI", long: "Found by AI · unreported", tip: "Defects nobody reported, found by the proactive audit, plus side-findings on reported questions." },
    { id: "closed", label: "Closed", long: "Closed · no change", tip: "Student was wrong, app/display issues, already fixed, unintelligible. Replies ready to send." },
  ];

  const VERDICT_TITLE = {
    no_error: "Question is correct; the student slipped",
    display_issue: "Display problem; content is fine",
    app_issue: "App problem, not this question",
    language_mode: "Asks for another language",
    already_fixed: "Already fixed by faculty",
    not_actionable: "Nothing to act on",
  };
  const REPAIR_TITLE = {
    answer_key: "Answer key is wrong", type_mismatch: "Stored as the wrong question type", unsound: "No option is correct as written",
    stem_data: "A value is missing from the question", option_repair: "An option is blank or missing", wrong_question: "Solution belongs to another question",
    diagram: "Figure is missing", identical_options: "Two options are identical", solution_fix: "Solution has a wrong step",
    solution_missing: "Solution missing", answer_form: "Unit / answer form not stated", notation: "Symbol or notation garbled",
    markup_leak: "Raw code visible to students", wrong_chapter: "Filed under the wrong chapter", syllabus: "Possibly outside the syllabus",
    spelling: "Spelling / wording", noise: "Stray text or markup", exam_reference: "Exam tag inside the question",
  };

  function laneOfItem(it) {
    if (it.verdict !== "defect") return "closed";
    if (it.route === "faculty_review") return "review";
    return it.kind === "finding" ? "found" : "auto";
  }
  function headline(it) {
    if (it.verdict !== "defect") return VERDICT_TITLE[it.verdict] || it.verdict;
    let t = REPAIR_TITLE[it.repair] || it.repair || "Defect";
    const e = listify(it.edit).find((x) => x.field === "answer");
    if (it.repair === "answer_key" && e) {
      const lf = letters(e.find), lr = letters(e.replace);
      const same = lf.length > 0 && JSON.stringify(lf) === JSON.stringify(lr); // letters unchanged = format-only fix
      t = same ? "Answer key stored in a broken format" : `Answer key is wrong: ${fmtAns(e.find)} → ${fmtAns(e.replace)}`;
    }
    return t;
  }
  function letters(a) {
    const arr = Array.isArray(a) ? a : [a];
    return arr.flatMap((x) => String(x).split(/[\s,]+/)).filter((x) => /^[A-D]$/.test(x)).sort();
  }
  const fmtAns = (a) => (Array.isArray(a) ? `[${a.map((x) => JSON.stringify(x)).join(", ")}]` : String(a));
  const isPending = (it) => !store.decisions[it.id];
  const pendingIn = (c) => c.items.filter(isPending);

  // ------------------------------------------------------------ html helpers ---
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const $ = (sel, root = document) => root.querySelector(sel);

  function sanitize(html) {
    return html
      .replace(/<script[\s\S]*?<\/script>/gi, "")
      .replace(/\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi, "")
      .replace(/\sstyle\s*=\s*("[^"]*"|'[^']*')/gi, "")
      .replace(/\s(mathcolor|color)\s*=\s*("[^"]*"|'[^']*')/gi, "")
      .replace(/[​‌‍]/g, "");
  }

  // ------------------------------------------------------------ diff engine ---
  // Tokens: whole tags, whole entities, single characters.
  const TOK = /<[^>]+>|&[#\w]+;|[\s\S]/g;
  const toks = (s) => s.match(TOK) || [];
  const VOID = new Set(["br", "img", "hr", "input", "meta", "link", "col", "wbr", "none", "mprescripts"]);
  const TOKEN_EL = new Set(["mi", "mn", "mo", "mtext", "ms"]);

  /** smallest differing token span between find and replace (char offsets) */
  function core(find, rep) {
    const a = toks(find), b = toks(rep);
    let p = 0;
    while (p < a.length && p < b.length && a[p] === b[p]) p++;
    let s = 0;
    while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++;
    const L = (t) => t.join("").length;
    return { fa: L(a.slice(0, p)), fb: L(a.slice(0, a.length - s)), ra: L(b.slice(0, p)), rb: L(b.slice(0, b.length - s)) };
  }

  function tagIter(html, from = 0, to = html.length) {
    const re = /<(\/?)([a-zA-Z][\w-]*)[^>]*?(\/?)>/g;
    re.lastIndex = from;
    const out = [];
    let m;
    while ((m = re.exec(html)) && m.index < to) {
      out.push({ at: m.index, end: m.index + m[0].length, close: !!m[1], name: m[2].toLowerCase(), self: !!m[3] || VOID.has(m[2].toLowerCase()) });
    }
    return out;
  }
  function stackAt(html, pos) {
    const st = [];
    for (const t of tagIter(html, 0, pos)) {
      if (t.end > pos) break;
      if (t.self) continue;
      if (t.close) { const i = st.map((x) => x.name).lastIndexOf(t.name); if (i >= 0) st.length = i; }
      else st.push(t);
    }
    return st;
  }
  function closeOf(html, open) {
    let depth = 0;
    for (const t of tagIter(html, open.at)) {
      if (t.name !== open.name || t.self) continue;
      depth += t.close ? -1 : 1;
      if (depth === 0) return t.end;
    }
    return html.length;
  }
  function balanced(frag) {
    const st = [];
    for (const t of tagIter(frag)) {
      if (t.self) continue;
      if (!t.close) st.push(t.name);
      else if (st.pop() !== t.name) return false;
    }
    return st.length === 0;
  }

  /** Decide how to visually wrap [a,b) of html without breaking the markup. */
  function markFor(html, a, b, cls) {
    const st = stackAt(html, a);
    const mi = st.findIndex((t) => t.name === "math");
    const gapLabel = cls === "d-del" ? "added here" : "removed here";
    if (a === b) return mi >= 0 ? null : { a, b, open: `<span class="d-gap" data-label="${gapLabel}" title="text ${gapLabel}"></span>`, close: "" };
    let frag = html.slice(a, b);
    if (mi >= 0) {
      const top = st[st.length - 1];
      if (!/[<>]/.test(frag) && top && TOKEN_EL.has(top.name)) { a = top.at; b = closeOf(html, top); frag = html.slice(a, b); }
      if (!balanced(frag)) { const m = st[mi]; return { a: m.at, b: closeOf(html, m), open: `<span class="${cls}">`, close: "</span>" }; }
      return { a, b, open: `<mrow class="${cls}">`, close: "</mrow>" };
    }
    if (!balanced(frag)) return null;
    return { a, b, open: `<span class="${cls}">`, close: "</span>" };
  }
  function applyMarks(html, marks) {
    const keep = [];
    for (const m of marks.filter(Boolean).sort((x, y) => x.a - y.a)) {
      const last = keep[keep.length - 1];
      if (last && m.a < last.b) continue; // overlapping after snapping - keep the first
      keep.push(m);
    }
    for (const m of keep.reverse()) html = html.slice(0, m.a) + m.open + html.slice(m.a, m.b) + m.close + html.slice(m.b);
    return html;
  }

  /** before/after HTML (with highlights) for one field and a list of text edits */
  function fieldDiff(field, edits) {
    const placed = edits.map((e) => ({ e, idx: field.indexOf(e.find), n: countOcc(field, e.find) }));
    const errors = placed.filter((p) => p.n !== 1).map((p) => `“${trunc(p.e.find, 40)}” found ${p.n}× in ${p.e.field} (must be exactly once)`);
    if (errors.length) return { errors };
    placed.sort((x, y) => x.idx - y.idx);
    const beforeMarks = [], afterMarks = [];
    let after = "", cur = 0;
    const hunks = [];
    for (const p of placed) {
      const k = core(p.e.find, p.e.replace);
      beforeMarks.push([p.idx + k.fa, p.idx + k.fb]);
      after += field.slice(cur, p.idx);
      const start = after.length;
      after += p.e.replace;
      afterMarks.push([start + k.ra, start + k.rb]);
      cur = p.idx + p.e.find.length;
      hunks.push({
        pre: field.slice(Math.max(0, p.idx + k.fa - 90), p.idx + k.fa), del: field.slice(p.idx + k.fa, p.idx + k.fb),
        ins: p.e.replace.slice(k.ra, k.rb), post: field.slice(p.idx + k.fb, p.idx + k.fb + 90), field: p.e.field,
      });
    }
    after += field.slice(cur);
    const b = applyMarks(field, beforeMarks.map(([x, y]) => markFor(field, x, y, "d-del")));
    const a = applyMarks(after, afterMarks.map(([x, y]) => markFor(after, x, y, "d-ins")));
    return { before: sanitize(b), after: sanitize(a), afterRaw: after, hunks, errors: [] };
  }
  const countOcc = (s, sub) => (sub ? s.split(sub).length - 1 : 0);
  const trunc = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

  // ------------------------------------------------------------ filtering ---
  function visibleCases() {
    const q = ui.q.trim().toLowerCase();
    return CASES.filter((c) => c.lane === ui.lane)
      .filter((c) => ui.subject === "All" || c.subject === ui.subject)
      .filter((c) => !q || [c.questionId, c.qid, c.chapter, c.subject, ...c.items.map((i) => i.repair || i.verdict),
        ...c.reports.map((r) => `${r.reportId} ${r.comment}`)].join(" ").toLowerCase().includes(q));
  }

  // ------------------------------------------------------------ render: chrome ---
  function renderLanes() {
    $("#lanes").innerHTML = LANES.map((l, i) => {
      const cs = CASES.filter((c) => c.lane === l.id);
      const pend = cs.filter((c) => pendingIn(c).length).length;
      return `<button class="lane" role="tab" data-lane="${l.id}" aria-selected="${ui.lane === l.id}" title="${esc(l.tip)} (${i + 1})">
        <span class="dot"></span><span class="label-long">${esc(l.label)}</span><span class="n">${pend}<span style="opacity:.55">/${cs.length}</span></span></button>`;
    }).join("");
  }
  function renderProgress() {
    const all = CASES.flatMap((c) => c.items);
    const done = all.filter((i) => !isPending(i)).length;
    const pct = all.length ? Math.round((100 * done) / all.length) : 0;
    $("#progress").innerHTML = `<span><b>${done}</b> of ${all.length} decided</span><span class="bar"><i style="width:${pct}%"></i></span>`;
    $("#log-count").textContent = store.log.length;
  }
  function renderSubjects() {
    const subs = ["All", ...new Set(CASES.map((c) => c.subject))];
    $("#subjects").innerHTML = subs.map((s) => `<button class="pill-btn" data-subject="${esc(s)}" aria-pressed="${ui.subject === s}">${esc(s)}</button>`).join("");
  }
  function renderQueue() {
    const list = visibleCases();
    if (!list.length) { $("#queue").innerHTML = `<li class="empty">Nothing here${ui.q ? " for this search" : ""}.</li>`; return; }
    $("#queue").innerHTML = list.map((c) => {
      const lead = c.items.find((i) => i.kind === "reported" && i.verdict === "defect") || c.items[0];
      const pend = pendingIn(c).length, tot = c.items.length;
      const st = pend === 0 ? (c.items.some((i) => store.decisions[i.id]?.action === "rejected") ? ["rejected", "✕ done"] : ["approved", "✓ done"]) : pend < tot ? ["partial", `${tot - pend}/${tot}`] : null;
      const quote = c.reports.length ? `“${trunc(c.reports[0].comment, 70)}”` : "No student report · found by AI audit";
      const title = lead ? headline(lead) : "Audited: no defects";
      return `<li class="qcard ${pend ? "" : "done"}" data-id="${c.questionId}" aria-current="${ui.current === c.questionId}">
        <span class="rank" title="Position in this queue (most urgent first)">${list.indexOf(c) + 1}</span>
        <div class="line1"><span class="title">${esc(title)}</span>${st ? `<span class="state ${st[0]}">${st[1]}</span>` : ""}</div>
        <div class="meta">${esc(c.subject)} · ${esc(c.chapter)} ${c.reports.length > 1 ? `· <b>${c.reports.length} reports</b>` : ""}</div>
        <div class="quote">${esc(quote)}</div></li>`;
    }).join("");
  }

  // ------------------------------------------------------------ render: workspace ---
  function renderWorkspace() {
    const ws = $("#workspace");
    const c = BY_ID[ui.current];
    if (!c) { ws.innerHTML = `<div class="ws-inner"><div class="card card-pad empty">Pick a question from the queue.</div></div>`; return; }
    if (!ui.focus || !c.items.some((i) => i.id === ui.focus)) ui.focus = (pendingIn(c)[0] || c.items[0] || {}).id;

    const items = c.items.length ? c.items.map((it) => renderItem(c, it)).join("") :
      `<section class="card card-pad item closed"><div class="headline">Audited by AI: no defects found</div><p class="evidence">Blind solve, key and solution agree; scanners found nothing.</p>${renderQuestionOnly(c)}</section>`;

    ws.innerHTML = `<div class="ws-inner">
      <div class="case-head"><span class="qid">${esc(c.questionId)}</span>${c.qid ? `<span>${esc(c.qid)}</span>` : ""}
        <span>${esc(c.subject)} · ${esc(c.chapter)}</span><span>${esc(c.questionType)}${c.level ? " · " + esc(c.level) : ""}</span>
        <span title="Urgency = severity × (1 + ln(1 + reports)) + pending bump">urgency ${c.urgency.score}</span></div>
      ${items}
      ${renderFacts(c)}
      ${renderActionBar(c)}
      <p class="footnote">${provenance()}</p>
    </div>`;
    ws.querySelectorAll(".rdiff").forEach((el) => fillDiff(el));
  }

  function renderItem(c, it) {
    const lane = laneOfItem(it);
    const dec = store.decisions[it.id];
    const edits = ui.editing?.id === it.id ? ui.editing.edits : listify(it.edit || it.suggestion);
    const routeChip = { auto_apply: it.kind === "finding" ? `<span class="chip auto">Safe to auto-apply</span>` : `<span class="chip auto">Auto-applied</span>`,
      faculty_review: `<span class="chip review">Needs your decision</span>`, no_change: `<span class="chip closed">No change</span>` }[it.route] || "";
    const kindChip = it.verdict === "defect" ? `<span class="chip ${lane}">${esc((it.repair || "defect").replace(/_/g, " "))}</span>` : `<span class="chip closed">${esc(it.verdict.replace(/_/g, " "))}</span>`;
    const tax = it.repair && TAX.repairs[it.repair];
    const humanOnly = tax && !tax.auto ? `<span class="chip outline" title="${esc(tax.label)}">human-only repair</span>` : "";
    const reportsChip = it.reports.length > 1 ? `<span class="chip plain">${it.reports.length} students reported this</span>` : it.kind === "finding" ? `<span class="chip found">Not reported by any student</span>` : "";
    const conf = Math.round((it.confidence || 0) * 100);

    return `<section class="card card-pad item ${lane} ${ui.focus === it.id ? "focus" : ""} ${dec ? "decided" : ""}" data-item="${esc(it.id)}">
      <div class="verdict-row">${kindChip}${routeChip}${humanOnly}${reportsChip}
        <span class="conf" title="Model confidence; the gate requires ≥ 85% to auto-apply">${conf}% <span class="meter"><i style="width:${conf}%"></i></span></span></div>
      <div class="headline">${esc(headline(it))}</div>
      <p class="evidence">${esc(it.evidence)}</p>
      ${dec ? decidedBanner(it, dec) : ""}
      ${renderChangeSummary(c, it, edits)}
      ${renderDiffBlock(c, it, edits)}
      ${ui.editing?.id === it.id ? renderEditor(c, it) : ""}
      ${it.checks?.length ? `<div class="checks" title="Safety gate (plain code, no model). It can only downgrade a route.">${it.checks.map((k) => `<span class="check ${k.ok ? "ok" : "bad"}" title="${esc(k.detail || "")}">${esc(k.name)}</span>`).join("")}</div>` : ""}
      ${it.reports.length ? `<div class="reports">${it.reports.map((r) => renderReport(it, r)).join("")}</div>` : ""}
    </section>`;
  }

  function decidedBanner(it, dec) {
    const verb = { approved: "Approved", edited: "Edited & approved", rejected: actionLabels(it).noPast }[dec.action] || dec.action;
    const cls = dec.action === "rejected" ? "rejected" : "approved";
    return `<div class="decided-banner ${cls}" style="margin-top:10px">${esc(verb)} by ${esc(dec.by)} · ${new Date(dec.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
      <button class="btn ghost" data-undo="${esc(it.id)}">Undo</button></div>`;
  }

  function renderChangeSummary(c, it, edits) {
    const out = [];
    for (const e of edits) {
      if (e.field === "answer") {
        const opt = (x) => { const l = letters(x); return l.length === 1 && c.options ? c.options[l[0]] : null; };
        const o1 = opt(e.find), o2 = opt(e.replace);
        out.push(`<div class="key-change"><span class="lbl">Answer key</span><span class="from">${esc(fmtAns(e.find))}</span>→<span class="to">${esc(fmtAns(e.replace))}</span>
          ${o1 != null || o2 != null ? `<span class="lbl">${esc(trunc(o1 ?? "", 40))} → <b>${esc(trunc(o2 ?? "", 40))}</b></span>` : ""}</div>`);
      } else if (e.field === "questionType") {
        out.push(`<div class="key-change"><span class="lbl">Question type</span><span class="from">${esc(e.find)}</span>→<span class="to">${esc(e.replace)}</span></div>`);
      }
    }
    const textFields = [...new Set(edits.filter((e) => e.field === "question" || e.field === "solution").map((e) => e.field))];
    if (textFields.length) out.push(`<span class="chip outline">${textFields.join(" + ")} · ${edits.filter((e) => textFields.includes(e.field)).length} surgical change(s)</span>`);
    if (!edits.length && it.verdict === "defect") out.push(`<span class="chip outline">No automatic edit — ${tax(it) && !tax(it).auto ? "this repair needs authored content or a human call" : "the content could not be recovered from the record"}</span>`);
    return out.length ? `<div class="change-summary">${out.join("")}</div>` : "";
  }
  const tax = (it) => it.repair && TAX.repairs[it.repair];

  function renderDiffBlock(c, it, edits) {
    const textEdits = edits.filter((e) => e.field === "question" || e.field === "solution");
    const answerEdit = edits.some((e) => e.field === "answer" || e.field === "questionType");
    if (!textEdits.length) {
      // nothing textual changes: show the live question (and key) so the faculty can verify the verdict
      if (it.kind === "finding" && !answerEdit) return "";
      return `<details class="more" ${ui.focus === it.id && (answerEdit || it.verdict === "defect") ? "open" : ""} style="margin-top:14px"><summary>Live question${answerEdit ? " (options for the key change)" : ""}</summary>${renderQuestionOnly(c, answerEdit ? edits : null)}</details>`;
    }
    return `<div class="diff-bar"><div class="seg" role="group" aria-label="Diff view">
        <button data-view="rendered" aria-pressed="${ui.view === "rendered"}">Rendered</button>
        <button data-view="source" aria-pressed="${ui.view === "source"}">Source</button></div>
        <span class="lbl" style="font-size:12px;color:var(--ink-3)">Red = removed · green = added · press <kbd>S</kbd> to switch</span></div>
      <div class="rdiff" data-q="${esc(c.questionId)}" data-item="${esc(it.id)}"></div>`;
  }

  function fillDiff(el) {
    const c = BY_ID[el.dataset.q];
    const it = ITEMS[el.dataset.item].item;
    const edits = (ui.editing?.id === it.id ? ui.editing.edits : listify(it.edit || it.suggestion)).filter((e) => e.field === "question" || e.field === "solution");
    const fields = ["question", "solution"];
    const touched = fields.filter((f) => edits.some((e) => e.field === f));
    const parts = [];
    for (const f of fields) {
      const raw = c.record[f] || "";
      if (!touched.includes(f)) {
        parts.push(`<div class="pane" style="margin-top:12px"><div class="pane-head">${f} · unchanged</div>
          ${f === "question" ? `<div class="qbody unchanged">${sanitize(raw)}</div>` : `<div class="collapsed-note">Solution not touched by this fix. <button data-expand>Show</button><div class="qbody" hidden>${sanitize(raw)}</div></div>`}</div>`);
        continue;
      }
      const d = fieldDiff(raw, edits.filter((e) => e.field === f));
      if (d.errors.length) { parts.push(`<div class="editor"><div class="err">${d.errors.map(esc).join("<br>")}</div></div>`); continue; }
      if (ui.view === "source") {
        parts.push(`<div class="hunks" style="margin-top:12px">${d.hunks.map((h, i) => `<div class="hunk"><div class="hunk-head"><span>${esc(f)} · change ${i + 1}</span><span>raw HTML / MathML</span></div>
          <pre>…${esc(h.pre)}${h.del ? `<del>${esc(h.del)}</del>` : ""}${h.ins ? `<ins>${esc(h.ins)}</ins>` : ""}${esc(h.post)}…</pre></div>`).join("")}</div>`);
      } else {
        parts.push(`<div class="diff-grid" style="margin-top:12px">
          <div class="pane before"><div class="pane-head"><b>Before</b><span>${esc(f)} · live now</span></div><div class="qbody">${d.before}</div></div>
          <div class="pane after"><div class="pane-head"><b>After</b><span>${esc(f)} · proposed</span></div><div class="qbody">${d.after}</div></div></div>`);
      }
    }
    // when only the solution changes, show the question first for context
    el.innerHTML = touched.includes("question") ? parts.join("") : [parts[0], parts[1]].join("");
  }

  function renderQuestionOnly(c, answerEdits) {
    let opts = "";
    if (answerEdits && c.options) {
      const from = letters(answerEdits.find((e) => e.field === "answer")?.find ?? c.record.answer);
      const to = letters(answerEdits.find((e) => e.field === "answer")?.replace ?? c.record.answer);
      opts = `<div class="pane" style="margin:10px 0"><div class="pane-head">Options · key before → after</div><div class="qbody" style="font-size:15px">${
        Object.entries(c.options).map(([k, v]) => `<div style="display:flex;gap:10px;align-items:baseline;padding:2px 0">
          <b style="width:2ch">${k}</b><span style="flex:1">${v == null ? "<i style='color:var(--danger)'>missing</i>" : v === "" ? "<i style='color:var(--danger)'>blank</i>" : esc(v)}</span>
          ${from.includes(k) && !to.includes(k) ? `<span class="chip danger">old key</span>` : ""}${to.includes(k) && !from.includes(k) ? `<span class="chip auto">new key</span>` : ""}${to.includes(k) && from.includes(k) ? `<span class="chip plain">key</span>` : ""}</div>`).join("")}</div></div>`;
    }
    return `${opts}<div class="pane"><div class="pane-head">Question · live</div><div class="qbody">${sanitize(c.record.question)}</div>
      <div class="collapsed-note">Stored key <b>${esc(fmtAns(c.record.answer))}</b> · <button data-expand>Show solution</button><div class="qbody" hidden>${sanitize(c.record.solution)}</div></div></div>`;
  }

  function renderReport(it, r) {
    const flags = (r.flags || []).map((f) => f.code === "prompt_injection" ? `<span class="flag inj" title="${esc(f.detail)}">instruction attempt · quarantined</span>`
      : `<span class="flag soft" title="${esc(f.detail)}">${esc(f.code.replace(/_/g, " "))}</span>`).join("");
    const editing = ui.editing?.id === it.id;
    const reply = editing ? ui.editing.replies[r.reportId] : (store.decisions[it.id]?.replies?.[r.reportId] ?? r.student_reply);
    const when = new Date(r.reportedAt).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    return `<div class="report"><div><div class="who"><b>${esc(r.reportId)}</b><span>${esc(r.source)}</span><span>${esc(when)}</span>${r.verdict !== it.verdict ? `<span class="chip plain">${esc(r.verdict)}</span>` : ""}${flags}</div>
      <blockquote>${esc(r.comment)}</blockquote></div>
      <div class="reply"><div class="lbl">Reply the student will see</div>${editing ? `<textarea rows="3" data-reply="${esc(r.reportId)}">${esc(reply)}</textarea>` : `<p>${esc(reply)}</p>`}</div></div>`;
  }

  function renderFacts(c) {
    const s = c.solve;
    const keyed = fmtAns(c.record.answer);
    const claims = c.reports.map((r) => r.comment).slice(0, 3);
    const solveRows = s ? `<dt>Blind solve</dt><dd><b>${esc(fmtAns(s.final_answer))}</b> · ${esc(s.value)} <span class="chip plain">${Math.round(s.confidence * 100)}%</span></dd>
      <dt>Solution ends at</dt><dd>${esc(c.solution_concludes ?? "— (does not conclude)")}</dd>
      <dt>Stored key</dt><dd>${esc(keyed)}</dd>
      ${claims.length ? `<dt>Student says</dt><dd>${claims.map((x) => `“${esc(trunc(x, 60))}”`).join("<br>")}</dd>` : ""}` : `<dt>Blind solve</dt><dd>not available</dd>`;
    const sig = c.signals.length ? `<ul style="margin:0;padding-left:18px">${c.signals.map((x) => `<li><b>${esc(x.code.replace(/_/g, " "))}</b> — ${esc(x.detail)}</li>`).join("")}</ul>` : `<span style="color:var(--ink-3)">Nothing flagged.</span>`;
    const hist = c.record.errorHistory?.length ? `<p style="margin:10px 0 0;font-size:13px"><b>Faculty history:</b> ${c.record.errorHistory.map((h) => `${esc(h.at.slice(0, 10))} — ${esc(h.note)}`).join("; ")}</p>` : "";
    return `<section class="card card-pad"><details class="more" ${c.items.some((i) => i.repair === "answer_key" || i.repair === "unsound") ? "open" : ""}>
      <summary>Why the AI believes this: three-way check, scanners, independent solve</summary>
      <div class="facts"><div><h4 class="section-title">Three-way check</h4><dl>${solveRows}</dl>
        ${s ? `<div class="solve-work">${esc(s.working)}</div>` : ""}
        ${s && (!s.stem_complete || s.chapter_fit === "mismatch") ? `<p style="font-size:13px;margin:8px 0 0">${!s.stem_complete ? `<b>Missing:</b> ${esc(s.missing)}. ` : ""}${s.chapter_fit === "mismatch" ? `<b>Chapter:</b> ${esc(s.suggested_chapter)}` : ""}</p>` : ""}
        <p style="font-size:12px;color:var(--ink-3);margin:8px 0 0">The solver never saw the key, the stored solution or any student comment.</p></div>
      <div><h4 class="section-title">Deterministic scanners (no AI)</h4>${sig}${hist}</div></div></details></section>`;
  }

  function actionLabels(it) {
    if (it.verdict !== "defect") return { ok: "Agree · close & send reply", no: "Reopen", noPast: "Reopened" };
    if (it.route === "auto_apply" && it.kind === "reported") return { ok: "Keep fix", no: "Roll back", noPast: "Rolled back" };
    if (listify(it.edit).length) return { ok: "Apply fix", no: "Reject fix", noPast: "Rejected" };
    return { ok: "Confirm · assign fix", no: "Not a defect", noPast: "Marked not a defect" };
  }

  function renderActionBar(c) {
    const it = ITEMS[ui.focus]?.item;
    if (!it) return "";
    const L = actionLabels(it);
    const dec = store.decisions[it.id];
    const idx = c.items.indexOf(it);
    const editing = ui.editing?.id === it.id;
    return `<div class="actionbar">
      <div class="target">${c.items.length > 1 ? `Item ${idx + 1} of ${c.items.length} · ` : ""}<b>${esc(headline(it))}</b>${it.reports.length ? ` · ${it.reports.map((r) => r.reportId).join(", ")}` : ""}</div>
      ${editing ? `<button class="btn ghost" data-act="cancel">Cancel <kbd>Esc</kbd></button><button class="btn primary" data-act="save">Save & approve <kbd>Ctrl ↵</kbd></button>` : `
      <button class="btn danger" data-act="reject" ${dec ? "disabled" : ""}>${esc(L.no)} <kbd>R</kbd></button>
      <button class="btn" data-act="edit" ${dec ? "disabled" : ""}>Edit <kbd>E</kbd></button>
      <button class="btn primary" data-act="approve" ${dec ? "disabled" : ""}>${esc(L.ok)} <kbd>A</kbd></button>
      ${c.items.length > 1 ? `<button class="btn ghost" data-act="nextitem" title="Next item in this question">Next item <kbd>N</kbd></button>` : ""}`}
    </div>`;
  }

  function renderEditor(c, it) {
    const ed = ui.editing;
    const rows = ed.edits.map((e, i) => {
      if (e.field === "answer" || e.field === "questionType") {
        return `<label>${esc(e.field)} · new value (JSON)<input data-edit="${i}" data-json="1" value="${esc(JSON.stringify(e.replace))}"></label>`;
      }
      return `<label>${esc(e.field)} · find (must stay exactly as stored)<code>${esc(e.find)}</code></label>
        <label>replace with<textarea rows="3" data-edit="${i}">${esc(e.replace)}</textarea></label>`;
    }).join("");
    const errs = validateEdits(c, ed.edits);
    return `<div class="editor"><div class="section-title" style="margin:0">Editing — preview updates as you type</div>
      ${rows || `<p style="margin:0;font-size:13px;color:var(--ink-2)">No content edit for this repair. You can still adjust the student replies below.</p>`}
      <div class="err" id="edit-err">${errs.map(esc).join("<br>")}</div></div>`;
  }

  function validateEdits(c, edits) {
    const errs = [];
    for (const e of edits) {
      if (e.field === "question" || e.field === "solution") {
        const n = countOcc(c.record[e.field], e.find);
        if (n !== 1) errs.push(`find occurs ${n}× in ${e.field}`);
        if (/<script|on\w+=/i.test(e.replace)) errs.push("scripts are not allowed");
      } else if (e.field === "answer") {
        const t = c.record.questionType === "mcq" && e.replace !== "B" ? "mcq" : null;
        if (c.questionType === "integerQuestion" && !/^-?\d+(\.\d+)?$/.test(String(e.replace))) errs.push("integer answer must be a number");
        if (c.questionType === "scq" && !(typeof e.replace === "string" && /^[A-D]$/.test(e.replace)) && !ed_typeChange(edits)) errs.push("SCQ answer must be one letter A–D");
        if (t && Array.isArray(e.replace) && e.replace.some((x) => !/^[A-D]$/.test(x))) errs.push("MCQ answer must be a list of single letters");
      }
    }
    return errs;
  }
  const ed_typeChange = (edits) => edits.some((e) => e.field === "questionType");

  // ------------------------------------------------------------ actions ---
  function decide(itemId, action, extra = {}) {
    const { item, c } = ITEMS[itemId];
    const dec = { action, at: new Date().toISOString(), by: store.reviewer || "Faculty", ...extra };
    store.decisions[itemId] = dec;
    store.log.push({
      at: dec.at, actor: dec.by, action, questionId: c.questionId, itemId, reportIds: item.reports.map((r) => r.reportId),
      verdict: item.verdict, repair: item.repair, aiRoute: item.route,
      edit: extra.edits ?? item.edit ?? null, replies: extra.replies ?? undefined,
      note: action === "rejected" ? actionLabels(item).noPast : undefined,
    });
    ui.undo.push(itemId);
    saveState();
    const L = actionLabels(item);
    toast(`${action === "rejected" ? L.noPast : action === "edited" ? "Edited & approved" : "Approved"} · ${trunc(headline(item), 48)}`, true);
    advance(c);
  }

  function advance(c) {
    ui.editing = null;
    const next = pendingIn(c)[0];
    if (next) { ui.focus = next.id; render(); return; }
    render();
    setTimeout(() => {
      const list = visibleCases();
      const i = list.indexOf(c);
      const nxt = list.slice(i + 1).find((x) => pendingIn(x).length) || list.find((x) => pendingIn(x).length);
      if (nxt) select(nxt.questionId);
    }, 150);
  }

  function undo(itemId) {
    const id = itemId || ui.undo.pop();
    if (!id || !store.decisions[id]) return;
    if (itemId) ui.undo = ui.undo.filter((x) => x !== id);
    const { c } = ITEMS[id];
    const prev = store.decisions[id];
    delete store.decisions[id];
    store.log.push({ at: new Date().toISOString(), actor: store.reviewer || "Faculty", action: "undo", questionId: c.questionId, itemId: id, undid: prev.action });
    saveState();
    ui.lane = c.lane; ui.current = c.questionId; ui.focus = id;
    toast("Undone", false);
    render();
  }

  function startEdit() {
    const { item } = ITEMS[ui.focus] || {};
    if (!item || store.decisions[item.id]) return;
    ui.editing = { id: item.id, edits: JSON.parse(JSON.stringify(listify(item.edit || item.suggestion))),
      replies: Object.fromEntries(item.reports.map((r) => [r.reportId, r.student_reply])) };
    renderWorkspace();
    const first = $("#workspace .editor textarea, #workspace .editor input, #workspace .reply textarea");
    if (first) first.focus();
  }
  function saveEdit() {
    const ed = ui.editing;
    if (!ed) return;
    const { c, item } = ITEMS[ed.id];
    const errs = validateEdits(c, ed.edits);
    if (errs.length) { $("#edit-err").innerHTML = errs.map(esc).join("<br>"); return; }
    const changedEdit = JSON.stringify(ed.edits) !== JSON.stringify(listify(item.edit || item.suggestion));
    const changedReply = item.reports.some((r) => ed.replies[r.reportId] !== r.student_reply);
    decide(ed.id, changedEdit || changedReply ? "edited" : "approved", { edits: ed.edits.length ? ed.edits : null, replies: ed.replies });
  }

  // ------------------------------------------------------------ navigation ---
  function select(id, keepFocus) {
    ui.current = id;
    try { history.replaceState(null, "", "#q=" + id); } catch (_) { /* file:// in some browsers */ }
    ui.editing = null;
    if (!keepFocus) ui.focus = null;
    render();
    $("#workspace").scrollTop = 0;
    const card = $(`.qcard[data-id="${id}"]`);
    if (card) card.scrollIntoView({ block: "nearest" });
  }
  function move(delta) {
    const list = visibleCases();
    if (!list.length) return;
    const i = list.findIndex((c) => c.questionId === ui.current);
    const n = list[Math.max(0, Math.min(list.length - 1, (i < 0 ? 0 : i + delta)))];
    select(n.questionId);
  }
  function setLane(l) {
    ui.lane = l;
    const list = visibleCases();
    ui.current = (list.find((c) => pendingIn(c).length) || list[0] || {}).questionId || null;
    ui.focus = null; ui.editing = null;
    render();
  }

  function render() { renderLanes(); renderProgress(); renderSubjects(); renderQueue(); renderWorkspace(); }

  let toastTimer;
  function toast(msg, withUndo) {
    const t = $("#toast");
    t.innerHTML = `<span>${esc(msg)}</span>${withUndo ? `<button data-toast-undo>Undo <kbd style="background:transparent;color:inherit">U</kbd></button>` : ""}`;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 5000);
  }

  function renderLog() {
    $("#log").innerHTML = store.log.length ? store.log.slice().reverse().map((e) => `<li><div class="t">${esc(e.at.replace("T", " ").slice(0, 19))} · ${esc(e.actor)}</div>
      <div><span class="a ${esc(e.action)}">${esc(e.action)}</span> · ${esc(e.questionId)} ${e.reportIds?.length ? "· " + esc(e.reportIds.join(", ")) : ""} ${e.repair ? "· " + esc(e.repair) : e.verdict ? "· " + esc(e.verdict) : ""}${e.undid ? " (undid " + esc(e.undid) + ")" : ""}</div></li>`).join("")
      : `<li class="empty">No decisions yet.</li>`;
  }
  function openDrawer(open) { const d = $("#drawer"); d.classList.toggle("open", open); d.setAttribute("aria-hidden", String(!open)); if (open) renderLog(); }
  function exportLog() {
    const blob = new Blob([JSON.stringify({ exportedAt: new Date().toISOString(), decisions: store.decisions, log: store.log }, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `audit-log-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function provenance() {
    const g = D.generatedWith;
    const meta = Object.entries(g.stageMeta || {}).map(([k, v]) => `${k}: ${v.join(", ")}`).join(" · ");
    return `Precomputed by the triage pipeline (prompts ${esc(g.promptVersion)}). Configured LLM provider: ${esc(g.provider)} — used for cost reasons; production target is the Claude API. Stage outputs in this build — ${esc(meta)}. Decisions are saved in this browser only.`;
  }

  // ------------------------------------------------------------ events ---
  document.addEventListener("click", (ev) => {
    const t = ev.target.closest("button, .qcard, [data-item]");
    if (!t) return;
    if (t.matches(".lane")) return setLane(t.dataset.lane);
    if (t.matches(".pill-btn")) { ui.subject = t.dataset.subject; return render(); }
    if (t.matches(".qcard")) return select(t.dataset.id);
    if (t.matches("[data-view]")) { ui.view = t.dataset.view; return renderWorkspace(); }
    if (t.matches("[data-expand]")) { const body = t.nextElementSibling; body.hidden = !body.hidden; t.textContent = body.hidden ? t.textContent.replace("Hide", "Show") : t.textContent.replace("Show", "Hide"); return; }
    if (t.matches("[data-undo]")) return undo(t.dataset.undo);
    if (t.matches("[data-toast-undo]")) return undo();
    if (t.matches("[data-act]")) {
      const a = t.dataset.act;
      if (a === "approve") return decide(ui.focus, "approved");
      if (a === "reject") return decide(ui.focus, "rejected");
      if (a === "edit") return startEdit();
      if (a === "save") return saveEdit();
      if (a === "cancel") { ui.editing = null; return renderWorkspace(); }
      if (a === "nextitem") return nextItem();
    }
    if (t.matches("#btn-log")) return openDrawer(true);
    if (t.matches("#btn-close-drawer")) return openDrawer(false);
    if (t.matches("#btn-export")) return exportLog();
    if (t.matches("#btn-help")) return $("#help").classList.add("open");
    if (t.matches("#btn-close-help")) return $("#help").classList.remove("open");
    if (t.matches("#btn-theme")) return toggleTheme();
    if (t.matches("[data-item]") && t.dataset.item !== ui.focus && !ui.editing) { ui.focus = t.dataset.item; renderWorkspace(); }
  });

  let previewTimer;
  document.addEventListener("input", (ev) => {
    const el = ev.target;
    if (el.id === "search") { ui.q = el.value; renderQueue(); return; }
    if (el.id === "reviewer") { store.reviewer = el.value || "Faculty"; saveState(); return; }
    if (!ui.editing) return;
    if (el.dataset.reply) { ui.editing.replies[el.dataset.reply] = el.value; return; }
    if (el.dataset.edit != null) {
      const e = ui.editing.edits[+el.dataset.edit];
      if (el.dataset.json) { try { e.replace = JSON.parse(el.value); } catch (_) { e.replace = el.value; } }
      else e.replace = el.value;
      clearTimeout(previewTimer);
      previewTimer = setTimeout(() => {
        const box = $(`.rdiff[data-item="${CSS.escape(ui.editing.id)}"]`);
        if (box) fillDiff(box);
        const { c } = ITEMS[ui.editing.id];
        $("#edit-err").innerHTML = validateEdits(c, ui.editing.edits).map(esc).join("<br>");
      }, 180);
    }
  });

  function nextItem() {
    const c = BY_ID[ui.current];
    if (!c || c.items.length < 2) return;
    const i = c.items.findIndex((x) => x.id === ui.focus);
    ui.focus = c.items[(i + 1) % c.items.length].id;
    renderWorkspace();
    const el = $(`[data-item="${CSS.escape(ui.focus)}"]`);
    if (el) el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  document.addEventListener("keydown", (ev) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName);
    if (ev.key === "Escape") {
      if (ui.editing) { ui.editing = null; renderWorkspace(); return; }
      $("#help").classList.remove("open"); openDrawer(false);
      if (typing) document.activeElement.blur();
      return;
    }
    if (ui.editing && ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); return saveEdit(); }
    if (typing || ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const k = ev.key.toLowerCase();
    const map = {
      j: () => move(1), k: () => move(-1), n: nextItem,
      a: () => { if (ui.focus && !store.decisions[ui.focus]) decide(ui.focus, "approved"); },
      r: () => { if (ui.focus && !store.decisions[ui.focus]) decide(ui.focus, "rejected"); },
      e: startEdit, u: () => undo(), l: () => openDrawer(!$("#drawer").classList.contains("open")),
      s: () => { ui.view = ui.view === "rendered" ? "source" : "rendered"; renderWorkspace(); },
      "?": () => $("#help").classList.toggle("open"), "/": () => $("#search").focus(),
      1: () => setLane("review"), 2: () => setLane("auto"), 3: () => setLane("found"), 4: () => setLane("closed"),
    };
    if (map[k] || map[ev.key]) { ev.preventDefault(); (map[k] || map[ev.key])(); }
  });

  function toggleTheme() {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    store.theme = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = store.theme;
    saveState();
  }

  // ------------------------------------------------------------ boot ---
  if (store.theme) document.documentElement.dataset.theme = store.theme;
  $("#reviewer").value = store.reviewer || "Faculty";
  const fromHash = (location.hash.match(/q=([0-9a-f]{24})/) || [])[1];
  if (fromHash && BY_ID[fromHash]) { ui.lane = BY_ID[fromHash].lane; ui.current = fromHash; render(); }
  else setLane("review");
})();
