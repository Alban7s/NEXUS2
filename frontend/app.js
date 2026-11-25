/* NEXUS dashboard — core: helpers, icons, router, shared components.
 * Views live in views.js. No framework: plain DOM + fetch. */
(function () {
  "use strict";
  const API = window.NexusAPI;

  // ------------------------------------------------------------ helpers
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function ago(epochSec) {
    if (!epochSec) return "—";
    const s = Math.max(0, Date.now() / 1000 - epochSec);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return new Date(epochSec * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  const dur = (s) => (s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`);
  const clock = (epochSec) => new Date(epochSec * 1000).toLocaleTimeString([], { hour12: false });
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : null);

  // ------------------------------------------------------------ icons
  const ICONS = {
    home: '<path d="M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10"/>',
    rocket: '<path d="M5 15c-1.5 1.3-2 4-2 6 2 0 4.7-.5 6-2M14 4c3-1.5 6-1 7-1 0 1 .5 4-1 7l-6 6-5-5 5-7zM9 14l-3-1 2-3 3 1"/>',
    upload: '<path d="M12 16V4M7 9l5-5 5 5M4 16v4h16v-4"/>',
    github: '<path d="M9 19c-4 1.3-4-2-6-2.5M15 21v-3.2c0-1 .1-1.5-.5-2.1 2.8-.3 5.5-1.4 5.5-6A4.7 4.7 0 0018.700 6.500 4.400 4.400 0 0018.600 3.300S17.500 3 15 4.700a12 12 0 00-6 0C6.500 3 5.400 3.300 5.400 3.300a4.400 4.400 0 00-.1 3.200A4.700 4.700 0 004 9.700c0 4.600 2.700 5.700 5.500 6-.6.600-.6 1.200-.5 2.100V21"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    flow: '<circle cx="5" cy="12" r="2.500"/><circle cx="19" cy="6" r="2.500"/><circle cx="19" cy="18" r="2.500"/><path d="M7.500 12h4c2 0 2-6 5-6M11.500 12c3 0 3 6 5 6"/>',
    cube: '<path d="M12 3l8 4.500v9L12 21l-8-4.500v-9L12 3zM12 12l8-4.500M12 12L4 7.500M12 12v9"/>',
    shield: '<path d="M12 3l8 3v6c0 4.500-3.200 7.800-8 9-4.800-1.200-8-4.500-8-9V6l8-3zM8.500 12l2.500 2.500 4.500-5"/>',
    pulse: '<path d="M3 12h4l2.500-6 4 12 2.500-6H21"/>',
    server: '<rect x="3" y="4" width="18" height="6" rx="1.500"/><rect x="3" y="14" width="18" height="6" rx="1.500"/><path d="M7 7h.01M7 17h.01"/>',
    branch: '<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="8" r="2"/><path d="M6 7v10M18 10c0 4-6 3-12 7"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.900 4.900l2.100 2.100M17 17l2.100 2.100M4.900 19.100L7 17M17 7l2.100-2.100"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M16 7l3 3"/>',
    refresh: '<path d="M20 11a8 8 0 10-2.300 5.700M20 4v7h-7"/>',
    download: '<path d="M12 4v12M7 11l5 5 5-5M4 20h16"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    check: '<path d="M5 12.500l4.500 4.500L19 7.500"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    spinner: '<path d="M12 3a9 9 0 019 9" />',
    dash: '<path d="M7 12h10"/>',
    circle: '<circle cx="12" cy="12" r="3"/>',
  };
  const icon = (n) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[n] || ""}</svg>`;
  function renderIcons(root) { $$("i[data-icon]", root).forEach((el) => { el.innerHTML = icon(el.dataset.icon); el.removeAttribute("data-icon"); }); }

  // ------------------------------------------------------------ badges
  const STATUS_BADGE = {
    success: ["green", "Succeeded"], failed: ["red", "Failed"], running: ["blue", "Running"], queued: ["gray", "Queued"],
    connected: ["green", "Connected"], configured: ["blue", "Configured"], available: ["green", "Built in"],
    installed: ["green", "Installed"], external: ["gray", "Run from terminal"], not_connected: ["red", "Not connected"],
    not_configured: ["gray", "Not configured"], not_installed: ["gray", "Not installed"],
    completed: ["green", "Completed"], skipped: ["gray", "Skipped"], error: ["red", "Error"],
  };
  function badge(status, labelOverride) {
    const [tone, label] = STATUS_BADGE[status] || ["gray", status || "Unknown"];
    return `<span class="badge badge--${tone}">${esc(labelOverride || label)}</span>`;
  }
  const dot = (tone, pulse) => `<span class="dot dot--${tone}${pulse ? " dot--pulse" : ""}" aria-hidden="true"></span>`;
  function sourceTag(res) {
    if (!res) return "";
    const live = res.mode === "live";
    return `<span class="mode ${live ? "mode--live" : "mode--off"}">${live ? "LIVE" : "NOT CONNECTED"}</span><span>${esc(res.source || "")}</span>`;
  }
  const skeleton = (n = 3) => Array.from({ length: n }, () => '<div class="skeleton" style="margin:10px 0"></div>').join("");
  const empty = (title, text, action) => `<div class="empty"><strong>${esc(title)}</strong>${esc(text || "")}${action || ""}</div>`;

  // ------------------------------------------------------------ toasts / dialogs
  function toast(message, type) {
    const el = document.createElement("div");
    el.className = `toast toast--${type || "info"}`;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.textContent = message;
    $("#toasts").appendChild(el);
    setTimeout(() => { el.classList.add("is-leaving"); setTimeout(() => el.remove(), 260); }, type === "error" ? 7000 : 4200);
  }

  function confirmDialog({ title, bodyHtml, okLabel, danger }) {
    const dlg = $("#dialog");
    $("#dialog-title").textContent = title;
    $("#dialog-body").innerHTML = bodyHtml || "";
    const ok = $("#dialog-ok");
    ok.textContent = okLabel || "Confirm";
    ok.className = `btn ${danger ? "btn--danger" : "btn--primary"}`;
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.returnValue = "cancel";
      dlg.showModal();
    });
  }

  function openDrawer(title, html) {
    $("#drawer-title").textContent = title;
    $("#drawer-body").innerHTML = html;
    const d = $("#drawer");
    if (!d.open) d.showModal();
    renderIcons(d);
    return $("#drawer-body");
  }

  /** Run an async action on a button: spinner → success/failure flash → restore. */
  async function runButton(btn, action, labels) {
    const label = btn.querySelector(".btn-label");
    const original = label ? label.textContent : null;
    btn.classList.remove("is-success", "is-error");
    btn.classList.add("is-loading"); btn.disabled = true;
    if (label && labels && labels.loading) label.textContent = labels.loading;
    try {
      const result = await action();
      btn.classList.remove("is-loading"); btn.classList.add("is-success");
      if (label && labels && labels.success) label.textContent = labels.success;
      return result;
    } catch (err) {
      btn.classList.remove("is-loading"); btn.classList.add("is-error");
      if (label && labels && labels.error) label.textContent = labels.error;
      throw err;
    } finally {
      setTimeout(() => {
        btn.classList.remove("is-success", "is-error", "is-loading"); btn.disabled = false;
        if (label && original != null) label.textContent = original;
      }, reduceMotion() ? 400 : 1400);
    }
  }

  // ------------------------------------------------------------ pipeline stages + run panel
  const STEP_LABEL = { upload: "Extract", inspect: "Inspect", test: "Test", build: "Build", security: "Scan", publish: "Publish", deploy: "Deploy", verify: "Verify", plan: "Plan", rollback: "Roll back" };
  const STEP_HINT = { upload: "Safe unzip", inspect: "Detect type", test: "Project tests", build: "Docker image", security: "Trivy", publish: "Registry", deploy: "Kubernetes", verify: "Health check", plan: "Pick revision", rollback: "Apply" };
  const STEP_DONE_ICON = { success: "check", failed: "x", skipped: "dash", running: "spinner", pending: "circle" };
  const DEPLOY_STEPS = ["upload", "inspect", "test", "build", "security", "publish", "deploy", "verify"];

  function stagesHtml(order, steps, isStatic) {
    return order.map((s) => {
      const st = isStatic ? "pending" : (steps && steps[s]) || "pending";
      const word = st === "pending" ? (STEP_HINT[s] || "") : { running: "Running…", success: "Done", failed: "Failed", skipped: "Skipped" }[st];
      return `<li class="stage" data-step="${s}" data-status="${st}"><span class="stage-dot">${icon(STEP_DONE_ICON[st])}</span><strong>${esc(STEP_LABEL[s] || s)}</strong><small>${esc(word)}</small></li>`;
    }).join("");
  }
  function applyStages(ol, order, steps) {
    if (ol.children.length !== order.length) ol.innerHTML = stagesHtml(order, steps);
    order.forEach((s, i) => {
      const li = ol.children[i]; const st = steps[s] || "pending";
      if (li.dataset.status === st) return;
      li.dataset.status = st;
      li.querySelector(".stage-dot").innerHTML = icon(STEP_DONE_ICON[st]);
      li.querySelector("small").textContent = st === "pending" ? (STEP_HINT[s] || "") : { running: "Running…", success: "Done", failed: "Failed", skipped: "Skipped" }[st];
    });
  }

  /** Live release/rollback panel: stages + progress + log console + final result. */
  function createRunPanel(host, opts) {
    opts = opts || {};
    host.hidden = false;
    host.innerHTML = `<article class="card run-panel" aria-live="polite">
      <div class="run-head"><div><h3 class="run-title">${esc(opts.title || "Release")}</h3><span class="hint run-meta">Starting…</span></div><span class="run-badge">${badge("queued")}</span></div>
      <ol class="stages" aria-label="Pipeline stages"></ol>
      <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span></span></div>
      <div class="run-result"></div>
      <pre class="console" tabindex="0" aria-label="Live log"></pre></article>`;
    const ol = $(".stages", host), bar = $(".progress", host), con = $(".console", host);
    const meta = $(".run-meta", host), badgeEl = $(".run-badge", host), result = $(".run-result", host);
    let controller = null;

    function update(job) {
      const order = job.stepOrder || DEPLOY_STEPS;
      applyStages(ol, order, job.steps || {});
      const done = order.filter((s) => ["success", "skipped", "failed"].includes((job.steps || {})[s])).length;
      const pct = Math.round((done / order.length) * 100);
      bar.firstElementChild.style.width = `${job.status === "success" ? 100 : pct}%`;
      bar.setAttribute("aria-valuenow", String(pct));
      bar.classList.toggle("is-failed", job.status === "failed");
      badgeEl.innerHTML = badge(job.status);
      const proj = job.project && job.project.framework ? ` · ${job.project.framework}` : "";
      meta.textContent = `${job.app || ""}${proj}${job.mode ? " · " + job.mode + " mode" : ""}`;
      const atBottom = con.scrollTop + con.clientHeight >= con.scrollHeight - 24;
      (job.logs || []).forEach((l) => {
        const span = document.createElement("span");
        span.className = `l-${l.level}`;
        span.innerHTML = `<span class="ts">${esc(clock(l.ts))}</span>`;
        span.append(document.createTextNode(l.text + "\n"));
        con.appendChild(span);
      });
      if (atBottom) con.scrollTop = con.scrollHeight;
      if (job.status === "success" || job.status === "failed") showResult(job);
    }

    function showResult(job) {
      if (result.dataset.done === job.status) return;
      result.dataset.done = job.status;
      if (job.status === "success") {
        const url = safeUrl(job.previewUrl);
        const h = job.health || {};
        const verified = h.verified ? (h.podsTotal ? `${h.podsReady}/${h.podsTotal} pods ready on ${esc((h.images || [])[0] || "")}` : "Health check passed") : "";
        result.innerHTML = `<div class="result result--success"><span class="dot dot--green" style="margin-top:7px"></span><div><strong>${job.kind === "rollback" ? "Rollback verified" : "Release succeeded"} in ${esc(dur(job.durationSec))}</strong>
          <span>${verified}${url ? ` · <a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open preview ↗</a>` : ""}</span></div></div>`;
      } else {
        result.innerHTML = `<div class="result result--failed"><span class="dot dot--red" style="margin-top:7px"></span><div><strong>${job.kind === "rollback" ? "Rollback failed" : "Release failed"}</strong><span>${esc(job.error || "See the log below.")}</span></div></div>`;
      }
    }

    return {
      update,
      async follow(jobId) {
        if (controller) controller.abort();
        controller = new AbortController();
        const final = await API.followJob(jobId, update, controller.signal);
        if (final) update(Object.assign({}, final, { logs: [] }));
        return final;
      },
      stop() { if (controller) controller.abort(); },
    };
  }

  // ------------------------------------------------------------ router + shell
  const views = {};            // name -> { title, enter() }
  let current = null;

  function register(name, def) { views[name] = def; }

  function moveIndicator(link) {
    const ind = $("#nav-indicator");
    if (!link) { ind.classList.remove("is-ready"); return; }
    ind.style.setProperty("--y", `${link.offsetTop}px`);
    ind.style.height = `${link.offsetHeight}px`;
    ind.classList.add("is-ready");
  }

  function route() {
    const name = (location.hash.replace(/^#\/?/, "").split("/")[0]) || "dashboard";
    const key = views[name] ? name : "dashboard";
    if (current === key && !$(`.view[data-view="${key}"]`).hidden) return;
    $$(".view").forEach((v) => { v.hidden = v.dataset.view !== key; v.classList.remove("is-entering"); });
    const view = $(`.view[data-view="${key}"]`);
    void view.offsetWidth; view.classList.add("is-entering");
    $$(".nav-link").forEach((a) => { const on = a.dataset.route === key; a.classList.toggle("is-active", on); if (on) { a.setAttribute("aria-current", "page"); moveIndicator(a); } else a.removeAttribute("aria-current"); });
    $("#page-title").textContent = views[key].title;
    document.title = `${views[key].title} · NEXUS`;
    closeRail();
    window.scrollTo(0, 0);
    current = key;
    Promise.resolve(views[key].enter()).catch((e) => console.error(`[NEXUS] view ${key}:`, e));
  }

  function openRail() { $("#rail").classList.add("is-open"); $("#scrim").hidden = false; $("#nav-toggle").setAttribute("aria-expanded", "true"); }
  function closeRail() { $("#rail").classList.remove("is-open"); $("#scrim").hidden = true; $("#nav-toggle").setAttribute("aria-expanded", "false"); }

  // ------------------------------------------------------------ header status + access key
  async function refreshHeaderStatus() {
    const pill = $("#header-status");
    try {
      const { data } = await API.get("/api/health", 12000);
      const probes = Object.values(data.checks).filter((c) => "connected" in c);
      const on = probes.filter((c) => c.connected).length;
      const tone = on === probes.length ? "green" : on ? "amber" : "red";
      pill.innerHTML = `${dot(tone, tone === "green")}<span>${on} of ${probes.length} services connected</span>`;
      return data;
    } catch (e) {
      pill.innerHTML = `${dot("red")}<span>${e.status === 401 ? "Access key needed" : "Backend not reachable"}</span>`;
      return null;
    }
  }

  async function promptForKey(message) {
    const dlg = $("#dialog");
    $("#dialog-title").textContent = "Access key";
    $("#dialog-body").innerHTML = `<p>${esc(message || "This NEXUS server requires an access key (NEXUS_API_KEY). It is kept in memory only, unless you tick the box.")}</p>
      <div class="field"><label for="key-input">Key</label><input id="key-input" class="input" type="password" autocomplete="off" spellcheck="false"></div>
      <label style="display:flex;gap:8px;align-items:center;margin-top:10px;font-size:13.5px"><input type="checkbox" id="key-remember"> Remember for this browser tab</label>`;
    const ok = $("#dialog-ok"); ok.textContent = "Save key"; ok.className = "btn btn--primary";
    const saved = await new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.returnValue = "cancel"; dlg.showModal(); setTimeout(() => $("#key-input").focus(), 30);
    });
    if (!saved) return false;
    API.setKey($("#key-input").value, $("#key-remember").checked);
    try { const s = await API.get("/api/session"); if (s.data.authRequired && !s.data.authenticated) throw new Error("rejected"); toast("Access key accepted.", "success"); }
    catch (_) { toast("That key was not accepted by the server.", "error"); return false; }
    current = null; route(); refreshHeaderStatus();
    return true;
  }

  let keyPrompted = false;
  window.addEventListener("nexus:unauthorized", () => { if (!keyPrompted) { keyPrompted = true; promptForKey("The server rejected the request. Enter the NEXUS access key."); setTimeout(() => (keyPrompted = false), 4000); } });

  // ------------------------------------------------------------ boot
  window.NEXUS = { $, $$, esc, sleep, ago, dur, clock, safeUrl, icon, renderIcons, badge, dot, sourceTag, skeleton, empty, toast, confirmDialog, openDrawer, runButton, stagesHtml, applyStages, createRunPanel, register, route, refreshHeaderStatus, promptForKey, reduceMotion, DEPLOY_STEPS, STEP_LABEL };

  document.addEventListener("DOMContentLoaded", async () => {
    renderIcons(document);
    $("#nav-toggle").addEventListener("click", () => ($("#rail").classList.contains("is-open") ? closeRail() : openRail()));
    $("#scrim").addEventListener("click", closeRail);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeRail(); });
    $("#drawer-close").addEventListener("click", () => $("#drawer").close());
    $("#drawer").addEventListener("click", (e) => { if (e.target === $("#drawer")) $("#drawer").close(); });
    $("#key-btn").addEventListener("click", () => promptForKey());
    window.addEventListener("hashchange", route);
    window.addEventListener("resize", () => moveIndicator($(".nav-link.is-active")));
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => moveIndicator($(".nav-link.is-active")));

    route();
    refreshHeaderStatus();
    setInterval(() => { if (!document.hidden) refreshHeaderStatus(); }, 30000);
    try { const s = await API.get("/api/session"); if (s.data.authRequired && !s.data.authenticated) promptForKey(); } catch (_) { /* header shows it */ }
  });
})();
