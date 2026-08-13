"use strict";

const S = {
  tab: "today",
  courses: [],
  meta: null,
  editId: null,
  af: { due: "tomorrow", min: 60 },
  matStatus: {},   // material id -> last-seen status (honest completion detection)
  uploading: 0,    // optimistic count of files being uploaded (pre-first-poll UI)
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
// The AI writes cards with light markdown (**bold**, *italic*). Review used to
// print those markers literally, so a card mid-study read "**Sugar:**". Inline
// only — block markdown belongs to mdToNode — and textContent throughout, never
// innerHTML. Bold is split first so **a** can't be mistaken for two *italics*.
// A marker must hug its text (no space just inside), or "2 * 3 * 4" on a maths
// card would italicise the arithmetic.
const MD_BOLD = /\*\*[^\s*](?:[^*]*[^\s*])?\*\*/;
const MD_ITAL = /\*[^\s*](?:[^*\n]*[^\s*])?\*/;
const mdInline = (text) => {
  const frag = document.createDocumentFragment();
  String(text == null ? "" : text).split(new RegExp("(" + MD_BOLD.source + ")")).forEach((seg) => {
    if (new RegExp("^" + MD_BOLD.source + "$").test(seg)) {
      frag.appendChild(el("strong", "", seg.slice(2, -2))); return;
    }
    seg.split(new RegExp("(" + MD_ITAL.source + ")")).forEach((p) => {
      if (new RegExp("^" + MD_ITAL.source + "$").test(p)) frag.appendChild(el("em", "", p.slice(1, -1)));
      else if (p) frag.appendChild(document.createTextNode(p));
    });
  });
  return frag;
};
const elMd = (tag, cls, text) => { const e = el(tag, cls); e.appendChild(mdInline(text)); return e; };
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

// ---------- loading state (make async actions unmistakably "working") ----------
// A button that clearly reads as busy: its label swaps for a spinner + busy text
// and it disables. Returns restore() to call in a finally. Pair with busyBanner()
// for long (20–60s) generations so the user never faces a silent, dead-looking UI.
function btnBusy(btn, busyText) {
  const orig = btn.textContent;
  btn.disabled = true; btn.classList.add("is-loading");
  btn.textContent = "";
  btn.appendChild(el("span", "btn-spin"));
  btn.appendChild(el("span", "", busyText || "処理中…"));
  return () => { btn.classList.remove("is-loading"); btn.disabled = false; btn.textContent = orig; };
}
// A prominent inline "working…" banner (spinner + message) so nothing on screen
// looks frozen while we wait for AI. Returns the node; caller removes it when done.
function busyBanner(text) {
  const b = el("div", "busy-banner");
  b.appendChild(el("span", "btn-spin"));
  const t = el("span", "busy-banner-t");
  t.appendChild(el("div", "", text || "処理中…"));
  t.appendChild(el("div", "busy-sub", "結果が出るまでお待ちください（このまま開いたままでOK）"));
  b.appendChild(t);
  return b;
}

// ---------- language helpers (auto / 日本語 / English for generated content) ----------
const LANG_OPTS = [["auto", "自動（教材に合わせる）"], ["ja", "日本語"], ["en", "English"]];
// Cheap client-side guess of a text's language, so a per-material picker can
// DEFAULT to what the material is written in. Latin-heavy -> en, else ja.
function detectLang(text) {
  const s = (text || "").slice(0, 4000);
  if (!s) return "auto";
  const jp = (s.match(/[぀-ヿ㐀-鿿]/g) || []).length;
  const latin = (s.match(/[A-Za-z]/g) || []).length;
  if (jp === 0 && latin > 8) return "en";
  if (jp > 0 && jp >= latin * 0.15) return "ja";
  return "auto";
}
// A labelled <select> for the output language. `value` sets the initial choice.
function langSelect(value) {
  const sel = el("select", "sm-lang-sel");
  LANG_OPTS.forEach(([v, label]) => { const o = el("option", "", label); o.value = v; sel.appendChild(o); });
  sel.value = value || "auto";
  sel.onclick = (e) => e.stopPropagation();
  return sel;
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
  // G3: the chosen strategies travel with the request — interleaving is done
  // server-side so it composes with the queue's due-first ordering.
  const strat = activeStrategies();
  const stratQS = strat.map((s) => "strategy=" + encodeURIComponent(s)).join("&");
  try {
    if (mode === "weak") cards = await api("/api/review/weak");
    else if (mode === "drill") cards = await api("/api/review/drill?" + new URLSearchParams(opts));
    else if (mode === "cram") { const d = await api("/api/review/cram?" + new URLSearchParams(opts)); cards = d.today; }
    else if (mode === "material") {
      // repeat material_id= for a merge across several materials (J)
      const qs = (opts.material_ids || []).map((id) => "material_id=" + encodeURIComponent(id)).join("&");
      cards = await api("/api/review/by-material?" + qs + (stratQS ? "&" + stratQS : ""));
    }
    else { const q = await api("/api/review/queue" + (stratQS ? "?" + stratQS : "")); cards = q.cards; }
  } catch (e) { toast("読み込み失敗"); return; }
  S.review = { queue: cards, idx: 0, revealed: false, mode, label: opts.label || "",
               strategies: strat, translateAll: wantsTranslateAll(),
               trans: {}, transOff: new Set() };
  if (!cards.length) toast("対象カードがありません");
  switchTab("review");
}
function startReview() { loadReviewQueue("normal"); }

// ---- G3: session review strategies -------------------------------------
// A layer on top of SM-2. None of these touch scheduling or card identity —
// they change the ORDER cards arrive in and how the question is asked. The
// choice persists in localStorage so a session picks up where you left off.
const STRATEGIES = [
  { id: "retrieval", icon: "✍️", name: "想起練習",
    hint: "答えを見る前に、自分の言葉で書き出す。思い出す努力そのものが記憶を作ります。" },
  { id: "interleave", icon: "🔀", name: "インターリーブ",
    hint: "続けて同じ教材が出ないように混ぜる。まとめて解くより手応えは重いですが、定着します。" },
  { id: "recognition", icon: "🔘", name: "選択式",
    hint: "入力ではなく4択で答える。同じコースの他のカードの答えが選択肢になります。" },
  { id: "elaborate", icon: "💡", name: "精緻化",
    hint: "答え合わせのあと「なぜ？何とつながる？」を一言で説明します。" },
];
function activeStrategies() {
  try {
    const raw = JSON.parse(localStorage.getItem("review_strategies") || "null");
    if (Array.isArray(raw)) return raw.filter((s) => STRATEGIES.some((x) => x.id === s));
  } catch (e) {}
  return ["retrieval"];                 // the default: recall, not re-reading
}
function setStrategies(list) {
  try { localStorage.setItem("review_strategies", JSON.stringify(list)); } catch (e) {}
}
function hasStrategy(id) { return (S.review.strategies || []).indexOf(id) >= 0; }

// G3 精緻化: after the answer, ask WHY. Explaining it to yourself is what turns a
// recognised fact into a usable one. The prompt varies by card so it does not
// become wallpaper; the note is scratch only — deliberately not persisted, since
// storing it would change what a card IS and drag in the D-5 identity question.
const ELAB_PROMPTS = [
  "なぜそうなるのか、一言で説明してみよう。",
  "これは今まで習った何とつながる？",
  "自分の言葉で言い換えるとどうなる？",
  "これが成り立たない例／例外はある？",
  "友達に説明するなら最初の一文は？",
];
function elaborateBox(card, r) {
  const box = el("div", "rc-elab");
  box.appendChild(el("div", "rc-elab-t", "💡 " + ELAB_PROMPTS[(card.id || 0) % ELAB_PROMPTS.length]));
  const ta = el("textarea", "rc-input");
  ta.placeholder = "ここは記録されません。声に出すだけでも効果があります。";
  ta.value = r.elab || "";
  ta.oninput = () => { r.elab = ta.value; };
  ta.onclick = (e) => e.stopPropagation();
  box.appendChild(ta);
  return box;
}

// H6 画像オクルージョン: the image with every region masked; the one this card
// asks about is highlighted, and revealing lifts only that mask. Rectangles are
// stored as FRACTIONS of the image, so a card drawn on a phone lines up on a
// laptop no matter what size the image renders at.
function occlusionBox(occ, revealed) {
  const wrap = el("div", "occ-wrap");
  const img = el("img", "occ-img");
  img.src = "/" + String(occ.image || "").replace(/^\/+/, "");
  img.alt = "";
  img.onerror = () => { wrap.appendChild(el("div", "review-hint", "画像を読み込めません")); img.remove(); };
  wrap.appendChild(img);
  (occ.rects || []).forEach((r, i) => {
    const isTarget = i === occ.target;
    if (isTarget && revealed) return;            // lift only the asked-about mask
    const box = el("div", "occ-rect" + (isTarget ? " target" : ""));
    box.style.left = (r.x * 100) + "%";
    box.style.top = (r.y * 100) + "%";
    box.style.width = (r.w * 100) + "%";
    box.style.height = (r.h * 100) + "%";
    wrap.appendChild(box);
  });
  return wrap;
}

// H5 多肢選択: the card's own stored distractors + its real answer. Shares the
// option UI with G3 recognition, but needs no fetch — and after the reveal it can
// mark which option was right, since the answer is known locally.
function choiceBox(card, r, choices) {
  const real = (card.back || "").trim();
  if (!r.opts || r.optsFor !== card.id) {
    const opts = choices.slice(0, 5).concat([real]);
    const k = (card.id || 0) % opts.length;      // deterministic slot for the answer
    r.opts = opts.slice(k).concat(opts.slice(0, k));
    r.optsFor = card.id;
  }
  const box = el("div", "rc-choices");
  r.opts.forEach((o) => {
    const b = el("button", "rc-choice" + (r.pick === o ? " picked" : ""));
    b.appendChild(mdInline(o));
    b.onclick = (e) => {
      e.stopPropagation();
      box.querySelectorAll(".rc-choice").forEach((x) => x.classList.remove("picked"));
      b.classList.add("picked");
      r.pick = o;
    };
    box.appendChild(b);
  });
  box.appendChild(el("div", "review-hint", "選んでからタップして答え合わせ"));
  return box;
}

// G3 選択式: 4 options — the real answer plus distractors pulled from other cards
// in the same course. Options are placed by a deterministic per-card offset, so
// the answer is not always in the same slot but a re-render does not move it.
function recognitionBox(card, r) {
  const box = el("div", "rc-choices");
  const real = (card.back || "").trim();
  // "have we TRIED for this card" is r.optsFor alone — r.opts stays part of the
  // guard only for rendering. Using a null r.opts as the empty-result sentinel
  // made the guard false, so every renderReview() refetched and re-rendered in an
  // unbounded loop for any card whose course yields no distractors (a singleton
  // course, a NULL-course manual card, or all-duplicate backs).
  if (r.optsFor === card.id && !r.optsLoading) {
    if (!r.opts || !r.opts.length) {
      // no plausible wrong options exist — degrade to the plain reveal flow
      box.appendChild(el("div", "review-hint", "タップして答えを見る"));
      return box;
    }
    const pick = (val, btn) => {
      box.querySelectorAll(".rc-choice").forEach((b) => b.classList.remove("picked"));
      btn.classList.add("picked");
      r.pick = val;
    };
    r.opts.forEach((o) => {
      const b = el("button", "rc-choice" + (r.pick === o ? " picked" : ""));
      b.appendChild(mdInline(o));
      b.onclick = (e) => { e.stopPropagation(); pick(o, b); };
      box.appendChild(b);
    });
    box.appendChild(el("div", "review-hint", "選んでからタップして答え合わせ"));
    return box;
  }
  box.appendChild(el("div", "review-hint", "選択肢を準備中…"));
  if (r.optsLoading === card.id) return box;     // fetch already in flight
  r.optsLoading = card.id;
  api(`/api/cards/${card.id}/distractors?limit=3`).then((d) => {
    const ds = (d && d.distractors) || [];
    const opts = ds.length ? ds.concat([real]) : [];
    // deterministic rotation by card id — stable across re-renders
    const k = opts.length ? (card.id || 0) % opts.length : 0;
    r.opts = opts.slice(k).concat(opts.slice(0, k));
    r.optsFor = card.id;
    r.optsLoading = null;
    renderReview();
  }).catch(() => { r.opts = []; r.optsFor = card.id; r.optsLoading = null; renderReview(); });
  return box;
}

function strategyPicker() {
  const wrap = el("div", "strat-wrap");
  const head = el("div", "strat-head");
  head.appendChild(el("span", "strat-title", "復習のやり方"));
  const hint = el("span", "strat-sub", "セッションごとに選べます");
  head.appendChild(hint);
  wrap.appendChild(head);
  const active = activeStrategies();
  const row = el("div", "strat-chips");
  STRATEGIES.forEach((st) => {
    const on = active.indexOf(st.id) >= 0;
    const b = el("button", "strat-chip" + (on ? " on" : ""));
    b.appendChild(el("span", "strat-ico", st.icon));
    b.appendChild(el("span", "", st.name));
    b.title = st.hint;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.onclick = () => {
      const cur = activeStrategies();
      const i = cur.indexOf(st.id);
      if (i >= 0) cur.splice(i, 1); else cur.push(st.id);
      setStrategies(cur);
      const box = wrap.parentNode;
      wrap.replaceWith(strategyPicker());
      if (box) { /* re-rendered in place */ }
    };
    row.appendChild(b);
  });
  wrap.appendChild(row);
  const on = STRATEGIES.filter((st) => active.indexOf(st.id) >= 0);
  wrap.appendChild(el("div", "strat-hint",
    on.length ? on.map((st) => st.name + "：" + st.hint).join("　/　")
              : "どれも選んでいません（そのままタップして答えを見る形式）"));
  return wrap;
}

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
  c.appendChild(strategyPicker());
  p.appendChild(c);

  // J — study by (study) material: pick one material's cards, or check several and
  // merge them into one session. Shown first so "review by material" is front-and-
  // center, per the request.
  let revMats = [];
  try { revMats = await api("/api/review/materials"); } catch (e) {}
  if (revMats.length) {
    p.appendChild(el("div", "section-title", "教材から選んで復習"));
    const mc = el("div", "card rev-mat-card");
    mc.appendChild(el("div", "rev-mat-hint", "教材ごとに復習できます。複数チェックすると、まとめて1セッションにできます。"));
    const sel = new Set();
    const startMerge = el("button", "btn small primary", "選択した教材をまとめて復習");
    startMerge.disabled = true;
    revMats.forEach((mm) => {
      const token = mm.material_id == null ? "none" : String(mm.material_id);
      const row = el("label", "rev-mat-row");
      const cb = el("input", "rev-mat-cb"); cb.type = "checkbox"; cb.value = token;
      cb.onchange = () => { cb.checked ? sel.add(token) : sel.delete(token); startMerge.disabled = sel.size === 0; };
      row.appendChild(cb);
      const body = el("div", "rev-mat-body");
      body.appendChild(el("div", "rmm-title", mm.label));
      const meta = [];
      if (mm.course_name) meta.push(mm.course_name);
      if (mm.due_count) meta.push("復習 " + mm.due_count);
      if (mm.new_count) meta.push("新規 " + mm.new_count);
      meta.push("計 " + mm.total);
      body.appendChild(el("div", "rmm-meta", meta.join(" ・ ")));
      row.appendChild(body);
      const go = el("button", "btn small ghost", "この教材"); go.type = "button";
      go.onclick = (e) => { e.preventDefault(); e.stopPropagation(); loadReviewQueue("material", { material_ids: [token], label: mm.label }); };
      row.appendChild(go);
      mc.appendChild(row);
    });
    const foot = el("div", "rev-mat-foot");
    startMerge.onclick = () => { if (sel.size) loadReviewQueue("material", { material_ids: [...sel], label: `${sel.size}教材をまとめて` }); };
    foot.appendChild(startMerge);
    mc.appendChild(foot);
    p.appendChild(mc);
  }

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

// H2 — modality-aware review. Cards are still front/back + self-graded (SM-2
// contract untouched); card_type + media_json only change the interaction so
// non-memory subjects are actually studyable. Unknown/legacy types → default.
const PRODUCE_TYPES = new Set(["produce", "explain", "interpret", "predict", "compare", "elaborate"]);
const STEP_TYPES = new Set(["steps", "worked"]);
const CLOZE_RE = /_{2,}|\{\{[^}]*\}\}|｛｛[^｝]*｝｝/;
function _lines(s) { return (s || "").split("\n").map((x) => x.trim()).filter(Boolean); }
function clozeText(frontText, backText, filled) {
  // Replacer FUNCTION (not a string) so a "$" in the answer isn't read as a
  // regex $-special. Keep /g: every blank must render the same way, else a 2nd
  // {{marker}} would leak its answer literally in the unfilled state.
  const fill = filled ? "【" + (backText || "？") + "】" : "____";
  return (frontText || "").replace(new RegExp(CLOZE_RE.source, "g"), () => fill);
}
function revealLabel(ct) {
  if (PRODUCE_TYPES.has(ct) || ct === "compute") return "答え合わせ";
  if (STEP_TYPES.has(ct)) return "手順を見る";
  return "答えを見る";
}
function renderCard(p) {
  const r = S.review;
  const card = r.queue[r.idx];
  r.trans = r.trans || {};
  r.transOff = r.transOff || new Set();
  // J — language flip. 全部翻訳 (r.translateAll, a BOOLEAN) shows every card in the
  // opposite of ITS OWN language, so a mixed JA/EN deck flips each card correctly
  // (never same-language). A per-card 🌐 overrides one card; r.transOff opts a card
  // out while 全部翻訳 is on. A translated card renders as plain Q&A (modality extras
  // dropped). _transErr guards against an infinite retry loop when a translate fails.
  const target = detectLang(card.front) === "ja" ? "en" : "ja";
  if (r.translateAll && !r.trans[card.id] && !r.transOff.has(card.id) && card._transErr !== target) {
    const cached = card.translation && card.translation[target];
    if (cached && cached.front && cached.back)
      r.trans[card.id] = { lang: target, front: cached.front, back: cached.back };
    else ensureTranslation(card, target);   // async -> re-renders when done
  }
  if (r.translateAll) prefetchTranslations();
  const tr = r.trans[card.id] || null;
  const dispFront = tr ? tr.front : card.front;
  const dispBack = tr ? tr.back : card.back;
  const ct = tr ? "qa" : (card.card_type || "qa");
  const mj = tr ? {} : (card.media_json || {});
  const isCloze = ct === "cloze" && CLOZE_RE.test(dispFront || "");
  const wrap = el("div", "review-wrap");

  const top = el("div", "review-top");
  top.appendChild(el("span", "pill", card.why_now));
  if (card.course_name) top.appendChild(el("span", "pill " + (card.subject_type || "other"), card.course_name));
  top.appendChild(el("span", "review-progress", `${r.idx + 1} / ${r.queue.length}${r.label ? " ・ " + r.label : ""}`));
  // language flip: per-card 🌐 (toggles this card) + a session 全部翻訳 toggle.
  const langWrap = el("div", "rc-langs");
  const flip = el("button", "btn small ghost lang-flip",
    tr ? "🌐 原文に戻す" : (target === "en" ? "🌐 English" : "🌐 日本語"));
  flip.title = tr ? "このカードを元の言語に戻します"
                  : "このカードだけ" + (target === "en" ? "英語" : "日本語") + "で表示します";
  flip.onclick = (e) => { e.stopPropagation(); flipCardLang(card); };
  langWrap.appendChild(flip);
  const allBtn = el("button", "btn small ghost lang-all" + (r.translateAll ? " on" : ""),
    r.translateAll ? "✓ 全部翻訳中" : "🌐 全部翻訳");
  allBtn.title = r.translateAll
    ? "全部翻訳をやめて、すべて元の言語に戻します"
    : "このセッションのカードをすべて翻訳して表示します（次のカードも自動で切り替わります）";
  allBtn.onclick = (e) => {
    e.stopPropagation();
    if (r.translateAll) { r.translateAll = false; r.trans = {}; r.transOff = new Set(); }   // off -> clear all
    else { r.translateAll = true; r.transOff = new Set(); }
    setTranslateAll(r.translateAll);   // remember it for the next session too
    renderReview();
  };
  langWrap.appendChild(allBtn);
  if (card._translating) langWrap.appendChild(el("span", "rc-tr-busy", "翻訳中…"));
  top.appendChild(langWrap);
  wrap.appendChild(top);

  const face = el("div", "review-card");
  face.appendChild(isCloze
    ? el("div", "review-front", clozeText(dispFront, dispBack, r.revealed))
    : elMd("div", "review-front", dispFront));
  // H6: the masked image sits right under the question, on both faces
  if (ct === "occlusion" && mj.occlusion) {
    face.appendChild(occlusionBox(mj.occlusion, r.revealed));
  }

  if (!r.revealed) {
    if (PRODUCE_TYPES.has(ct)) {
      const ta = el("textarea", "rc-input"); ta.placeholder = "自分の言葉で答えを書いてみよう（採点は自分で）";
      ta.value = r.draft || ""; ta.oninput = () => { r.draft = ta.value; }; ta.onclick = (e) => e.stopPropagation();
      face.appendChild(ta);
      face.appendChild(el("div", "review-hint", "書けたら「答え合わせ」で模範解答と照合"));
    } else if (ct === "compute") {
      const inp = el("input", "rc-input"); inp.placeholder = "自分で計算して答えを入力";
      inp.value = r.draft || ""; inp.oninput = () => { r.draft = inp.value; }; inp.onclick = (e) => e.stopPropagation();
      face.appendChild(inp);
      face.appendChild(el("div", "review-hint", "解けたら「答え合わせ」"));
    } else if (STEP_TYPES.has(ct)) {
      face.appendChild(el("div", "review-hint", "手順を思い出してからタップ"));
    } else if (isCloze) {
      face.appendChild(el("div", "review-hint", "空所に入る語を考えてタップ"));
    } else if (ct === "choice" && Array.isArray(mj.choices) && mj.choices.length) {
      // H5 多肢選択: this card carries its OWN distractors, so no fetch is needed
      face.appendChild(choiceBox(card, r, mj.choices));
    } else if (hasStrategy("recognition")) {
      // G3 選択式: options built from other cards' answers (fetched per card)
      face.appendChild(recognitionBox(card, r));
    } else if (hasStrategy("retrieval")) {
      // G3 想起練習: make the recall attempt explicit before the answer shows
      const ta = el("textarea", "rc-input");
      ta.placeholder = "答えを思い出して書いてみよう（見る前に）";
      ta.value = r.draft || ""; ta.oninput = () => { r.draft = ta.value; };
      ta.onclick = (e) => e.stopPropagation();
      face.appendChild(ta);
      face.appendChild(el("div", "review-hint", "書けたらタップして答え合わせ"));
    } else {
      face.appendChild(el("div", "review-hint", "タップして答えを見る"));
    }
  } else {
    face.appendChild(el("hr", "review-sep"));
    if ((PRODUCE_TYPES.has(ct) || ct === "compute" || hasStrategy("retrieval")) && r.draft) {
      const y = el("div", "rc-yourans"); y.appendChild(el("div", "rc-yourans-t", "あなたの答え")); y.appendChild(el("div", "", r.draft)); face.appendChild(y);
    }
    if (r.pick != null) {
      // the answer is known locally, so say plainly whether the pick was right —
      // the 4-level self-grade below still decides the scheduling (H5 keeps it)
      const ok = String(r.pick).trim() === String(card.back || "").trim();
      const p = el("div", "rc-yourans" + (ok ? " ok" : " ng"));
      p.appendChild(el("div", "rc-yourans-t", ok ? "選んだ答え（正解）" : "選んだ答え（不正解）"));
      p.appendChild(elMd("div", "", r.pick));
      face.appendChild(p);
    }
    if (STEP_TYPES.has(ct)) {
      const steps = (Array.isArray(mj.steps) && mj.steps.length) ? mj.steps : _lines(card.back);
      const ol = el("ol", "rc-steps"); steps.forEach((s) => ol.appendChild(elMd("li", "", s))); face.appendChild(ol);
    } else if (ct === "list") {
      const items = (Array.isArray(mj.items) && mj.items.length) ? mj.items : _lines(card.back);
      const box = el("div", "rc-list"); items.forEach((s) => { const lab = el("label", "rc-check"); const cb = el("input"); cb.type = "checkbox"; lab.appendChild(cb); lab.appendChild(elMd("span", "", s)); box.appendChild(lab); }); face.appendChild(box);
    } else if (!isCloze) {
      face.appendChild(elMd("div", "review-back", dispBack));
    }
    // compute shows the answer (back) above; add the worked steps if provided.
    if (ct === "compute" && Array.isArray(mj.steps) && mj.steps.length) {
      const ol = el("ol", "rc-steps"); mj.steps.forEach((s) => ol.appendChild(elMd("li", "", s))); face.appendChild(ol);
    }
    if (mj.rubric) { const rb = el("div", "rc-rubric"); rb.appendChild(el("div", "rc-rubric-t", "自己採点の観点")); rb.appendChild(elMd("div", "", mj.rubric)); face.appendChild(rb); }
    if (hasStrategy("elaborate")) face.appendChild(elaborateBox(card, r));
    if (card.thumb_url) { const img = el("img", "review-thumb"); img.src = card.thumb_url; img.alt = ""; img.onerror = () => img.remove(); face.appendChild(img); }
    if (card.source_quote) { const q = el("div", "cp-quote"); q.textContent = "「" + card.source_quote + "」"; face.appendChild(q); }
    if (card.material_id && (card.source_loc || card.source_quote)) {
      const src = el("button", "btn small ghost", "📍 出典を見る");
      src.onclick = (e) => { e.stopPropagation(); openMaterial(card.material_id, card.source_loc || card.source_quote); };
      face.appendChild(src);
    }
    const bad = el("button", "btn small ghost", "この問題おかしい");
    bad.onclick = async (e) => { e.stopPropagation(); await api("/api/review/report", { method: "POST", body: JSON.stringify({ card_id: card.id, verdict: "wrong" }) }); toast("報告しました"); };
    face.appendChild(bad);
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
    const b = el("button", "btn primary reveal-btn", revealLabel(ct));
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
  // every per-card scratch field must clear, or the next card inherits this
  // card's typed answer / chosen option / generated choices (G3)
  r.idx++; r.revealed = false; r.draft = "";
  r.pick = null; r.opts = null; r.optsFor = null; r.optsLoading = null; r.elab = "";
  await refreshMeta();
  renderReview();
}

// J — flip ONE card between its original and the opposite language (ja<->en).
// Cached translations (card.translation[lang], also refreshed onto the object here)
// make a re-flip instant; the first flip calls the AI once.
// 全部翻訳 means "show this whole deck in the other language". Each card still
// needs one AI call the first time (5-60s), so without this the next card always
// appears in its original language while you wait — which reads as the toggle not
// working. Warm the next few cards in the background so advancing is instant.
const TRANSLATE_AHEAD = 3;
// 全部翻訳 persists across sessions: someone studying an English deck in Japanese
// wants that every time, not once per session.
function wantsTranslateAll() {
  try { return localStorage.getItem("review_translate_all") === "1"; } catch (e) { return false; }
}
function setTranslateAll(on) {
  try { localStorage.setItem("review_translate_all", on ? "1" : "0"); } catch (e) {}
}

function prefetchTranslations() {
  const r = S.review;
  if (!r || !r.translateAll || !r.queue) return;
  let started = 0;
  for (let i = r.idx + 1; i < r.queue.length && started < TRANSLATE_AHEAD; i++) {
    const c = r.queue[i];
    if (!c) continue;
    // an in-flight translation COUNTS toward the budget: skipping it meant every
    // quick grade() started 3 more calls past the still-running ones, stacking
    // concurrent claude processes linearly with grading speed
    if (c._translating) { started++; continue; }
    const t = detectLang(c.front) === "ja" ? "en" : "ja";
    if (c._transErr === t) continue;
    if (r.transOff && r.transOff.has(c.id)) continue;
    if (c.translation && c.translation[t] && c.translation[t].front) continue;  // cached
    started++;
    // silent: a background warm-up must never raise a toast at the learner
    ensureTranslation(c, t, { silent: true, prefetch: true });
  }
}

async function flipCardLang(card) {
  const r = S.review; r.trans = r.trans || {}; r.transOff = r.transOff || new Set();
  if (r.trans[card.id]) {
    delete r.trans[card.id];
    if (r.translateAll) r.transOff.add(card.id);   // opt this card out of 全部翻訳
    renderReview(); return;
  }
  r.transOff.delete(card.id);                       // re-including it
  const target = detectLang(card.front) === "ja" ? "en" : "ja";
  const cached = card.translation && card.translation[target];
  if (cached && cached.front && cached.back) {
    r.trans[card.id] = { lang: target, front: cached.front, back: cached.back };
    renderReview(); return;
  }
  await ensureTranslation(card, target, { manual: true });
}

// Fetch+cache a card's translation into `target`; applies it to the current review
// session (r.trans) and re-renders. `_translating` guards double-fetches (the 全部
// 翻訳 auto-trigger re-enters across renders). It's applied only if STILL wanted on
// resolve — manual flips always, auto only while 全部翻訳 is on and the card isn't
// opted out — so turning translation off mid-request doesn't snap the card back.
async function ensureTranslation(card, target, opts) {
  opts = opts || {};
  if (card._translating) return;
  card._translating = true;
  try {
    const res = await api(`/api/cards/${card.id}/translate`, { method: "POST", body: JSON.stringify({ lang: target }) });
    if (!res.ok) { card._transErr = target; if (!opts.silent) toast(res.message || "翻訳に失敗しました"); return; }
    delete card._transErr;
    card.translation = card.translation || {};
    card.translation[target] = { front: res.translation.front, back: res.translation.back };
    const r = S.review; r.trans = r.trans || {};
    const wanted = opts.manual || (r.translateAll && !(r.transOff && r.transOff.has(card.id)));
    if (wanted) r.trans[card.id] = { lang: target, front: res.translation.front, back: res.translation.back };
  } catch (e) { card._transErr = target; if (!opts.silent) toast("翻訳に失敗: " + e.message); }
  finally {
    card._translating = false;
    // Re-render ONLY if the card this translation belongs to is the one on
    // screen. A resolution for a card the user already graded past has nothing
    // to show — re-rendering anyway rebuilt the CURRENT card's DOM, dropping
    // textarea focus (and any IME composition) mid-answer.
    const r2 = S.review;
    const onScreen = r2 && r2.queue && r2.queue[r2.idx] && r2.queue[r2.idx].id === card.id;
    if (S.tab === "review" && onScreen) renderReview();
  }
}

// ---------- MATERIALS (Phase 3) ----------
const STAGE = { extracting: "文字を読む…", generating: "カード作成…", done: "完了", failed: "失敗" };
const selMat = new Set();

// ---- E1: honest upload/analysis progress (indeterminate — no fake %) ----
const STAGE_STEPS = ["upload", "extract", "generate", "done"];
function stageIndex(status) {
  if (status === "extracting") return 1;
  if (status === "generating") return 2;
  if (status === "done") return 3;
  return 1;
}
function fmtElapsed(iso) {
  if (!iso) return "";
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 1000));
  const m = Math.floor(s / 60);
  return m > 0 ? m + "分" + String(s % 60).padStart(2, "0") + "秒" : s + "秒";
}
// Rough completion estimate — an honest 目安 from typical extract+generate time
// (no real sub-progress signal exists). Overshoot degrades to "まもなく…".
const ETA_TOTAL_SEC = 60;
function fmtEta(iso, status) {
  if (!iso || status === "done" || status === "failed") return "";
  const elapsed = Math.max(0, Math.floor((Date.now() - new Date(iso)) / 1000));
  let rem = ETA_TOTAL_SEC - elapsed;
  const soon = rem < 10;
  if (soon) rem = 10;   // soft floor: keep showing an approx time even past the estimate
  const m = Math.floor(rem / 60), s = rem % 60;
  const t = m > 0 ? m + "分" + String(s).padStart(2, "0") + "秒" : s + "秒";
  return (soon ? "まもなく完了 — 残りおよそ " : "残りおよそ ") + t + "（目安）";
}
function buildProgress(m) {
  const box = el("div", "mat-progress");
  const bar = el("div", "mat-bar"); bar.appendChild(el("div", "mat-bar-fill")); box.appendChild(bar);
  const steps = el("div", "mat-steps");
  const active = stageIndex(m.status);
  for (let i = 0; i < STAGE_STEPS.length; i++)
    steps.appendChild(el("span", "mat-dot" + (i < active ? " done" : i === active ? " active" : "")));
  box.appendChild(steps);
  const meta = el("div", "mat-elapsed");
  meta.appendChild(el("span", "", m.status === "extracting" ? "読み取り中… " : "カード作成中… "));
  meta.appendChild(el("span", "mat-elapsed-t", fmtElapsed(m.created_at)));
  box.appendChild(meta);
  const eta = el("div", "mat-eta");
  eta.appendChild(el("span", "mat-eta-t", fmtEta(m.created_at, m.status)));
  box.appendChild(eta);
  return box;
}
function uploadingTile() {
  const t = el("div", "mat-tile inflight uploading");
  t.appendChild(el("div", "mat-ph", "⬆️"));
  const box = el("div", "mat-progress");
  const bar = el("div", "mat-bar"); bar.appendChild(el("div", "mat-bar-fill")); box.appendChild(bar);
  box.appendChild(el("div", "mat-elapsed", "アップロード中…"));
  t.appendChild(box);
  return t;
}
let elapsedTimer = null;
function ensureElapsedTicker() {
  if (elapsedTimer) return;
  elapsedTimer = setInterval(() => {
    let any = false;
    document.querySelectorAll(".mat-tile.inflight[data-created]").forEach((t) => {
      const tt = t.querySelector(".mat-elapsed-t"); if (!tt || !t.dataset.created) return;
      tt.textContent = fmtElapsed(t.dataset.created);
      const et = t.querySelector(".mat-eta-t"); if (et) et.textContent = fmtEta(t.dataset.created);
      any = true;
    });
    if (!any) { clearInterval(elapsedTimer); elapsedTimer = null; }
  }, 1000);
}

// ---- E2: failure explainer — cause / how-to-succeed / rough odds (heuristic) ----
function oddsClass(level) { return level === "高い" ? "good" : level === "低い" ? "bad" : "mid"; }
function classifyFailure(msg, exhausted, hasText) {
  msg = msg || "";
  const at = (p) => msg.indexOf(p) >= 0;
  if (exhausted || at("何度か試しました")) return {
    cause: "何度試しても読み取れませんでした（画像が不鮮明・内容が複雑など）",
    fix: ["明るく・正面から・ピントを合わせて撮り直す", "1枚に詰め込みすぎず、ページごとに分ける", "手書きは濃く、できれば活字を使う", "難しければ手動入力が確実です"],
    odds: { level: "中", note: "撮り直しで改善することが多いですが、内容次第です" }, stage: hasText ? "generate" : "extract" };
  if (at("AI呼び出し失敗")) return {
    cause: "AIの呼び出しに失敗（タイムアウト・CLI未検出・混雑など）",
    fix: ["少し待ってから「もう一度試す」", "claude CLI が使えるか確認する"],
    odds: { level: "高い", note: "一時的なことが多く、再試行で成功しやすいです" }, stage: "extract" };
  if (at("解析結果を読めません")) return {
    cause: "AIは応答しましたが、結果の形式を読み取れませんでした",
    fix: ["「もう一度試す」で再抽出する", "画像を鮮明なものに差し替える"],
    odds: { level: "高い", note: "再試行で整うことが多いです" }, stage: "extract" };
  if (at("生成失敗")) return {
    cause: "カード生成のAI呼び出しに失敗しました",
    fix: ["「再生成」で作り直す（抽出結果は再利用）", "少し待ってから再試行する"],
    odds: { level: "高い", note: "抽出は成功済み。再生成で通ることが多いです" }, stage: "generate" };
  if (at("生成結果を読めません")) return {
    cause: "カード生成の結果を読み取れませんでした",
    fix: ["「再生成」で作り直す"],
    odds: { level: "高い", note: "再生成でほぼ解決します" }, stage: "generate" };
  return { cause: msg || "解析に失敗しました",
    fix: ["「もう一度試す」または「手動で入力する」"],
    odds: { level: "中", note: "" }, stage: hasText ? "generate" : "extract" };
}

function aiOffNote() {
  const n = el("div", "ai-off-note");
  n.appendChild(el("div", "", "⚠️ AI機能は現在使えません（claude CLI 未検出）"));
  n.appendChild(el("div", "sub", "写真は保存できますが、カード生成はされません。「＋追加」から手動で入力できます。"));
  return n;
}

async function renderMaterials() {
  const p = $("#panel-materials"); p.innerHTML = "";
  if (S.meta && !S.meta.claude_ok) p.appendChild(aiOffNote());  // pre-empt wasted drops
  // click-to-upload (not just drag & drop): works on any device, accepts 写真・PDF
  const upbar = el("div", "mat-upbar");
  const upbtn = el("button", "btn primary", "＋ 教材をアップロード");
  upbtn.onclick = () => { const fi = $("#file-input"); if (fi) fi.click(); };
  upbar.appendChild(upbtn);
  upbar.appendChild(el("span", "mat-uphint", "写真・PDF に対応（ドラッグ&ドロップもOK）"));
  p.appendChild(upbar);
  let mats;
  try { mats = await api("/api/materials"); } catch (e) { p.appendChild(el("div", "empty", "読み込み失敗")); return; }
  if (!mats.length && !S.uploading) {
    const e = el("div", "empty");
    e.appendChild(el("div", "big", "📄"));
    e.appendChild(el("div", "", "上のボタンから写真・PDFを選ぶか、どこにでもドロップすると解析して復習カードを作ります"));
    p.appendChild(e); return;
  }
  // range presets (only when there are stored materials)
  if (mats.length) {
    const bar = el("div", "filters");
    [["week", "今週"], ["month", "先月〜"], ["all", "全部"]].forEach(([k, l]) => {
      const b = el("button", "btn small ghost", l);
      b.onclick = () => { selectRange(mats, k); renderMaterials(); };
      bar.appendChild(b);
    });
    p.appendChild(bar);
  }

  const grid = el("div", "mat-grid");
  for (let i = 0; i < (S.uploading || 0); i++) grid.appendChild(uploadingTile());
  mats.forEach((m) => grid.appendChild(matTile(m)));
  p.appendChild(grid);
  ensureElapsedTicker();

  if (selMat.size) {
    const tb = el("div", "select-bar");
    tb.appendChild(el("span", "", `${selMat.size}件 選択`));
    const gen = el("button", "btn primary small", "選択から復習教材を作る");
    gen.onclick = async () => {
      for (const id of selMat) await api(`/api/materials/${id}/regenerate`, { method: "POST" });
      toast("再生成をキューに入れました", true); selMat.clear(); await refreshMeta(); renderMaterials();
    };
    tb.appendChild(gen);
    const del = el("button", "btn small danger", "🗑 削除");
    del.onclick = () => confirmDeleteMaterials(mats, p);
    tb.appendChild(del);
    const clear = el("button", "btn small ghost", "選択を解除");
    clear.onclick = () => { selMat.clear(); renderMaterials(); };
    tb.appendChild(clear);
    p.appendChild(tb);
  }
}

// Deleting a material does NOT have to take its cards with it — cards are
// detached (material_id -> NULL) and keep their review history. That is the safe
// default, but it is only safe if the user is told, so the count is shown and
// taking the cards too is an explicit opt-in rather than a surprise.
function confirmDeleteMaterials(mats, p) {
  const chosen = mats.filter((m) => selMat.has(m.id));
  const cardCount = chosen.reduce((n, m) => n + (m.card_count || 0), 0);
  const old = p.querySelector(".mat-del-confirm");
  if (old) old.remove();

  const box = el("div", "card mat-del-confirm");
  box.appendChild(el("div", "focus-title", `${chosen.length}件の教材を削除しますか？`));
  const names = chosen.slice(0, 5).map((m) => m.summary || m.original_path || `教材 ${m.id}`);
  box.appendChild(el("div", "meta-line",
    names.map((s) => "・" + String(s).slice(0, 40)).join("\n")
    + (chosen.length > 5 ? `\n…ほか${chosen.length - 5}件` : "")));

  let dropCards = false;
  if (cardCount) {
    const lab = el("label", "mat-del-opt");
    const cb = el("input"); cb.type = "checkbox";
    cb.onchange = () => { dropCards = cb.checked; hint.textContent = msg(); };
    lab.appendChild(cb);
    lab.appendChild(el("span", "", `ひもづくカード ${cardCount}枚 も一緒に削除する`));
    box.appendChild(lab);
  }
  const msg = () => (!cardCount
    ? "この教材にカードはありません。"
    : dropCards
      ? `カード ${cardCount}枚 も削除されます。復習の履歴も消えます。`
      : `カード ${cardCount}枚 は残ります（「教材なし」に移動し、復習はそのまま続けられます）。`);
  const hint = el("div", "meta-line", msg());
  box.appendChild(hint);

  const row = el("div", "proposal-actions");
  const go = el("button", "btn small danger", "削除する");
  go.onclick = async () => {
    const restore = btnBusy(go, "削除中…");
    let gone = 0, cards = 0;
    try {
      for (const id of Array.from(selMat)) {
        const res = await api(`/api/materials/${id}` + (dropCards ? "?cards=1" : ""),
                              { method: "DELETE" });
        gone++; cards += (res && res.deleted_cards) || 0;
      }
      toast(`${gone}件の教材を削除しました` + (cards ? `（カード${cards}枚も削除）` : ""), true);
      selMat.clear();
      await refreshMeta();
      renderMaterials();
    } catch (e) {
      toast("削除に失敗: " + e.message);
      restore();
    }
  };
  const no = el("button", "btn small ghost", "やめる");
  no.onclick = () => box.remove();
  row.appendChild(go); row.appendChild(no);
  box.appendChild(row);
  p.appendChild(box);
  box.scrollIntoView({ behavior: "smooth", block: "center" });
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
  const flight = inFlight(m.status);
  const t = el("div", "mat-tile" + (selMat.has(m.id) ? " sel" : "") + (flight ? " inflight" : "") + (m.status === "failed" ? " failed" : ""));
  if (m.created_at) t.dataset.created = m.created_at;
  if (m.thumb_url && m.kind === "photo") {
    const img = el("img"); img.src = m.thumb_url; img.loading = "lazy"; img.alt = "";
    // broken thumbnail -> same neutral placeholder as a missing one (no weird icon)
    img.onerror = () => img.replaceWith(el("div", "mat-ph", "📸"));
    t.appendChild(img);
  } else { t.appendChild(el("div", "mat-ph", m.kind === "pdf" ? "📄" : "📸")); }
  if (flight) {
    t.appendChild(buildProgress(m));
  } else {
    // G1: a done material with un-approved drafts shows 確認待ち, not 完了.
    if (m.status !== "failed" && m.proposed_count > 0)
      t.appendChild(el("div", "mat-badge proposed", `確認待ち ${m.proposed_count}`));
    else
      t.appendChild(el("div", "mat-badge " + m.status, STAGE[m.status] || m.status));
    if (m.status === "failed") t.appendChild(el("div", "mat-fail-hint", classifyFailure(m.error_message, m.attempts_exhausted, m.has_text).cause));
    else if (m.summary) t.appendChild(el("div", "mat-sum", m.summary));
  }
  const sel = el("button", "mat-sel" + (selMat.has(m.id) ? " on" : ""), selMat.has(m.id) ? "✓" : "");
  sel.onclick = (e) => { e.stopPropagation(); if (selMat.has(m.id)) selMat.delete(m.id); else selMat.add(m.id); renderMaterials(); };
  t.appendChild(sel);
  t.onclick = () => openMaterial(m.id);
  return t;
}

// The material workspace. On a wide screen it lays out as three panes — the FILE
// on the left, the 文字起こし (transcript) in the middle, and the study OPTIONS
// (top-right) with the flashcards scrolling below them — so pressing 出典を
// ハイライト shows the file and its source quote side by side. It collapses to a
// single scroll on narrow screens. Failed / in-flight materials keep the simpler
// single-column view. Every zone below is appended to fileZone/transcriptZone/
// optionsZone/cardsZone, then assembled per layout at the end.
async function openMaterial(mid, highlight) {
  let m;
  try { m = await api(`/api/materials/${mid}`); } catch (e) { toast("読み込み失敗"); return; }
  const ov = el("div", "modal-overlay");
  const box = el("div", "modal mat-modal");
  const close = el("button", "modal-close", "✕"); close.onclick = () => ov.remove();
  box.appendChild(close);

  const cards = m.cards || [];
  const trFull = m.extracted_text || "";
  const hasTranscript = !!trFull.trim();
  const settled = m.status !== "failed" && !inFlight(m.status);
  const useWorkspace = settled && hasTranscript;   // 3-pane view only when there's a file + transcript to study

  // header (title + status)
  const head = el("div", "mat-head");
  head.appendChild(el("h3", "mat-head-title", m.summary || "教材"));
  if (inFlight(m.status)) head.appendChild(buildProgress(m));
  else head.appendChild(el("div", "mat-badge inline " + m.status, STAGE[m.status] || m.status));
  box.appendChild(head);

  // ---- transcript element + E5 quote-highlight — built once, placed per layout ----
  const anyLoc = cards.some((c) => c.source_loc || c.source_quote);
  let trPre = null;
  if (hasTranscript) { trPre = el("pre", "guide-md tr-pre"); trPre.textContent = trFull; }
  function findSpan(loc) {
    if (loc && Number.isInteger(loc.char_start) && Number.isInteger(loc.char_end)
        && loc.char_end > loc.char_start && loc.char_end <= trFull.length)
      return [loc.char_start, loc.char_end];
    const q = (loc && loc.quote) || (typeof loc === "string" ? loc : "");
    if (!q) return null;
    const i = trFull.indexOf(q);
    if (i >= 0) return [i, i + q.length];
    const esc = q.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
    try { const mm = new RegExp(esc).exec(trFull); if (mm) return [mm.index, mm.index + mm[0].length]; } catch (e) {}
    return null;
  }
  function highlightQuote(loc) {
    if (!trPre) { toast("文字起こしがありません"); return; }
    const det = trPre.closest("details"); if (det) det.open = true;
    const span = findSpan(loc);
    if (!span) { trPre.textContent = trFull; toast("該当箇所が見つかりませんでした"); return; }
    trPre.textContent = "";
    trPre.appendChild(document.createTextNode(trFull.slice(0, span[0])));
    const mark = el("mark", "tr-hit", trFull.slice(span[0], span[1]));
    trPre.appendChild(mark);
    trPre.appendChild(document.createTextNode(trFull.slice(span[1])));
    mark.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  const addLoc = (node, c) => {
    if (c.source_loc || c.source_quote) {
      node.classList.add("locatable");
      node.title = "クリックで出典をハイライト";
      node.appendChild(el("div", "cp-locate", "📍 出典をハイライト"));
      node.onclick = () => highlightQuote(c.source_loc || c.source_quote);
    }
    return node;
  };

  // ---- ZONES ----
  const fileZone = el("div", "mw-file");
  fileZone.appendChild(buildViewer(m));

  const optionsZone = el("div", "mw-options");
  // Phase I — study modes (options) live top-right; need transcription + a settled material.
  if (useWorkspace) optionsZone.appendChild(studyModesSection(m, ov));

  const cardsZone = el("div", "mw-cards");
  // `box2` is the container the rest of this function appends to (failure box,
  // proposal gate, cards, guides, add-card, footer). In the workspace it's the
  // right-hand cards column; otherwise the modal body directly.
  const box2 = cardsZone;

  if (m.status === "failed") {
    // Explain the failure: cause + how to succeed + a rough (heuristic) success
    // likelihood, then offer the right recovery. If extraction already succeeded
    // (has_text) 再生成 reuses it; else もう一度試す re-runs from scratch; when the
    // retry budget is spent, 手動で入力する becomes the primary path.
    const info = classifyFailure(m.error_message, m.attempts_exhausted, m.has_text);
    const err = el("div", "err-box");
    const head = el("div", "err-head");
    head.appendChild(el("span", "err-ico", "⚠️"));
    head.appendChild(el("span", "err-cause", info.cause));
    err.appendChild(head);
    const odds = el("div", "err-odds odds-" + oddsClass(info.odds.level));
    odds.appendChild(el("span", "odds-tag", "成功の見込み " + info.odds.level));
    if (info.odds.note) odds.appendChild(el("span", "odds-note", info.odds.note));
    err.appendChild(odds);
    err.appendChild(el("div", "err-sub", "こうすると成功しやすい"));
    const ul = el("ul", "err-fix");
    info.fix.forEach((f) => ul.appendChild(el("li", "", f)));
    err.appendChild(ul);
    if (m.error_message) {
      const det = el("details", "err-raw");
      det.appendChild(el("summary", "", "詳細メッセージ"));
      det.appendChild(el("div", "", m.error_message));
      err.appendChild(det);
    }
    const exhausted = m.attempts_exhausted;
    const actions = el("div", "add-row end");
    const mk = (cls, label, fn) => { const b = el("button", "btn small " + cls, label); b.onclick = fn; return b; };
    const retryFn = async () => { await api(`/api/materials/${mid}/retry`, { method: "POST" }); toast("再試行中…"); ov.remove(); await refreshMeta(); renderMaterials(); };
    const regenFn = async () => { await api(`/api/materials/${mid}/regenerate`, { method: "POST" }); toast("再生成中…"); ov.remove(); await refreshMeta(); renderMaterials(); };
    const manFn = () => { ov.remove(); switchTab("assignments"); openAdd(); };
    if (exhausted) {
      actions.appendChild(mk("primary", "手動で入力する", manFn));
      if (m.has_text) actions.appendChild(mk("ghost", "再生成", regenFn));
      actions.appendChild(mk("ghost", "もう一度試す", retryFn));
    } else if (m.has_text) {
      actions.appendChild(mk("primary", "再生成", regenFn));
      actions.appendChild(mk("ghost", "手動で入力する", manFn));
    } else {
      actions.appendChild(mk("primary", "もう一度試す", retryFn));
      actions.appendChild(mk("ghost", "手動で入力する", manFn));
    }
    err.appendChild(actions);
    box2.appendChild(err);
  }

  // G1: proposal / confirmation gate — proposed cards await approval (nothing
  // enters the review queue until confirmed). Recommended = one click; 項目を選ぶ
  // = per-item checklist, each with its E5 source-highlight.
  const proposed = cards.filter((c) => c.state === "proposed");
  if (proposed.length) {
    const panel = el("div", "proposal");
    panel.appendChild(el("div", "proposal-head", `🎯 学習プランの提案 — ${proposed.length}項目`));
    panel.appendChild(el("div", "proposal-sub", "この教材から学べる項目です。推奨のまま学ぶか、項目を選んでください（承認するまで復習には入りません）。"));
    const approve = async (cardIds) => {
      try {
        const body = cardIds == null ? {} : { card_ids: cardIds };
        const r = await api(`/api/materials/${m.id}/approve`, { method: "POST", body: JSON.stringify(body) });
        toast(`${r.approved}項目を学習に追加しました`, true);
        ov.remove(); await refreshMeta(); renderMaterials(); openMaterial(m.id);
      } catch (e) { toast("承認に失敗: " + e.message); }
    };
    const recRow = el("div", "proposal-actions");
    const rec = el("button", "btn primary", `推奨で学ぶ（全${proposed.length}項目）`);
    rec.onclick = () => approve(null);
    recRow.appendChild(rec);
    panel.appendChild(recRow);
    const pick = el("details", "proposal-pick");
    pick.appendChild(el("summary", "", `項目を選ぶ（${proposed.length}）`));
    const rowHost = el("div", "pp-rows");
    const boxes = [];
    // A 327-word vocabulary PDF proposes hundreds of items, and every row carries
    // a method <select>. Building all of that up front stalled the material view
    // for a list that is collapsed by default, so rows are built on first open.
    let built = false;
    const buildRows = () => {
      if (built) return;
      built = true;
      proposed.forEach((c) => {
        const row = el("label", "pp-row");
        const cb = el("input", "pp-cb"); cb.type = "checkbox"; cb.checked = true; cb.value = String(c.id);
        boxes.push(cb); row.appendChild(cb);
        const bd = el("div", "pp-body");
        bd.appendChild(el("div", "pp-front", c.front));
        bd.appendChild(el("div", "pp-back", c.back));
        if (c.source_loc || c.source_quote) {
          const loc = el("button", "pp-loc", "📍 出典"); loc.type = "button";
          loc.onclick = (e) => { e.preventDefault(); e.stopPropagation(); highlightQuote(c.source_loc || c.source_quote); };
          bd.appendChild(loc);
        }
        row.appendChild(bd);
        bd.appendChild(recastRow(c, () => { ov.remove(); openMaterial(m.id); }));
        rowHost.appendChild(row);
      });
    };
    pick.addEventListener("toggle", () => { if (pick.open) buildRows(); });
    pick.appendChild(rowHost);
    const pickRow = el("div", "proposal-actions");
    const pb = el("button", "btn small primary", "選んだ項目で学ぶ");
    pb.onclick = () => {
      // untouched picker => every item is still checked, so approve them all
      const ids = built ? boxes.filter((x) => x.checked).map((x) => Number(x.value))
                        : proposed.map((c) => Number(c.id));
      if (!ids.length) { toast("1項目以上選んでください"); return; }
      approve(ids);
    };
    pickRow.appendChild(pb); pick.appendChild(pickRow);
    panel.appendChild(pick);
    box2.appendChild(panel);
  }

  // already-approved cards (new/review/suspended): generated vs extracted grouping
  const settledCards = cards.filter((c) => c.state !== "proposed");
  const lowConf = settledCards.filter((c) => c.confidence === "low");
  const normal = settledCards.filter((c) => c.confidence !== "low");
  const withCtl = (c, low) => {
    const n = addLoc(cardPreview(c, low), c);
    n.appendChild(recastRow(c, () => { ov.remove(); openMaterial(m.id); }));
    return n;
  };
  if (lowConf.length) box2.appendChild(el("div", "section-title", `要確認 ${lowConf.length}件`));
  lowConf.forEach((c) => box2.appendChild(withCtl(c, true)));
  if (normal.length) box2.appendChild(el("div", "section-title", `カード ${normal.length}枚`));
  normal.forEach((c) => box2.appendChild(withCtl(c, false)));

  if (m.guides && m.guides.length) {
    box2.appendChild(el("div", "section-title", "要点まとめ"));
    const g = el("pre", "guide-md"); g.textContent = m.guides[0].content_md; box2.appendChild(g);
  }

  // E4 — manually add a review card sourced from this material. The optional
  // 引用 is stored as source_loc {quote} so the card points back at the file
  // (a first, quote-anchored cut; richer offsets/regions come later).
  // Phase H3: add cards from this material — 🤖 auto (analyze -> propose, with an
  // optional range + free-text instruction; cards enter the G1 gate) or ✎ manual.
  const addWrap = el("div", "mat-addcard");
  const btnRow = el("div", "add-row");
  const smartBtn = el("button", "btn small primary", "🤖 この教材から自動でカードを作る");
  const manualBtn = el("button", "btn small ghost", "✎ 手動で1枚追加");
  btnRow.appendChild(smartBtn); btnRow.appendChild(manualBtn);
  addWrap.appendChild(btnRow);
  const addBody = el("div", "ac-body");
  addWrap.appendChild(addBody);
  smartBtn.onclick = () => openSmartAdd(m, ov, addBody, trPre);
  manualBtn.onclick = () => openManualAdd(m, ov, addBody);
  box2.appendChild(addWrap);

  const foot = el("div", "add-row end");
  const regen = el("button", "btn small ghost", "再生成");
  regen.onclick = async () => { await api(`/api/materials/${mid}/regenerate`, { method: "POST" }); toast("再生成中…"); ov.remove(); await refreshMeta(); };
  const del = el("button", "btn small danger", "削除");
  del.onclick = async () => { if (confirm("この教材を削除しますか？")) { await api(`/api/materials/${mid}`, { method: "DELETE" }); ov.remove(); await refreshMeta(); renderMaterials(); } };
  foot.appendChild(regen); foot.appendChild(del);
  box2.appendChild(foot);

  // ---- ASSEMBLE per layout ----
  if (useWorkspace) {
    const ws = el("div", "mat-workspace");
    const trZone = el("div", "mw-transcript");
    trZone.appendChild(el("div", "mw-h", "文字起こし（カードの出典）"));
    const trWrap = el("div", "tr-wrap"); trWrap.appendChild(trPre); trZone.appendChild(trWrap);
    ws.appendChild(fileZone);
    ws.appendChild(optionsZone);
    ws.appendChild(trZone);
    ws.appendChild(cardsZone);
    box.appendChild(ws);
  } else {
    // failed / in-flight / transcript-less: simpler single column
    box.appendChild(fileZone);
    if (hasTranscript) {
      const det = el("details", "extract-det mat-transcript"); det.open = anyLoc;
      det.appendChild(el("summary", "", "文字起こし（カードの出典）"));
      det.appendChild(trPre);
      box.appendChild(det);
    }
    box.appendChild(cardsZone);
  }

  ov.appendChild(box); ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  document.body.appendChild(ov);
  if (highlight) highlightQuote(highlight);   // E5: opened from "出典を見る"
}

// -------------------------------------------------------------------------
// Phase I — study modes (quiz + summary). Flashcards stay the existing proposal
// / card flow; these two add a comprehension test and a 要点まとめ over the same
// material. Generated content follows the MATERIAL's language (handled server-side
// in the prompts); the UI chrome stays Japanese.
// -------------------------------------------------------------------------
// Output language for AI-generated study content (cards/quiz/summary). Global
// setting (persisted) so it governs background card generation AND on-demand
// quiz/summary; 'auto' follows the material, ja/en force it (translation-style).
function langToggleRow() {
  const row = el("div", "sm-lang");
  row.appendChild(el("span", "sm-lang-lb", "生成する学習内容の言語"));
  const sel = el("select", "sm-lang-sel");
  [["auto", "自動（教材に合わせる）"], ["ja", "日本語"], ["en", "English"]].forEach(([v, label]) => {
    const o = el("option", "", label); o.value = v; sel.appendChild(o);
  });
  sel.value = (S.meta && S.meta.content_lang) || "auto";
  sel.onclick = (e) => e.stopPropagation();
  sel.onchange = async () => {
    try {
      await api("/api/settings", { method: "PATCH", body: JSON.stringify({ content_lang: sel.value }) });
      if (S.meta) S.meta.content_lang = sel.value;
      toast("出力言語を変更しました（次に生成する内容から反映されます）", true);
    } catch (e) { toast("変更に失敗: " + e.message); }
  };
  row.appendChild(sel);
  return row;
}

function smChip(icon, label, sub) {
  const b = el("button", "sm-chip"); b.type = "button";
  b.appendChild(el("span", "sm-ico", icon));
  const t = el("span", "sm-txt");
  t.appendChild(el("span", "sm-lb", label));
  t.appendChild(el("span", "sm-sb", sub));
  b.appendChild(t);
  return b;
}

function studyModesSection(m, ov) {
  const sec = el("div", "study-modes");
  sec.appendChild(el("div", "sm-title", "学習モード"));
  sec.appendChild(el("div", "sm-sub", "この教材をどう学ぶか選べます。フラッシュカードは下のカード一覧、クイズとまとめはここから。"));
  sec.appendChild(langToggleRow());
  const chips = el("div", "sm-chips");
  const flashN = (m.cards || []).length;
  const cFlash = smChip("📇", "フラッシュカード", flashN ? `${flashN}枚` : "下の一覧へ");
  cFlash.onclick = () => {
    const t = ov.querySelector(".proposal") || ov.querySelector(".section-title") || ov.querySelector(".mat-addcard");
    if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const cQuiz = smChip("📝", "クイズ", m.quiz ? `${(m.quiz.questions || []).length}問 作成済み` : "テスト形式で理解確認");
  const cSum = smChip("📄", "まとめ", m.summary_guide ? "作成済み" : "要点を整理");
  const glossN = (m.glossary || []).length;
  const cGloss = smChip("📖", "用語表", glossN ? `${glossN}語` : "訳語をそろえる");
  const body = el("div", "sm-body");
  cQuiz.onclick = () => { setActiveChip(chips, cQuiz); openQuizPanel(m, ov, body); };
  cSum.onclick = () => { setActiveChip(chips, cSum); openSummaryPanel(m, ov, body); };
  cGloss.onclick = () => { setActiveChip(chips, cGloss); openGlossaryPanel(m, ov, body); };
  chips.appendChild(cFlash); chips.appendChild(cQuiz); chips.appendChild(cSum); chips.appendChild(cGloss);
  sec.appendChild(chips); sec.appendChild(body);
  return sec;
}
function setActiveChip(chips, chip) {
  chips.querySelectorAll(".sm-chip").forEach((c) => c.classList.remove("active"));
  chip.classList.add("active");
}

// Default output language for on-demand generation from THIS material: honor a
// globally-forced language, else fall back to the material's own detected language
// (so an English material defaults to English — the #7 fix).
function defaultGenLang(m) {
  const g = (S.meta && S.meta.content_lang) || "auto";
  if (g === "ja" || g === "en") return g;
  return detectLang(m && m.extracted_text);
}

// ---- K: term-table panel (inside the material modal) ----
// The glossary binds every later card, quiz, summary and translation for this
// material, so it is worth showing and worth letting the user correct. A hand
// edit is authoritative: only 作り直す re-derives it from the material.
function openGlossaryPanel(m, ov, host) {
  host.innerHTML = "";
  const panel = el("div", "sm-panel");
  panel.appendChild(el("div", "sm-panel-h", "📖 用語表"));
  panel.appendChild(el("div", "sm-panel-sub",
    "この教材の専門用語の対訳表です。ここを直すと、これから作るカード・クイズ・まとめ・翻訳が" +
    "すべて同じ訳語を使います（表記ゆれを防ぎます）。"));

  const rows = el("div", "gl-rows");
  const addRow = (t) => {
    const row = el("div", "gl-row");
    const ja = el("input", "gl-in"); ja.value = (t && t.ja) || ""; ja.placeholder = "日本語";
    const en = el("input", "gl-in"); en.value = (t && t.en) || ""; en.placeholder = "English";
    const del = el("button", "btn small ghost gl-del", "✕");
    del.title = "この行を削除";
    del.onclick = () => row.remove();
    row.appendChild(ja); row.appendChild(el("span", "gl-eq", "＝")); row.appendChild(en);
    row.appendChild(del);
    rows.appendChild(row);
    return row;
  };
  (m.glossary || []).forEach(addRow);
  if (!(m.glossary || []).length) {
    panel.appendChild(el("div", "sm-existing",
      "まだ用語表がありません。「AIで作り直す」で教材から自動作成できます（数十秒）。"));
  }
  panel.appendChild(rows);

  const addBtn = el("button", "btn small ghost", "＋ 用語を追加");
  addBtn.onclick = () => { const r = addRow(null); const i = r.querySelector("input"); if (i) i.focus(); };
  panel.appendChild(addBtn);

  const acts = el("div", "sm-actions");
  const save = el("button", "btn primary", "保存");
  const rebuild = el("button", "btn ghost", "AIで作り直す");

  const collect = () => Array.from(rows.querySelectorAll(".gl-row")).map((r) => {
    const ins = r.querySelectorAll("input");
    return { ja: ins[0].value.trim(), en: ins[1].value.trim() };
  }).filter((t) => t.ja && t.en);

  save.onclick = async () => {
    const restore = btnBusy(save, "保存中…");
    try {
      const r = await api(`/api/materials/${m.id}/glossary`,
        { method: "POST", body: JSON.stringify({ terms: collect() }) });
      if (!r.ok) { toast(r.message || "保存に失敗しました"); return; }
      m.glossary = r.glossary;
      toast(`用語表を保存しました（${r.glossary.length}語）`, true);
      openMaterial(m.id);
    } catch (e) {
      toast("保存に失敗: " + e.message);
    } finally { restore(); }
  };

  rebuild.onclick = async () => {
    if ((m.glossary || []).length &&
        !confirm("いまの用語表を破棄して、AIで作り直しますか？")) return;
    const restore = btnBusy(rebuild, "作成中…");
    const banner = busyBanner("用語表を作成中…（30〜60秒ほどかかることがあります）");
    panel.appendChild(banner);
    try {
      const r = await api(`/api/materials/${m.id}/glossary`,
        { method: "POST", body: JSON.stringify({ rebuild: true }) });
      if (!r.ok) { toast(r.message || "作成に失敗しました"); return; }
      m.glossary = r.glossary;
      toast(`用語表を作りました（${r.glossary.length}語）`, true);
      openMaterial(m.id);
    } catch (e) {
      toast("作成に失敗: " + e.message);
    } finally { restore(); banner.remove(); }
  };

  acts.appendChild(save); acts.appendChild(rebuild);
  panel.appendChild(acts);
  host.appendChild(panel);
}

// ---- Quiz setup panel (inside the material modal) ----
function openQuizPanel(m, ov, host) {
  host.innerHTML = "";
  const panel = el("div", "sm-panel");
  panel.appendChild(el("div", "sm-panel-h", "📝 クイズ（テスト形式）"));
  panel.appendChild(el("div", "sm-panel-sub", "教材の範囲を通しで確認するテストです。途中で答えは出さず、最後にまとめて答え合わせ・自己採点します。"));

  if (m.quiz && (m.quiz.questions || []).length) {
    const q = m.quiz;
    const has = el("div", "sm-existing");
    has.appendChild(el("span", "", `前回のクイズ：${q.questions.length}問・${q.format === "mixed" ? "AIおまかせ（記述＋選択）" : "記述式"}`));
    const take = el("button", "btn small primary", "テストを受ける");
    take.onclick = () => openQuizRunner(q, m);
    has.appendChild(take);
    panel.appendChild(has);
  }

  const form = el("div", "sm-form");
  form.appendChild(el("div", "sm-flabel", "出題形式"));
  const fmtWrap = el("div", "sm-radio");
  const fmt = radioGroup("quizfmt", [
    { v: "written", label: "記述式（最後に自己採点）", checked: true },
    { v: "mixed", label: "AIおまかせ（記述＋4択の混合）" },
  ]);
  fmtWrap.appendChild(fmt.node); form.appendChild(fmtWrap);

  form.appendChild(el("div", "sm-flabel", "範囲（任意）"));
  const scope = el("input", "sm-in");
  scope.placeholder = "例：第3章／pp.10-14／光合成の部分 だけ など";
  form.appendChild(scope);

  form.appendChild(el("div", "sm-flabel", "出題する言語"));
  const langRow = el("div", "sm-lang");
  const langSel = langSelect(defaultGenLang(m));
  langRow.appendChild(langSel);
  form.appendChild(langRow);

  const actions = el("div", "sm-actions");
  const gen = el("button", "btn small primary", m.quiz ? "作り直す" : "クイズを作る");
  gen.onclick = () => runQuizGen(m, ov, host, { format: fmt.value(), scope: scope.value.trim() || null, lang: langSel.value }, gen);
  actions.appendChild(gen);
  if (m.quiz) {
    const more = el("button", "btn small ghost", "＋10問で作り直す");
    more.onclick = () => runQuizGen(m, ov, host,
      { format: m.quiz.format, scope: m.quiz.scope_desc || null, count: (m.quiz.questions || []).length + 10, lang: langSel.value }, more);
    actions.appendChild(more);
  }
  form.appendChild(actions);
  panel.appendChild(form);
  host.appendChild(panel);
}

async function runQuizGen(m, ov, host, body, btn) {
  const restore = btnBusy(btn, "生成中…");
  const banner = busyBanner("クイズを作成中…（20〜60秒ほどかかることがあります）");
  host.appendChild(banner);
  try {
    const r = await api(`/api/materials/${m.id}/quiz`, { method: "POST", body: JSON.stringify(body) });
    if (!r.ok) { toast(r.message || "生成に失敗しました"); return; }
    m.quiz = r.quiz;
    toast(`${r.quiz.questions.length}問のクイズを作りました`, true);
    openQuizPanel(m, ov, host);   // rebuilds host (clears the banner)
    openQuizRunner(r.quiz, m);
  } catch (e) {
    toast("生成に失敗: " + e.message);
  } finally {
    banner.remove(); restore();
  }
}

// ---- Quiz runner (own overlay): answer all, then self/auto-grade ----
function openQuizRunner(quiz, m) {
  const questions = quiz.questions || [];
  if (!questions.length) { toast("問題がありません"); return; }
  const state = questions.map(() => ({ answer: "" }));   // per-question response

  const ov = el("div", "modal-overlay");
  const box = el("div", "modal quiz-modal");
  const close = el("button", "modal-close", "✕"); close.onclick = () => ov.remove();
  box.appendChild(close);
  box.appendChild(el("h3", "", "📝 クイズ"));
  box.appendChild(el("div", "quiz-meta", `${questions.length}問・${quiz.format === "mixed" ? "記述＋選択" : "記述式"}${quiz.scope_desc ? "・範囲: " + quiz.scope_desc : ""}`));
  box.appendChild(el("div", "quiz-hint", "全問に答えてから「答え合わせ」を押してください（途中で答えは出ません）。"));

  const list = el("div", "quiz-list");
  questions.forEach((q, i) => {
    const item = el("div", "quiz-q"); item.dataset.i = i;
    item.appendChild(el("div", "quiz-qh", `問 ${i + 1}`));
    item.appendChild(el("div", "quiz-qt", q.question));
    if (q.type === "choice" && q.choices) {
      const opts = el("div", "quiz-choices");
      q.choices.forEach((choice) => {
        const lb = el("label", "quiz-choice");
        const rb = el("input", ""); rb.type = "radio"; rb.name = "qz" + i; rb.value = choice;
        rb.onchange = () => { state[i].answer = choice; };
        lb.appendChild(rb); lb.appendChild(el("span", "", choice));
        opts.appendChild(lb);
      });
      item.appendChild(opts);
    } else {
      const ta = el("textarea", "quiz-input"); ta.rows = 2; ta.placeholder = "答えを入力";
      ta.oninput = () => { state[i].answer = ta.value; };
      item.appendChild(ta);
    }
    list.appendChild(item);
  });
  box.appendChild(list);

  const foot = el("div", "quiz-foot");
  const submit = el("button", "btn primary", "答え合わせ");
  submit.onclick = () => gradeQuiz(quiz, state, box, m);
  foot.appendChild(submit);
  box.appendChild(foot);

  ov.appendChild(box); ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  document.body.appendChild(ov);
  box.scrollTop = 0;
}

function gradeQuiz(quiz, state, box, m) {
  const questions = quiz.questions || [];
  const norm = (s) => (s || "").trim().replace(/\s+/g, " ").toLowerCase();
  const results = [];        // {i, correct: true|false|null}  (null = written, self-graded)
  questions.forEach((q, i) => {
    const item = box.querySelector(`.quiz-q[data-i="${i}"]`);
    if (!item) return;
    item.querySelectorAll(".quiz-input,.quiz-choices").forEach((n) => n.classList.add("locked"));
    item.querySelectorAll(".quiz-input").forEach((n) => { n.readOnly = true; });
    item.querySelectorAll('input[type="radio"]').forEach((n) => { n.disabled = true; });

    const rev = el("div", "quiz-reveal");
    rev.appendChild(el("div", "quiz-your", "あなたの答え：" + (state[i].answer || "（未回答）")));
    rev.appendChild(el("div", "quiz-ans", "模範解答：" + q.answer));

    if (q.type === "choice") {
      const ok = norm(state[i].answer) === norm(q.answer);
      item.classList.add(ok ? "q-ok" : "q-ng");
      rev.appendChild(el("div", "quiz-verdict " + (ok ? "ok" : "ng"), ok ? "✓ 正解" : "✗ 不正解"));
      results.push({ i, correct: ok });
    } else {
      // written = self-graded: ○ / ×, default unset. rubric optional.
      const rr = { i, correct: null };
      results.push(rr);
      const grp = el("div", "quiz-selfgrade");
      grp.appendChild(el("span", "", "自己採点："));
      const mk = (label, val, cls) => {
        const b = el("button", "sg-btn " + cls, label); b.type = "button";
        b.onclick = () => {
          rr.correct = val;
          grp.querySelectorAll(".sg-btn").forEach((x) => x.classList.remove("on"));
          b.classList.add("on");
          item.classList.remove("q-ok", "q-ng");
          item.classList.add(val ? "q-ok" : "q-ng");
          updateQuizScore(box, results);
        };
        return b;
      };
      grp.appendChild(mk("○ 正解", true, "ok"));
      grp.appendChild(mk("× 不正解", false, "ng"));
      rev.appendChild(grp);
    }
    item.appendChild(rev);
  });

  // Replace footer with score + wrong-to-card action.
  const foot = box.querySelector(".quiz-foot"); if (foot) foot.innerHTML = "";
  const score = el("div", "quiz-score"); score.dataset.role = "score";
  foot.appendChild(score);
  if (m && m.id) {
    const toCards = el("button", "btn small ghost", "間違えた問題をカードに追加");
    toCards.onclick = () => addWrongToCards(questions, results, m, toCards);
    foot.appendChild(toCards);
  }
  updateQuizScore(box, results);
  const first = box.querySelector(".quiz-reveal"); if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
}

function updateQuizScore(box, results) {
  // correct = auto-graded ✓ + self-graded ○; graded = anything with a verdict
  // (auto items always; written items once the user taps ○/×). null = not yet.
  const correct = results.filter((r) => r.correct === true).length;
  const graded = results.filter((r) => r.correct !== null).length;
  const total = results.length;
  const score = box.querySelector('[data-role="score"]'); if (!score) return;
  score.textContent = `スコア ${correct} / ${total}` + (graded < total ? `（未採点 ${total - graded}問）` : "");
}

async function addWrongToCards(questions, results, m, btn) {
  const wrong = results.filter((r) => r.correct === false);
  if (!wrong.length) { toast("追加する間違いがありません"); return; }
  btn.disabled = true;
  try {
    for (const r of wrong) {
      const q = questions[r.i];
      await api(`/api/materials/${m.id}/card`, { method: "POST", body: JSON.stringify({ front: q.question, back: q.answer }) });
    }
    toast("間違えた問題を復習に追加しました（重複は自動で除外されます）", true);
    btn.textContent = "追加しました";
  } catch (e) {
    toast("追加に失敗: " + e.message); btn.disabled = false;
  }
}

// ---- Summary panel (要点まとめ) ----
function openSummaryPanel(m, ov, host) {
  host.innerHTML = "";
  const panel = el("div", "sm-panel");
  panel.appendChild(el("div", "sm-panel-h", "📄 まとめ（要点整理）"));
  panel.appendChild(el("div", "sm-panel-sub", "教材の内容を、あとで見返せる要点にまとめます。"));

  const view = el("div", "sm-summary-view");
  const render = (guide) => {
    view.innerHTML = "";
    if (guide && guide.content_md) {
      if (guide.scope_desc) view.appendChild(el("div", "sm-scope", "範囲: " + guide.scope_desc));
      view.appendChild(mdToNode(guide.content_md));
    }
  };
  render(m.summary_guide);
  panel.appendChild(view);

  const form = el("div", "sm-form");
  form.appendChild(el("div", "sm-flabel", "範囲（任意）"));
  const scope = el("input", "sm-in");
  scope.placeholder = "例：第3章 だけ／全体 など";
  if (m.summary_guide && m.summary_guide.scope_desc) scope.value = m.summary_guide.scope_desc;
  form.appendChild(scope);

  form.appendChild(el("div", "sm-flabel", "まとめの言語"));
  const langRow = el("div", "sm-lang");
  const langSel = langSelect(defaultGenLang(m));
  langRow.appendChild(langSel);
  form.appendChild(langRow);

  const actions = el("div", "sm-actions");
  const gen = el("button", "btn small primary", m.summary_guide ? "作り直す" : "まとめを作る");
  gen.onclick = async () => {
    const restore = btnBusy(gen, "生成中…");
    const banner = busyBanner("まとめを作成中…（10〜40秒ほどかかることがあります）");
    form.appendChild(banner);
    try {
      const r = await api(`/api/materials/${m.id}/summary`, { method: "POST", body: JSON.stringify({ scope: scope.value.trim() || null, lang: langSel.value }) });
      if (!r.ok) { toast(r.message || "生成に失敗しました"); return; }
      m.summary_guide = r.summary;
      render(r.summary);
      toast("まとめを作成しました", true);
    } catch (e) { toast("生成に失敗: " + e.message); }
    finally { banner.remove(); restore(); }
  };
  actions.appendChild(gen); form.appendChild(actions);
  panel.appendChild(form);
  host.appendChild(panel);
}

// ---- H3: smart add — analyze this material and PROPOSE cards (G1 gate), with an
// optional range + free-text steer. Reopens the material so the proposal panel
// shows the drafts for confirm/pick/steer. ----
function openSmartAdd(m, ov, host, trPre) {
  host.innerHTML = "";
  const panel = el("div", "sm-panel");
  panel.appendChild(el("div", "sm-panel-h", "🤖 自動でカードを作る"));
  panel.appendChild(el("div", "sm-panel-sub", "教材を解析して復習カードの案を作ります。範囲や作り方を指定できます（承認するまで復習には入りません）。"));

  panel.appendChild(el("div", "sm-flabel", "範囲（どこから どこまで）"));
  const scope = el("input", "sm-in");
  scope.placeholder = "例：第3章／pp.10-14／光合成の部分だけ（空欄＝全体）";
  panel.appendChild(scope);
  const rangeRow = el("div", "sm-actions");
  const useSel = el("button", "btn small ghost", "文字起こしで選択した部分を使う");
  // Clicking a <button> steals focus and CLEARS the page text selection BEFORE the
  // click handler runs — so reading getSelection() in onclick always saw empty (the
  // "button does nothing" bug). Grab the selection on mousedown/touchstart (and
  // preventDefault so the selection survives), then act on the captured value.
  let grabbed = null;
  const grabSel = () => {
    const s = window.getSelection ? window.getSelection() : null;
    const txt = (s && s.toString() || "").trim();
    // require BOTH ends of the range inside the transcription, so a drag that
    // starts or ends outside it can't slip non-transcription text into the scope.
    const inside = !!(trPre && s && s.anchorNode && s.focusNode &&
      trPre.contains(s.anchorNode) && trPre.contains(s.focusNode));
    grabbed = txt ? { txt, inside } : null;
  };
  useSel.addEventListener("mousedown", (e) => { e.preventDefault(); grabSel(); });
  useSel.addEventListener("touchstart", grabSel, { passive: true });
  useSel.onclick = () => {
    if (!grabbed) { toast("下の「文字起こし」の中で範囲をドラッグ選択してから押してください"); return; }
    if (!grabbed.inside) { toast("「文字起こし」の中だけを選択してください"); return; }
    scope.value = grabbed.txt.length > 120 ? grabbed.txt.slice(0, 120) + "…" : grabbed.txt;
    toast("選択範囲を設定しました", true);
  };
  const whole = el("button", "btn small ghost", "全体にする");
  whole.onclick = () => { scope.value = ""; toast("範囲を全体にしました（教材全体）", true); };
  rangeRow.appendChild(useSel); rangeRow.appendChild(whole);
  panel.appendChild(rangeRow);

  panel.appendChild(el("div", "sm-flabel", "作り方の指示（任意）"));
  const instr = el("textarea", "sm-in"); instr.rows = 2;
  instr.placeholder = "例：計算練習を多めに／用語中心で／英文の穴埋めを作って など";
  panel.appendChild(instr);

  panel.appendChild(el("div", "sm-flabel", "カードの言語"));
  const langRow = el("div", "sm-lang");
  const langSel = langSelect(defaultGenLang(m));
  langRow.appendChild(langSel);
  panel.appendChild(langRow);

  const actions = el("div", "sm-actions");
  const gen = el("button", "btn small primary", "この内容で作る");
  gen.onclick = async () => {
    const restore = btnBusy(gen, "解析中…");
    const banner = busyBanner("この教材を解析してカードを作成中…（20〜60秒ほどかかることがあります）");
    panel.appendChild(banner);
    try {
      const r = await api(`/api/materials/${m.id}/draft`, {
        method: "POST",
        body: JSON.stringify({ scope: scope.value.trim() || null, instruction: instr.value.trim() || null, lang: langSel.value }),
      });
      if (!r.ok) { toast(r.message || "生成に失敗しました"); return; }
      if (!r.added) { toast("新しいカードは作られませんでした（範囲や指示を変えて試してください）"); return; }
      const tp = (r.topics && r.topics.length) ? "「" + r.topics.join("・") + "」" : "";
      toast(`${tp}の内容で${r.added}項目を用意しました。下で確認して承認してください。`, true);
      ov.remove(); openMaterial(m.id);        // reopen -> proposal gate shows the drafts
    } catch (e) { toast("生成に失敗: " + e.message); }
    finally { banner.remove(); restore(); }
  };
  actions.appendChild(gen); panel.appendChild(actions);
  host.appendChild(panel);
  scope.focus();
}

// ---- manual single-card add (front/back + optional quote) ----
function openManualAdd(m, ov, host) {
  host.innerHTML = "";
  const f = el("div", "ac-form");
  const front = el("input", "ac-in"); front.placeholder = "表（問い）";
  const back = el("input", "ac-in"); back.placeholder = "裏（答え）";
  const quote = el("input", "ac-in"); quote.placeholder = "引用（任意：ファイル中の該当箇所）";
  const save = el("button", "btn small primary", "追加");
  save.onclick = async () => {
    const fr = front.value.trim(), bk = back.value.trim();
    if (!fr || !bk) { toast("表と裏を入力してください"); return; }
    const q = quote.value.trim();
    try {
      await api(`/api/materials/${m.id}/card`, { method: "POST", body: JSON.stringify({
        front: fr, back: bk, source_quote: q || null, source_loc: q ? { quote: q } : null }) });
      toast("カードを追加しました", true); ov.remove(); openMaterial(m.id);
    } catch (e) { toast("追加に失敗: " + e.message); }
  };
  [front, back, quote, save].forEach((x) => f.appendChild(x));
  host.appendChild(f);
  front.focus();
}

// small radio-group helper
function radioGroup(name, items) {
  const node = el("div", "rg");
  const inputs = [];
  items.forEach((it) => {
    const lb = el("label", "rg-item");
    const rb = el("input", ""); rb.type = "radio"; rb.name = name; rb.value = it.v;
    if (it.checked) rb.checked = true;
    inputs.push(rb);
    lb.appendChild(rb); lb.appendChild(el("span", "", it.label));
    node.appendChild(lb);
  });
  return { node, value: () => (inputs.find((x) => x.checked) || {}).value || items[0].v };
}

// minimal, SAFE markdown -> DOM (textContent only; no innerHTML). Handles
// ## headings, - bullets, **bold**, and paragraphs. Enough for AI まとめ output.
function mdToNode(md) {
  const wrap = el("div", "md");
  let ul = null;
  const flush = () => { if (ul) { wrap.appendChild(ul); ul = null; } };
  const inline = (parent, text) => {
    (text.split(/(\*\*[^*]+\*\*)/)).forEach((seg) => {
      if (/^\*\*[^*]+\*\*$/.test(seg)) parent.appendChild(el("strong", "", seg.slice(2, -2)));
      else if (seg) parent.appendChild(document.createTextNode(seg));
    });
  };
  (md || "").split("\n").forEach((raw) => {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) { flush(); return; }
    let mm;
    if ((mm = /^(#{1,6})\s+(.*)$/.exec(line))) {
      flush();
      const h = el("div", "md-h md-h" + Math.min(mm[1].length, 4)); inline(h, mm[2]); wrap.appendChild(h);
    } else if ((mm = /^\s*[-*・]\s+(.*)$/.exec(line))) {
      if (!ul) ul = el("ul", "md-ul");
      const li = el("li", ""); inline(li, mm[1]); ul.appendChild(li);
    } else {
      flush();
      const p = el("p", "md-p"); inline(p, line.trim()); wrap.appendChild(p);
    }
  });
  flush();
  return wrap;
}

// E3 — in-app viewer: open the uploaded file inside the modal. Photos render as
// a zoomable <img>; PDFs use the browser's native viewer via a same-origin
// <iframe> (no JS PDF library — dependency-free, offline). thumb_url = "/" +
// original_path, which is the real file URL for both photo and pdf.
function buildViewer(m) {
  const url = m.thumb_url;
  const wrap = el("div", "mat-view");
  if (!url) return wrap;
  if (m.kind === "pdf") {
    const frame = el("iframe", "mat-view-pdf");
    frame.src = url; frame.loading = "lazy"; frame.setAttribute("title", "PDFプレビュー");
    wrap.appendChild(frame);
  } else {
    wrap.appendChild(viewerImg(url));
  }
  const bar = el("div", "mat-view-bar");
  const open = el("a", "btn small ghost", "元ファイルを新しいタブで開く ↗");
  open.href = url; open.target = "_blank"; open.rel = "noopener";
  bar.appendChild(open);
  // H6: occlusion only makes sense on an image (a PDF page is not addressable
  // by fraction here), so the entry point only appears for one.
  if (m.kind !== "pdf") {
    const occ = el("button", "btn small ghost", "🫥 かくして覚える");
    occ.title = "図の上に四角を描いて、隠した部分を答えるカードを作ります";
    occ.onclick = () => openOcclusionEditor(m, wrap);
    bar.appendChild(occ);
  }
  wrap.appendChild(bar);
  return wrap;
}

// H6 画像オクルージョン editor: drag on the image to draw a box, name what it
// hides, and each box becomes one card. Pointer events cover mouse, touch and
// pen with one code path; coordinates are stored as fractions of the image so
// they survive any later render size.
function openOcclusionEditor(m, host) {
  if (host.querySelector(".occ-edit")) return;      // already open
  const panel = el("div", "occ-edit");
  panel.appendChild(el("div", "sm-panel-h", "🫥 かくして覚える"));
  panel.appendChild(el("div", "sm-panel-sub",
    "図の上をドラッグして、隠したい部分を四角で囲みます。四角ごとに「答え」を入れると、" +
    "それぞれが1枚のカードになります。"));

  const stage = el("div", "occ-stage");
  const img = el("img", "occ-img");
  img.src = m.thumb_url; img.alt = ""; img.draggable = false;
  stage.appendChild(img);
  const rects = [];
  const listBox = el("div", "occ-list");

  const redraw = () => {
    stage.querySelectorAll(".occ-rect").forEach((n) => n.remove());
    rects.forEach((r, i) => {
      const b = el("div", "occ-rect target");
      b.style.left = (r.x * 100) + "%"; b.style.top = (r.y * 100) + "%";
      b.style.width = (r.w * 100) + "%"; b.style.height = (r.h * 100) + "%";
      b.appendChild(el("span", "occ-num", String(i + 1)));
      stage.appendChild(b);
    });
    listBox.innerHTML = "";
    rects.forEach((r, i) => {
      const row = el("div", "occ-row");
      row.appendChild(el("span", "occ-badge", String(i + 1)));
      const inp = el("input", "gl-in");
      inp.placeholder = "ここに隠れているものの名前（答え）";
      inp.value = r.label || "";
      inp.oninput = () => { r.label = inp.value; };
      const del = el("button", "btn small ghost gl-del", "✕");
      del.onclick = () => { rects.splice(i, 1); redraw(); };
      row.appendChild(inp); row.appendChild(del);
      listBox.appendChild(row);
    });
  };

  let start = null, ghost = null;
  const frac = (e) => {
    const b = img.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - b.left) / (b.width || 1))),
             y: Math.min(1, Math.max(0, (e.clientY - b.top) / (b.height || 1))) };
  };
  stage.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".occ-rect")) return;
    e.preventDefault();
    start = frac(e);
    ghost = el("div", "occ-rect ghost");
    stage.appendChild(ghost);
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener("pointermove", (e) => {
    if (!start || !ghost) return;
    const p = frac(e);
    ghost.style.left = (Math.min(start.x, p.x) * 100) + "%";
    ghost.style.top = (Math.min(start.y, p.y) * 100) + "%";
    ghost.style.width = (Math.abs(p.x - start.x) * 100) + "%";
    ghost.style.height = (Math.abs(p.y - start.y) * 100) + "%";
  });
  const finish = (e) => {
    if (!start) return;
    const p = frac(e);
    const r = { x: Math.min(start.x, p.x), y: Math.min(start.y, p.y),
                w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y), label: "" };
    start = null;
    if (ghost) { ghost.remove(); ghost = null; }
    if (r.w < 0.02 || r.h < 0.02) return;      // a stray tap is not a rectangle
    rects.push(r);
    redraw();
    const last = listBox.querySelector(".occ-row:last-child .gl-in");
    if (last) last.focus();
  };
  stage.addEventListener("pointerup", finish);
  stage.addEventListener("pointercancel", () => { start = null; if (ghost) { ghost.remove(); ghost = null; } });

  panel.appendChild(stage);
  panel.appendChild(listBox);

  const acts = el("div", "sm-actions");
  const save = el("button", "btn primary", "カードにする");
  save.onclick = async () => {
    if (!rects.length) { toast("先に図の上をドラッグして範囲を囲んでください"); return; }
    if (rects.some((r) => !(r.label || "").trim())) { toast("すべての範囲に答えを入れてください"); return; }
    const restore = btnBusy(save, "作成中…");
    try {
      const res = await api(`/api/materials/${m.id}/occlusion`,
        { method: "POST", body: JSON.stringify({ rects }) });
      if (!res.ok) { toast(res.message || "作成に失敗しました"); return; }
      const dup = res.duplicates ? `（同じ答えの${res.duplicates}件は既存とまとめました）` : "";
      toast(`${res.created.length}枚のカードを作りました${dup}`, true);
      openMaterial(m.id);
    } catch (e) { toast("作成に失敗: " + e.message); }
    finally { restore(); }
  };
  const cancel = el("button", "btn ghost", "やめる");
  cancel.onclick = () => panel.remove();
  acts.appendChild(save); acts.appendChild(cancel);
  panel.appendChild(acts);
  host.appendChild(panel);
}
// A zoomable viewer image that, if it fails to load, cleanly swaps itself for a
// labeled placeholder with a one-tap 再読み込み (cache-busted) — so a missing or
// still-processing file reads as intentional, not a broken-image glitch.
function viewerImg(url) {
  const img = el("img", "mat-view-img");
  img.src = url; img.alt = "アップロード画像"; img.loading = "lazy";
  img.title = "クリックで拡大／縮小";
  img.onclick = () => img.classList.toggle("zoomed");
  img.onerror = () => img.replaceWith(brokenImageCard(url));
  return img;
}
function brokenImageCard(url) {
  const box = el("div", "img-broken");
  box.appendChild(el("div", "img-broken-ico", "🖼"));
  box.appendChild(el("div", "img-broken-msg", "画像を読み込めませんでした"));
  box.appendChild(el("div", "img-broken-sub", "ファイルが移動・削除されたか、まだ処理中の可能性があります。"));
  const row = el("div", "img-broken-actions");
  const retry = el("button", "btn small", "再読み込み");
  retry.onclick = () => box.replaceWith(viewerImg(url + (url.includes("?") ? "&" : "?") + "r=" + Date.now()));
  const open = el("a", "btn small ghost", "元ファイルを開く ↗");
  open.href = url; open.target = "_blank"; open.rel = "noopener";
  row.appendChild(retry); row.appendChild(open);
  box.appendChild(row);
  return box;
}
function cardPreview(c, low) {
  const d = el("div", "card-preview" + (c.origin === "generated" ? " generated" : "") + (low ? " low" : ""));
  d.appendChild(elMd("div", "cp-front", c.front));
  d.appendChild(elMd("div", "cp-back", c.back));
  const tags = el("div", "cp-tags");
  tags.appendChild(el("span", "pill", c.origin === "generated" ? "AI生成" : "原本"));
  if (c.topic) tags.appendChild(el("span", "pill", c.topic));
  d.appendChild(tags);
  if (c.source_quote) { const q = el("div", "cp-quote"); q.textContent = "「" + c.source_quote + "」"; d.appendChild(q); }
  // J — per-card translate toggle: shows the card in the other language beneath.
  const trBox = el("div", "cp-trans hidden");
  const trBtn = el("button", "btn small ghost cp-trans-btn", "🌐 翻訳");
  const showTrans = (t) => { trBox.innerHTML = ""; trBox.appendChild(elMd("div", "cp-front", t.front)); trBox.appendChild(elMd("div", "cp-back", t.back)); trBox.classList.remove("hidden"); };
  trBtn.onclick = async (e) => {
    e.stopPropagation();
    if (!trBox.classList.contains("hidden")) { trBox.classList.add("hidden"); return; }   // toggle off
    const target = detectLang(c.front) === "ja" ? "en" : "ja";
    const cached = c.translation && c.translation[target];
    if (cached && cached.front && cached.back) { showTrans(cached); return; }
    const restore = btnBusy(trBtn, "翻訳中…");
    try {
      const res = await api(`/api/cards/${c.id}/translate`, { method: "POST", body: JSON.stringify({ lang: target }) });
      if (!res.ok) { toast(res.message || "翻訳に失敗しました"); return; }
      c.translation = c.translation || {}; c.translation[target] = { front: res.translation.front, back: res.translation.back };
      showTrans(res.translation);
    } catch (err) { toast("翻訳に失敗: " + err.message); }
    finally { restore(); }
  };
  d.appendChild(trBtn); d.appendChild(trBox);

  // H5 双方向: study the other direction too. Mechanical swap, no AI — and the
  // mirror is a separate card, so the original keeps its own SRS history.
  if (["qa", "term"].indexOf((c.card_type || "qa").toLowerCase()) >= 0) {
    const revBtn = el("button", "btn small ghost cp-trans", "🔁 逆向きを作る");
    revBtn.title = "裏→表のカードを作ります（元のカードはそのまま残ります）";
    revBtn.onclick = async (e) => {
      e.stopPropagation();
      const restore = btnBusy(revBtn, "作成中…");
      try {
        const res = await api(`/api/cards/${c.id}/reverse`, { method: "POST" });
        if (!res.ok) { toast(res.message || "作れませんでした"); return; }
        toast("逆向きのカードを作りました", true);
        if (c.material_id) openMaterial(c.material_id);
      } catch (err) { toast("作成に失敗: " + err.message); }
      finally { restore(); }
    };
    d.appendChild(revBtn);
  }
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
  S.uploading = files.length;
  switchTab("materials");
  renderMaterials();   // optimistic "アップロード中" tiles the instant files are dropped
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
  } catch (e) { toast("アップロード失敗: " + e.message); }
  finally { S.uploading = 0; await refreshMeta(); renderMaterials(); }
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
  // U+FE0E forces text (monochrome) presentation so the sun takes the CSS color
  // (a crisp light glyph in dark mode) instead of the black emoji sun.
  btn.textContent = dark ? "☀︎" : "☾";
  btn.setAttribute("aria-pressed", dark ? "true" : "false");
  btn.setAttribute("aria-label", dark ? "ライトに切替" : "ダークに切替");
}
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("theme", t); } catch (e) {}
  syncThemeButton();
}
function toggleTheme() { applyTheme(effectiveTheme() === "dark" ? "light" : "dark"); }

// ---------- accent palette (user-switchable, persisted; CSS in style.css) ----------
// ids MUST match the :root[data-accent=…] rules in style.css. sw = the LIGHT swatch.
const ACCENTS = [
  { id: "indigo",   name: "Indigo",   sw: "#4b45c4" },
  { id: "sapphire", name: "Sapphire", sw: "#3358d4" },
  { id: "teal",     name: "Teal",     sw: "#0f766e" },
  { id: "emerald",  name: "Emerald",  sw: "#0e8a5f" },
  { id: "amethyst", name: "Amethyst", sw: "#6d40c4" },
  { id: "rose",     name: "Rose",     sw: "#c02a66" },
  { id: "amber",    name: "Amber",    sw: "#b26a00" },
  { id: "graphite", name: "Graphite", sw: "#475569" },
];
function currentAccent() { return document.documentElement.getAttribute("data-accent") || "indigo"; }
function applyAccent(id) {
  if (id && id !== "indigo") document.documentElement.setAttribute("data-accent", id);
  else document.documentElement.removeAttribute("data-accent");
  try { localStorage.setItem("accent", id || "indigo"); } catch (e) {}
  const m = $("#accent-menu"); if (m) renderAccentSwatches(m);
}
function renderAccentSwatches(menu) {
  const grid = menu.querySelector(".accent-grid"); if (!grid) return;
  grid.innerHTML = "";
  const cur = currentAccent();
  ACCENTS.forEach((a) => {
    const b = el("button", "accent-sw" + (a.id === cur ? " current" : ""));
    b.type = "button"; b.style.background = a.sw; b.title = a.name; b.setAttribute("aria-label", a.name);
    b.onclick = () => { applyAccent(a.id); closeAccentMenu(); };
    grid.appendChild(b);
  });
}
function openAccentMenu() {
  closeAccentMenu();
  const btn = $("#accent-toggle"); if (!btn) return;
  const menu = el("div", "accent-menu"); menu.id = "accent-menu";
  menu.appendChild(el("div", "accent-menu-title", "アクセント色"));
  menu.appendChild(el("div", "accent-grid"));
  document.body.appendChild(menu);
  renderAccentSwatches(menu);
  const r = btn.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.max(8, r.top - menu.offsetHeight - 8) + "px";
  setTimeout(() => document.addEventListener("click", accentOutside), 0);
}
function accentOutside(e) {
  const m = $("#accent-menu"); if (!m) return;
  if (!m.contains(e.target) && e.target.id !== "accent-toggle") closeAccentMenu();
}
function closeAccentMenu() {
  const m = $("#accent-menu"); if (m) m.remove();
  document.removeEventListener("click", accentOutside);
}
function toggleAccentMenu() { if ($("#accent-menu")) closeAccentMenu(); else openAccentMenu(); }

// ---------- init ----------
// Classroom sync — make the "手動/同期" chip actually actionable: explain the
// state, one-click sync when set up, and clear (optional) setup steps otherwise.
function syncLink(href, text) {
  const a = el("a", "sync-link", text + " ↗");
  a.href = href; a.target = "_blank"; a.rel = "noopener";
  return a;
}
function syncSetupSteps() {
  const d = el("details", "sync-help"); d.open = true;
  d.appendChild(el("summary", "", "接続する手順（任意・5分ほど）"));
  const ol = el("ol", "sync-steps");
  const li1 = el("li", "");
  li1.appendChild(document.createTextNode("Classroom API を有効化："));
  li1.appendChild(syncLink("https://console.cloud.google.com/apis/library/classroom.googleapis.com", "APIを有効化"));
  ol.appendChild(li1);
  const li2 = el("li", "");
  li2.appendChild(document.createTextNode("OAuth クライアント（種類：デスクトップ アプリ）を作成："));
  li2.appendChild(syncLink("https://console.cloud.google.com/apis/credentials", "認証情報を作成"));
  ol.appendChild(li2);
  ol.appendChild(el("li", "", "その JSON をダウンロードし、StudyDash フォルダに credentials.json という名前で置く"));
  const li4 = el("li", "");
  li4.appendChild(document.createTextNode("ターミナルで初回認証（ブラウザで許可）："));
  li4.appendChild(el("code", "sync-cmd", "./venv/bin/python classroom.py test"));
  ol.appendChild(li4);
  ol.appendChild(el("li", "", "アプリを再起動し、この画面の「今すぐ同期」で取り込む"));
  d.appendChild(ol);
  d.appendChild(el("div", "sync-note", "Classroom は任意です。接続しなくても、課題の手入力と写真取り込みで通常どおり使えます。"));
  return d;
}
function openSyncModal() {
  const configured = !!(S.meta && S.meta.classroom_configured);
  const ov = el("div", "modal-overlay");
  const box = el("div", "modal sync-modal");
  const close = el("button", "modal-close", "✕"); close.onclick = () => ov.remove();
  box.appendChild(close);
  box.appendChild(el("h3", "", "課題の同期（Google Classroom）"));
  if (S.meta && S.meta.last_sync) {
    const ago = Math.round((Date.now() - new Date(S.meta.last_sync)) / 60000);
    box.appendChild(el("div", "sync-last", ago < 60 ? `最終同期: ${ago}分前` : `最終同期: 約${Math.round(ago / 60)}時間前`));
  }
  box.appendChild(el("div", "sync-status", configured
    ? "✅ 接続設定あり。ボタンで最新の課題を取り込めます。"
    : "現在は「手動」（Classroom未接続）。課題は手入力・写真取り込みで問題なく使えます。接続すると課題が自動で取り込まれます。"));
  const result = el("div", "sync-result");
  const doSync = async (btn) => {
    if (btn) btn.disabled = true;
    result.textContent = "同期中…";
    try {
      const r = await api("/api/sync", { method: "POST" });
      if (r.ok) {
        toast(`${r.upserted}件の課題を同期しました`, true);
        result.textContent = `✅ ${r.upserted}件を同期（${(r.courses || []).length}コース）`;
        await refreshMeta();
      } else if (r.reason === "unavailable") {
        result.textContent = "";
        result.appendChild(el("div", "", "初回だけ端末での認証が必要です："));
        result.appendChild(el("code", "sync-cmd", "./venv/bin/python classroom.py test"));
        result.appendChild(el("div", "sync-note", "実行してブラウザで許可 → 戻って「今すぐ同期」。"));
      } else {
        result.textContent = r.message || "同期できませんでした。";
      }
    } catch (e) { result.textContent = "同期に失敗: " + e.message; }
    if (btn) btn.disabled = false;
  };
  if (configured) {
    const btn = el("button", "btn primary", "今すぐ同期");
    btn.onclick = () => doSync(btn);
    box.appendChild(btn);
  }
  box.appendChild(result);
  // no-connection fallback: read a Classroom screenshot through the photo
  // pipeline (it already extracts assignments from a 課題一覧 screenshot).
  const fb = el("div", "sync-fallback");
  fb.appendChild(el("div", "sync-fb-title", "接続できないときは（接続なしでOK）"));
  fb.appendChild(el("div", "sync-note", "Classroom の課題一覧のスクリーンショットを取り込むと、課題を自動で読み取って登録します。"));
  const fbBtn = el("button", "btn small", "スクショ／画像を取り込む");
  fbBtn.onclick = () => { ov.remove(); switchTab("materials"); const fi = $("#file-input"); if (fi) fi.click(); };
  fb.appendChild(fbBtn);
  box.appendChild(fb);
  box.appendChild(syncSetupSteps());
  ov.appendChild(box); ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  document.body.appendChild(ov);
}

// ---------- G2: 学び方（study methods）----------
async function loadMethods() {
  try { S.methods = await api("/api/methods"); } catch (e) { S.methods = []; }
}
// A per-card "学び方" picker: pick a method and re-cast the card into it (1 AI call).
function recastRow(card, refresh) {
  const row = el("div", "recast-row");
  row.onclick = (e) => e.stopPropagation();   // don't toggle an enclosing checkbox
  row.appendChild(el("span", "recast-label", "学び方"));
  const sel = el("select", "recast-sel");
  (S.methods || []).forEach((m) => {
    const o = el("option", "", m.name + (m.builtin ? "" : "（独自）"));
    o.value = m.id; if (m.card_type === card.card_type) o.selected = true;
    sel.appendChild(o);
  });
  sel.onmousedown = (e) => e.stopPropagation();
  row.appendChild(sel);
  const btn = el("button", "btn small ghost", "この学び方にする");
  btn.onclick = async () => {
    const restore = btnBusy(btn, "AIで変換中…");
    try {
      const r = await api(`/api/cards/${card.id}/recast`, { method: "POST", body: JSON.stringify({ method: sel.value }) });
      if (r.ok) { toast("学び方を変えました", true); if (refresh) refresh(); return; }
      toast(r.message || "変換に失敗");
    } catch (e) { toast("変換に失敗: " + e.message); }
    finally { restore(); }
  };
  row.appendChild(btn);
  const mk = el("button", "btn small ghost", "＋独自");
  mk.onclick = () => openMethodModal(() => loadMethods().then(() => refresh && refresh()));
  row.appendChild(mk);
  return row;
}
function openMethodModal(afterFn) {
  const ov = el("div", "modal-overlay");
  const box = el("div", "modal");
  const close = el("button", "modal-close", "✕"); close.onclick = () => ov.remove();
  box.appendChild(close);
  box.appendChild(el("h3", "", "独自の学び方を作る"));
  box.appendChild(el("div", "sync-note", "保存すると「学び方」の選択肢に加わり、どの教材でも使えます。"));
  const name = el("input", "ac-in"); name.placeholder = "名前（例：英単語＝例文＋語源）";
  const base = el("select", "recast-sel");
  (S.methods || []).filter((m) => m.builtin).forEach((m) => { const o = el("option", "", "ベース: " + m.name); o.value = m.id; base.appendChild(o); });
  const instr = el("textarea", "ac-in ac-area"); instr.placeholder = "AIへの追加指示（例：例文と語源を必ず添える）"; instr.rows = 3;
  const save = el("button", "btn primary", "保存");
  save.onclick = async () => {
    const n = name.value.trim(); if (!n) { toast("名前を入力してください"); return; }
    try {
      await api("/api/methods", { method: "POST", body: JSON.stringify({ name: n, base: base.value, instruction: instr.value.trim() }) });
      toast("独自メソッドを保存しました", true); ov.remove(); if (afterFn) afterFn();
    } catch (e) { toast("保存に失敗: " + e.message); }
  };
  [name, base, instr, save].forEach((x) => box.appendChild(x));
  ov.appendChild(box); ov.onclick = (e) => { if (e.target === ov) ov.remove(); };
  document.body.appendChild(ov);
}

function init() {
  document.querySelectorAll(".tab").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
  loadMethods();
  $("#theme-toggle").onclick = toggleTheme; syncThemeButton();
  { const si = $("#sync-info"); if (si) si.onclick = openSyncModal; }
  { const fi = $("#file-input"); if (fi) fi.onchange = () => { const files = [...fi.files]; fi.value = ""; if (files.length) uploadFiles(files); }; }
  { const at = $("#accent-toggle"); if (at) at.onclick = (e) => { e.stopPropagation(); toggleAccentMenu(); }; }
  $("#add-fab").onclick = openAdd;
  $("#af-cancel").onclick = hideForm;
  $("#af-save").onclick = saveForm;
  $("#af-title").addEventListener("keydown", (e) => { if (e.key === "Enter") saveForm(); });
  document.querySelectorAll("#af-due button").forEach((b) => b.onclick = () => setDueChip(b.dataset.due));
  $("#af-due-date").onchange = () => setDueChip("date");
  document.querySelectorAll("#af-minutes button").forEach((b) => b.onclick = () => setMinChip(Number(b.dataset.min)));
  document.addEventListener("keydown", (e) => {
    // "typing" now also covers contenteditable (the B1 notes rem blocks are DIVs),
    // else global shortcuts would hijack note typing. notesMode gates study
    // shortcuts so a live review session can't grade cards while editing a note.
    const ae = document.activeElement;
    const typing = !!ae && (/input|select|textarea/i.test(ae.tagName) || ae.isContentEditable);
    const notesMode = typeof SD !== "undefined" && SD.mode === "notes";
    if (e.key === "n" && !typing && !notesMode && $("#add-form").classList.contains("hidden")) { e.preventDefault(); openAdd(); }
    if (e.key === "Escape") hideForm();
    // review shortcuts (keyboard is the secondary path; touch is primary)
    if (S.tab === "review" && !typing && !notesMode && S.review.queue.length && S.review.idx < S.review.queue.length) {
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
