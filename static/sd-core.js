// sd-core.js — 3-zone shell controller (Phase B1).
//
// Loaded AFTER app.js as a classic script, so it shares app.js's global scope and
// reuses its helpers ($ / el, defined there) without redeclaring them. It owns the
// app-vs-notes mode switch, sidebar navigation, breadcrumb, and right panel.
// outline.js (loaded next) fills in SD.onNav.
//
// NOTE: app.js keeps toggling .active on the tab buttons; the visible sidebar
// highlight is .side-item.current, managed here — the two never fight.

const SD = {
  mode: "app",          // "app" | "notes"
  onNav: null,          // set by outline.js: (nav) => {...}

  setMode(m) {
    SD.mode = m;
    const main = $("#main"), nv = $("#notes-view");
    if (main) main.classList.toggle("hidden", m === "notes");
    if (nv) nv.classList.toggle("hidden", m !== "notes");
    const fab = $("#add-fab");
    if (fab) fab.classList.toggle("hidden", m === "notes");   // quick-add is assignment-only
    if (m !== "notes") { const cr = $("#crumbs"); if (cr) { cr.hidden = true; cr.innerHTML = ""; } }
    const sb = $("#sidebar"); if (sb) sb.classList.remove("open");   // close mobile drawer
  },

  setActiveNav(nav) {
    document.querySelectorAll(".side-item").forEach((b) => b.classList.remove("current"));
    if (nav) { const b = document.querySelector('.side-item[data-nav="' + nav + '"]'); if (b) b.classList.add("current"); }
  },

  setCurrentByTab(name) {
    document.querySelectorAll(".side-item").forEach((b) => b.classList.remove("current"));
    const b = document.querySelector('.side-item[data-tab="' + name + '"]'); if (b) b.classList.add("current");
  },

  setCrumbs(items) {
    const cr = $("#crumbs"); if (!cr) return;
    cr.innerHTML = "";
    if (!items || !items.length) { cr.hidden = true; return; }
    cr.hidden = false;
    items.forEach((it, i) => {
      if (i) cr.appendChild(el("span", "sep", "›"));
      if (it.onClick) { const b = el("button", "", it.label); b.onclick = it.onClick; cr.appendChild(b); }
      else cr.appendChild(el("span", "", it.label));
    });
  },

  right(node) {
    const rb = $("#rightbar"), shell = $("#shell");
    if (!rb) return;
    if (node) { rb.innerHTML = ""; rb.appendChild(node); rb.hidden = false; if (shell) shell.classList.add("has-right"); }
    else { rb.innerHTML = ""; rb.hidden = true; if (shell) shell.classList.remove("has-right"); }
  },
};

// app.js owns tab/panel switching via switchTab(); wrap it so every tab switch
// also leaves notes mode + moves the sidebar highlight. Drop-to-upload and review
// deep-links call switchTab() directly, so wrapping it (not the click handler) is
// what makes those paths mode-correct too.
if (typeof switchTab === "function") {
  const _origSwitchTab = switchTab;
  // eslint-disable-next-line no-global-assign, no-func-assign
  switchTab = function (name) { SD.setMode("app"); SD.setCurrentByTab(name); return _origSwitchTab(name); };
}

function sdInitShell() {
  document.querySelectorAll(".side-item[data-nav]").forEach((b) => {
    if (b.disabled) return;
    b.addEventListener("click", () => {
      SD.setMode("notes");
      SD.setActiveNav(b.dataset.nav);
      if (SD.onNav) SD.onNav(b.dataset.nav);
    });
  });
  const st = $("#side-toggle"), sb = $("#sidebar");
  if (st && sb) st.addEventListener("click", () => sb.classList.toggle("open"));
  SD.setCurrentByTab("today");   // default view is the Today tab (app mode)
}
document.addEventListener("DOMContentLoaded", sdInitShell);
