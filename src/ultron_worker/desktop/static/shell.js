// Ultron desktop shell: native controls around the runner's own status GUI (the iframe).
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const TOKEN = document.querySelector('meta[name="ultron-token"]').content;
  const POLL_MS = 2000;
  let last = null, offerOpen = false, lastLog = "";

  const api = async (action, body = {}) => {
    const r = await fetch(`/__ultron/api/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-Ultron-Token": TOKEN },
      body: JSON.stringify(body),
    });
    const out = await r.json().catch(() => ({ ok: false, error: `HTTP ${r.status}` }));
    if (!out.ok && out.error) flash("bad", out.error);
    setTimeout(poll, 150);
    return out;
  };
  const flash = (kind, text) => { $("flash").dataset.kind = kind; $("flash-text").textContent = text; $("flash").hidden = false; };
  const light = (name, state, label, detail) => {
    const el = $(`light-${name}`);
    el.dataset.state = state; el.querySelector("span").textContent = label; el.title = detail || label;
  };
  const ago = (iso) => {
    if (!iso) return "never";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };
  const commit = (c) => c ? [c.short, c.subject] : ["—", ""];

  function renderRunner(r) {
    const map = { running: "ok", stopping: "busy", crashed: "bad", failed: "bad", stopped: "bad" };
    let label = r.state, detail = r.pid ? `pid ${r.pid}, started ${ago(r.started_at)}` : "";
    if (r.external) { label = "outside app"; detail = "a runner started outside the app is running"; }
    else if (r.state === "running" && r.activity) label = r.activity.replace(/-/g, " ");
    else if (r.state === "crashed") label = `crashed (exit ${r.exit_code}), retry ${r.crashes}/${r.max_restarts} in ${r.restart_in}s`;
    else if (r.state === "failed") { label = "will not start - see log"; detail = `exited with code ${r.exit_code} ${r.crashes + 1} times in a row; press Start to try again`; }
    else if (r.state === "stopped" && r.exit_code != null) detail = `exited with code ${r.exit_code} ${ago(r.exited_at)}`;
    light("runner", r.external ? "warn" : r.state === "running" && r.activity === "working" ? "ok" : map[r.state] || "unknown", label, detail);
    const up = r.state === "running" || r.state === "stopping";
    $("btn-start").disabled = up || r.external;
    $("btn-stop").disabled = !up;
    $("btn-restart").disabled = !up;
    $("external").hidden = !r.external;
  }

  function renderFailed(s) {
    const r = s.runner, failed = r.state === "failed";
    $("runner-failed").hidden = !failed;
    if (!failed) return;
    const out = s.output || [];
    $("runner-failed-code").textContent = r.exit_code == null ? "without starting" : `with code ${r.exit_code}`;
    $("runner-failed-tries").textContent = String(r.crashes + 1);
    $("runner-failed-why").textContent = out.length ? out[out.length - 1] : "it wrote nothing.";
  }

  function renderHealth(s) {
    const h = s.health, na = !!(h && h.unavailable);
    // The check itself couldn't run: one "check unavailable" light, not three component failures.
    $("light-check").hidden = !na;
    if (na) light("check", "warn", "unavailable", `${h.unavailable}\n(--check ${ago(h.checked_at)})`);
    for (const name of ["backbone", "queue", "ollama"]) {
      $(`light-${name}`).hidden = na;
      if (na) continue;
      if (s.checking && !h) { light(name, "busy", "checking…"); continue; }
      if (!h) { light(name, "unknown", "not checked"); continue; }
      const l = h.lights[name];
      light(name, l.state, l.label, `${l.detail || l.label}\n(--check ${ago(h.checked_at)})`);
    }
    $("btn-check").disabled = s.checking;
    $("btn-check").textContent = s.checking ? "Checking…" : "Re-check";
  }

  function renderUpdate(s) {
    const u = s.update, job = s.job;
    const active = job && !["failed", "cancelled"].includes(job.phase);
    $("update-panel").hidden = !(u && u.can_update) || !!active;
    if (u && u.can_update) {
      const [cs, csub] = commit(u.current), [ns, nsub] = commit(u.available);
      $("cur-sha").textContent = cs; $("cur-subject").textContent = csub || (u.error || "");
      $("new-sha").textContent = ns; $("new-subject").textContent = nsub;
      $("update-count").textContent = u.behind ? `· ${u.behind} commit${u.behind === 1 ? "" : "s"}` : "";
    }
    if (!offerOpen) $("busy-confirm").hidden = true;
    const st = $("update-state");
    if (s.checking_update) st.textContent = "checking for updates…";
    else if (!u) st.textContent = "";
    else if (u.state === "equal") st.textContent = `up to date with origin/main · checked ${ago(u.checked_at)}`;
    else if (u.state === "behind") st.textContent = `update available · checked ${ago(u.checked_at)}`;
    else if (u.state === "ahead") st.textContent = `installed is ${u.ahead} commit(s) ahead of origin/main`;
    else if (u.state === "diverged") st.textContent = "installed version has commits not on origin/main";
    else if (u.state === "unknown") st.textContent = u.error || "installed version unknown";
    else st.textContent = `update check failed: ${u.error}`;
    $("btn-update-check").disabled = s.checking_update || !!active;

    $("job-panel").hidden = !job;
    if (job) {
      $("job-sha").textContent = job.target ? job.target.short : "";
      const phase = {
        starting: "Starting…", waiting: `Waiting for the current job to finish: ${job.waiting_for || ""}`,
        building: "Building the new version beside the current one (pull, venv, pip install, --check)…",
        switching: "Check passed. Stopping the runner and restarting Ultron on the new version…",
        done: "Restarting…", failed: `Update failed: ${job.error}. The previous install is still running.`,
        cancelled: "Update cancelled. Nothing changed.",
      }[job.phase] || job.phase;
      $("job-phase").textContent = phase;
      $("btn-job-now").hidden = job.phase !== "waiting";
      $("btn-job-cancel").hidden = job.phase !== "waiting";
      $("btn-job-dismiss").hidden = !["failed", "cancelled"].includes(job.phase);
      const text = job.log.join("\n");
      if ($("job-log").textContent !== text) { $("job-log").textContent = text; $("job-log").scrollTop = 1e9; }
    }
  }

  function renderLog(s) {
    $("log-path").textContent = s.log_path;
    // The runner's own stdout/stderr (startup errors, tracebacks) after its log file's tail.
    const out = (s.output || []).map((ln) => `[runner output] ${ln}`);
    const lines = [...s.log, ...out];
    const text = lines.join("\n");
    if (text === lastLog) return;
    lastLog = text;
    const pre = $("log");
    pre.replaceChildren(...lines.map((ln, i) => {
      const d = document.createElement("div");
      d.textContent = ln;
      if (i >= s.log.length) d.className = "out";
      else if (/ (ERROR|CRITICAL) /.test(ln)) d.className = "err";
      else if (/ WARNING /.test(ln)) d.className = "warn";
      return d;
    }));
    if ($("follow").checked) pre.scrollTop = pre.scrollHeight;
  }

  function render(s) {
    last = s;
    renderRunner(s.runner);
    renderFailed(s);
    renderHealth(s);
    renderUpdate(s);
    renderLog(s);
    if (s.flash) flash(s.flash.kind, s.flash.text);
    const a = $("autostart");
    a.disabled = !s.autostart.supported;
    if (document.activeElement !== a) a.checked = s.autostart.enabled;
    $("installed").textContent = s.installed.sha ? `installed ${s.installed.sha.slice(0, 7)}` : "installed version unknown";
  }

  async function poll() {
    try {
      const r = await fetch("/__ultron/api/status", { headers: { "X-Ultron-Token": TOKEN }, cache: "no-store" });
      if (r.ok) render(await r.json());
    } catch (e) { /* app restarting */ }
  }

  $("btn-start").onclick = () => api("start");
  $("btn-stop").onclick = () => api("stop");
  $("btn-restart").onclick = () => api("restart");
  $("btn-check").onclick = () => api("check");
  $("btn-update-check").onclick = () => api("update-check");
  $("btn-update").onclick = () => {
    const cur = last && last.runner.current;
    if (cur || (last && last.runner.state === "running" && !last.runner.activity)) {
      $("busy-what").textContent = cur ? `${cur.title || cur.task_id} (${cur.percent}% done)` : "unknown (status page not answering)";
      offerOpen = true; $("busy-confirm").hidden = false;
    } else api("update", { force: false });
  };
  $("btn-update-idle").onclick = () => { offerOpen = false; api("update", { force: false }); };
  $("btn-update-now").onclick = () => { offerOpen = false; api("update", { force: true }); };
  $("btn-update-back").onclick = () => { offerOpen = false; $("busy-confirm").hidden = true; };
  $("btn-job-now").onclick = () => api("update", { force: true });
  $("btn-job-cancel").onclick = () => api("update-cancel");
  $("btn-job-dismiss").onclick = () => api("dismiss");
  $("btn-dismiss").onclick = () => { $("flash").hidden = true; api("dismiss"); };
  $("autostart").onchange = (e) => api("autostart", { enabled: e.target.checked });
  $("btn-log-toggle").onclick = () => {
    const p = $("logpane"); p.classList.toggle("collapsed");
    $("btn-log-toggle").textContent = p.classList.contains("collapsed") ? "Show" : "Hide";
  };
  poll();
  setInterval(poll, POLL_MS);
})();
