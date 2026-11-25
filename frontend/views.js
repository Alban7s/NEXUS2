/* NEXUS views. Every number, status and table row comes from a backend response.
 * If an integration is unavailable the view says so ("Not connected") — nothing is invented. */
(function () {
  "use strict";
  const N = window.NEXUS, API = window.NexusAPI;
  const { $, $$, esc, ago, dur, icon, badge, dot, sourceTag, skeleton, empty, toast, confirmDialog, openDrawer, runButton, stagesHtml, createRunPanel, register, DEPLOY_STEPS } = N;

  const safe = async (p) => { try { return await p; } catch (e) { return { __error: e }; } };
  const failed = (r) => !r || r.__error;
  const errText = (r) => (r && r.__error ? r.__error.message : "Unavailable");
  const authBanner = (r) => (r && r.__error && r.__error.status === 401
    ? `<div class="banner banner--warn"><div><strong>Access key required</strong>Use the “Access key” button at the top right.</div></div>` : "");
  const notConnected = (title, text, steps) => `<div class="banner banner--warn"><div><strong>${esc(title)}</strong>${esc(text || "")}${steps ? `<ul>${steps.map((s) => `<li>${s}</li>`).join("")}</ul>` : ""}</div></div>`;
  const stat = (label, value, sub, off) => `<article class="card stat"><span class="stat-label">${label}</span><span class="stat-value${off ? " is-off" : ""}">${value}</span><span class="stat-sub">${sub || ""}</span></article>`;
  const dl = (pairs) => `<dl class="facts">${pairs.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const sevTone = { CRITICAL: "#D94743", HIGH: "#EE7A3C", MEDIUM: "#D9A12B", LOW: "#3C93DD", UNKNOWN: "#8A9BB0" };
  const SEV = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"];
  const sevBadge = (s) => `<span class="badge" style="background:${sevTone[s]}22;color:${sevTone[s]}">${esc(s)}</span>`;
  const setText = (sel, t) => { const e = $(sel); if (e) e.textContent = t; };

  // ======================================================= DASHBOARD
  register("dashboard", {
    title: "Dashboard",
    async enter() {
      $("#dash-stats").innerHTML = skeleton(1).repeat(4);
      const [jobs, integ, sec, k8s] = await Promise.all([safe(API.get("/api/deploy/jobs")), safe(API.get("/api/integrations")), safe(API.get("/api/security")), safe(API.get("/api/kubernetes"))]);
      const list = failed(jobs) ? [] : jobs.data.filter((j) => j.kind === "deploy");
      const ok = list.filter((j) => j.status === "success").length, bad = list.filter((j) => j.status === "failed").length;
      const finished = ok + bad;
      const latest = list[0];
      const items = failed(integ) ? [] : integ.data.items;
      const get = (k) => items.find((i) => i.key === k);
      const docker = get("docker");

      $("#dash-stats").innerHTML = [
        stat("Releases", failed(jobs) ? "—" : String(list.length), failed(jobs) ? errText(jobs) : `${ok} succeeded · ${bad} failed`, failed(jobs)),
        stat("Success rate", finished ? `${Math.round((ok / finished) * 100)}%` : "—", finished ? `of ${finished} finished releases` : "No finished releases yet", !finished),
        stat(`${dot(docker && docker.status === "connected" ? "green" : "red")}Docker`, docker && docker.status === "connected" ? "Connected" : "Not connected", docker ? esc((docker.meta && docker.meta.code) || "") : "", !(docker && docker.status === "connected")),
        stat("Kubernetes", failed(k8s) || k8s.mode !== "live" ? "Not connected" : esc(k8s.data.clusterStatus), failed(k8s) || k8s.mode !== "live" ? "No cluster reachable by the backend" : `${k8s.data.pods.running}/${k8s.data.pods.total} pods running`, failed(k8s) || k8s.mode !== "live"),
      ].join("");

      $("#dash-latest").innerHTML = !latest ? empty("No releases yet", "Upload a ZIP to run your first release.", '<br><a class="btn btn--primary btn--sm" href="#/zip">Upload a ZIP</a>') : `
        <div class="mini-row" style="margin-bottom:12px"><strong>${esc(latest.app)}</strong>${badge(latest.status)}</div>
        <ol class="stages">${stagesHtml(latest.stepOrder, latest.steps)}</ol>
        <p class="note">${esc(latest.mode ? latest.mode + " mode · " : "")}${esc(ago(latest.createdAt))}${latest.error ? ` · ${esc(latest.error).slice(0, 140)}` : ""}</p>`;

      $("#dash-services").innerHTML = !items.length ? empty("Unavailable", errText(integ)) : `<div class="mini-list">${items.filter((i) => !["zip", "terraform", "ansible"].includes(i.key)).map((i) =>
        `<div class="mini-row"><span>${dot({ connected: "green", installed: "green", configured: "blue" }[i.status] || (i.status === "not_connected" ? "red" : "gray"))}${esc(i.name)}</span>${badge(i.status)}</div>`).join("")}</div>`;

      // hero rail: live text only
      setText('[data-live="history"]', failed(jobs) ? "History unavailable" : `${list.length} release${list.length === 1 ? "" : "s"}`);
      setText('[data-live="kubernetes"]', failed(k8s) || k8s.mode !== "live" ? "Not connected" : k8s.data.clusterStatus);
      const t = failed(sec) ? null : sec.data;
      setText('[data-live="security"]', !t ? "Unavailable" : t.trivy.installed ? `Trivy ${t.trivy.version || "ready"}` : "Trivy not installed");
      setText('[data-live="infrastructure"]', "Host details");
    },
  });

  // ======================================================= NEW DEPLOYMENT
  register("deploy", {
    title: "New Deployment",
    async enter() {
      $("#deploy-overview-steps").innerHTML = stagesHtml(DEPLOY_STEPS, {}, true);
      const cfg = await safe(API.get("/api/deploy/config"));
      $("#deploy-target").innerHTML = failed(cfg) ? "" : `${dot("blue")}<span>${esc(cfg.data.label)}</span>`;
    },
  });

  // ======================================================= ZIP UPLOAD
  (function zipView() {
    let file = null, panel = null, busy = false, maxMb = 100;
    const input = () => $("#zip-input"), form = () => $("#zip-form");

    function setFile(f) {
      const err = $("#zip-error"); err.hidden = true;
      if (f && !/\.zip$/i.test(f.name)) { showError("Please choose a .zip file."); f = null; }
      else if (f && f.size > maxMb * 1024 * 1024) { showError(`That file is ${(f.size / 1048576).toFixed(1)} MB; the limit is ${maxMb} MB.`); f = null; }
      else if (f && f.size === 0) { showError("That file is empty."); f = null; }
      file = f;
      const dz = $("#dropzone");
      dz.classList.toggle("has-file", !!f);
      $("#dropzone-title").textContent = f ? f.name : "Drop your project ZIP here";
      $("#dropzone-sub").textContent = f ? `${(f.size / 1048576).toFixed(2)} MB · ready` : "or click to browse";
      $("#zip-submit").disabled = !f || busy; $("#zip-clear").hidden = !f;
    }
    const showError = (m) => { const e = $("#zip-error"); e.textContent = m; e.hidden = false; };

    register("zip", {
      title: "ZIP Upload",
      async enter() {
        const cfg = await safe(API.get("/api/deploy/config"));
        if (!failed(cfg)) { maxMb = cfg.data.maxUploadMb || 100; setText("#max-upload", String(maxMb)); $("#zip-target").innerHTML = `${dot("blue")}<span>${esc(cfg.data.label)}</span>`; }
        if (!form().dataset.bound) {
          form().dataset.bound = "1";
          const dz = $("#dropzone");
          dz.addEventListener("click", () => input().click());
          dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input().click(); } });
          ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("is-dragover"); }));
          ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("is-dragover"); }));
          dz.addEventListener("drop", (e) => setFile(e.dataTransfer.files[0] || null));
          input().addEventListener("change", () => setFile(input().files[0] || null));
          $("#zip-clear").addEventListener("click", () => { input().value = ""; setFile(null); });
          form().addEventListener("submit", onSubmit);
        }
      },
    });

    async function onSubmit(e) {
      e.preventDefault();
      if (!file || busy) return;
      busy = true; $("#zip-error").hidden = true;
      const fd = new FormData(); fd.append("file", file);
      const name = $("#zip-name").value.trim(); if (name) fd.append("app_name", name);
      const bar = $("#upload-bar"); $("#upload-progress").hidden = false; bar.style.width = "0";
      try {
        const res = await runButton($("#zip-submit"), () => API.upload("/api/deploy", fd, (p) => (bar.style.width = `${Math.round(p * 100)}%`)), { loading: "Uploading…", success: "Accepted", error: "Rejected" });
        const { jobId, app, project } = res.data;
        toast(`Accepted ${app} (${project.framework}). Release started.`, "success");
        panel = createRunPanel($("#zip-run"), { title: app });
        $("#zip-run").scrollIntoView({ behavior: N.reduceMotion() ? "auto" : "smooth", block: "start" });
        const final = await panel.follow(jobId);
        toast(final && final.status === "success" ? `${app} released.` : `${app} failed: ${(final && final.error) || "see log"}`, final && final.status === "success" ? "success" : "error");
      } catch (err) {
        showError(err.message);
      } finally { busy = false; $("#upload-progress").hidden = true; $("#zip-submit").disabled = !file; }
    }
  })();

  // ======================================================= GITHUB
  (function githubView() {
    let panel = null, cfg = null;
    async function load() {
      const res = await safe(API.get("/api/github/config"));
      if (failed(res)) { $("#gh-config").innerHTML = authBanner(res) || notConnected("Cannot load configuration", errText(res)); return; }
      cfg = res.data;
      $("#gh-source").innerHTML = sourceTag(res);
      $("#gh-config").innerHTML = dl([
        ["Repository", cfg.repoUrl ? `<span class="mono">${esc(cfg.repoUrl)}</span>` : badge("not_configured")],
        ["Default branch", `<span class="mono">${esc(cfg.branch)}</span>`],
        ["Jenkins job", `<span class="mono">${esc(cfg.jenkins.job)}</span> ${badge(cfg.jenkins.status)}`],
        ["Webhook secret", cfg.webhookSecretConfigured ? badge("configured") : badge("not_configured")],
        ["Webhook payload URL", `<span class="mono">${esc(location.origin + cfg.webhookPath)}</span>`],
      ]);
      const bi = $("#gh-branch"); if (!bi.value) bi.value = cfg.branch;
      $("#gh-submit").disabled = !cfg.repoConfigured;
      const items = [
        [cfg.repoConfigured, "Set <code>GITHUB_REPO_URL</code> and <code>GITHUB_BRANCH</code> in <code>.env</code>"],
        [cfg.jenkins.status === "connected", "Set <code>JENKINS_URL</code>, <code>JENKINS_USER</code>, <code>JENKINS_API_TOKEN</code> and create the job from <code>jenkins/Jenkinsfile</code>"],
        [true, "Add the Git credential for private repositories <strong>inside Jenkins</strong> (id <code>github-repo-creds</code>)"],
        [cfg.webhookSecretConfigured, "Set <code>GITHUB_WEBHOOK_SECRET</code>; add a GitHub webhook (push events, JSON) to the payload URL"],
      ];
      $("#gh-checklist").innerHTML = items.map(([done, html]) => `<li class="${done ? "is-done" : ""}">${html}</li>`).join("");
      $("#gh-deliveries").innerHTML = !cfg.recentDeliveries.length ? empty("No webhook activity yet", "Pushes and manual deploys appear here.") :
        `<div class="table-scroll"><table><thead><tr><th>When</th><th>Event</th><th>Branch</th><th>Result</th></tr></thead><tbody>${cfg.recentDeliveries.map((d) =>
          `<tr><td>${esc(ago(d.at))}</td><td>${esc(d.event)}</td><td class="mono">${esc(d.branch || "—")}</td><td>${esc(d.result)}</td></tr>`).join("")}</tbody></table></div>`;
    }

    async function watchForJob(sinceSec) {
      for (let i = 0; i < 60; i++) {
        await N.sleep(4000);
        const r = await safe(API.get("/api/deploy/jobs?limit=10"));
        if (failed(r)) continue;
        const job = r.data.find((j) => j.source === "github" && j.createdAt >= sinceSec - 2);
        if (job) {
          panel = createRunPanel($("#gh-run"), { title: job.app });
          const final = await panel.follow(job.id);
          toast(final && final.status === "success" ? `${job.app} released from GitHub.` : `GitHub release failed: ${(final && final.error) || "see log"}`, final && final.status === "success" ? "success" : "error");
          load();
          return;
        }
      }
      toast("Jenkins did not hand a build to NEXUS within 4 minutes. Check the Jenkins job.", "error");
    }

    register("github", {
      title: "GitHub Deployment",
      async enter() {
        await load();
        const f = $("#gh-form");
        if (!f.dataset.bound) {
          f.dataset.bound = "1";
          f.addEventListener("submit", async (e) => {
            e.preventDefault();
            $("#gh-error").hidden = true;
            const since = Date.now() / 1000;
            try {
              const r = await runButton($("#gh-submit"), () => API.post("/api/github/deploy", { branch: $("#gh-branch").value.trim() || null }), { loading: "Asking Jenkins…", success: "Queued", error: "Failed" });
              toast(`Jenkins queued a build of ${r.data.branch}. NEXUS will pick it up when Jenkins hands the code over.`, "success");
              load(); watchForJob(since);
            } catch (err) { const el = $("#gh-error"); el.textContent = err.message; el.hidden = false; }
          });
        }
      },
    });
  })();

  // ======================================================= HISTORY
  (function historyView() {
    let filter = "all", jobs = [];
    function render() {
      const rows = jobs.filter((j) => filter === "all" || (filter === "rollback" ? j.kind === "rollback" : j.status === filter));
      $("#history-table").innerHTML = !rows.length ? empty("Nothing here yet", jobs.length ? "No releases match this filter." : "Releases and rollbacks appear here after they run.") :
        `<table><thead><tr><th>Release</th><th>Source</th><th>Mode</th><th>Image</th><th>Status</th><th>Started</th><th class="num">Duration</th></tr></thead><tbody>${rows.map((j) =>
          `<tr class="is-clickable" tabindex="0" data-id="${esc(j.id)}"><td><strong>${esc(j.app)}</strong><br><span class="hint mono">${esc(j.id)}</span></td>
          <td>${j.kind === "rollback" ? '<span class="badge badge--amber">rollback</span>' : `<span class="badge badge--gray">${esc(j.source)}</span>`}</td>
          <td>${esc(j.mode || "—")}</td><td class="mono">${esc((j.imageTag || "—").split("/").pop())}</td><td>${badge(j.status)}</td><td>${esc(ago(j.createdAt))}</td><td class="num">${esc(dur(j.durationSec))}</td></tr>`).join("")}</tbody></table>`;
    }
    async function load() {
      $("#history-table").innerHTML = `<div style="padding:16px">${skeleton(4)}</div>`;
      const r = await safe(API.get("/api/deployments"));
      if (failed(r)) { $("#history-table").innerHTML = authBanner(r) || empty("History unavailable", errText(r)); return; }
      jobs = r.data; render();
    }
    async function openJob(id) {
      const body = openDrawer("Loading…", skeleton(4));
      const r = await safe(API.get(`/api/deploy/jobs/${encodeURIComponent(id)}`));
      if (failed(r)) { body.innerHTML = empty("Could not load", errText(r)); return; }
      const j = r.data; $("#drawer-title").textContent = `${j.app} · ${j.kind}`;
      const h = j.health || {};
      body.innerHTML = `<div class="run-head"><span class="hint mono">${esc(j.id)}</span>${badge(j.status)}</div>
        <ol class="stages">${stagesHtml(j.stepOrder, j.steps)}</ol>
        ${j.error ? `<div class="result result--failed"><div><strong>Failure</strong>${esc(j.error)}</div></div>` : ""}
        ${dl([["Source", esc(j.source)], ["Mode", esc(j.mode || "—")], ["Image", `<span class="mono">${esc(j.imageTag || "—")}</span>`], ["Project", esc(j.project ? j.project.framework : "—")],
          ["Git", j.git ? `<span class="mono">${esc(j.git.repo)} @ ${esc(j.git.branch)}${j.git.commit ? " " + esc(j.git.commit.slice(0, 7)) : ""}</span>` : "—"],
          ["Health check", h.verified === true ? "Verified" : h.verified === false ? esc(h.reason || "Not verified") : "—"], ["Duration", esc(dur(j.durationSec))]])}
        ${(j.scans || []).length ? `<div><h3 style="margin-bottom:8px">Security scans</h3>${j.scans.map((s) => `<div class="mini-row"><span>${esc(s.scanType)} scan ${badge(s.status)}</span><span class="mono">${s.status === "completed" ? SEV.map((k) => `${k[0]}:${s.counts[k]}`).join(" ") : esc(s.error || "")}</span></div>`).join("")}</div>` : ""}
        <div><h3 style="margin-bottom:8px">Log</h3><pre class="console">${j.logs.map((l) => `<span class="l-${l.level}"><span class="ts">${esc(N.clock(l.ts))}</span>${esc(l.text)}\n</span>`).join("") || "No log lines."}</pre></div>`;
    }
    register("history", {
      title: "Deployment History",
      async enter() {
        await load();
        const t = $("#history-table");
        if (!t.dataset.bound) {
          t.dataset.bound = "1";
          t.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openJob(tr.dataset.id); });
          t.addEventListener("keydown", (e) => { if (e.key === "Enter") { const tr = e.target.closest("tr[data-id]"); if (tr) openJob(tr.dataset.id); } });
          $("#history-filter").addEventListener("click", (e) => { const c = e.target.closest(".chip"); if (!c) return; filter = c.dataset.filter; $$(".chip", $("#history-filter")).forEach((x) => x.classList.toggle("is-active", x === c)); render(); });
          $("#history-refresh").addEventListener("click", (e) => runButton(e.currentTarget, load, { success: "Updated" }));
        }
      },
    });
  })();

  // ======================================================= CI/CD PIPELINE
  register("pipeline", {
    title: "CI/CD Pipeline",
    async enter() {
      const [jobs, integ, actions] = await Promise.all([safe(API.get("/api/deploy/jobs")), safe(API.get("/api/integrations")), safe(API.get("/api/pipelines"))]);
      const latest = failed(jobs) ? null : jobs.data.find((j) => j.kind === "deploy");
      $("#pipeline-source").innerHTML = failed(jobs) ? "" : sourceTag(jobs);
      $("#pipeline-stages").innerHTML = stagesHtml(latest ? latest.stepOrder : DEPLOY_STEPS, latest ? latest.steps : {}, !latest);
      setText("#pipeline-note", latest ? `Showing ${latest.app} (${latest.id}) — ${latest.status}.` : "No release has run yet, so every stage is shown as pending.");
      const jk = failed(integ) ? null : integ.data.items.find((i) => i.key === "jenkins");
      $("#pipeline-jenkins").innerHTML = jk ? `<div class="mini-row">${badge(jk.status)}</div><p class="note">${esc(jk.detail)}</p><a class="btn btn--secondary btn--sm" style="margin-top:10px" href="#/github">GitHub deployment</a>` : empty("Unavailable", errText(integ));
      $("#pipeline-actions").innerHTML = failed(actions) ? empty("Unavailable", errText(actions)) : actions.mode !== "live" ? empty("Not configured", actions.error || "") :
        `<div class="mini-list">${actions.data.slice(0, 6).map((r) => `<div class="mini-row"><span>${dot(r.status === "SUCCESS" ? "green" : r.status === "FAILURE" ? "red" : "gray")}${esc(r.name)} <span class="hint">${esc(r.branch)} ${esc(r.commit)}</span></span>${badge(r.status === "SUCCESS" ? "success" : r.status === "FAILURE" ? "failed" : "queued", r.status)}</div>`).join("")}</div>`;
      $("#pipeline-roles").innerHTML = [
        ["NEXUS", "The one shared pipeline: inspect, test, build, scan, publish, deploy, verify."],
        ["Jenkins", "Fetches GitHub code with admin-held credentials and hands it to NEXUS."],
        ["Trivy", "Scans source and the built image; the gate can stop a release."],
        ["Kubernetes", "Runs the app; NEXUS verifies rollout and supports rollback."],
        ["Argo CD", "Optional: keeps the cluster in sync with Git manifests."],
        ["Terraform · Ansible", "Create and configure the AWS server (run from your terminal)."],
      ].map(([h, p]) => `<div class="role"><h4>${h}</h4><p>${p}</p></div>`).join("");
    },
  });

  // ======================================================= KUBERNETES
  (function k8sView() {
    let selected = null, deployments = [], runPanel = null;
    async function loadRevisions() {
      const host = $("#k8s-revisions");
      if (!selected) { host.innerHTML = empty("No deployment selected", "Release something to Kubernetes first."); return; }
      host.innerHTML = skeleton(3);
      const r = await safe(API.get(`/api/k8s/deployments/${encodeURIComponent(selected)}/revisions`));
      if (failed(r) || r.mode !== "live") { host.innerHTML = empty("Revisions unavailable", failed(r) ? errText(r) : r.error); return; }
      host.innerHTML = !r.data.length ? empty("No revision history", "") : `<div class="table-scroll"><table><thead><tr><th>Revision</th><th>Image</th><th>Created</th><th>Ready</th><th>State</th><th></th></tr></thead><tbody>${r.data.map((v) =>
        `<tr><td><strong>#${v.revision}</strong></td><td class="mono">${esc((v.image || "").split("/").pop())}</td><td>${esc(v.createdAt ? new Date(v.createdAt).toLocaleString() : "—")}</td><td>${v.readyReplicas}/${v.replicas}</td>
        <td>${v.current ? '<span class="badge badge--blue">current</span>' : ""} ${v.knownGood ? '<span class="badge badge--green">verified by NEXUS</span>' : ""}</td>
        <td><button class="btn btn--secondary btn--sm" data-rev="${v.revision}" data-image="${esc(v.image)}" ${v.current ? "disabled" : ""}><span class="btn-label">Roll back</span></button></td></tr>`).join("")}</tbody></table></div>`;
    }
    async function load() {
      const [snap, wl] = await Promise.all([safe(API.get("/api/kubernetes")), safe(API.get("/api/k8s/workloads"))]);
      const live = !failed(snap) && snap.mode === "live";
      $("#k8s-source").innerHTML = failed(snap) ? "" : sourceTag(snap);
      $("#k8s-stats").innerHTML = live ? [
        stat("Cluster", esc(snap.data.clusterStatus), `${snap.data.namespaces} namespaces`),
        stat("Nodes", `${snap.data.nodes.ready}/${snap.data.nodes.total}`, "ready"),
        stat("Pods", `${snap.data.pods.running}/${snap.data.pods.total}`, `${snap.data.pods.pending} pending · ${snap.data.pods.failed} failed`),
        stat("Deployments", `${snap.data.deployments.available}/${snap.data.deployments.total}`, "available"),
      ].join("") : notConnected("Kubernetes is not connected", "The backend cannot reach a cluster, so no cluster numbers are shown.", [
        "Start Minikube: <code>minikube start --driver=docker</code>", "Make sure the user running NEXUS has a working <code>~/.kube/config</code>", "Set <code>K8S_IMAGE_SOURCE=minikube</code> (or a registry) in <code>.env</code> and restart NEXUS"]);
      const ok = !failed(wl) && wl.mode === "live";
      const w = ok ? wl.data : { deployments: [], pods: [], services: [] };
      deployments = w.deployments;
      $("#k8s-deployments").innerHTML = !w.deployments.length ? empty(ok ? "No deployments" : "Not connected", ok ? "Nothing is deployed in this namespace." : (wl && wl.error) || "") :
        `<table><thead><tr><th>Name</th><th>Image</th><th>Ready</th><th>Revision</th><th>Status</th><th></th></tr></thead><tbody>${w.deployments.map((d) =>
          `<tr><td><strong>${esc(d.name)}</strong></td><td class="mono">${esc((d.images[0] || "").split("/").pop())}</td><td>${d.ready}/${d.desired}</td><td>#${d.revision}</td><td>${badge(d.healthy ? "success" : "failed", d.healthy ? "Healthy" : "Unhealthy")}</td><td><button class="btn btn--ghost btn--sm" data-pick="${esc(d.name)}">Revisions</button></td></tr>`).join("")}</tbody></table>`;
      $("#k8s-pods").innerHTML = !w.pods.length ? empty("No pods", "") : `<table><thead><tr><th>Pod</th><th>Phase</th><th>Ready</th><th class="num">Restarts</th></tr></thead><tbody>${w.pods.map((p) => `<tr><td class="mono">${esc(p.name)}</td><td>${badge(p.phase === "Running" ? "success" : p.phase === "Pending" ? "queued" : "failed", p.phase)}</td><td>${p.ready ? "Yes" : "No"}</td><td class="num">${p.restarts}</td></tr>`).join("")}</tbody></table>`;
      $("#k8s-services").innerHTML = !w.services.length ? empty("No services", "") : `<table><thead><tr><th>Service</th><th>Type</th><th>Ports</th></tr></thead><tbody>${w.services.map((s) => `<tr><td><strong>${esc(s.name)}</strong></td><td>${esc(s.type)}</td><td class="mono">${s.ports.map((p) => esc(p.port + (p.nodePort ? ":" + p.nodePort : ""))).join(", ")}</td></tr>`).join("")}</tbody></table>`;
      const sel = $("#rollback-select");
      sel.innerHTML = w.deployments.map((d) => `<option value="${esc(d.name)}">${esc(d.name)}</option>`).join("");
      if (!selected || !w.deployments.some((d) => d.name === selected)) selected = (w.deployments.find((d) => d.managedByNexus) || w.deployments[0] || {}).name || null;
      if (selected) sel.value = selected;
      sel.disabled = !selected;
      await loadRevisions();
    }

    async function rollback(btn) {
      const rev = Number(btn.dataset.rev), image = btn.dataset.image;
      const yes = await confirmDialog({ title: `Roll back ${selected} to revision #${rev}?`, danger: true, okLabel: "Roll back",
        bodyHtml: `<p>The deployment will be switched back to <span class="mono">${esc(image.split("/").pop())}</span>. NEXUS then waits for the rollout and checks the real pod state. It reports success only if the pods are Ready on that image.</p>` });
      if (!yes) return;
      try {
        const r = await runButton(btn, () => API.post(`/api/k8s/deployments/${encodeURIComponent(selected)}/rollback`, { revision: rev }), { loading: "Starting…" });
        runPanel = createRunPanel($("#rollback-run"), { title: `Rollback · ${selected} → #${rev}` });
        const final = await runPanel.follow(r.data.jobId);
        toast(final && final.status === "success" ? "Rollback verified against the cluster." : `Rollback failed: ${(final && final.error) || "see log"}`, final && final.status === "success" ? "success" : "error");
        load();
      } catch (err) { toast(err.message, "error"); }
    }

    register("kubernetes", {
      title: "Kubernetes",
      async enter() {
        await load();
        const v = $('.view[data-view="kubernetes"]');
        if (!v.dataset.bound) {
          v.dataset.bound = "1";
          $("#k8s-refresh").addEventListener("click", (e) => runButton(e.currentTarget, load, { success: "Updated" }));
          $("#rollback-select").addEventListener("change", (e) => { selected = e.target.value; loadRevisions(); });
          $("#k8s-deployments").addEventListener("click", (e) => { const b = e.target.closest("[data-pick]"); if (b) { selected = b.dataset.pick; $("#rollback-select").value = selected; loadRevisions(); $("#rollback-card").scrollIntoView({ behavior: N.reduceMotion() ? "auto" : "smooth" }); } });
          $("#k8s-revisions").addEventListener("click", (e) => { const b = e.target.closest("[data-rev]"); if (b && !b.disabled) rollback(b); });
        }
      },
    });
  })();

  // ======================================================= SECURITY
  (function securityView() {
    let scans = [], findingsScan = null;
    async function load() {
      const r = await safe(API.get("/api/security"));
      if (failed(r) || !r.data) { $("#sec-banner").innerHTML = authBanner(r) || notConnected("Security data unavailable", failed(r) ? errText(r) : r.error); $("#sec-totals").innerHTML = $("#sec-gate").innerHTML = $("#sec-scans").innerHTML = ""; return; }
      const d = r.data;
      $("#sec-source").innerHTML = sourceTag(r);
      $("#sec-banner").innerHTML = !d.trivy.installed ? notConnected("Trivy is not installed", "Scan stages are marked “skipped” and no vulnerability data is shown.", [
        "Ubuntu: <code>ansible-playbook site.yml --tags trivy</code>", "Windows: <code>winget install AquaSecurity.Trivy</code> (or Scoop/Chocolatey), then restart NEXUS"]) : "";
      const max = Math.max(1, ...SEV.map((s) => d.totals[s]));
      $("#sec-totals").innerHTML = d.scanCount === 0 ? empty("No scan results yet", "Run a release with Trivy installed.") :
        SEV.map((s) => `<div class="sev-row"><span>${s[0] + s.slice(1).toLowerCase()}</span><div class="bar"><span style="--w:${(d.totals[s] / max) * 100}%;--c:${sevTone[s]}"></span></div><strong class="mono">${d.totals[s]}</strong></div>`).join("");
      $("#sec-gate").innerHTML = dl([["Fail pipeline at", d.gate.failOn === "NONE" ? "Never (report only)" : `${esc(d.gate.failOn)} or higher`], ["Ignore unfixed", d.gate.ignoreUnfixed ? "Yes" : "No"], ["Trivy", d.trivy.installed ? esc(d.trivy.version || "installed") : badge("not_installed")], ["Scans recorded", String(d.scanCount)]])
        + `<p class="note">Change with <code>TRIVY_FAIL_ON_SEVERITY</code> (CRITICAL, HIGH, MEDIUM, LOW, NONE) in <code>.env</code>.</p>`;
      scans = d.scans;
      $("#sec-scans").innerHTML = !scans.length ? empty("No scans yet", "") : `<table><thead><tr><th>Scanned</th><th>App</th><th>Type</th><th>Target</th><th>Status</th><th>C / H / M / L / U</th><th>Gate</th><th></th></tr></thead><tbody>${scans.map((s, i) =>
        `<tr><td>${esc(new Date(s.scannedAt).toLocaleString())}</td><td><strong>${esc(s.app)}</strong></td><td>${esc(s.scanType)}</td><td class="mono">${esc((s.image || s.target || "").split("/").pop())}</td><td>${badge(s.status)}</td>
        <td class="mono">${s.status === "completed" ? SEV.map((k) => s.counts[k]).join(" / ") : "—"}</td><td>${s.gate ? badge(s.gate.passed ? "success" : "failed", s.gate.passed ? "Passed" : "Failed") : "—"}</td>
        <td>${s.status === "completed" ? `<button class="btn btn--ghost btn--sm" data-i="${i}">Findings</button>` : `<span class="hint">${esc((s.error || "").slice(0, 60))}</span>`}</td></tr>`).join("")}</tbody></table>`;
    }
    async function showFindings(i) {
      const s = scans[i]; $("#sec-findings-card").hidden = false;
      setText("#sec-findings-title", `Findings · ${s.app} (${s.scanType})`);
      $("#sec-findings").innerHTML = `<div style="padding:16px">${skeleton(4)}</div>`;
      const r = await safe(API.get(`/api/security/scans/${encodeURIComponent(s.jobId)}`));
      if (failed(r)) { $("#sec-findings").innerHTML = empty("Unavailable", errText(r)); return; }
      const full = r.data.scans.find((x) => x.scanType === s.scanType && x.scannedAt === s.scannedAt) || {};
      findingsScan = full;
      const f = full.findings || [];
      $("#sec-download").hidden = !full.reportFile;
      $("#sec-findings").innerHTML = !f.length ? empty("No findings", "Trivy reported nothing for this scan.") : `<table><thead><tr><th>Severity</th><th>ID</th><th>Package</th><th>Installed</th><th>Fixed in</th><th>Title</th></tr></thead><tbody>${f.slice(0, 200).map((x) =>
        `<tr><td>${sevBadge(x.severity)}</td><td class="mono">${esc(x.id)}</td><td class="mono">${esc(x.package || x.kind)}</td><td class="mono">${esc(x.installed || "—")}</td><td class="mono">${esc(x.fixed || "—")}</td><td>${esc(x.title)}</td></tr>`).join("")}</tbody></table>`;
      $("#sec-findings-card").scrollIntoView({ behavior: N.reduceMotion() ? "auto" : "smooth" });
    }
    register("security", {
      title: "Security",
      async enter() {
        await load();
        const v = $('.view[data-view="security"]');
        if (!v.dataset.bound) {
          v.dataset.bound = "1";
          $("#sec-refresh").addEventListener("click", (e) => runButton(e.currentTarget, load, { success: "Updated" }));
          $("#sec-scans").addEventListener("click", (e) => { const b = e.target.closest("[data-i]"); if (b) showFindings(Number(b.dataset.i)); });
          $("#sec-download").addEventListener("click", (e) => { if (findingsScan && findingsScan.reportFile) runButton(e.currentTarget, () => API.downloadJson(`/api/security/reports/${encodeURIComponent(findingsScan.reportFile)}`, findingsScan.reportFile), { success: "Saved" }).catch((er) => toast(er.message, "error")); });
        }
      },
    });
  })();

  // ======================================================= MONITORING
  (function monitoringView() {
    function chart(series) {
      const pts = series.points;
      if (!pts || pts.length < 2) return `<article class="card chart-card"><h3>${esc(series.label)}</h3>${empty("No data", "Prometheus returned no points.")}</article>`;
      const W = 320, H = 120, pad = 4;
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      const x0 = Math.min(...xs), x1 = Math.max(...xs), y1 = Math.max(...ys, 0.0001), y0 = 0;
      const X = (t) => pad + ((t - x0) / (x1 - x0 || 1)) * (W - 2 * pad), Y = (v) => H - pad - ((v - y0) / (y1 - y0 || 1)) * (H - 2 * pad - 8);
      const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ");
      const last = ys[ys.length - 1];
      return `<article class="card chart-card"><h3>${esc(series.label)}<small>last 30 min</small></h3><div class="chart-latest">${last.toFixed(2)} ${esc(series.unit)}</div>
        <svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(series.label)} over the last 30 minutes">
        <defs><linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1479D4" stop-opacity=".25"/><stop offset="1" stop-color="#1479D4" stop-opacity="0"/></linearGradient></defs>
        <line class="grid-line" x1="0" x2="${W}" y1="${H - pad}" y2="${H - pad}"/><line class="grid-line" x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}"/>
        <path class="area" d="${d} L${X(x1)},${H - pad} L${X(x0)},${H - pad} Z"/><path class="line" pathLength="1" d="${d}"/></svg></article>`;
    }
    async function load() {
      const [obs, ser] = await Promise.all([safe(API.get("/api/observability")), safe(API.get("/api/observability/series"))]);
      if (failed(obs)) { $("#mon-banner").innerHTML = authBanner(obs) || notConnected("Monitoring unavailable", errText(obs)); return; }
      $("#mon-source").innerHTML = sourceTag(obs);
      const live = obs.mode === "live";
      $("#mon-banner").innerHTML = live ? "" : notConnected("Prometheus is not connected", obs.error || "", [
        "Start the stack: <code>cd monitoring && cp .env.example .env && docker compose up -d</code>", "Set <code>PROMETHEUS_URL=http://localhost:9090</code> in <code>.env</code> and restart NEXUS"]);
      $("#mon-panels").innerHTML = live ? obs.data.panels.map((p) => stat(esc(p.label), p.value == null ? "No data" : esc(p.value), p.status === "LIVE" ? "from Prometheus" : "query returned no data", p.value == null)).join("") : "";
      const g = N.safeUrl(live ? obs.data.grafanaUrl : null); const gl = $("#grafana-link"); gl.hidden = !g; if (g) gl.href = g;
      $("#mon-charts").innerHTML = !failed(ser) && ser.mode === "live" ? ser.data.map(chart).join("") : "";
      const t = live ? obs.data.targets : null;
      $("#mon-targets").innerHTML = !t ? empty("Not connected", "") : !t.length ? empty("No targets", "") : `<table><thead><tr><th>Job</th><th>Instance</th><th>Health</th><th>Last scrape</th><th>Error</th></tr></thead><tbody>${t.map((x) =>
        `<tr><td><strong>${esc(x.job)}</strong></td><td class="mono">${esc(x.instance)}</td><td>${badge(x.health === "up" ? "success" : "failed", x.health)}</td><td>${esc(x.lastScrape ? new Date(x.lastScrape).toLocaleTimeString() : "—")}</td><td class="hint">${esc(x.lastError)}</td></tr>`).join("")}</tbody></table>`;
    }
    register("monitoring", {
      title: "Monitoring",
      async enter() {
        await load();
        const b = $("#mon-refresh"); if (!b.dataset.bound) { b.dataset.bound = "1"; b.addEventListener("click", (e) => runButton(e.currentTarget, load, { success: "Updated" })); }
      },
    });
  })();

  // ======================================================= INFRASTRUCTURE
  register("infrastructure", {
    title: "Infrastructure",
    async enter() {
      const load = async () => {
        const r = await safe(API.get("/api/infrastructure", 30000));
        if (failed(r)) { $("#infra-host").innerHTML = authBanner(r) || empty("Unavailable", errText(r)); return; }
        const d = r.data; $("#infra-source").innerHTML = sourceTag(r);
        $("#infra-host").innerHTML = dl([["Operating system", esc(d.host.os)], ["CPU cores", String(d.host.cpuCount || "—")], ["Memory", d.host.memoryGb ? `${d.host.memoryGb} GB` : "—"], ["Python", esc(d.host.python)], ["AWS region", esc(d.region)]]);
        const disks = Object.entries(d.host.disks);
        $("#infra-disk").innerHTML = !disks.length ? empty("Unavailable", "") : disks.map(([k, v]) => `<div style="margin-bottom:14px"><div class="mini-row"><strong>${esc(k)}</strong><span class="mono">${v.freeGb} GB free of ${v.totalGb} GB</span></div><div class="bar" style="margin-top:6px"><span style="--w:${v.usedPct}%;--c:${v.usedPct > 85 ? "#D94743" : v.usedPct > 70 ? "#D9A12B" : "#1479D4"}"></span></div><span class="hint mono">${esc(v.path)}</span></div>`).join("")
          + `<p class="note">Docker images fill a 25 GB disk quickly. Run <code>docker system prune -af --filter until=72h</code> when usage passes 80%.</p>`;
        $("#infra-aws").innerHTML = d.aws ? (!d.aws.instances.length ? empty("No tagged instances", "Instances tagged Project=nexus appear here.") : `<table><thead><tr><th>Name</th><th>ID</th><th>Type</th><th>State</th><th>Public IP</th></tr></thead><tbody>${d.aws.instances.map((i) => `<tr><td><strong>${esc(i.name || "—")}</strong></td><td class="mono">${esc(i.id)}</td><td>${esc(i.type)}</td><td>${badge(i.state === "running" ? "success" : "queued", i.state)}</td><td class="mono">${esc(i.publicIp || "—")}</td></tr>`).join("")}</tbody></table>`) : notConnected("AWS is not connected", d.awsError || "");
      };
      await load();
      const b = $("#infra-refresh"); if (!b.dataset.bound) { b.dataset.bound = "1"; b.addEventListener("click", (e) => runButton(e.currentTarget, load, { success: "Updated" })); }
    },
  });

  // ======================================================= GITOPS
  register("gitops", {
    title: "GitOps / Argo CD",
    async enter() {
      const load = async () => {
        const r = await safe(API.get("/api/gitops"));
        if (failed(r)) { $("#gitops-body").innerHTML = authBanner(r) || empty("Unavailable", errText(r)); return; }
        $("#gitops-source").innerHTML = sourceTag(r);
        if (r.mode !== "live") { $("#gitops-body").innerHTML = notConnected("Argo CD is not configured (optional)", r.error || "NEXUS deploys directly or to a local preview without it.", [
          "Install Argo CD: see <code>argocd/README.md</code>", "Set <code>ARGOCD_URL</code>, <code>ARGOCD_TOKEN</code>, <code>GIT_REPO_URL</code>, <code>GIT_TOKEN</code> in <code>.env</code>", "Argo CD stays <strong>unverified</strong> until an application shows Synced + Healthy here"]); return; }
        $("#gitops-body").innerHTML = !r.data.length ? empty("No applications", "Argo CD is connected but has no applications yet.") : `<div class="grid grid--2">${r.data.map((a) => `<article class="card"><div class="card-head"><h3>${esc(a.app)}</h3><span>${badge(a.sync === "SYNCED" ? "success" : "queued", a.sync)} ${badge(a.health === "HEALTHY" ? "success" : a.health === "DEGRADED" ? "failed" : "queued", a.health)}</span></div>
          ${dl([["Repository", `<span class="mono">${esc(a.repo || "—")}</span>`], ["Path", `<span class="mono">${esc(a.path || "—")}</span>`], ["Revision", `<span class="mono">${esc((a.live || a.targetRevision || "—").slice(0, 10))}</span>`], ["Last sync", esc(a.lastSync ? new Date(a.lastSync).toLocaleString() : "—")]])}</article>`).join("")}</div>`;
      };
      await load();
      const b = $("#gitops-refresh"); if (!b.dataset.bound) { b.dataset.bound = "1"; b.addEventListener("click", (e) => runButton(e.currentTarget, load, { success: "Updated" })); }
    },
  });

  // ======================================================= SETTINGS
  register("settings", {
    title: "Settings",
    async enter() {
      const loadDocker = async () => {
        $("#docker-panel").innerHTML = skeleton(2);
        const r = await safe(API.get("/api/docker/status", 30000));
        if (failed(r)) { $("#docker-panel").innerHTML = authBanner(r) || empty("Unavailable", errText(r)); return; }
        const d = r.data;
        $("#docker-panel").innerHTML = d.connected ? dl([["Status", `${badge("connected")}`], ["Engine", `Docker ${esc(d.serverVersion || "")} (API ${esc(d.apiVersion || "")})`], ["Endpoint", `<span class="mono">${esc(d.host)}</span>`], ["Found via", esc(d.source)], ["Platform", esc(d.platform)]]) :
          `<div class="banner banner--error"><div><strong>${esc(({ desktop_not_running: "Docker Desktop is not running", not_installed: "Docker is not installed", engine_unavailable: "Docker Engine is unavailable", bad_connection: "Incorrect Docker connection", permission_denied: "Docker permission problem", sdk_missing: "Docker Python package missing" })[d.code] || "Docker is not reachable")}</strong>${esc(d.message)}<ul>${d.hints.map((h) => `<li>${esc(h)}</li>`).join("")}</ul></div></div>
          <details style="margin-top:12px"><summary class="hint" style="cursor:pointer">Endpoints NEXUS tried</summary>${d.candidates.length ? `<div class="table-scroll"><table><thead><tr><th>Source</th><th>Endpoint</th><th>Exists</th></tr></thead><tbody>${d.candidates.map((c) => `<tr><td>${esc(c.label)}</td><td class="mono">${esc(c.host)}</td><td>${c.endpointExists === true ? "Yes" : c.endpointExists === false ? "No" : "Unknown"}</td></tr>`).join("")}</tbody></table></div>` : "<p class='note'>None.</p>"}</details>`;
      };
      const loadAll = async () => {
        const [integ, cfg] = await Promise.all([safe(API.get("/api/integrations", 30000)), safe(API.get("/api/config"))]);
        if (failed(integ)) { $("#integration-list").innerHTML = authBanner(integ) || empty("Unavailable", errText(integ)); }
        else {
          $("#settings-source").innerHTML = sourceTag(integ);
          $("#integration-list").innerHTML = integ.data.items.map((i) => `<div class="int-row">${dot({ connected: "green", installed: "green", available: "green", configured: "blue" }[i.status] || (i.status === "not_connected" ? "red" : "gray"))}<div class="int-main"><strong>${esc(i.name)}</strong><span>${esc(i.detail)}</span></div>${badge(i.status)}</div>`).join("");
        }
        if (!failed(cfg)) { const c = cfg.data; $("#config-panel").innerHTML = dl([["Version", esc(c.version)], ["Access key required", c.authRequired ? "Yes" : '<span class="badge badge--amber">No — set NEXUS_API_KEY before exposing NEXUS</span>'], ["Max upload", `${c.limits.maxUploadMb} MB (expands to ${c.limits.maxUncompressedMb} MB, ${c.limits.maxFiles} files, ratio ${c.limits.maxCompressionRatio}:1)`],
          ["npm lifecycle scripts", c.build.npmIgnoreScripts ? "Ignored (safer)" : "Allowed"], ["Run project tests", c.build.runTests ? "Yes" : "No"], ["Concurrent builds", String(c.build.maxConcurrentJobs)], ["Trivy gate", c.security.failOn === "NONE" ? "Report only" : `Fail at ${esc(c.security.failOn)}+`], ["Kubernetes namespace", esc(c.kubernetes.namespace)], ["Image source", esc(c.kubernetes.imageSource)], ["CORS origins", c.corsOrigins.length ? esc(c.corsOrigins.join(", ")) : "Same-origin only"]]); }
      };
      await Promise.all([loadDocker(), loadAll()]);
      const b = $("#docker-refresh");
      if (!b.dataset.bound) { b.dataset.bound = "1"; b.addEventListener("click", (e) => runButton(e.currentTarget, async () => { await loadDocker(); N.refreshHeaderStatus(); }, { loading: "Checking…", success: "Checked" })); }
    },
  });
})();
