// Ultron status GUI: polls the runner's own /api/state (localhost) and renders it.
// Display only: this page sends nothing but GETs; closing it never affects a job.
"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const POLL_MS = 1500;
  const ARC = 2 * Math.PI * 52;

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const dur = (s) => {
    s = Math.max(0, Math.round(Number(s) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(sec).padStart(2, "0")}s` : `${sec}s`;
  };
  const when = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso), ago = (Date.now() - d.getTime()) / 1000;
    const rel = ago < 60 ? "just now" : ago < 3600 ? `${Math.round(ago / 60)} min ago`
      : ago < 86400 ? `${Math.round(ago / 3600)} h ago` : `${Math.round(ago / 86400)} d ago`;
    return `${d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })} · ${rel}`;
  };
  const pill = (el, state, label) => { el.dataset.state = state; el.querySelector("span").textContent = label; };
  const show = (el, on) => { el.hidden = !on; };

  let lastOk = 0;
  let energy = 0.2; // graph animation intensity: higher while working

  function render(s) {
    const r = s.runner, c = s.current;
    $("host").textContent = `${r.host} · ${r.actor} · v${r.version}${r.commit ? ` · ${r.commit}` : ""}`;
    $("activity-word").textContent = r.activity.replace(/-/g, " ");
    pill($("pill-activity"), r.activity, r.activity);
    pill($("pill-conn"), r.connection, r.connection === "online" ? "backbone online" : `backbone ${r.connection}`);
    energy = r.activity === "working" ? 1 : r.activity === "idle" ? 0.35 : 0.12;
    const bits = [];
    if (r.connection !== "online" && r.connection_detail) bits.push(r.connection_detail);
    if (r.slept) bits.push(`woke ${when(r.slept.woke_at)} after ~${dur(r.slept.seconds)} asleep`);
    bits.push(`last poll ${when(r.last_poll_at)}`, `heartbeat ${when(r.last_heartbeat_at)}`);
    $("runner-detail").textContent = bits.join(" · ");

    show($("current-empty"), !c); show($("current-body"), !!c);
    document.querySelector(".panel.current").classList.toggle("live", !!c);
    if (c) {
      const pct = Number(c.percent) || 0;
      $("cur-title").textContent = c.title;
      $("cur-id").textContent = c.task_id;
      $("cur-kind").textContent = c.kind;
      $("cur-model").textContent = c.model;
      $("cur-pct").textContent = `${pct.toFixed(pct >= 10 ? 0 : 1)}%`;
      $("cur-arc").style.strokeDashoffset = String(ARC * (1 - pct / 100));
      $("cur-fill").style.width = `${pct}%`;
      $("cur-items").textContent = `${c.done} / ${c.total}`;
      $("cur-elapsed").textContent = dur(c.elapsed_s);
      $("cur-budget").textContent = c.budget_s ? dur(c.budget_s) : "—";
      $("cur-attempt").textContent = c.resumed_from ? `${c.attempt} · resumed @${c.resumed_from}` : String(c.attempt);
      $("cur-item").textContent = c.current_item || "starting…";
      $("cur-output").textContent = c.output || "—";
    }

    $("queue-count").textContent = s.queue.length;
    show($("queue-empty"), !s.queue.length);
    $("queue").innerHTML = s.queue.map((q, i) => `<li><span class="idx">${String(i + 1).padStart(2, "0")}</span>
      <span class="t">${esc(q.title)}<span class="sub">${esc(q.task_id)} · ${esc(q.kind)} · ${esc(q.model)}${q.filed_at ? " · filed " + esc(when(q.filed_at)) : ""}</span></span>
      <span class="tag">${q.waiting === "new" ? "waiting" : esc(q.waiting) + (q.items_done ? " @" + q.items_done : "")}</span></li>`).join("");

    $("attn-count").textContent = s.attention.length;
    show($("attention-empty"), !s.attention.length);
    $("attention").innerHTML = s.attention.map((a) => `<li><span class="t">${esc(a.title)}
      <span class="sub">${esc(a.task_id)} · ${esc(a.kind)} · attempt ${esc(a.attempts)} · ${esc(when(a.at))}</span>
      <span class="err">${esc(a.error)}</span></span>
      <span class="tag">${a.status === "retry_wait" ? "retrying" : "needs david"}</span></li>`).join("");

    show($("recent-empty"), !s.recent.length);
    $("recent").innerHTML = s.recent.map((j) => `<tr>
      <td>${esc(j.title)}<span class="sub">${esc(j.task_id)} · ${esc(j.kind)} · ${esc(j.model)}</span></td>
      <td>${esc(when(j.finished_at))}</td><td class="mono">${esc(dur(j.duration_s))}</td>
      <td>${esc(j.summary)}${j.reported ? "" : '<span class="sub">reporting to backbone…</span>'}</td>
      <td class="o">${(j.outputs || []).map(esc).join("<br>") || "—"}</td></tr>`).join("");
    $("foot-poll").textContent = `updated ${new Date().toLocaleTimeString()}`;
  }

  async function poll() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      if (!res.ok) throw new Error(String(res.status));
      render(await res.json());
      lastOk = Date.now();
    } catch {
      if (Date.now() - lastOk > 5000) {
        pill($("pill-activity"), "gui-lost", "runner not answering");
        $("runner-detail").textContent = "The page can't reach the runner on localhost. Is ultron-runner running?";
        energy = 0.08;
      }
    } finally {
      setTimeout(poll, POLL_MS);
    }
  }

  // Red neural-graph backdrop: drifting nodes, links by proximity, pulses travelling the links.
  const cv = $("graph"), cx = cv.getContext("2d");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let W = 0, H = 0, nodes = [], pulses = [];
  function resize() {
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    W = cv.clientWidth; H = cv.clientHeight;
    cv.width = W * dpr; cv.height = H * dpr; cx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const n = Math.round(Math.min(90, (W * H) / 16000));
    nodes = Array.from({ length: n }, () => ({ x: Math.random() * W, y: Math.random() * H,
      vx: (Math.random() - 0.5) * 0.25, vy: (Math.random() - 0.5) * 0.25, r: 1 + Math.random() * 1.8 }));
  }
  function frame() {
    cx.clearRect(0, 0, W, H);
    const reach = 150, speed = 0.4 + energy;
    for (const p of nodes) {
      p.x += p.vx * speed; p.y += p.vy * speed;
      if (p.x < 0 || p.x > W) p.vx *= -1;
      if (p.y < 0 || p.y > H) p.vy *= -1;
    }
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j], d = Math.hypot(a.x - b.x, a.y - b.y);
        if (d < reach) {
          cx.strokeStyle = `rgba(255, 42, 61, ${(1 - d / reach) * (0.10 + 0.16 * energy)})`;
          cx.lineWidth = 0.7; cx.beginPath(); cx.moveTo(a.x, a.y); cx.lineTo(b.x, b.y); cx.stroke();
          if (Math.random() < 0.0009 * energy) pulses.push({ a, b, t: 0 });
        }
      }
    }
    for (const p of nodes) {
      cx.fillStyle = `rgba(255, 70, 85, ${0.35 + 0.4 * energy})`;
      cx.beginPath(); cx.arc(p.x, p.y, p.r, 0, Math.PI * 2); cx.fill();
    }
    pulses = pulses.filter((q) => (q.t += 0.02) < 1);
    for (const q of pulses) {
      const x = q.a.x + (q.b.x - q.a.x) * q.t, y = q.a.y + (q.b.y - q.a.y) * q.t;
      const g = cx.createRadialGradient(x, y, 0, x, y, 9);
      g.addColorStop(0, "rgba(255, 120, 130, 0.95)"); g.addColorStop(1, "rgba(255, 42, 61, 0)");
      cx.fillStyle = g; cx.beginPath(); cx.arc(x, y, 9, 0, Math.PI * 2); cx.fill();
    }
    if (!reduce) requestAnimationFrame(frame);
  }
  window.addEventListener("resize", resize);
  resize(); frame(); poll();
})();
