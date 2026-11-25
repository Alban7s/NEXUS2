/* NEXUS API client. Same-origin by default (the backend serves this page).
 * The access key (only needed when the server sets NEXUS_API_KEY) is kept in memory;
 * "remember for this tab" stores it in sessionStorage, which is cleared when the tab closes. */
(function () {
  "use strict";
  const STORE = "nexus.accessKey";
  let accessKey = "";
  try { accessKey = sessionStorage.getItem(STORE) || ""; } catch (_) { /* storage blocked */ }

  class ApiError extends Error {
    constructor(message, status, code) { super(message); this.status = status; this.code = code; }
  }

  function headers(extra) {
    const h = Object.assign({}, extra || {});
    if (accessKey) h["X-Nexus-Api-Key"] = accessKey;
    return h;
  }

  async function parse(res) {
    let body = null;
    try { body = await res.json(); } catch (_) { /* not JSON */ }
    if (!res.ok) {
      if (res.status === 401) window.dispatchEvent(new CustomEvent("nexus:unauthorized"));
      const msg = (body && (body.detail || body.error)) ||
        (res.status === 401 ? "Access key required." : `Request failed (HTTP ${res.status}).`);
      throw new ApiError(typeof msg === "string" ? msg : JSON.stringify(msg), res.status, body && body.code);
    }
    return body;
  }

  async function request(method, path, body, timeoutMs) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs || 20000);
    try {
      const opts = { method, headers: headers(), signal: ctrl.signal };
      if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
      return await parse(await fetch(path, opts));
    } catch (err) {
      if (err instanceof ApiError) throw err;
      if (err.name === "AbortError") throw new ApiError("The request timed out.", 0, "timeout");
      throw new ApiError("Cannot reach the NEXUS backend. Is it running?", 0, "network");
    } finally { clearTimeout(timer); }
  }

  /** Upload with progress (fetch cannot report upload progress). */
  function upload(path, formData, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      if (accessKey) xhr.setRequestHeader("X-Nexus-Api-Key", accessKey);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onerror = () => reject(new ApiError("Cannot reach the NEXUS backend. Is it running?", 0, "network"));
      xhr.ontimeout = () => reject(new ApiError("The upload timed out.", 0, "timeout"));
      xhr.onload = () => {
        let body = null;
        try { body = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(body);
        if (xhr.status === 401) window.dispatchEvent(new CustomEvent("nexus:unauthorized"));
        reject(new ApiError((body && body.detail) || `Upload failed (HTTP ${xhr.status}).`, xhr.status, body && body.code));
      };
      xhr.timeout = 10 * 60 * 1000;
      xhr.send(formData);
    });
  }

  /** Follow a job via server-sent events (fetch stream so the key header can be sent).
   *  Falls back to polling. Resolves with the final job object. */
  async function followJob(jobId, onUpdate, signal) {
    let last = null;
    try {
      const res = await fetch(`/api/deploy/jobs/${encodeURIComponent(jobId)}/stream`, { headers: headers(), signal });
      if (!res.ok || !res.body) throw new Error("stream unavailable");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (line) { last = JSON.parse(line.slice(6)); onUpdate(last); }
        }
      }
      if (last && (last.status === "success" || last.status === "failed")) return last;
    } catch (err) {
      if (signal && signal.aborted) return last;
    }
    // Polling fallback
    let since = last ? last.logCount : 0;
    for (let i = 0; i < 600; i++) {
      if (signal && signal.aborted) return last;
      const body = await request("GET", `/api/deploy/jobs/${encodeURIComponent(jobId)}?since=${since}`);
      last = body.data; since = last.logCount; onUpdate(Object.assign({}, last, { logs: [] }));
      if (last.status === "success" || last.status === "failed") return last;
      await new Promise((r) => setTimeout(r, 2000));
    }
    return last;
  }

  async function downloadJson(path, filename) {
    const res = await fetch(path, { headers: headers() });
    if (!res.ok) throw new ApiError("Report could not be downloaded.", res.status);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  window.NexusAPI = {
    ApiError,
    get: (p, t) => request("GET", p, undefined, t),
    post: (p, b, t) => request("POST", p, b === undefined ? {} : b, t),
    upload, followJob, downloadJson,
    hasKey: () => !!accessKey,
    setKey(key, remember) {
      accessKey = (key || "").trim();
      try { remember && accessKey ? sessionStorage.setItem(STORE, accessKey) : sessionStorage.removeItem(STORE); } catch (_) { /* ignore */ }
    },
  };
})();
