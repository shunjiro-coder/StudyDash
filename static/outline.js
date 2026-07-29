// outline.js — RemNote-style outliner (Phase B1).
//
// Loaded after app.js + sd-core.js as a classic script; reuses app.js's
// api()/el()/$()/toast() and sd-core's SD.* without redeclaring them.
//
// Editing model (B1): 1 rem = 1 plain plaintext-only block. Plain typing NEVER
// re-renders the tree (so the caret is left completely alone — the source/preview
// live-render + caret restoration is Phase B2). Only STRUCTURAL ops (Enter, Tab,
// collapse, DnD, delete) re-render, after which we re-focus the relevant block.
// IME-safe: while composing (compositionstart..end / isComposing) Enter/Tab/
// Backspace handling and saves are suppressed. Autosave: 700ms debounce +
// sendBeacon on unload.

"use strict";

const OUT = {
  doc: null,          // {id, title, is_daily, daily_date, rems:[...]}
  dirty: new Map(),   // rem id -> pending text
  saveTimer: null,
  drag: null,         // rem id currently being dragged
  busy: false,        // a structural op (create/reorder/delete) is in flight
};

// Serialize structural ops so a fast double-press (Enter auto-repeat, double Tab)
// can't re-enter on stale DOM and double-submit (e.g. duplicate a rem). Keydown
// checks OUT.busy and drops repeats; each op runs inside this guard.
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
    const left = el("div");
    left.appendChild(el("div", "title", docLabel(d)));
    left.appendChild(el("div", "meta", relTime(d.updated_at)));
    it.appendChild(left);
    it.onclick = () => openDoc(d.id);
    list.appendChild(it);
  });
  host.appendChild(list);
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

  const tree = el("div", "rem-tree");
  tree.onclick = (e) => { if (e.target === tree) focusLastRem(); };   // click empty space -> last rem
  host.appendChild(tree);
  renderTree();

  if (!doc.rems.length) createRem({ parent_id: null, after_id: null, focus: true });
}

function tree() { return $(".rem-tree"); }

function childrenOf(pid) {
  return OUT.doc.rems
    .filter((r) => r.parent_id === pid)
    .sort((a, b) => a.position - b.position || a.id - b.id);
}

function renderTree() {
  const t = tree(); if (!t) return;
  t.innerHTML = "";
  childrenOf(null).forEach((r) => t.appendChild(remNode(r)));
}

function remNode(r) {
  const node = el("div", "rem-node" + (r.done ? " done" : ""));
  node.dataset.id = r.id;

  const row = el("div", "rem-row");
  const handle = el("span", "rem-handle", "⠿");
  handle.draggable = true;
  handle.addEventListener("dragstart", (e) => { OUT.drag = r.id; if (e.dataTransfer) e.dataTransfer.effectAllowed = "move"; });
  handle.addEventListener("dragend", () => { OUT.drag = null; clearDropMarks(); });
  row.appendChild(handle);

  const kids = childrenOf(r.id);
  const caret = el("span", "rem-caret" + (kids.length ? "" : " leaf"), r.collapsed ? "▸" : "▾");
  if (kids.length) caret.onclick = () => toggleCollapse(r);
  row.appendChild(caret);

  row.appendChild(el("span", "rem-bullet", "•"));

  const text = el("div", "rem-text" + (r.text ? "" : " empty"));
  text.contentEditable = "plaintext-only";
  text.dataset.id = r.id;
  text.dataset.ph = "";
  text.textContent = r.text || "";
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

// -------------------------------------------------------------------------
// Editing one rem block
// -------------------------------------------------------------------------
function wireRemText(text, r) {
  let composing = false;
  text.addEventListener("compositionstart", () => { composing = true; });
  text.addEventListener("compositionend", () => { composing = false; markDirty(r.id, text.textContent, text); });
  text.addEventListener("input", () => { text.classList.toggle("empty", !text.textContent); if (!composing) markDirty(r.id, text.textContent, text); });
  text.addEventListener("keydown", (e) => {
    if (composing || e.isComposing) return;   // never act mid-IME
    const structural =
      (e.key === "Enter" && !e.shiftKey) ||
      e.key === "Tab" ||
      (e.key === "Backspace" && caretOffset(text) === 0 && !hasSelection());
    if (!structural) return;
    e.preventDefault();                        // consume even while busy, so the key never inserts
    if (OUT.busy) return;                      // an op is in flight — drop this repeat
    if (e.key === "Enter") withBusy(() => onEnter(r, text));
    else if (e.key === "Tab" && !e.shiftKey) withBusy(() => indent(r, caretOffset(text)));
    else if (e.key === "Tab" && e.shiftKey) withBusy(() => outdent(r, caretOffset(text)));
    else withBusy(() => onBackspaceStart(r, text));
  });
  text.addEventListener("blur", () => flushSoon(0));
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
    if (restored) flushSoon(2000);   // re-arm so the restored items are resent
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
    // drop makes `dragged` a sibling of `r`, before or after it
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
    node.focus();
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

function relTime(iso) {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "たった今";
  if (m < 60) return m + "分前";
  const h = Math.round(m / 60);
  if (h < 24) return h + "時間前";
  return Math.round(h / 24) + "日前";
}
