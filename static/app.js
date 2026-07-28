"use strict";

const S = {
  tab: "today",
  courses: [],
  meta: null,
  editId: null,
  af: { due: "tomorrow", min: 60 },
  matStatus: {},   // material id -> last-seen status (honest completion detection)
  review: { queue: [], idx: 0, revealed: false, mode: "", label: "" },
};

// ---------- api ----------
async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  if (!res.ok) throw new Error((await res.text()) || res.status);
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res.text();
}
const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
// SVG needs createElementNS (SVGElement.className is read-only, so el() can't build it)
const SVGNS = "http://www.w3.org/2000/svg";
const elNS = (tag, attrs) => { const e = document.createElementNS(SVGNS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
// mastery ring: track (--line) + progress arc (--ok). Colors set via .style so var() resolves in Safari.
function masteryRing(pct) {
  const SZ = 88, SW = 8, R = (SZ - SW) / 2, C = 2 * Math.PI * R, MID = SZ / 2;
  pct = Math.max(0, Math.min(100, Math.round(pct || 0)));
  const svg = elNS("svg", { class: "ring", viewBox: `0 0 ${SZ} ${SZ}`, width: SZ, height: SZ, role: "img", "aria-label": `定着 ${pct}%` });
  const track = elNS("circle", { cx: MID, cy: MID, r: R, fill: "none", "stroke-width": SW });
  track.style.stroke = "var(--line)";
  const prog = elNS("circle", { cx: MID, cy: MID, r: R, fill: "none", "stroke-width": SW,
    "stroke-linecap": "round", "stroke-dasharray": C, transform: `rotate(-90 ${MID} ${MID})` });
  prog.style.stroke = "var(--ok)";
  prog.style.transition = "stroke-dashoffset .5s ease";
  prog.style.strokeDashoffset = C;                                        // start empty…
  requestAnimationFrame(() => { prog.style.strokeDashoffset = C * (1 - pct / 100); }); // …sweep to pct
  svg.appendChild(track); svg.appendChild(prog);
  const wrap = el("div", "ring-wrap"); wrap.appendChild(svg); wrap.appendChild(el("div", "ring-num", pct + "%"));
  return wrap;
}

// ---------- time formatting (server stores UTC; browser localizes) ----------
function fmtDue(iso) {
  if (!iso) return { text: "期限なし", soon: false, overdue: false };
  const d = new Date(iso), now = new Date();
  const ms = d - now, h = ms / 3.6e6;
  const overdue = ms < 0;
  const soon = !overdue && h <= 48;
  let text;
  if (overdue) {
    const days = Math.ceil(-h / 24);
    text = -h < 24 ? `${Math.ceil(-h)}時間 超過` : `${days}日 超過`;
  } else if (h <= 48) {
    text = h < 1 ? "まもなく" : `残り${Math.round(h)}時間`;
  } else {
    text = d.toLocaleDateString("ja-JP", { month: "numeric", day: "numeric", weekday: "short" });
  }
  return { text, soon, overdue };
}
const SUBJ = { stem: "数理", memo: "暗記", lang: "語学", other: "実技" };
function subjectPill(t) { const p = el("span", "pill " + (t || "other"), SUBJ[t] || "教科"); return p; }

// ---------- toast ----------
let toastT;
function toast(msg, ok) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (ok ? " ok" : "");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.add("hidden"), 2600);
}

// ---------- tabs ----------
function switchTab(name) {
  S.tab = name;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
  render();
}

// ---------- meta / badges ----------
async function refreshMeta() {
  try { S.meta = await api("/api/meta"); } catch (e) { return; }
  const b = S.meta.badges;
  setBadge("badge-today", b.today, false);
  setBadge("badge-assignments", b.assignments, false);
  setBadge("badge-review", b.review, false);
  setBadge("badge-materials", b.materials, false);
  const an = $("#analyzing");
  if (b.materials > 0) { an.classList.remove("hidden"); $("#analyzing-n").textContent = b.materials; }
  else an.classList.add("hidden");
  // honest completion: celebrate ONLY on done+cards; failed is a quiet, non-
  // celebratory signal. Watch while anything is (or just was) in flight.
  const watching = b.materials > 0 || Object.values(S.matStatus).some(inFlight);
  if (watching) await checkMaterialTransitions();
  // sync info
  const si = $("#sync-info");
  if (S.meta.last_sync) {
    const ago = Math.round((Date.now() - new Date(S.meta.last_sync)) / 60000);
    si.textContent = ago < 60 ? `同期 ${ago}分前` : `同期 ${Math.round(ago / 60)}時間前`;
    si.classList.toggle("warn", ago > 60);
  } else { si.textContent = S.meta.classroom_configured ? "未同期" : "手動"; }
  // cost meter (count-frame only, no dollars)
  $("#cost-meter").textContent = `今月 目安 ${S.meta.cost.monthly_budget}回`;
  // passive backup health: surface only when stale (a silently-stopped timer)
  const bk = $("#backup-info"), bs = S.meta.backup;
  if (bk) {
    if (bs && (bs.age_days == null || bs.age_days >= 2)) {
      bk.textContent = bs.age_days == null ? "バックアップ未" : `バックアップ ${bs.age_days}日前`;
      bk.classList.remove("hidden");
    } else { bk.classList.add("hidden"); }
  }
}

// in-flight = still being analyzed (extract/generate not finished)
const inFlight = (s) => s === "extracting" || s === "generating";

// Detect per-material status transitions since the last poll. Success is only
// signalled when a material reaches `done` AND actually produced cards; `failed`
// is a quiet toast (no sound / no notification / no title flash).
async function checkMaterialTransitions() {
  let mats;
  try { mats = await api("/api/materials"); } catch (e) { return; }
  const prev = S.matStatus, next = {};
  let doneCards = 0, doneNoCards = 0, failed = 0;
  for (const m of mats) {
    next[m.id] = m.status;
    const was = prev[m.id];
    if (inFlight(was) && was !== m.status) {
      if (m.status === "done") { m.card_count > 0 ? doneCards++ : doneNoCards++; }
      else if (m.status === "failed") failed++;
    }
  }
  S.matStatus = next;
  // order matters: the celebratory toast should win the shared toast slot
  if (failed > 0) toast("一部の解析に失敗しました。教材タブで確認してください");
  if (doneNoCards > 0) toast("解析が完了しました", true);
  if (doneCards > 0) notifyDone();
  if ((doneCards || doneNoCards || failed) && S.tab === "materials") renderMaterials();
}
function setBadge(id, n, alert) {
  const e = document.getElementById(id);
  if (n > 0) { e.textContent = n; e.classList.add("show"); e.classList.toggle("alert", !!alert); }
  else e.classList.remove("show");
}

// ---------- render dispatch ----------
async function render() {
  if (S.tab === "today") return renderToday();
  if (S.tab === "assignments") return renderAssignments();
  if (S.tab === "review") return renderReview();
  if (S.tab === "materials") return renderMaterials();
}

// ---------- assignment row ----------
function assignmentRow(a, opts = {}) {
  const row = el("div", "a-row");
  const check = el("button", "a-check" + (a.status === "submitted" || a.status === "graded" ? " done" : ""), "✓");
  check.onclick = () => toggleDone(a);
  const body = el("div", "a-body");
  const title = el("div", "a-title" + (a.status === "submitted" || a.status === "graded" ? " done" : ""), a.title);
  const sub = el("div", "a-sub");
  if (a.subject_type) sub.appendChild(subjectPill(a.subject_type));
  if (a.course_name) sub.appendChild(el("span", "", a.course_name));
  const due = fmtDue(a.due_at);
  const duePill = el("span", "pill" + (due.soon || due.overdue ? " due-soon" : ""), due.text);
  sub.appendChild(duePill);
  if (a.max_points) sub.appendChild(el("span", "", `配点${a.max_points}`));
  if (a.estimated_minutes) sub.appendChild(el("span", "", `~${a.estimated_minutes}分`));
  body.appendChild(title); body.appendChild(sub);
  body.onclick = (e) => { if (e.target === body || e.target === title) openEdit(a); };
  row.appendChild(check); row.appendChild(body);
  if (!opts.noPin) {
    const pin = el("button", "a-pin" + (a.pinned ? " on" : ""), "📌");
    pin.title = "最上位に固定"; pin.onclick = () => togglePin(a);
    row.appendChild(pin);
  }
  return row;
}

async function toggleDone(a) {
  const done = a.status === "submitted" || a.status === "graded";
  const next = done ? "todo" : "submitted";
  await api(`/api/assignments/${a.id}`, { method: "PATCH", body: JSON.stringify({ status: next }) });
  if (!done && a.max_points) toast(`✓ ${a.course_name || ""} を完了`, true);
  await refreshMeta(); render();
}
async function togglePin(a) {
  await api(`/api/assignments/${a.id}`, { method: "PATCH", body: JSON.stringify({ pinned: !a.pinned }) });
  render();
}

// ---------- TODAY ----------
async function renderToday() {
  const p = $("#panel-today"); p.innerHTML = "";
  let data;
  try { data = await api("/api/today"); } catch (e) { p.appendChild(el("div", "empty", "読み込みに失敗しました")); return; }

  // 1) the one move — focus card (its 完了にする is the ONLY primary on screen).
  //    Left rule takes the subject colour; indigo (accent) stays reserved for nav/actions.
  if (data.focus) {
    p.appendChild(el("div", "section-title", "いま、これ"));
    const c = el("div", "card focus-card " + (data.focus.subject_type || "other"));
    c.appendChild(el("div", "focus-title", data.focus.title));
    if (data.focus_reason) c.appendChild(el("div", "focus-reason", data.focus_reason));
    else {
      const due = fmtDue(data.focus.due_at);
      c.appendChild(el("div", "focus-reason", `${data.focus.course_name || ""} ・ ${due.text}`));
    }
    const doneBtn = el("button", "btn primary small", "完了にする");
    doneBtn.style.marginTop = "12px";
    doneBtn.onclick = () => toggleDone(data.focus);
    c.appendChild(doneBtn);
    p.appendChild(c);
  }

  // 2) the quiet rest of today
  const rest = (data.todo || []).filter((a) => !data.focus || a.id !== data.focus.id);
  if (rest.length) {
    p.appendChild(el("div", "section-title", `今日やる（あと${rest.length}件）`));
    const c = el("div", "card");
    rest.forEach((a) => c.appendChild(assignmentRow(a)));
    p.appendChild(c);
  }

  // achievement moment — all of today's assignments cleared (overdue term kept intact)
  if (!data.focus && !rest.length && !(data.overdue || []).length) {
    p.appendChild(doneBanner());
  }

  // 3) review — a quiet one-line link, never a second primary
  const rn = data.review_due || 0;
  if (rn > 0) {
    const rl = el("button", "review-link", `今日の復習 ${rn}枚 ›`);
    rl.onclick = startReview;
    p.appendChild(rl);
  } else {
    p.appendChild(el("div", "review-link none", "今日の復習はありません"));
  }

  // 4) overdue = "あとで" — demoted to the very bottom, red only on the caret
  if (data.overdue && data.overdue.length) {
    const wrap = el("div", "overdue-section");
    const head = el("div", "collapse-head");
    const caret = el("span", "caret", "▸");
    head.appendChild(caret);
    head.appendChild(el("span", "", `期限切れ ${data.overdue.length}件（あとで）`));
    const box = el("div", "card hidden");
    data.overdue.forEach((a) => box.appendChild(assignmentRow(a)));
    head.onclick = () => { box.classList.toggle("hidden"); caret.textContent = box.classList.contains("hidden") ? "▸" : "▾"; };
    wrap.appendChild(head); wrap.appendChild(box); p.appendChild(wrap);
  }
}
function doneBanner() {
  const d = el("div", "done-banner");
  d.appendChild(el("div", "", "🎉 今日の分は終わりです"));
  return d;
}

// ---------- ASSIGNMENTS ----------
async function renderAssignments() {
  const p = $("#panel-assignments"); p.innerHTML = "";
  // filters
  const filters = el("div", "filters");
  const fCourse = el("select"); fCourse.appendChild(new Option("全教科", ""));
  S.courses.forEach((c) => fCourse.appendChild(new Option(c.name, c.id)));
  const fStatus = el("select");
  [["", "全状態"], ["todo", "未着手"], ["in_progress", "着手中"], ["submitted", "提出済"], ["graded", "採点済"]]
    .forEach(([v, l]) => fStatus.appendChild(new Option(l, v)));
  filters.appendChild(fCourse); filters.appendChild(fStatus);
  p.appendChild(filters);

  const listWrap = el("div");
  p.appendChild(listWrap);

  async function load() {
    listWrap.innerHTML = "";
    // approval band (proposed)
    const proposed = await api("/api/assignments?status=proposed");
    if (proposed.length) {
      const band = el("div", "approve-band");
      band.appendChild(el("div", "head", `承認待ち ${proposed.length}件`));
      proposed.forEach((a) => {
        const it = el("div", "approve-item");
        it.appendChild(el("div", "grow", a.title));
        const add = el("button", "btn small primary", "追加");
        add.onclick = async () => { await api(`/api/assignments/${a.id}`, { method: "PATCH", body: JSON.stringify({ status: "todo" }) }); toast("追加しました", true); await refreshMeta(); load(); };
        const no = el("button", "btn small", "違う");
        no.onclick = async () => { await api(`/api/assignments/${a.id}`, { method: "DELETE" }); await refreshMeta(); load(); };
        it.appendChild(add); it.appendChild(no); band.appendChild(it);
      });
      listWrap.appendChild(band);
    }
    // main list
    let q = [];
    if (fCourse.value) q.push("course_id=" + fCourse.value);
    if (fStatus.value) q.push("status=" + fStatus.value);
    const rows = await api("/api/assignments" + (q.length ? "?" + q.join("&") : ""));
    const visible = rows.filter((a) => a.status !== "proposed");
    if (!visible.length) { listWrap.appendChild(el("div", "empty", "課題はまだありません。＋追加 または 写真ドロップで。")); return; }
    const card = el("div", "card");
    visible.forEach((a) => {
      const row = assignmentRow(a);
      const del = el("button", "a-pin", "🗑");
      del.title = "削除"; del.onclick = async () => { if (confirm("削除しますか？")) { await api(`/api/assignments/${a.id}`, { method: "DELETE" }); await refreshMeta(); load(); } };
      row.appendChild(del);
      card.appendChild(row);
    });
    listWrap.appendChild(card);
  }
  fCourse.onchange = load; fStatus.onchange = load;
  load();
}

// ---------- REVIEW (Phase 4) ----------
const GRADES = [["again", "もう一度", "g-again"], ["hard", "難しい", "g-hard"],
                ["good", "できた", "g-good"], ["easy", "簡単", "g-easy"]];

async function renderReview() {
  const p = $("#panel-review"); p.innerHTML = "";
  const r = S.review;
  if (r.queue.length && r.idx < r.queue.length) return renderCard(p);
  return renderReviewHome(p);
}

async function loadReviewQueue(mode, opts = {}) {
  let cards = [];
  try {
    if (mode === "weak") cards = await api("/api/review/weak");
    else if (mode === "drill") cards = await api("/api/review/drill?" + new URLSearchParams(opts));
    else if (mode === "cram") { const d = await api("/api/review/cram?" + new URLSearchParams(opts)); cards = d.today; }
    else { const q = await api("/api/review/queue"); cards = q.cards; }
  } catch (e) { toast("読み込み失敗"); return; }
  S.review = { queue: cards, idx: 0, revealed: false, mode, label: opts.label || "" };
  if (!cards.length) toast("対象カードがありません");
  switchTab("review");
}
function startReview() { loadReviewQueue("normal"); }

async function renderReviewHome(p) {
  // finished session banner
  if (S.review.mode) {
    p.appendChild(doneBanner());
    S.review = { queue: [], idx: 0, revealed: false, mode: "", label: "" };
  }
  let mastery = { pct: 0, total: 0 };
  try { mastery = await api("/api/mastery"); } catch (e) {}
  const due = (S.meta && S.meta.badges.review) || 0;

  const c = el("div", "card");
  c.appendChild(el("div", "focus-title", due > 0 ? `今日の復習 ${due}枚` : "今日の復習はありません"));
  if (mastery.total) {
    const mrow = el("div", "mastery-row");
    mrow.appendChild(masteryRing(mastery.pct));
    mrow.appendChild(el("div", "meta-line", `定着 ${mastery.pct}%（${mastery.total}枚中 ${mastery.mastered}枚）`));
    c.appendChild(mrow);
  }
  if (due > 0) { const b = el("button", "btn primary", "始める"); b.style.marginTop = "12px"; b.onclick = () => loadReviewQueue("normal"); c.appendChild(b); }
  p.appendChild(c);

  // test mode (cram) — pick an upcoming exam
  let exams = [];
  try { exams = await api("/api/exams"); } catch (e) {}
  if (exams.length) {
    p.appendChild(el("div", "section-title", "テスト対策（範囲を前倒し）"));
    const ec = el("div", "card");
    exams.forEach((a) => {
      const row = el("div", "a-row");
      const body = el("div", "a-body");
      body.appendChild(el("div", "a-title", a.title));
      const due = fmtDue(a.due_at);
      body.appendChild(el("div", "a-sub", `${a.course_name || ""} ・ ${due.text}`));
      row.appendChild(body);
      const b = el("button", "btn small primary", "対策開始");
      b.onclick = () => loadReviewQueue("cram", { assignment_id: a.id, label: a.title });
      row.appendChild(b); ec.appendChild(row);
    });
    p.appendChild(ec);
  }

  // weak-card drill
  let weak = [];
  try { weak = await api("/api/review/weak"); } catch (e) {}
  if (weak.length) {
    const wc = el("div", "card");
    wc.style.display = "flex"; wc.style.justifyContent = "space-between"; wc.style.alignItems = "center";
    wc.appendChild(el("div", "", `苦手 ${weak.length}枚`));
    const b = el("button", "btn small danger", "今すぐドリル");
    b.onclick = () => loadReviewQueue("weak");
    wc.appendChild(b); p.appendChild(wc);
  }
  if (due === 0 && !exams.length && !weak.length) {
    const e = el("div", "empty"); e.appendChild(el("div", "big", "🗂"));
    e.appendChild(el("div", "", "教材を取り込むと復習カードがここに並びます"));
    p.appendChild(e);
  }
}

function renderCard(p) {
  const r = S.review;
  const card = r.queue[r.idx];
  const wrap = el("div", "review-wrap");

  const top = el("div", "review-top");
  top.appendChild(el("span", "pill", card.why_now));
  if (card.course_name) top.appendChild(el("span", "pill " + (card.subject_type || "other"), card.course_name));
  top.appendChild(el("span", "review-progress", `${r.idx + 1} / ${r.queue.length}${r.label ? " ・ " + r.label : ""}`));
  wrap.appendChild(top);

  const face = el("div", "review-card");
  face.appendChild(el("div", "review-front", card.front));
  if (r.revealed) {
    face.appendChild(el("hr", "review-sep"));
    face.appendChild(el("div", "review-back", card.back));
    if (card.thumb_url) { const img = el("img", "review-thumb"); img.src = card.thumb_url; face.appendChild(img); }
    if (card.source_quote) { const q = el("div", "cp-quote"); q.textContent = "「" + card.source_quote + "」"; face.appendChild(q); }
    const bad = el("button", "btn small ghost", "この問題おかしい");
    bad.onclick = async (e) => { e.stopPropagation(); await api("/api/review/report", { method: "POST", body: JSON.stringify({ card_id: card.id, verdict: "wrong" }) }); toast("報告しました"); };
    face.appendChild(bad);
  } else {
    face.appendChild(el("div", "review-hint", "タップして答えを見る"));
  }
  // tap to reveal + swipe (left=again / right=easy)
  face.onclick = () => { if (!r.revealed) { r.revealed = true; renderReview(); } };
  attachSwipe(face);
  wrap.appendChild(face);

  const bar = el("div", "grade-bar");
  if (r.revealed) {
    GRADES.forEach(([g, label, cls]) => {
      const b = el("button", "grade-btn " + cls, label);
      b.onclick = () => grade(g);
      bar.appendChild(b);
    });
  } else {
    const b = el("button", "btn primary reveal-btn", "答えを見る");
    b.onclick = () => { r.revealed = true; renderReview(); };
    bar.appendChild(b);
  }
  wrap.appendChild(bar);
  p.appendChild(wrap);
}

function attachSwipe(node) {
  let x0 = null;
  node.addEventListener("touchstart", (e) => { x0 = e.changedTouches[0].clientX; }, { passive: true });
  node.addEventListener("touchend", (e) => {
    if (x0 == null) return;
    const dx = e.changedTouches[0].clientX - x0; x0 = null;
    if (!S.review.revealed) return;
    if (dx < -60) grade("again");
    else if (dx > 60) grade("easy");
  }, { passive: true });
}

async function grade(g) {
  const r = S.review;
  const card = r.queue[r.idx];
  try { await api("/api/review/answer", { method: "POST", body: JSON.stringify({ card_id: card.id, grade: g }) }); }
  catch (e) { toast("記録に失敗"); return; }
  r.idx++; r.revealed = false;
  await refreshMeta();
  renderReview();
}

// ---------- MATERIALS (Phase 3) ----------
const STAGE = { extracting: "文字を読む…", generating: "カード作成…", done: "完了", failed: "失敗" };
const selMat = new Set();

function aiOffNote() {
  const n = el("div", "ai-off-note");
  n.appendChild(el("div", "", "⚠️ AI機能は現在使えません（claude CLI 未検出）"));
  n.appendChild(el("div", "sub", "写真は保存できますが、カード生成はされません。「＋追加」から手動で入力できます。"));
  return n;
}

async function renderMaterials() {
  const p = $("#panel-materials"); p.innerHTML = "";
  if (S.meta && !S.meta.claude_ok) p.appendChild(aiOffNote());  // pre-empt wasted drops
  let mats;
  try { mats = await api("/api/materials"); } catch (e) { p.appendChild(el("div", "empty", "読み込み失敗")); return; }
  if (!mats.length) {
    const e = el("div", "empty");
    e.appendChild(el("div", "big", "📸"));
    e.appendChild(el("div", "", "写真・PDFをどこにでもドロップすると解析して復習カードを作ります"));
    p.appendChild(e); return;
  }
  // range presets
  const bar = el("div", "filters");
  [["week", "今週"], ["month", "先月〜"], ["all", "全部"]].forEach(([k, l]) => {
    const b = el("button", "btn small ghost", l);
    b.onclick = () => { selectRange(mats, k); renderMaterials(); };
    bar.appendChild(b);
  });
  p.appendChild(bar);

  const grid = el("div", "mat-grid");
  mats.forEach((m) => grid.appendChild(matTile(m)));
  p.appendChild(grid);

  if (selMat.size) {
    const tb = el("div", "select-bar");
    tb.appendChild(el("span", "", `${selMat.size}件 選択`));
    const gen = el("button", "btn primary small", "選択から復習教材を作る");
    gen.onclick = async () => {
      for (const id of selMat) await api(`/api/materials/${id}/regenerate`, { method: "POST" });
      toast("再生成をキューに入れました", true); selMat.clear(); await refreshMeta(); renderMaterials();
    };
    tb.appendChild(gen);
    p.appendChild(tb);
  }
}
function selectRange(mats, k) {
  selMat.clear();
  const now = Date.now();
  mats.forEach((m) => {
    const age = (now - new Date(m.created_at)) / 86400000;
    if (k === "all" || (k === "week" && age <= 7) || (k === "month" && age <= 60)) selMat.add(m.id);
  });
}
function matTile(m) {
  const t = el("div", "mat-tile" + (selMat.has(m.id) ? " sel" : ""));
  if (m.thumb_url && m.kind === "photo") { const img = el("img"); img.src = m.thumb_url; img.loading = "lazy"; t.appendChild(img); }
  else { const ph = el("div", "mat-ph", m.kind === "pdf" ? "📄" : "📸"); t.appendChild(ph); }
  const badge = el("div", "mat-badge " + m.status, STAGE[m.status] || m.status);
  t.appendChild(badge);
  if (m.summary) t.appendChild(el("div", "mat-sum", m.summary));
  const sel = el("button", "mat-sel" + (selMat.has(m.id) ? " on" : ""), selMat.has(m.id) ? "✓" : "");
  sel.onclick = (e) => { e.stopPropagation(); if (selMat.has(m.id)) selMat.delete(m.id); else selMat.add(m.id); renderMaterials(); };
  t.appendChild(sel);
  t.onclick = () => openMaterial(m.id);
  return t;
}

async function openMaterial(mid) {
  let m;
  try { m = await api(`/api/materials/${mid}`); } catch (e) { toast("読み込み失敗"); return; }
  const ov = el("div", "modal-overlay");
  const box = el("div", "modal");
  const close = el("button", "modal-close", "✕"); close.onclick = () => ov.remove();
  box.appendChild(close);
  box.appendChild(el("h3", "", m.summary || "教材"));
  box.appendChild(el("div", "mat-badge inline " + m.status, STAGE[m.status] || m.status));

  if (m.status === "failed") {
    // failed-after-N is a dead-end unless we offer a way out. When the retry
    // budget is spent, make "手動で入力する" the primary path; retry stays
    // available (it resets the counter server-side) but is demoted.
    const exhausted = m.attempts_exhausted;
    const err = el("div", "err-box");
    err.appendChild(el("div", "", m.error_message || "解析に失敗しました"));
    if (exhausted) err.appendChild(el("div", "err-hint", "何度か試しましたが読み取れませんでした。手動で入力するのが確実です。"));
    const actions = el("div", "add-row end");
    const retry = el("button", "btn small " + (exhausted ? "ghost" : "primary"), "もう一度試す");
    retry.onclick = async () => { await api(`/api/materials/${mid}/retry`, { method: "POST" }); toast("再試行中…"); ov.remove(); await refreshMeta(); renderMaterials(); };
    const man = el("button", "btn small " + (exhausted ? "primary" : ""), "手動で入力する");
    man.onclick = () => { ov.remove(); switchTab("assignments"); openAdd(); };
    if (exhausted) { actions.appendChild(man); actions.appendChild(retry); }
    else { actions.appendChild(retry); actions.appendChild(man); }
    err.appendChild(actions);
    box.appendChild(err);
  }

  if (m.thumb_url && m.kind === "photo") { const img = el("img", "modal-img"); img.src = m.thumb_url; box.appendChild(img); }

  // cards: visual separation of generated vs extracted; 要確認 grouping
  const cards = m.cards || [];
  const lowConf = cards.filter((c) => c.confidence === "low");
  const normal = cards.filter((c) => c.confidence !== "low");
  if (lowConf.length) box.appendChild(el("div", "section-title", `要確認 ${lowConf.length}件`));
  lowConf.forEach((c) => box.appendChild(cardPreview(c, true)));
  if (normal.length) box.appendChild(el("div", "section-title", `カード ${normal.length}枚`));
  normal.forEach((c) => box.appendChild(cardPreview(c, false)));

  if (m.guides && m.guides.length) {
    box.appendChild(el("div", "section-title", "要点まとめ"));
    const g = el("pre", "guide-md"); g.textContent = m.guides[0].content_md; box.appendChild(g);
  }
  if (m.extracted_text) {
    const det = el("details", "extract-det");
    det.appendChild(el("summary", "", "抽出テキストを表示"));
    const pre = el("pre", "guide-md"); pre.textContent = m.extracted_text; det.appendChild(pre);
    box.appendChild(det);
  }

  const foot = el("div", "add-row end");
  const regen = el("button", "btn small ghost", "再生成");
  regen.onclick = async () => { await api(`/api/materials/${mid}/regenerate`, { method: "POST" }); toast("再生成中…"); ov.remove(); await refreshMeta(); };
  const del = el("button", "btn small danger", "削除");
  del.onclick = async () => { if (confirm("この教材を削除しますか？")) { await api(`/api/materials/${mid}`, { method: "DELETE" }); ov.remove(); await refreshMeta(); renderMaterials(); } };
  foot.appendChild(regen); foot.appendChild(del);
  box.appendChild(foot);

  ov.appendChild(box); ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  document.body.appendChild(ov);
}
function cardPreview(c, low) {
  const d = el("div", "card-preview" + (c.origin === "generated" ? " generated" : "") + (low ? " low" : ""));
  d.appendChild(el("div", "cp-front", c.front));
  d.appendChild(el("div", "cp-back", c.back));
  const tags = el("div", "cp-tags");
  tags.appendChild(el("span", "pill", c.origin === "generated" ? "AI生成" : "原本"));
  if (c.topic) tags.appendChild(el("span", "pill", c.topic));
  d.appendChild(tags);
  if (c.source_quote) { const q = el("div", "cp-quote"); q.textContent = "「" + c.source_quote + "」"; d.appendChild(q); }
  return d;
}

// ---------- add / edit form ----------
function openAdd() { S.editId = null; resetForm(); $("#af-title").value = ""; $("#af-course").value = ""; $("#af-points").value = ""; $("#af-save").textContent = "追加"; showForm(); $("#af-title").focus(); }
function openEdit(a) {
  S.editId = a.id; resetForm();
  $("#af-title").value = a.title;
  $("#af-course").value = a.course_name || "";
  $("#af-points").value = a.max_points || "";
  $("#af-category").value = a.category || "homework";
  // due -> explicit date
  if (a.due_at) { setDueChip("date"); $("#af-due-date").value = new Date(a.due_at).toISOString().slice(0, 10); }
  else setDueChip("none");
  setMinChip(a.estimated_minutes || 60);
  $("#af-save").textContent = "保存";
  showForm(); $("#af-title").focus();
}
function showForm() { $("#add-form").classList.remove("hidden"); }
function hideForm() { $("#add-form").classList.add("hidden"); }
function resetForm() { S.af = { due: "tomorrow", min: 60 }; setDueChip("tomorrow"); setMinChip(60); $("#af-category").value = "homework"; $("#af-due-date").value = ""; }
function setDueChip(v) { S.af.due = v; document.querySelectorAll("#af-due button").forEach((b) => b.classList.toggle("on", b.dataset.due === v)); }
function setMinChip(v) { S.af.min = v; document.querySelectorAll("#af-minutes button").forEach((b) => b.classList.toggle("on", Number(b.dataset.min) === v)); }

function dueISO() {
  const v = S.af.due;
  if (v === "none") return null;
  let d = new Date();
  if (v === "today") { /* today */ }
  else if (v === "tomorrow") d.setDate(d.getDate() + 1);
  else if (v === "week") { const add = (6 - d.getDay() + 7) % 7 || 6; d.setDate(d.getDate() + add); }
  else if (v === "date") {
    const val = $("#af-due-date").value; if (!val) return null;
    const [y, m, dd] = val.split("-").map(Number); d = new Date(y, m - 1, dd);
  }
  d.setHours(23, 59, 0, 0);
  return d.toISOString();
}

async function saveForm() {
  const title = $("#af-title").value.trim();
  if (!title) { $("#af-title").focus(); return; }
  const payload = {
    title,
    course_name: $("#af-course").value.trim(),
    category: $("#af-category").value,
    estimated_minutes: S.af.min,
    due_at: dueISO(),
    max_points: $("#af-points").value ? Number($("#af-points").value) : null,
  };
  try {
    if (S.editId) await api(`/api/assignments/${S.editId}`, { method: "PATCH", body: JSON.stringify(payload) });
    else await api("/api/assignments", { method: "POST", body: JSON.stringify(payload) });
    hideForm(); toast(S.editId ? "保存しました" : "追加しました", true);
    await loadCourses(); await refreshMeta(); render();
  } catch (e) { toast("保存に失敗: " + e.message); }
}

// ---------- courses ----------
async function loadCourses() {
  try { S.courses = await api("/api/courses"); } catch (e) { S.courses = []; }
  const dl = $("#course-list"); dl.innerHTML = "";
  S.courses.forEach((c) => dl.appendChild(new Option(c.name)));
}

// ---------- sync ----------
async function doSync() {
  toast("同期中…");
  try {
    const r = await api("/api/sync", { method: "POST" });
    if (r.ok) toast(`同期完了：${r.upserted}件`, true);
    else toast(r.message || "同期できませんでした");
  } catch (e) { toast("同期エラー"); }
  await refreshMeta(); render();
}

// ---------- drag & drop (upload wired in Phase 3) ----------
let dragDepth = 0;
function initDrop() {
  const ov = $("#drop-overlay");
  window.addEventListener("dragenter", (e) => { e.preventDefault(); if (hasFiles(e)) { dragDepth++; ov.classList.remove("hidden"); } });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("dragleave", (e) => { e.preventDefault(); if (--dragDepth <= 0) { dragDepth = 0; ov.classList.add("hidden"); } });
  window.addEventListener("drop", async (e) => {
    e.preventDefault(); dragDepth = 0; ov.classList.add("hidden");
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    await uploadFiles(files);
  });
}
function hasFiles(e) { return [...(e.dataTransfer?.types || [])].includes("Files"); }
async function uploadFiles(files) {
  try { if (window.Notification && Notification.permission === "default") Notification.requestPermission(); } catch (e) {}
  const fd = new FormData();
  files.forEach((f) => fd.append("files", f));
  toast(`${files.length}件を取り込み中…`);
  switchTab("materials");
  try {
    const res = await fetch("/api/upload", { method: "POST", body: fd });
    if (res.status === 404) { toast("解析パイプラインはフェーズ3で有効化されます"); return; }
    if (!res.ok) throw new Error(await res.text());
    const r = await res.json();
    if (r.errors && r.errors.length) {
      const okN = r.materials.length;
      // specific per-file wording (e.g. oversize); valid photos in the batch still pass
      toast(okN ? `${okN}件取り込み・${r.errors.length}件エラー: ${r.errors[0]}` : r.errors[0]);
    } else {
      toast(`${r.materials.length}件を取り込みました`, true);
    }
    await refreshMeta(); renderMaterials();
  } catch (e) { toast("アップロード失敗: " + e.message); }
}

// ---------- completion notifications ----------
let titleTimer;
function notifyDone() {
  toast("✅ 解析が完了しました", true);
  document.title = "(✓) StudyDash";
  clearTimeout(titleTimer);
  titleTimer = setTimeout(() => { document.title = "StudyDash"; }, 6000);
  try {
    if (window.Notification && Notification.permission === "granted")
      new Notification("StudyDash", { body: "解析が完了しました。復習カードができました。" });
  } catch (e) { /* ignore */ }
  beep();
}
function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(), g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.type = "sine"; o.frequency.value = 660; g.gain.value = 0.06;
    o.start(); o.stop(ctx.currentTime + 0.15);
  } catch (e) { /* ignore */ }
}

// ---------- dynamic poll: fast while analyzing, slow when idle ----------
function scheduleTick() {
  const fast = S.meta && S.meta.badges.materials > 0;
  setTimeout(async () => { await refreshMeta(); scheduleTick(); }, fast ? 3000 : 20000);
}

// ---------- theme (light/dark toggle + persist; FOUC handled by inline <head> script) ----------
function effectiveTheme() {
  const set = document.documentElement.getAttribute("data-theme");
  if (set === "dark" || set === "light") return set;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function syncThemeButton() {
  const btn = $("#theme-toggle"); if (!btn) return;
  const dark = effectiveTheme() === "dark";
  btn.textContent = dark ? "☀" : "☾";
  btn.setAttribute("aria-pressed", dark ? "true" : "false");
  btn.setAttribute("aria-label", dark ? "ライトに切替" : "ダークに切替");
}
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("theme", t); } catch (e) {}
  syncThemeButton();
}
function toggleTheme() { applyTheme(effectiveTheme() === "dark" ? "light" : "dark"); }

// ---------- init ----------
function init() {
  document.querySelectorAll(".tab").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
  $("#theme-toggle").onclick = toggleTheme; syncThemeButton();
  $("#add-fab").onclick = openAdd;
  $("#af-cancel").onclick = hideForm;
  $("#af-save").onclick = saveForm;
  $("#af-title").addEventListener("keydown", (e) => { if (e.key === "Enter") saveForm(); });
  document.querySelectorAll("#af-due button").forEach((b) => b.onclick = () => setDueChip(b.dataset.due));
  $("#af-due-date").onchange = () => setDueChip("date");
  document.querySelectorAll("#af-minutes button").forEach((b) => b.onclick = () => setMinChip(Number(b.dataset.min)));
  document.addEventListener("keydown", (e) => {
    const typing = /input|select|textarea/i.test(document.activeElement.tagName);
    if (e.key === "n" && !typing && $("#add-form").classList.contains("hidden")) { e.preventDefault(); openAdd(); }
    if (e.key === "Escape") hideForm();
    // review shortcuts (keyboard is the secondary path; touch is primary)
    if (S.tab === "review" && !typing && S.review.queue.length && S.review.idx < S.review.queue.length) {
      if (!S.review.revealed && (e.key === " " || e.key === "Enter")) { e.preventDefault(); S.review.revealed = true; renderReview(); }
      else if (S.review.revealed) {
        if (e.key === "1") grade("again");
        else if (e.key === "2") grade("hard");
        else if (e.key === "3" || e.key === " ") { e.preventDefault(); grade("good"); }
        else if (e.key === "4") grade("easy");
      }
    }
  });
  initDrop();
  loadCourses().then(refreshMeta).then(render);
  scheduleTick(); // dynamic poll: 3s while analyzing, 20s idle
}
document.addEventListener("DOMContentLoaded", init);
