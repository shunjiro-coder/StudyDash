// outline.js — RemNote-style outliner (Phase B1 + B2).
//
// Loaded after app.js + sd-core.js (+ katex) as a classic script; reuses app.js's
// api()/el()/$()/toast() and sd-core's SD.* without redeclaring them.
//
// EDITING MODEL
//   B1: 1 rem = 1 plaintext-only block; plain typing never re-renders.
//   B2: "focused line = source / unfocused line = rendered preview" (§4). A rem in
//   preview shows rich HTML (**bold** ==hl== `code` [[ref]] $$latex$$ + card
//   separators ::/>>/<>/;; and block types: heading/todo/code/quote/divider/
//   image/latex). On focus it swaps to RAW text for editing; on blur it renders
//   again. The caret is restored across that swap (click -> caretRangeFromPoint,
//   keyboard -> offset). Structural ops (Enter/Tab/Backspace/DnD) still re-render
//   then re-focus. IME-safe: while composing, nothing switches / parses / saves.
//   Slash menu ("/") inserts block types. Large docs use content-visibility
//   virtualization + a one-time soft warning. Autosave: 700ms debounce + beacon.

"use strict";

const OUT = {
  doc: null,          // {id, title, is_daily, daily_date, rems:[...]}
  dirty: new Map(),   // rem id -> pending text
  saveTimer: null,
  drag: null,         // rem id currently being dragged
  busy: false,        // a structural op (create/reorder/delete) is in flight
  editing: null,      // rem id currently focused in SOURCE mode (B2), else null
  slash: null,        // {rid, active, items, start} while the "/" menu is open
  clickPt: null,      // last pointerdown {x,y} on a rem, for caret-on-click
  warnedDoc: null,    // doc id we've already shown the "large doc" warning for
};

const BIG_DOC = 200;    // >this many rems -> enable content-visibility virtualization
const WARN_DOC = 600;   // >this many rems -> one-time soft performance warning

// Serialize structural ops so a fast double-press (Enter auto-repeat, double Tab)
// can't re-enter on stale DOM and double-submit. Keydown checks OUT.busy.
async function withBusy(fn) {
  if (OUT.busy) return;
  OUT.busy = true;
  try { await fn(); }
  finally { OUT.busy = false; }
}

// entry point from the sidebar (sd-core calls this)
SD.onNav = function (nav) {
  if (nav === "daily") openDaily();
  else if (nav === "docs") renderDocList();
};

// -------------------------------------------------------------------------
// Loading docs
// -------------------------------------------------------------------------
async function openDaily() {
  try { OUT.doc = await api("/api/docs/daily"); }
  catch (e) { toast("ノートを開けませんでした"); return; }
  renderDoc();
}

async function openDoc(id) {
  try { OUT.doc = await api("/api/docs/" + id); }
  catch (e) { toast("ノートを開けませんでした"); return; }
  SD.setMode("notes"); SD.setActiveNav(null);
  renderDoc();
}

async function renderDocList() {
  await flushDirty();
  closeSlash();
  const host = $("#notes-view"); host.innerHTML = "";
  SD.setCrumbs([]);
  const head = el("div", "notes-head");
  head.appendChild(el("div", "focus-title", "ノート"));
  const add = el("button", "btn primary small", "＋ 新規ノート");
  add.onclick = async () => {
    try {
      const d = await api("/api/docs", { method: "POST", body: JSON.stringify({ title: "" }) });
      openDoc(d.id);
    } catch (e) { toast("作成に失敗"); }
  };
  head.appendChild(add);
  host.appendChild(head);

  let docs = [];
  try { docs = await api("/api/docs"); } catch (e) { host.appendChild(el("div", "empty", "読み込みに失敗しました")); return; }
  if (!docs.length) { host.appendChild(el("div", "empty", "まだノートはありません。「今日のノート」から始めましょう。")); return; }

  const list = el("div", "doc-list");
  docs.forEach((d) => {
    const it = el("div", "doc-item");
    it.dataset.id = d.id;
    const left = el("div", "doc-main");
    const titleEl = el("div", "title", docLabel(d));
    left.appendChild(titleEl);
    left.appendChild(el("div", "meta", relTime(d.updated_at)));
    it.appendChild(left);

    const actions = el("div", "doc-actions");
    const renameBtn = el("button", "doc-act", "✎");
    renameBtn.type = "button"; renameBtn.title = "名前を変更"; renameBtn.setAttribute("aria-label", "名前を変更");
    renameBtn.onclick = (e) => { e.stopPropagation(); startRenameDoc(d, it, titleEl); };
    const delBtn = el("button", "doc-act danger", "🗑");
    delBtn.type = "button"; delBtn.title = "削除"; delBtn.setAttribute("aria-label", "削除");
    delBtn.onclick = (e) => { e.stopPropagation(); deleteDocItem(d); };
    actions.appendChild(renameBtn); actions.appendChild(delBtn);
    it.appendChild(actions);

    it.onclick = () => openDoc(d.id);
    list.appendChild(it);
  });
  host.appendChild(list);
}

// inline rename inside the docs list: swap the title for an input, commit on
// Enter/blur (PATCH /api/docs/<id>), cancel on Escape; re-render either way.
function startRenameDoc(d, it, titleEl) {
  if (it.querySelector(".rename-input")) return;
  const input = el("input", "rename-input");
  input.value = d.title || (d.is_daily ? d.daily_date : "");
  input.placeholder = "タイトル";
  input.onclick = (e) => e.stopPropagation();
  titleEl.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    const v = input.value.trim();
    if (save && v !== (d.title || "")) {
      try { await api("/api/docs/" + d.id, { method: "PATCH", body: JSON.stringify({ title: v }) }); }
      catch (e) { toast("名前の変更に失敗"); }
    }
    renderDocList();
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); finish(true); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  };
  input.onblur = () => finish(true);
}

async function deleteDocItem(d) {
  if (!confirm("「" + docLabel(d) + "」を削除しますか？この操作は取り消せません。")) return;
  try { await api("/api/docs/" + d.id, { method: "DELETE" }); toast("削除しました"); }
  catch (e) { toast("削除に失敗"); return; }
  renderDocList();
}

function docLabel(d) {
  if (d.title) return d.title;
  if (d.is_daily) return "📅 " + d.daily_date;
  return "無題のノート";
}

// -------------------------------------------------------------------------
// Rendering a doc
// -------------------------------------------------------------------------
function renderDoc() {
  const host = $("#notes-view"); host.innerHTML = "";
  closeSlash();
  const doc = OUT.doc;
  SD.setCrumbs([{ label: "ノート", onClick: renderDocList }, { label: docLabel(doc) }]);

  const title = el("input", "doc-title");
  title.value = doc.title || (doc.is_daily ? doc.daily_date : "");
  title.placeholder = "タイトル";
  title.onchange = async () => {
    try { await api("/api/docs/" + doc.id, { method: "PATCH", body: JSON.stringify({ title: title.value }) }); doc.title = title.value; }
    catch (e) { toast("保存に失敗"); }
  };
  host.appendChild(title);

  const treeEl = el("div", "rem-tree");
  treeEl.onclick = (e) => { if (e.target === treeEl) focusLastRem(); };   // click empty space -> last rem
  host.appendChild(treeEl);
  renderTree();

  if (!doc.rems.length) createRem({ parent_id: null, after_id: null, focus: true });
  else if (doc.rems.length > WARN_DOC && OUT.warnedDoc !== doc.id) {
    OUT.warnedDoc = doc.id;
    toast(doc.rems.length + " 項目：大きなノートです。分割すると軽くなります");
  }
}

function tree() { return $(".rem-tree"); }

function childrenOf(pid) {
  return OUT.doc.rems
    .filter((r) => r.parent_id === pid)
    .sort((a, b) => a.position - b.position || a.id - b.id);
}

// flattened depth-first order of the currently VISIBLE rems (collapsed subtrees
// excluded) — the order ArrowUp/ArrowDown walk.
function visibleRems() {
  const out = [];
  (function walk(pid) {
    childrenOf(pid).forEach((r) => { out.push(r); if (!r.collapsed) walk(r.id); });
  })(null);
  return out;
}

function renderTree() {
  OUT.editing = null;           // DOM about to be rebuilt; focus() will re-establish
  const t = tree(); if (!t) return;
  t.classList.toggle("big", OUT.doc.rems.length > BIG_DOC);
  t.innerHTML = "";
  childrenOf(null).forEach((r) => t.appendChild(remNode(r)));
}

function remNode(r) {
  const type = r.rem_type || "bullet";
  const node = el("div", "rem-node type-" + type + (r.done ? " done" : ""));
  node.dataset.id = r.id;
  if (type === "heading") node.dataset.level = String((r.props && r.props.level) || 1);

  const row = el("div", "rem-row");

  const handle = el("span", "rem-handle", "⠿");
  handle.draggable = true;
  handle.setAttribute("aria-hidden", "true");
  handle.addEventListener("dragstart", (e) => { OUT.drag = r.id; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; });
  handle.addEventListener("dragend", () => { OUT.drag = null; clearDropMarks(); });
  row.appendChild(handle);

  const kids = childrenOf(r.id);
  const caret = el("span", "rem-caret" + (kids.length ? "" : " leaf"), r.collapsed ? "▸" : "▾");
  if (kids.length) {
    caret.setAttribute("role", "button");
    caret.setAttribute("aria-label", r.collapsed ? "展開" : "折りたたむ");
    caret.setAttribute("aria-expanded", r.collapsed ? "false" : "true");
    caret.tabIndex = 0;
    caret.onclick = () => toggleCollapse(r);
    caret.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleCollapse(r); } };
  }
  row.appendChild(caret);

  if (type === "todo") {
    const cb = el("span", "rem-check" + (r.done ? " checked" : ""), r.done ? "✓" : "");
    cb.setAttribute("role", "checkbox");
    cb.setAttribute("aria-checked", r.done ? "true" : "false");
    cb.tabIndex = 0;
    cb.onclick = () => toggleRemDone(r);
    cb.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggleRemDone(r); } };
    row.appendChild(cb);
  } else {
    row.appendChild(el("span", "rem-bullet", type === "divider" ? "" : "•"));
  }

  const text = el("div", "rem-text");
  text.contentEditable = getCE();
  text.dataset.id = r.id;
  text.dataset.ph = placeholderFor(r);
  text.setAttribute("role", "textbox");
  text.setAttribute("aria-label", "ノート項目");
  setPreview(text, r);
  wireRemText(text, r);
  row.appendChild(text);

  node.appendChild(row);
  wireDrop(row, r);

  if (kids.length && !r.collapsed) {
    const box = el("div", "rem-children");
    kids.forEach((c) => box.appendChild(remNode(c)));
    node.appendChild(box);
  }
  return node;
}

function placeholderFor(r) {
  if (r.rem_type === "code") return "コード";
  const roots = childrenOf(null);
  if (r.parent_id == null && roots[0] && roots[0].id === r.id) return "入力…（「/」でブロック挿入）";
  return "";
}

// contenteditable=plaintext-only is IME-safe (Chrome/Safari). Feature-detect once;
// fall back to =true (with paste sanitizing, see wireRemText) for anything else.
let _CE = null;
function getCE() {
  if (_CE) return _CE;
  try {
    const d = document.createElement("div");
    d.contentEditable = "plaintext-only";
    _CE = d.contentEditable === "plaintext-only" ? "plaintext-only" : "true";
  } catch (e) { _CE = "true"; }
  return _CE;
}

// -------------------------------------------------------------------------
// Inline / block rendering (preview mode)
// -------------------------------------------------------------------------
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function renderInline(raw) {
  if (!raw) return "";
  // single left-to-right pass: each token type is handled exactly once, so no
  // escaping/decoration leaks into code or math and no sentinel chars are needed.
  const re = /(\$\$[^$]+?\$\$)|(\$[^$\n]+?\$)|(`[^`]+?`)|(\*\*[^*]+?\*\*)|(\*[^*\n]+?\*)|(==[^=]+?==)|(\[\[[^\][]+?\]\])/g;
  let out = "", last = 0, m;
  while ((m = re.exec(raw))) {
    out += escInline(raw.slice(last, m.index));
    const tok = m[0];
    if (m[1] || m[2]) out += renderMath(tok.replace(/^\$+|\$+$/g, ""), false);
    else if (m[3]) out += '<code class="rem-code">' + esc(tok.slice(1, -1)) + "</code>";
    else if (m[4]) out += "<strong>" + esc(tok.slice(2, -2)) + "</strong>";
    else if (m[5]) out += "<em>" + esc(tok.slice(1, -1)) + "</em>";
    else if (m[6]) out += "<mark>" + esc(tok.slice(2, -2)) + "</mark>";
    else { const t = tok.slice(2, -2); out += '<span class="rem-ref" data-ref="' + esc(t) + '">' + esc(t) + "</span>"; }
    last = m.index + tok.length;
  }
  out += escInline(raw.slice(last));
  return out;
}

// escape a plain-text segment + style top-level card separators (Phase C makes
// these real cards; B2 only styles them). Matches the escaped forms of < and >.
function escInline(s) {
  s = esc(s);
  s = s.replace(/(^|\s)(::|&gt;&gt;|&lt;&gt;|;;)(?=\s|$)/g, '$1<span class="rem-sep">$2</span>');
  return s;
}

function renderMath(tex, display) {
  if (window.katex) {
    try { return window.katex.renderToString(tex, { displayMode: !!display, throwOnError: false }); }
    catch (e) { /* fall through to plain */ }
  }
  return '<code class="rem-math-fallback">' + esc(tex) + "</code>";
}

function previewHTML(r) {
  const t = r.rem_type || "bullet";
  if (t === "divider") return '<hr class="rem-hr">';
  if (t === "latex") return r.text ? renderMath(r.text, true) : '<span class="rem-ph-in">数式（クリックで入力）</span>';
  if (t === "image") {
    const src = r.props && r.props.src;
    return src ? '<img class="rem-img" src="' + esc(src) + '" alt="' + esc(r.text || "") + '">'
               : '<span class="rem-ph-in">画像URL未設定</span>';
  }
  if (t === "code") return r.text ? esc(r.text) : "";
  return renderInline(r.text);   // bullet / heading / quote / todo
}

function setPreview(text, r) {
  text.innerHTML = previewHTML(r);
  const textual = ["bullet", "heading", "todo", "quote", "code"].indexOf(r.rem_type || "bullet") >= 0;
  text.classList.toggle("empty", textual && !r.text);
}

// -------------------------------------------------------------------------
// Source <-> preview swap (B2 core)
// -------------------------------------------------------------------------
function enterEdit(text, r, hint) {
  if (OUT.editing === r.id) {                    // already editing this rem
    if (typeof hint === "number") setCaret(text, hint);
    return;
  }
  OUT.editing = r.id;
  const node = text.closest(".rem-node"); if (node) node.classList.add("editing");
  text.textContent = r.text || "";               // rendered HTML -> raw source
  text.classList.toggle("empty", !r.text);
  if (typeof hint === "number") setCaret(text, hint);
  else if (hint === "point" && OUT.clickPt) placeCaretFromPoint(text, OUT.clickPt);
  else setCaret(text, (r.text || "").length);
  OUT.clickPt = null;
}

function exitEdit(text, r) {
  if (OUT.editing === r.id) OUT.editing = null;
  const node = text.closest(".rem-node"); if (node) node.classList.remove("editing");
  setPreview(text, r);                            // raw source -> rendered preview
  flushSoon(0);
}

// -------------------------------------------------------------------------
// Editing one rem block
// -------------------------------------------------------------------------
function wireRemText(text, r) {
  let composing = false;
  text.addEventListener("compositionstart", () => { composing = true; });
  text.addEventListener("compositionend", () => {
    composing = false;
    markDirty(r.id, text.textContent, text);
    updateSlash(text, r);
  });

  text.addEventListener("pointerdown", (e) => { OUT.clickPt = { x: e.clientX, y: e.clientY }; });
  text.addEventListener("focus", () => { enterEdit(text, r, OUT.clickPt ? "point" : undefined); });
  text.addEventListener("blur", () => { closeSlash(); exitEdit(text, r); });

  // paste sanitize — only needed on the contenteditable=true fallback; in
  // plaintext-only the browser already strips markup.
  text.addEventListener("paste", (e) => {
    if (getCE() === "plaintext-only") return;
    e.preventDefault();
    const cb = e.clipboardData || window.clipboardData;
    const t = cb && cb.getData ? cb.getData("text") : "";
    document.execCommand("insertText", false, t);
  });

  text.addEventListener("input", () => {
    text.classList.toggle("empty", !text.textContent);
    if (composing) return;
    markDirty(r.id, text.textContent, text);
    updateSlash(text, r);
  });

  text.addEventListener("keydown", (e) => {
    // 1) the slash menu owns keys while it's open on THIS rem
    if (OUT.slash && OUT.slash.rid === r.id) {
      if (e.key === "ArrowDown") { e.preventDefault(); moveSlash(1); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); moveSlash(-1); return; }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault(); e.stopPropagation();
        const it = OUT.slash.items[OUT.slash.active];
        if (it && !it.soon) applySlash(it, text, r);
        else if (it) toast("Phase D で対応します");
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeSlash(); return; }
    }

    if (composing || e.isComposing) return;      // never act mid-IME

    // 2) vertical navigation between rems (only when the caret is on the edge line)
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const dir = e.key === "ArrowUp" ? -1 : 1;
      if (atEdgeLine(text, dir)) { e.preventDefault(); moveVertical(r, dir, caretX()); }
      return;
    }

    // 3) structural edits
    const structural =
      (e.key === "Enter" && !e.shiftKey) ||
      e.key === "Tab" ||
      (e.key === "Backspace" && caretOffset(text) === 0 && !hasSelection());
    if (!structural) return;
    e.preventDefault();                           // consume even while busy
    if (OUT.busy) return;                         // an op is in flight — drop repeat
    if (e.key === "Enter") withBusy(() => onEnter(r, text));
    else if (e.key === "Tab" && !e.shiftKey) withBusy(() => indent(r, caretOffset(text)));
    else if (e.key === "Tab" && e.shiftKey) withBusy(() => outdent(r, caretOffset(text)));
    else withBusy(() => onBackspaceStart(r, text));
  });
}

function markDirty(id, txt, node) {
  OUT.dirty.set(id, txt);
  const local = OUT.doc.rems.find((x) => x.id === id);
  if (local) local.text = txt;
  flushSoon(700);
}

function flushSoon(ms) {
  clearTimeout(OUT.saveTimer);
  OUT.saveTimer = setTimeout(flushDirty, ms);
}

async function flushDirty() {
  clearTimeout(OUT.saveTimer);
  if (!OUT.dirty.size) return;
  const items = [...OUT.dirty.entries()].map(([id, text]) => ({ id, text }));
  OUT.dirty.clear();
  try { await api("/api/rems/batch", { method: "POST", body: JSON.stringify({ items, doc_id: OUT.doc && OUT.doc.id }) }); }
  catch (e) {
    // retry the failed snapshot, but NEVER clobber a rem the user re-edited while
    // this batch was in flight (that newer text must win, else a reload loses it).
    let restored = false;
    items.forEach((it) => { if (!OUT.dirty.has(it.id)) { OUT.dirty.set(it.id, it.text); restored = true; } });
    if (restored) flushSoon(2000);
  }
}

// fetch is unreliable during unload -> sendBeacon (carries the sdkey cookie in PUBLIC mode)
window.addEventListener("beforeunload", () => {
  if (!OUT.dirty.size) return;
  const items = [...OUT.dirty.entries()].map(([id, text]) => ({ id, text }));
  try {
    const blob = new Blob([JSON.stringify({ items, doc_id: OUT.doc && OUT.doc.id })], { type: "application/json" });
    navigator.sendBeacon("/api/rems/batch", blob);
  } catch (e) { /* ignore */ }
});

// -------------------------------------------------------------------------
// Slash menu (block insertion)
// -------------------------------------------------------------------------
const SLASH_ITEMS = [
  { key: "h1", label: "見出し1", hint: "大見出し", type: "heading", props: { level: 1 } },
  { key: "h2", label: "見出し2", hint: "中見出し", type: "heading", props: { level: 2 } },
  { key: "h3", label: "見出し3", hint: "小見出し", type: "heading", props: { level: 3 } },
  { key: "todo", label: "TODO", hint: "チェックボックス", type: "todo" },
  { key: "quote", label: "引用", hint: "引用ブロック", type: "quote" },
  { key: "code", label: "コード", hint: "等幅ブロック", type: "code" },
  { key: "divider", label: "区切り", hint: "水平線", type: "divider", clears: true },
  { key: "latex math", label: "数式 (LaTeX)", hint: "$$ e=mc^2 $$", type: "latex" },
  { key: "image", label: "画像", hint: "URL を挿入", type: "image", image: true },
  { key: "bullet text", label: "箇条書き", hint: "標準の行に戻す", type: "bullet" },
  { key: "table", label: "表", hint: "後日対応 (Phase D)", soon: true },
  { key: "portal", label: "ポータル", hint: "後日対応 (Phase D)", soon: true },
];

// If the text just before the caret is a "/word" token (start of line or after a
// space), return {q, start}; else null.
function slashQuery(text) {
  const off = caretOffset(text);
  const s = (text.textContent || "").slice(0, off);
  const m = s.match(/(?:^|\s)\/([^\s/]*)$/);
  return m ? { q: m[1], start: off - m[1].length - 1 } : null;
}

function slashMatches(q) {
  q = (q || "").toLowerCase();
  if (!q) return SLASH_ITEMS;
  return SLASH_ITEMS.filter((it) => it.key.indexOf(q) >= 0 || it.label.toLowerCase().indexOf(q) >= 0);
}

function updateSlash(text, r) {
  const sq = slashQuery(text);
  if (!sq) { closeSlash(); return; }
  const items = slashMatches(sq.q);
  if (!items.length) { closeSlash(); return; }
  if (!OUT.slash || OUT.slash.rid !== r.id) OUT.slash = { rid: r.id, active: 0, items, start: sq.start };
  else {
    OUT.slash.items = items; OUT.slash.start = sq.start;
    if (OUT.slash.active >= items.length) OUT.slash.active = 0;
  }
  renderSlashMenu(text);
}

function closeSlash() {
  OUT.slash = null;
  const m = document.getElementById("slash-menu");
  if (m) m.remove();
}

function moveSlash(d) {
  if (!OUT.slash) return;
  const n = OUT.slash.items.length;
  OUT.slash.active = (OUT.slash.active + d + n) % n;
  renderSlashMenu();
}

function renderSlashMenu(text) {
  if (!OUT.slash) return;
  let m = document.getElementById("slash-menu");
  if (!m) { m = el("div", "slash-menu"); m.id = "slash-menu"; m.setAttribute("role", "listbox"); document.body.appendChild(m); }
  m.innerHTML = "";
  OUT.slash.items.forEach((it, i) => {
    const row = el("div", "slash-item" + (i === OUT.slash.active ? " active" : "") + (it.soon ? " soon" : ""));
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", i === OUT.slash.active ? "true" : "false");
    row.appendChild(el("span", "slash-label", it.label));
    row.appendChild(el("span", "slash-hint", it.hint));
    row.addEventListener("pointerdown", (e) => {
      e.preventDefault();                         // keep focus in the rem
      if (it.soon) { toast("Phase D で対応します"); return; }
      const t = document.querySelector('.rem-text[data-id="' + OUT.slash.rid + '"]');
      const rr = OUT.doc.rems.find((x) => x.id === OUT.slash.rid);
      if (t && rr) applySlash(it, t, rr);
    });
    m.appendChild(row);
  });
  if (text) {
    const cr = caretRect(text);
    if (cr) {
      const mh = 280;   // keep the popover on-screen (flip above the caret if needed)
      const below = cr.bottom + 4;
      m.style.left = Math.round(Math.min(cr.left, window.innerWidth - 240)) + "px";
      m.style.top = Math.round(below + mh > window.innerHeight ? Math.max(4, cr.top - mh) : below) + "px";
    }
  }
}

async function applySlash(item, text, r) {
  const start = OUT.slash ? OUT.slash.start : null;
  closeSlash();
  if (!text || !r) return;
  // strip the "/query" token the user typed
  let raw = text.textContent || "";
  const off = caretOffset(text);
  if (start != null && start >= 0 && start <= off) raw = raw.slice(0, start) + raw.slice(off);

  let props = item.props;
  const newText = item.clears ? "" : raw;
  if (item.image) {
    const url = window.prompt("画像URL（http... または file...）");
    if (!url) { text.textContent = raw; markDirty(r.id, raw, text); focusRem(r.id, raw.length); return; }
    props = { src: url };
  }
  await setRemType(r, item.type, props, newText);
}

async function setRemType(r, type, props, newText) {
  const body = { rem_type: type };
  if (props !== undefined) body.props = props;
  if (newText !== undefined) body.text = newText;
  OUT.dirty.delete(r.id);
  clearTimeout(OUT.saveTimer);
  try {
    const out = await api("/api/rems/" + r.id, { method: "PATCH", body: JSON.stringify(body) });
    r.rem_type = out.rem_type; r.props = out.props; r.text = out.text;
  } catch (e) { toast("変更に失敗"); return; }
  renderTree();
  focusRem(r.id, (r.text || "").length);
}

async function toggleRemDone(r) {
  r.done = !r.done;
  renderTree();
  try { await api("/api/rems/" + r.id, { method: "PATCH", body: JSON.stringify({ done: r.done }) }); }
  catch (e) { /* non-critical */ }
}

// -------------------------------------------------------------------------
// Structural operations
// -------------------------------------------------------------------------
async function createRem({ parent_id, after_id, text = "", focus = false }) {
  let res;
  try { res = await api("/api/rems", { method: "POST", body: JSON.stringify({ doc_id: OUT.doc.id, parent_id, after_id, text }) }); }
  catch (e) { toast("追加に失敗"); return null; }
  applyRenorm(res.renormalized);
  OUT.doc.rems.push(res.rem);
  renderTree();
  if (focus) focusRem(res.rem.id, 0);
  return res.rem;
}

function applyRenorm(list) {
  if (!list) return;
  const map = new Map(list.map((x) => [x.id, x.position]));
  OUT.doc.rems.forEach((r) => { if (map.has(r.id)) r.position = map.get(r.id); });
}

async function onEnter(r, text) {
  const caret = caretOffset(text);
  const full = text.textContent;
  const before = full.slice(0, caret);
  const after = full.slice(caret);
  OUT.dirty.delete(r.id);
  clearTimeout(OUT.saveTimer);
  if (before !== r.text) {
    try { await api("/api/rems/" + r.id, { method: "PATCH", body: JSON.stringify({ text: before }) }); r.text = before; }
    catch (e) { toast("保存に失敗"); return; }
  }
  // a new line starts as a plain bullet even if this one is a heading/todo/etc.
  await createRem({ parent_id: r.parent_id, after_id: r.id, text: after, focus: true });
}

async function indent(r, offset) {
  await flushDirty();
  const sibs = childrenOf(r.parent_id);
  const i = sibs.findIndex((x) => x.id === r.id);
  if (i <= 0) return;                       // no previous sibling to nest under
  const newParent = sibs[i - 1];
  const lastChild = childrenOf(newParent.id).slice(-1)[0];
  await reorder(r, newParent.id, lastChild ? lastChild.id : null, offset);
}

async function outdent(r, offset) {
  await flushDirty();
  if (r.parent_id == null) return;          // already top level
  const parent = OUT.doc.rems.find((x) => x.id === r.parent_id);
  if (!parent) return;
  await reorder(r, parent.parent_id, parent.id, offset);
}

async function reorder(r, newParent, afterId, offset) {
  let res;
  try { res = await api("/api/rems/reorder", { method: "POST", body: JSON.stringify({ rem_id: r.id, new_parent_id: newParent, after_id: afterId }) }); }
  catch (e) { toast("移動に失敗"); return; }
  applyRenorm(res.renormalized);
  const local = OUT.doc.rems.find((x) => x.id === r.id);
  local.parent_id = res.rem.parent_id;
  local.position = res.rem.position;
  renderTree();
  focusRem(r.id, offset);
}

async function onBackspaceStart(r, text) {
  const sibs = childrenOf(r.parent_id);
  const i = sibs.findIndex((x) => x.id === r.id);
  const hasKids = childrenOf(r.id).length > 0;

  if (!text.textContent) {
    if (OUT.doc.rems.length === 1) return;   // keep the last rem in the doc
    if (hasKids) { if (r.parent_id != null) outdent(r, 0); return; }   // don't cascade-delete children
    const prev = i > 0 ? lastVisibleDescendant(sibs[i - 1])
                       : (r.parent_id != null ? OUT.doc.rems.find((x) => x.id === r.parent_id) : null);
    await deleteRem(r);
    if (prev) focusRem(prev.id, (prev.text || "").length);
  } else if (i > 0 && !hasKids) {
    // merge into the previous sibling's tail (only when this rem has no children)
    const prev = sibs[i - 1];
    const at = (prev.text || "").length;
    OUT.dirty.delete(r.id);
    await flushDirty();
    try { await api("/api/rems/" + prev.id, { method: "PATCH", body: JSON.stringify({ text: (prev.text || "") + text.textContent }) }); }
    catch (e) { toast("結合に失敗"); return; }
    prev.text = (prev.text || "") + text.textContent;
    await deleteRem(r, true);
    focusRem(prev.id, at);
  } else if (r.parent_id != null) {
    outdent(r, 0);
  }
}

function lastVisibleDescendant(r) {
  if (r.collapsed) return r;
  const kids = childrenOf(r.id);
  return kids.length ? lastVisibleDescendant(kids[kids.length - 1]) : r;
}

async function deleteRem(r, silent) {
  OUT.dirty.delete(r.id);
  try { await api("/api/rems/" + r.id, { method: "DELETE" }); }
  catch (e) { if (!silent) toast("削除に失敗"); return; }
  const ids = new Set();
  (function collect(id) { ids.add(id); OUT.doc.rems.filter((x) => x.parent_id === id).forEach((c) => collect(c.id)); })(r.id);
  OUT.doc.rems = OUT.doc.rems.filter((x) => !ids.has(x.id));
  renderTree();
}

async function toggleCollapse(r) {
  r.collapsed = !r.collapsed;
  renderTree();
  focusRem(r.id, (r.text || "").length);      // keep focus on the rem (a11y)
  try { await api("/api/rems/" + r.id, { method: "PATCH", body: JSON.stringify({ collapsed: r.collapsed }) }); } catch (e) { /* non-critical */ }
}

// -------------------------------------------------------------------------
// Drag & drop reorder
// -------------------------------------------------------------------------
function wireDrop(row, r) {
  row.addEventListener("dragover", (e) => {
    if (OUT.drag == null || OUT.drag === r.id) return;
    e.preventDefault();
    clearDropMarks();
    const rect = row.getBoundingClientRect();
    const after = (e.clientY - rect.top) > rect.height / 2;
    row.classList.add(after ? "drop-after" : "drop-before");
    row._dropAfter = after;
  });
  row.addEventListener("dragleave", () => { row.classList.remove("drop-before", "drop-after"); });
  row.addEventListener("drop", (e) => {
    if (OUT.drag == null || OUT.drag === r.id) return;
    e.preventDefault();
    const after = row._dropAfter;
    clearDropMarks();
    const dragId = OUT.drag; OUT.drag = null;
    if (OUT.busy) return;
    const dragged = OUT.doc.rems.find((x) => x.id === dragId);
    if (!dragged) return;
    const sibs = childrenOf(r.parent_id).filter((x) => x.id !== dragId);
    const idx = sibs.findIndex((x) => x.id === r.id);
    const afterId = after ? r.id : (idx > 0 ? sibs[idx - 1].id : null);
    withBusy(() => reorder(dragged, r.parent_id, afterId, 0));
  });
}

function clearDropMarks() {
  document.querySelectorAll(".drop-before, .drop-after").forEach((n) => n.classList.remove("drop-before", "drop-after"));
}

// -------------------------------------------------------------------------
// Caret / focus helpers
// -------------------------------------------------------------------------
function caretOffset(node) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return 0;
  const range = sel.getRangeAt(0);
  if (!node.contains(range.endContainer)) return 0;
  const pre = range.cloneRange();
  pre.selectNodeContents(node);
  pre.setEnd(range.endContainer, range.endOffset);
  return pre.toString().length;
}

function hasSelection() { const s = window.getSelection(); return s && !s.isCollapsed; }

function focusRem(id, offset) {
  requestAnimationFrame(() => {
    const node = document.querySelector('.rem-text[data-id="' + id + '"]');
    if (!node) return;
    node.focus();                 // -> enterEdit swaps to raw source
    setCaret(node, offset);
  });
}

function focusLastRem() {
  const roots = childrenOf(null);
  if (!roots.length) return;
  const last = lastVisibleDescendant(roots[roots.length - 1]);
  focusRem(last.id, (last.text || "").length);
}

function setCaret(node, offset) {
  const sel = window.getSelection();
  if (!sel) return;
  const range = document.createRange();
  const len = (node.textContent || "").length;
  const off = Math.max(0, Math.min(offset, len));
  try {
    if (node.firstChild && node.firstChild.nodeType === 3) range.setStart(node.firstChild, off);
    else range.setStart(node, 0);
    range.collapse(true);
    sel.removeAllRanges();
    sel.addRange(range);
  } catch (e) { /* ignore */ }
}

// place the caret at a viewport point inside `node` (used after a click swaps the
// rendered preview to raw source). Falls back to end-of-text.
function placeCaretFromPoint(node, pt) {
  try {
    const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(pt.x, pt.y) : null;
    if (r && node.contains(r.startContainer)) {
      const sel = window.getSelection();
      sel.removeAllRanges(); sel.addRange(r);
      return;
    }
  } catch (e) { /* ignore */ }
  setCaret(node, (node.textContent || "").length);
}

function caretRect(node) {
  const s = window.getSelection();
  if (s && s.rangeCount) {
    const r = s.getRangeAt(0).cloneRange();
    const rects = r.getClientRects();
    let rc = rects[rects.length - 1];
    if (!rc || !rc.height) rc = r.getBoundingClientRect();
    if (rc && (rc.height || rc.width || rc.top)) return rc;
  }
  return node.getBoundingClientRect();
}

function caretX() {
  const s = window.getSelection();
  if (!s || !s.rangeCount) return null;
  const rc = caretRect(document.activeElement || document.body);
  return rc ? rc.left : null;
}

// is the caret on the first (dir<0) / last (dir>0) visual line of `node`?
function atEdgeLine(node, dir) {
  const s = window.getSelection();
  if (!s || !s.rangeCount) return true;
  const range = s.getRangeAt(0);
  if (!node.contains(range.endContainer)) return true;
  const cr = range.getBoundingClientRect();
  if (!cr || (!cr.height && !cr.top)) return true;            // empty line -> edge
  const er = node.getBoundingClientRect();
  const lh = parseFloat(getComputedStyle(node).lineHeight) || 20;
  return dir < 0 ? (cr.top - er.top) < lh * 0.75 : (er.bottom - cr.bottom) < lh * 0.75;
}

function moveVertical(r, dir, x) {
  const vis = visibleRems();
  const i = vis.findIndex((v) => v.id === r.id);
  const target = vis[i + dir];
  if (!target) return;
  requestAnimationFrame(() => {
    const node = document.querySelector('.rem-text[data-id="' + target.id + '"]');
    if (!node) return;
    node.focus();                                 // -> enterEdit swaps to raw
    const rect = node.getBoundingClientRect();
    const y = dir < 0 ? rect.bottom - 4 : rect.top + 4;
    placeCaretFromPoint(node, { x: x != null ? x : rect.left + 4, y });
  });
}

function relTime(iso) {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "たった今";
  if (m < 60) return m + "分前";
  const h = Math.round(m / 60);
  if (h < 24) return h + "時間前";
  return Math.round(h / 24) + "日前";
}
