/* Fleet / spaceship visualisation.
 * Every project is a spaceship; every department is a room; every temporary worker
 * is a crewmate who flies in, walks to its department's console, works, shows the
 * result and leaves through the airlock. The Project Manager (captain hat) patrols
 * rooms that have open or waiting tasks. All motion is driven by real state from
 * /api/state; "Demo crew" adds clearly-labelled simulated traffic. */
"use strict";

(function () {
  const DEPT_COLORS = {
    research: "#38fedc", marketing: "#ed54ba", development: "#50ef39", content: "#f5f557",
    design: "#a36bff", data_analysis: "#3b6cf5", finance: "#1fae4d", automation: "#ef7d0d",
  };
  const EXTRA = ["#c51111", "#132ed1", "#71491e", "#d6e0f0", "#3f474e", "#6b2fbb"];
  const W = 1400, H = 720, CORR_Y = 360;
  const SPEED = 120;               // world units / second
  const OUTLINE = "#0a0a0f";

  const now = () => performance.now() / 1000;
  const hash = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) | 0; return Math.abs(h); };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  function shade(hex, amt) {
    const n = parseInt(hex.slice(1), 16);
    const f = (c) => clamp(Math.round(c + amt * 255), 0, 255);
    return `rgb(${f(n >> 16)},${f((n >> 8) & 255)},${f(n & 255)})`;
  }
  function rgba(hex, a) {
    const n = parseInt(hex.slice(1), 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function rr(ctx, x, y, w, h, r) { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); }
  function trunc(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  function wrap(ctx, text, maxW, maxLines) {
    const words = String(text).split(/\s+/); const lines = []; let line = "";
    for (const w of words) {
      const t = line ? line + " " + w : w;
      if (ctx.measureText(t).width > maxW && line) { lines.push(line); line = w; } else line = t;
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && line) lines.push(line);
    if (lines.length === maxLines && words.join(" ").length > lines.join(" ").length) lines[maxLines - 1] = trunc(lines[maxLines - 1], lines[maxLines - 1].length);
    return lines;
  }

  // ---------------------------------------------------------------- crewmate art
  function drawCrew(ctx, x, y, color, o) {
    const s = o.scale || 1, face = o.facing || 1, ph = o.phase || 0;
    ctx.save();
    ctx.translate(x, y);
    ctx.globalAlpha = o.alpha == null ? 1 : o.alpha;
    // shadow
    ctx.fillStyle = "rgba(0,0,0,.35)";
    ctx.beginPath(); ctx.ellipse(0, 0, 17 * s, 5 * s, 0, 0, Math.PI * 2); ctx.fill();
    ctx.scale(s * face, s);
    const bob = o.walking ? -Math.abs(Math.sin(ph * 2)) * 2.5 : o.working ? Math.sin(ph * 4) * 0.8 : 0;
    const swing = o.walking ? Math.sin(ph * 2) * 5 : 0;
    ctx.lineWidth = 2.6; ctx.strokeStyle = OUTLINE; ctx.lineJoin = "round";
    const dark = shade(color, -0.28);
    // legs
    for (const [lx, sw] of [[-11, swing], [2, -swing]]) {
      ctx.fillStyle = sw > 0 ? color : dark;
      rr(ctx, lx + sw * 0.6, -15 - Math.max(0, sw) * 0.4, 10, 14, [2, 2, 5, 5]); ctx.fill(); ctx.stroke();
    }
    ctx.translate(0, bob);
    // backpack
    ctx.fillStyle = dark;
    rr(ctx, -21, -38, 10, 23, 4); ctx.fill(); ctx.stroke();
    // body
    ctx.fillStyle = color;
    rr(ctx, -14, -48, 28, 38, [14, 14, 8, 8]); ctx.fill();
    ctx.save(); ctx.clip();
    ctx.fillStyle = "rgba(0,0,0,.22)"; ctx.fillRect(-14, -48, 7, 40);       // back shading
    ctx.fillStyle = "rgba(255,255,255,.18)"; ctx.fillRect(4, -46, 5, 10);    // top highlight
    ctx.restore();
    rr(ctx, -14, -48, 28, 38, [14, 14, 8, 8]); ctx.stroke();
    // visor
    ctx.fillStyle = o.visor || "#9fd8ef";
    rr(ctx, -2, -41, 19, 12, 6); ctx.fill(); ctx.stroke();
    ctx.fillStyle = "#e9f8ff"; rr(ctx, 7, -39, 6, 3.4, 2); ctx.fill();
    ctx.fillStyle = "rgba(40,90,120,.35)"; rr(ctx, 0, -34, 15, 3, 2); ctx.fill();
    // hat
    if (o.hat === "captain") {
      ctx.fillStyle = "#1d2b5a";
      rr(ctx, -12, -60, 22, 11, [7, 7, 1, 1]); ctx.fill(); ctx.stroke();
      rr(ctx, -15, -51, 30, 4, 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#ffd23f"; ctx.fillRect(-9, -55, 16, 2.4);
    }
    ctx.restore();
  }

  // ---------------------------------------------------------------- starfield
  class Stars {
    constructor() {
      this.layers = [0.15, 0.35, 0.7].map((speed, i) => ({
        speed, stars: Array.from({ length: 70 - i * 18 }, () => ({
          x: Math.random(), y: Math.random(), r: Math.random() * (0.6 + i * 0.6) + 0.3, tw: Math.random() * 6,
        })),
      }));
      this.shooting = null;
    }
    draw(ctx, w, h, t) {
      const g = ctx.createLinearGradient(0, 0, 0, h);
      g.addColorStop(0, "#050817"); g.addColorStop(1, "#0b0f26");
      ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);
      // nebula + planet
      const n = ctx.createRadialGradient(w * 0.82, h * 0.2, 10, w * 0.82, h * 0.2, w * 0.45);
      n.addColorStop(0, "rgba(124,92,255,.18)"); n.addColorStop(1, "rgba(124,92,255,0)");
      ctx.fillStyle = n; ctx.fillRect(0, 0, w, h);
      const px = w * 0.9, py = h * 0.86, pr = Math.min(w, h) * 0.16;
      const pg = ctx.createRadialGradient(px - pr * 0.4, py - pr * 0.4, pr * 0.1, px, py, pr);
      pg.addColorStop(0, "#3b4fa0"); pg.addColorStop(1, "#141b3d");
      ctx.fillStyle = pg; ctx.beginPath(); ctx.arc(px, py, pr, 0, Math.PI * 2); ctx.fill();
      for (const layer of this.layers) {
        for (const s of layer.stars) {
          const x = ((s.x - t * layer.speed * 0.01) % 1 + 1) % 1 * w;
          ctx.globalAlpha = 0.55 + 0.45 * Math.sin(t * 1.5 + s.tw);
          ctx.fillStyle = "#dfe8ff"; ctx.fillRect(x, s.y * h, s.r, s.r);
        }
      }
      ctx.globalAlpha = 1;
      if (!this.shooting && Math.random() < 0.003) this.shooting = { x: Math.random() * w, y: Math.random() * h * 0.5, t0: t };
      if (this.shooting) {
        const k = (t - this.shooting.t0) / 0.8;
        if (k > 1) this.shooting = null;
        else {
          const sx = this.shooting.x + k * 260, sy = this.shooting.y + k * 110;
          const gg = ctx.createLinearGradient(sx - 80, sy - 34, sx, sy);
          gg.addColorStop(0, "rgba(255,255,255,0)"); gg.addColorStop(1, "rgba(255,255,255,.9)");
          ctx.strokeStyle = gg; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(sx - 80, sy - 34); ctx.lineTo(sx, sy); ctx.stroke();
        }
      }
    }
  }

  // ---------------------------------------------------------------- ship interior layout
  function layoutFor(depts) {
    const cols = Math.max(4, Math.ceil(depts.length / 2));
    const x0 = 200, x1 = 1170, gap = 26;
    const rw = (x1 - x0 - (cols - 1) * gap) / cols;
    const rooms = {};
    depts.forEach((d, i) => {
      const top = i < cols, col = top ? i : i - cols;
      const x = x0 + col * (rw + gap);
      const y = top ? 105 : 415, h = 200;
      rooms[d] = {
        name: d, x, y, w: rw, h, top,
        door: { x: x + rw / 2, y: top ? y + h : y },
        console: { x: x + rw / 2, y: top ? y + 50 : y + h - 42 },
        stand: { x: x + rw / 2, y: top ? y + 118 : y + 118 },
        color: DEPT_COLORS[d] || EXTRA[i % EXTRA.length],
      };
    });
    rooms.__airlock = { name: "airlock", x: 88, y: 300, w: 92, h: 120, spawn: { x: 128, y: 372 } };
    rooms.__bridge = { name: "bridge", x: 1196, y: 250, w: 120, h: 220, stand: { x: 1250, y: 395 } };
    return rooms;
  }

  function routeTo(crew, rooms, targetRoom, target) {
    const path = [];
    const jitter = ((hash(crew.id) % 5) - 2) * 7;
    const cur = rooms[crew.room];
    if (cur && cur.door && crew.room !== targetRoom) {
      path.push({ x: cur.door.x, y: cur.door.y });
      path.push({ x: cur.door.x, y: CORR_Y + jitter });
    } else if (crew.room !== targetRoom) {
      path.push({ x: crew.x, y: CORR_Y + jitter });
    }
    const tr = rooms[targetRoom];
    if (crew.room !== targetRoom) {
      if (tr && tr.door) {
        path.push({ x: tr.door.x, y: CORR_Y + jitter });
        path.push({ x: tr.door.x, y: tr.door.y });
      } else {
        path.push({ x: target.x, y: CORR_Y + jitter });
      }
    }
    path.push({ x: target.x, y: target.y });
    crew.path = path;
    crew.room = targetRoom;
    crew.state = "walking";
  }

  // ---------------------------------------------------------------- the view
  class FleetView {
    constructor(root, handlers) {
      this.h = handlers || {};
      this.root = root;
      root.classList.add("fleet");
      root.innerHTML = `
        <canvas></canvas>
        <div class="fleet-bar">
          <div class="group">
            <button class="btn small back hidden">← Fleet</button>
            <div class="glass fleet-title"></div>
          </div>
          <div class="group">
            <button class="btn small ship-details hidden">Project ▸</button>
            <button class="btn small ship-chat hidden">💬 Chat</button>
            <button class="btn small tasks-btn">☰ Tasks</button>
            <button class="btn small primary new-project">+ New project</button>
            <label class="glass demo-toggle"><input type="checkbox" class="demo"> Demo crew</label>
          </div>
        </div>
        <div class="glass legend"></div>
        <div class="glass tasklist hidden"></div>
        <div class="glass ship-tip hidden"></div>
        <div class="glass demo-flag hidden">DEMO — SIMULATED CREW, NOT REAL WORK</div>`;
      this.canvas = root.querySelector("canvas");
      this.ctx = this.canvas.getContext("2d");
      this.el = {
        back: root.querySelector(".back"), title: root.querySelector(".fleet-title"),
        legend: root.querySelector(".legend"), demo: root.querySelector(".demo"),
        tasks: root.querySelector(".tasklist"), tip: root.querySelector(".ship-tip"),
        flag: root.querySelector(".demo-flag"),
      };
      this.stars = new Stars();
      this.rooms = layoutFor(Object.keys(DEPT_COLORS));
      this.mode = "fleet";
      this.selected = null;
      this.state = { projects: [], agents: [], departments: {} };
      this.crews = new Map();          // projectId -> Map(crewId -> crew)
      this.beams = [];
      this.hits = [];
      this.hover = null;
      this.mouse = null;
      this.demo = { on: false, agents: [], next: 0, project: null };
      this.seenAgents = new Set();
      this.t0 = now();

      this.el.back.addEventListener("click", () => this.showFleet());
      this.el.details = root.querySelector(".ship-details");
      this.el.chat = root.querySelector(".ship-chat");
      root.querySelector(".new-project").addEventListener("click", () => this.h.onNewProject && this.h.onNewProject());
      root.querySelector(".tasks-btn").addEventListener("click", () => this.h.onTasks && this.h.onTasks(this.selected));
      this.el.details.addEventListener("click", () => this.h.onProject && this.selected && this.h.onProject(this.selected));
      this.el.chat.addEventListener("click", () => this.h.onChat && this.selected && this.h.onChat(this.selected));
      this.el.demo.addEventListener("change", () => this.setDemo(this.el.demo.checked));
      this.el.tasks.addEventListener("click", (e) => {
        const row = e.target.closest("[data-task]");
        if (row && this.h.onTask) this.h.onTask(row.dataset.task);
        if (e.target.closest("h4")) this.el.tasks.classList.toggle("collapsed");
      });
      this.canvas.addEventListener("mousemove", (e) => this.onMove(e));
      this.canvas.addEventListener("mouseleave", () => { this.mouse = null; this.el.tip.classList.add("hidden"); });
      this.canvas.addEventListener("click", (e) => this.onClick(e));
      // Drag / swipe: look around inside a ship, scroll through the fleet.
      this.pan = null; this.scrollY = 0; this.drag = null; this.justDragged = false;
      this.canvas.addEventListener("pointerdown", (e) => {
        this.drag = { x: e.clientX, y: e.clientY, px: this.pan ? this.pan.x : 0, py: this.pan ? this.pan.y : 0, sy: this.scrollY, moved: false };
        this.canvas.setPointerCapture(e.pointerId);
      });
      this.canvas.addEventListener("pointermove", (e) => {
        if (!this.drag) return;
        const dx = e.clientX - this.drag.x, dy = e.clientY - this.drag.y;
        if (!this.drag.moved && Math.hypot(dx, dy) < 6) return;
        this.drag.moved = true;
        if (this.mode === "ship" && this.pan) { this.pan.x = this.drag.px + dx; this.pan.y = this.drag.py + dy; }
        else if (this.mode === "fleet") this.scrollY = this.drag.sy - dy;
      });
      const endDrag = () => { if (this.drag) this.justDragged = this.drag.moved; this.drag = null; };
      this.canvas.addEventListener("pointerup", endDrag);
      this.canvas.addEventListener("pointercancel", endDrag);
      this.canvas.addEventListener("wheel", (e) => {
        if (this.mode !== "fleet") return;
        this.scrollY += e.deltaY; e.preventDefault();
      }, { passive: false });
      this.ro = new ResizeObserver(() => this.resize());
      this.ro.observe(root);
      this.resize();
      this.renderLegend();
    }

    // ---------- public API
    start() { if (!this.raf) { this.last = now(); this.raf = requestAnimationFrame(() => this.frame()); } }
    stop() { cancelAnimationFrame(this.raf); this.raf = null; }
    destroy() { this.stop(); this.ro.disconnect(); }

    update(state) {
      this.state = state;
      this.sync();
      if (this.mode === "ship") this.renderTaskList();
      this.renderTitle();
    }

    showFleet() {
      this.mode = "fleet"; this.selected = null;
      this.el.back.classList.add("hidden"); this.el.tasks.classList.add("hidden");
      this.el.details.classList.add("hidden"); this.el.chat.classList.add("hidden");
      this.renderTitle();
    }

    showShip(projectId) {
      this.mode = "ship"; this.selected = projectId; this.pan = null; this.enteredAt = now();
      this.el.back.classList.remove("hidden"); this.el.tasks.classList.remove("hidden");
      const real = projectId !== "demo_ship";
      this.el.details.classList.toggle("hidden", !real); this.el.chat.classList.toggle("hidden", !real);
      if (this.w < 1100) this.el.tasks.classList.add("collapsed");   // keep the ship visible
      this.renderTaskList(); this.renderTitle();
    }

    setDemo(on) {
      this.demo.on = on; this.el.demo.checked = on;
      this.el.flag.classList.toggle("hidden", !on);
      if (!on) { this.demo.agents = []; this.demo.project = null; }
      this.sync();
    }

    // ---------- data -> crews
    projects() {
      const real = this.state.projects || [];
      if (this.demo.on && !real.some((p) => p.status === "ACTIVE")) {
        if (!this.demo.project) {
          const tasks = Object.keys(this.depts()).map((d, i) => ({
            id: "demo_t" + i, department: d, status: ["READY", "PENDING", "WAITING", "COMPLETED"][i % 4],
            description: `Demo task for ${d.replace("_", " ")}`, priority: 2,
          }));
          this.demo.project = { id: "demo_ship", name: "Demo Ship", status: "ACTIVE", budget_eur: 20, spent: 3.5,
            progress: 25, counts: { READY: 2, WAITING: 2, COMPLETED: 2, RUNNING: 0 }, tasks, demo: true };
        }
        return real.concat([this.demo.project]);
      }
      return real;
    }
    depts() {
      const d = this.state.departments || {};
      return Object.keys(d).length ? d : Object.fromEntries(Object.keys(DEPT_COLORS).map((k) => [k, k]));
    }
    agents() { return (this.state.agents || []).concat(this.demo.on ? this.demo.agents : []); }

    sync() {
      const depts = Object.keys(this.depts());
      const rooms = this.rooms = layoutFor(depts);
      const t = now();
      const serverNow = Date.parse(this.state.server_time || new Date().toISOString());
      const byProject = new Map();
      for (const a of this.agents()) {
        if (!byProject.has(a.project_id)) byProject.set(a.project_id, []);
        byProject.get(a.project_id).push(a);
      }
      for (const p of this.projects()) {
        if (!this.crews.has(p.id)) this.crews.set(p.id, new Map());
        const crews = this.crews.get(p.id);
        // Project manager
        if (!crews.has("pm")) {
          const b = rooms.__bridge.stand;
          crews.set("pm", { id: "pm", kind: "pm", color: "#d6e0f0", x: b.x, y: b.y, room: "__bridge",
            path: [], state: "idle", idleUntil: t + 2, facing: -1, phase: 0, alpha: 1 });
        }
        // Workers
        for (const a of byProject.get(p.id) || []) {
          const room = rooms[a.department] ? a.department : depts[0];
          let c = crews.get(a.id);
          const finished = a.status !== "RUNNING";
          if (!c) {
            const age = a.finished_at ? (serverNow - Date.parse(a.finished_at)) / 1000 : 0;
            if (finished && age > 20) continue;              // too old to animate
            const color = DEPT_COLORS[a.department] || EXTRA[hash(a.id) % EXTRA.length];
            const spawn = rooms.__airlock.spawn;
            c = { id: a.id, kind: "worker", color, x: spawn.x, y: spawn.y, room: "__airlock", path: [],
              state: "walking", facing: 1, phase: 0, alpha: 0, dept: room, task: a.task, taskId: a.task_id,
              model: a.model_key, started: Date.parse(a.created_at) || serverNow, bubbleUntil: t + 6 };
            const slot = [...crews.values()].filter((x) => x.kind === "worker" && x.dept === room && x.state !== "gone").length;
            const r = rooms[room];
            c.target = { x: r.stand.x + ((slot % 3) - 1) * 34, y: r.stand.y + Math.floor(slot / 3) * 16 };
            if (finished && age <= 20) { c.x = c.target.x; c.y = c.target.y; c.room = room; c.alpha = 1; c.state = "working"; }
            else routeTo(c, rooms, room, c.target);
            crews.set(a.id, c);
            if (!this.seenAgents.has(a.id) && !finished) this.beams.push({ project: p.id, t0: t });
            this.seenAgents.add(a.id);
          }
          c.model = a.model_key || c.model;
          if (finished && !c.result) {
            c.result = a.status === "COMPLETED" ? "ok" : "fail";
            if (c.state === "working") { c.state = "result"; c.resultUntil = t + 2.8; }
          }
        }
        // crews whose agent vanished while still working -> leave
        for (const c of crews.values()) {
          if (c.kind === "worker" && !c.result && !(byProject.get(p.id) || []).some((a) => a.id === c.id)) {
            c.result = "ok"; if (c.state === "working") { c.state = "result"; c.resultUntil = t + 1; }
          }
        }
      }
    }

    // ---------- simulation step
    step(dt) {
      const t = now();
      if (this.demo.on) this.demoTick(t);
      for (const [pid, crews] of this.crews) {
        const p = this.projects().find((x) => x.id === pid);
        for (const c of [...crews.values()]) {
          if (c.alpha < 1 && c.state !== "leaving" && c.state !== "gone") c.alpha = Math.min(1, c.alpha + dt * 2.5);
          if (c.state === "walking" || c.state === "leaving") {
            const target = c.path[0];
            if (!target) {
              if (c.state === "leaving") {            // reached the airlock: fade out, then gone
                c.alpha -= dt * 2.5;
                if (c.alpha <= 0) crews.delete(c.id);
              } else this.arrive(c, p, t);
              continue;
            }
            const dx = target.x - c.x, dy = target.y - c.y, dist = Math.hypot(dx, dy);
            const stepLen = SPEED * dt * (c.kind === "pm" ? 0.8 : 1);
            if (Math.abs(dx) > 0.5) c.facing = dx > 0 ? 1 : -1;
            if (dist <= stepLen) { c.x = target.x; c.y = target.y; c.path.shift(); }
            else { c.x += dx / dist * stepLen; c.y += dy / dist * stepLen; }
            c.phase += stepLen * 0.07;
          } else if (c.state === "working") {
            c.phase += dt;
          } else if (c.state === "result") {
            c.phase += dt;
            if (t > c.resultUntil) { c.state = "leaving"; routeTo(c, this.rooms, "__airlock", this.rooms.__airlock.spawn); c.state = "leaving"; }
          } else if (c.state === "idle") {
            c.phase += dt;
            if (c.kind === "pm" && t > c.idleUntil && p) this.pmNext(c, p, t);
          }
        }
      }
      this.beams = this.beams.filter((b) => t - b.t0 < 1.4);
    }

    arrive(c, p, t) {
      if (c.kind === "pm") { c.state = "idle"; c.idleUntil = t + 2.5 + Math.random() * 3.5; return; }
      if (c.result) { c.state = "result"; c.resultUntil = t + 2.8; }
      else { c.state = "working"; c.bubbleUntil = t + 5; }
      c.facing = c.x < (this.rooms[c.room] ? this.rooms[c.room].console.x : c.x) ? 1 : -1;
    }

    pmNext(c, p, t) {
      if (p.status !== "ACTIVE") {
        if (c.room !== "__bridge") routeTo(c, this.rooms, "__bridge", this.rooms.__bridge.stand);
        else c.idleUntil = t + 5;
        return;
      }
      const open = (p.tasks || []).filter((x) => ["WAITING", "READY", "RUNNING"].includes(x.status));
      const waiting = open.filter((x) => x.status === "WAITING");
      const pool = (waiting.length && Math.random() < 0.6 ? waiting : open).map((x) => x.department).filter((d) => this.rooms[d]);
      const goBridge = !pool.length || Math.random() < 0.25;
      if (goBridge) {
        if (c.room === "__bridge") { c.idleUntil = t + 3; return; }
        routeTo(c, this.rooms, "__bridge", this.rooms.__bridge.stand);
      } else {
        const d = pool[Math.floor(Math.random() * pool.length)];
        const r = this.rooms[d];
        routeTo(c, this.rooms, d, { x: r.stand.x + 48, y: r.stand.y + (r.top ? -8 : 8) });
      }
    }

    demoTick(t) {
      if (t < this.demo.next) return;
      this.demo.next = t + 2.8 + Math.random() * 2;
      const iso = (d) => new Date(Date.now() - d * 1000).toISOString();
      for (const a of this.demo.agents) {
        if (a.status === "RUNNING" && t > a._end) { a.status = Math.random() < 0.85 ? "COMPLETED" : "FAILED"; a.finished_at = iso(0); }
      }
      this.demo.agents = this.demo.agents.filter((a) => a.status === "RUNNING" || t - a._end < 25);
      const targets = this.projects().filter((p) => p.status === "ACTIVE" && (this.mode === "fleet" || p.id === this.selected));
      if (targets.length && this.demo.agents.filter((a) => a.status === "RUNNING").length < 5) {
        const p = targets[Math.floor(Math.random() * targets.length)];
        const depts = Object.keys(this.depts());
        const d = depts[Math.floor(Math.random() * depts.length)];
        const tasks = ["Analyse wedding planner niche", "Write listing copy", "Draft pricing model", "Design brief for cover",
          "Compare 3 competitors", "Outline ebook chapters", "Plan organic launch", "Sketch automation flow"];
        this.demo.agents.push({ id: "demo_" + Math.random().toString(36).slice(2, 8), project_id: p.id, department: d,
          status: "RUNNING", task: tasks[Math.floor(Math.random() * tasks.length)], model_key: "demo",
          created_at: iso(0), finished_at: null, _end: t + 6 + Math.random() * 7 });
      }
      this.sync();
    }

    // ---------- rendering
    resize() {
      const r = this.root.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      this.w = Math.max(320, r.width); this.hgt = Math.max(320, r.height);
      this.canvas.width = this.w * dpr; this.canvas.height = this.hgt * dpr;
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    frame() {
      // dt capped at 0.25 s: correct speed even at low frame rates, no huge jump after a tab switch
      const t = now(); const dt = Math.min(0.25, t - this.last); this.last = t;
      this.step(dt);
      const ctx = this.ctx;
      ctx.clearRect(0, 0, this.w, this.hgt);
      this.stars.draw(ctx, this.w, this.hgt, t - this.t0);
      this.hits = [];
      if (this.mode === "fleet") this.drawFleet(ctx, t);
      else this.drawInterior(ctx, t);
      this.updateTip();
      this.raf = requestAnimationFrame(() => this.frame());
    }

    // ----- fleet
    drawFleet(ctx, t) {
      const projects = this.projects();
      const small = this.w < 700;
      const top = small ? 170 : 150;
      const cols = clamp(Math.floor(this.w / 330), 1, 4);
      const cellW = this.w / cols, cellH = small ? 235 : 250;
      // Vertical scrolling when the fleet is taller than the screen (wheel, drag or swipe).
      const contentH = top + 70 + Math.ceil(projects.length / cols) * cellH;
      const maxScroll = Math.max(0, contentH - this.hgt);
      this.scrollY = clamp(this.scrollY, 0, maxScroll);
      ctx.save(); ctx.translate(0, -this.scrollY);
      this.drawFleetContent(ctx, t, projects, top, cols, cellW, cellH, small);
      ctx.restore();
      for (const h of this.hits) h.y -= this.scrollY;
      if (maxScroll > 0 && this.scrollY < maxScroll - 4) {
        ctx.fillStyle = "rgba(10,16,32,.8)"; rr(ctx, this.w / 2 - 70, this.hgt - 40, 140, 26, 13); ctx.fill();
        ctx.fillStyle = "#cfd8ee"; ctx.font = "600 12px system-ui"; ctx.textAlign = "center";
        ctx.fillText("▼ more ships", this.w / 2, this.hgt - 22);
      }
    }

    drawFleetContent(ctx, t, projects, top, cols, cellW, cellH, small) {
      // Mothership: the Master Orchestrator
      const mx = this.w / 2, my = small ? 100 : 78;
      this.drawMothership(ctx, mx, my, t);
      if (!projects.length) {
        ctx.fillStyle = "#8a98b8"; ctx.font = `600 ${small ? 13 : 16}px system-ui`; ctx.textAlign = "center";
        ctx.fillText("No ships yet — create a project,", this.w / 2, this.hgt / 2 + 20);
        ctx.fillText("or tick “Demo crew” to preview.", this.w / 2, this.hgt / 2 + 42);
        return;
      }
      projects.forEach((p, i) => {
        const col = i % cols, row = Math.floor(i / cols);
        const cx = cellW * col + cellW / 2;
        const cy = top + 70 + row * cellH + Math.sin(t * 1.2 + i) * 5;
        // tractor beam from mothership on new work
        for (const b of this.beams.filter((x) => x.project === p.id)) {
          const k = (t - b.t0) / 1.4;
          const g = ctx.createLinearGradient(mx, my + 20, cx, cy);
          g.addColorStop(0, `rgba(56,225,255,${0.5 * (1 - k)})`); g.addColorStop(1, `rgba(56,225,255,${0.12 * (1 - k)})`);
          ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(mx - 10, my + 22); ctx.lineTo(mx + 10, my + 22);
          ctx.lineTo(cx + 60, cy); ctx.lineTo(cx - 60, cy); ctx.closePath(); ctx.fill();
        }
        const hovered = this.hover && this.hover.type === "ship" && this.hover.id === p.id;
        this.drawShip(ctx, cx, cy, p, t, hovered);
        this.hits.push({ type: "ship", id: p.id, x: cx - 150, y: cy - 70, w: 300, h: 200, p });
      });
    }

    drawMothership(ctx, x, y, t) {
      const sc = this.w < 420 ? 0.72 : 1;
      ctx.save(); ctx.translate(x, y); ctx.scale(sc, sc);
      ctx.fillStyle = "#2b3656"; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 3;
      ctx.beginPath(); ctx.ellipse(0, 0, 150, 26, 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#3d4b75"; ctx.beginPath(); ctx.ellipse(0, -10, 62, 26, 0, Math.PI, 0); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "rgba(159,216,239,.85)"; ctx.beginPath(); ctx.ellipse(0, -14, 40, 14, 0, Math.PI, 0); ctx.fill(); ctx.stroke();
      for (let i = -4; i <= 4; i++) {
        const on = (Math.floor(t * 3) + i) % 3 === 0;
        ctx.fillStyle = on ? "#38e1ff" : "#1d6d80";
        ctx.beginPath(); ctx.arc(i * 30, 6, 4, 0, Math.PI * 2); ctx.fill();
      }
      ctx.fillStyle = "#cfd8ee"; ctx.font = "800 11px system-ui"; ctx.textAlign = "center";
      ctx.fillText("MASTER ORCHESTRATOR", 0, 44);
      ctx.restore();
      this.hits.push({ type: "mothership", x: x - 150 * sc, y: y - 40 * sc, w: 300 * sc, h: 90 * sc });
      const running = this.agents().filter((a) => a.status === "RUNNING").length;
      const rs = (this.state.runner || {}).state || "stopped";
      ctx.fillStyle = "#8a98b8"; ctx.font = "12px system-ui"; ctx.textAlign = "center";
      ctx.fillText(`loop ${rs} · ${running} crew at work`, x, y + 60);
    }

    drawShip(ctx, x, y, p, t, hovered) {
      const active = p.status === "ACTIVE", paused = p.status === "PAUSED";
      const done = p.status === "FINISHED" || p.status === "CANCELLED";
      const hull = done ? "#6d7894" : paused ? "#8e98b3" : "#c9d3e8";
      const agents = this.agents().filter((a) => a.project_id === p.id && a.status === "RUNNING");
      ctx.save(); ctx.translate(x, y);
      if (hovered) { ctx.shadowColor = "#38e1ff"; ctx.shadowBlur = 24; }
      // engine flames
      if (active) {
        const power = agents.length ? 1 : 0.5;
        for (const ey of [-16, 16]) {
          const len = (30 + Math.sin(t * 20 + ey) * 7) * power + 10;
          const g = ctx.createLinearGradient(-122, 0, -122 - len, 0);
          g.addColorStop(0, "rgba(255,240,180,.95)"); g.addColorStop(0.4, "rgba(255,140,40,.8)"); g.addColorStop(1, "rgba(255,60,20,0)");
          ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(-122, ey - 8); ctx.quadraticCurveTo(-122 - len * 1.6, ey, -122, ey + 8); ctx.fill();
        }
      }
      ctx.lineWidth = 3; ctx.strokeStyle = OUTLINE;
      // engines
      ctx.fillStyle = "#4a5577";
      for (const ey of [-16, 16]) { rr(ctx, -122, ey - 10, 22, 20, 5); ctx.fill(); ctx.stroke(); }
      // fins
      ctx.fillStyle = shade(hull.startsWith("#") ? hull : "#c9d3e8", -0.25);
      ctx.beginPath(); ctx.moveTo(-70, -30); ctx.lineTo(-100, -58); ctx.lineTo(-40, -30); ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(-70, 30); ctx.lineTo(-100, 58); ctx.lineTo(-40, 30); ctx.closePath(); ctx.fill(); ctx.stroke();
      // body
      ctx.fillStyle = hull;
      ctx.beginPath(); ctx.moveTo(-104, -34); ctx.lineTo(60, -34);
      ctx.quadraticCurveTo(128, -30, 138, 0); ctx.quadraticCurveTo(128, 30, 60, 34);
      ctx.lineTo(-104, 34); ctx.quadraticCurveTo(-114, 0, -104, -34); ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "rgba(0,0,0,.14)"; ctx.fillRect(-104, 12, 190, 20);
      // cockpit
      ctx.fillStyle = active ? "#9fd8ef" : "#5b6f86";
      ctx.beginPath(); ctx.moveTo(84, -22); ctx.quadraticCurveTo(124, -16, 128, 0); ctx.lineTo(84, 0); ctx.closePath(); ctx.fill(); ctx.stroke();
      // portholes with crew
      for (let i = 0; i < 5; i++) {
        const wx = -78 + i * 32, a = agents[i];
        ctx.fillStyle = a ? rgba(DEPT_COLORS[a.department] || "#ffffff", 0.95) : active ? "#23324f" : "#1a2236";
        ctx.beginPath(); ctx.arc(wx, -6, 9, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        if (a) { ctx.fillStyle = "#9fd8ef"; rr(ctx, wx - 1, -11, 8, 5, 2); ctx.fill(); }
      }
      // status light
      const light = done ? "#3ddc84" : paused ? (Math.sin(t * 4) > 0 ? "#ffc53d" : "#6b5520") : "#38e1ff";
      ctx.fillStyle = light; ctx.beginPath(); ctx.arc(40, -40, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      if (p.counts && p.counts.WAITING) {
        const pulse = 1 + Math.sin(t * 6) * 0.12;
        ctx.save(); ctx.translate(100, -52); ctx.scale(pulse, pulse);
        ctx.fillStyle = "#ff4d5e"; ctx.beginPath(); ctx.moveTo(0, -14); ctx.lineTo(13, 10); ctx.lineTo(-13, 10); ctx.closePath(); ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#fff"; ctx.font = "900 14px system-ui"; ctx.textAlign = "center"; ctx.fillText("!", 0, 7);
        ctx.restore();
      }
      if (done) {
        ctx.fillStyle = "rgba(61,220,132,.9)"; ctx.font = "900 13px system-ui"; ctx.textAlign = "center";
        ctx.fillText(p.status === "FINISHED" ? "DOCKED · MISSION OVER" : "ABANDONED", 0, 5);
      }
      ctx.restore();

      // labels
      ctx.textAlign = "center"; ctx.fillStyle = "#e6ecf8"; ctx.font = "700 15px system-ui";
      ctx.fillText(trunc(p.name, 30), x, y + 80);
      ctx.font = "12px system-ui"; ctx.fillStyle = paused ? "#ffc53d" : done ? "#8a98b8" : "#38e1ff";
      const c = p.counts || {};
      ctx.fillText(`${p.status} · ${agents.length} working · ${c.WAITING || 0} need you · ${c.READY || 0} ready`, x, y + 98);
      this.miniBar(ctx, x - 110, y + 122, 105, 7, (p.progress || 0) / 100, "#3ddc84", `Tasks ${p.progress || 0}%`);
      const spentFrac = p.budget_eur ? (p.spent || 0) / p.budget_eur : 0;
      this.miniBar(ctx, x + 5, y + 122, 105, 7, spentFrac, spentFrac > 0.9 ? "#ff4d5e" : "#ffc53d",
        `€${(p.spent || 0).toFixed(2)} / €${(p.budget_eur || 0).toFixed(0)}`);
    }

    miniBar(ctx, x, y, w, h, frac, color, label) {
      ctx.fillStyle = "#8a98b8"; ctx.font = "11px system-ui"; ctx.textAlign = "left";
      ctx.fillText(label, x, y - 4);
      ctx.fillStyle = "#0a1120"; rr(ctx, x, y, w, h, 4); ctx.fill();
      ctx.fillStyle = color; rr(ctx, x, y, Math.max(0, Math.min(1, frac)) * w, h, 4); ctx.fill();
    }

    // ----- interior
    view() {
      const fit = Math.min(this.w / W, (this.hgt - 40) / H);
      // On phones the whole ship would be tiny: keep it readable and let the user swipe around.
      const s = this.w < 700 ? Math.max(fit, Math.min(0.8, (this.hgt - 70) / H)) : fit;
      const cw = W * s, ch = H * s;
      if (!this.pan) this.pan = { x: (this.w - cw) / 2, y: (this.hgt - ch) / 2 + 20 };
      if (cw <= this.w) this.pan.x = (this.w - cw) / 2; else this.pan.x = clamp(this.pan.x, this.w - cw, 0);
      if (ch <= this.hgt - 40) this.pan.y = (this.hgt - ch) / 2 + 20; else this.pan.y = clamp(this.pan.y, this.hgt - ch, 0);
      return { s, ox: this.pan.x, oy: this.pan.y, scrollable: cw > this.w + 1 || ch > this.hgt - 39 };
    }

    drawInterior(ctx, t) {
      const p = this.projects().find((x) => x.id === this.selected);
      if (!p) { this.showFleet(); return; }
      const { s, ox, oy } = this.view();
      const rooms = this.rooms;
      ctx.save(); ctx.translate(ox, oy); ctx.scale(s, s);
      const active = p.status === "ACTIVE";

      // hull
      ctx.lineWidth = 8; ctx.strokeStyle = "#0b0f1a";
      if (active) {
        for (const ey of [290, 430]) {
          const len = 50 + Math.sin(t * 18 + ey) * 10;
          const g = ctx.createLinearGradient(60, ey, 60 - len, ey);
          g.addColorStop(0, "rgba(255,240,180,.95)"); g.addColorStop(0.5, "rgba(255,140,40,.7)"); g.addColorStop(1, "rgba(255,60,20,0)");
          ctx.fillStyle = g; ctx.beginPath(); ctx.moveTo(62, ey - 20); ctx.quadraticCurveTo(60 - len, ey, 62, ey + 20); ctx.fill();
        }
      }
      ctx.fillStyle = "#3a4560";
      for (const ey of [290, 430]) { rr(ctx, 56, ey - 26, 40, 52, 10); ctx.fill(); ctx.stroke(); }
      const hg = ctx.createLinearGradient(0, 70, 0, 650);
      hg.addColorStop(0, "#56617e"); hg.addColorStop(1, "#2c3550");
      ctx.fillStyle = hg;
      ctx.beginPath(); ctx.moveTo(80, 80); ctx.lineTo(1150, 80);
      ctx.quadraticCurveTo(1350, 95, 1380, 360); ctx.quadraticCurveTo(1350, 625, 1150, 640);
      ctx.lineTo(80, 640); ctx.quadraticCurveTo(62, 360, 80, 80); ctx.closePath(); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#161d30";
      ctx.beginPath(); ctx.moveTo(100, 96); ctx.lineTo(1146, 96);
      ctx.quadraticCurveTo(1334, 110, 1360, 360); ctx.quadraticCurveTo(1334, 610, 1146, 624);
      ctx.lineTo(100, 624); ctx.quadraticCurveTo(84, 360, 100, 96); ctx.closePath(); ctx.fill();

      // corridor
      ctx.fillStyle = "#232c45"; ctx.fillRect(100, CORR_Y - 48, 1180, 96);
      ctx.strokeStyle = "rgba(255,210,63,.35)"; ctx.lineWidth = 3; ctx.setLineDash([18, 16]);
      ctx.beginPath(); ctx.moveTo(110, CORR_Y); ctx.lineTo(1280, CORR_Y); ctx.stroke(); ctx.setLineDash([]);
      for (let x = 150; x < 1280; x += 90) {
        ctx.fillStyle = `rgba(56,225,255,${0.25 + 0.2 * Math.sin(t * 2 + x)})`;
        ctx.fillRect(x, CORR_Y - 46, 30, 3); ctx.fillRect(x, CORR_Y + 43, 30, 3);
      }

      // tasks per room
      const byRoom = {};
      for (const task of p.tasks || []) (byRoom[task.department] = byRoom[task.department] || []).push(task);
      const crews = this.crews.get(p.id) || new Map();
      const working = {};
      for (const c of crews.values()) if (c.kind === "worker" && c.state === "working") working[c.room] = true;

      for (const d of Object.keys(this.depts())) this.drawRoom(ctx, rooms[d], byRoom[d] || [], working[d], t);
      this.drawAirlock(ctx, rooms.__airlock, t);
      this.drawBridge(ctx, rooms.__bridge, t, p);

      // crew, depth-sorted
      const list = [...crews.values()].sort((a, b) => a.y - b.y);
      for (const c of list) this.drawCrewmate(ctx, c, t, p);
      ctx.restore();

      if (this.view().scrollable && t - (this.enteredAt || 0) < 5) {
        ctx.globalAlpha = Math.min(1, 5 - (t - this.enteredAt));
        ctx.fillStyle = "rgba(10,16,32,.8)"; rr(ctx, this.w / 2 - 120, this.hgt - 58, 240, 30, 15); ctx.fill();
        ctx.fillStyle = "#e6ecf8"; ctx.font = "600 13px system-ui"; ctx.textAlign = "center";
        ctx.fillText("◀  swipe to look around  ▶", this.w / 2, this.hgt - 38);
        ctx.globalAlpha = 1;
      }
      if (p.status !== "ACTIVE") {
        ctx.fillStyle = p.status === "PAUSED" ? "rgba(255,197,61,.9)" : "rgba(61,220,132,.9)";
        ctx.font = `900 ${this.w < 700 ? 18 : 28}px system-ui`; ctx.textAlign = "center";
        ctx.fillText(p.status === "PAUSED" ? "⏸ SHIP PAUSED — no new work" : "MISSION COMPLETE", this.w / 2, this.hgt - 22);
      }
    }

    toScreen(x, y) { const { s, ox, oy } = this.view(); return { x: ox + x * s, y: oy + y * s }; }

    drawRoom(ctx, r, tasks, busy, t) {
      const col = r.color;
      ctx.fillStyle = rgba(col, 0.1); ctx.fillRect(r.x, r.y, r.w, r.h);
      ctx.strokeStyle = "rgba(255,255,255,.035)"; ctx.lineWidth = 1;
      for (let gx = r.x; gx < r.x + r.w; gx += 24) { ctx.beginPath(); ctx.moveTo(gx, r.y); ctx.lineTo(gx, r.y + r.h); ctx.stroke(); }
      for (let gy = r.y; gy < r.y + r.h; gy += 24) { ctx.beginPath(); ctx.moveTo(r.x, gy); ctx.lineTo(r.x + r.w, gy); ctx.stroke(); }
      // walls with door gap
      ctx.strokeStyle = "#4a5a82"; ctx.lineWidth = 8; ctx.lineCap = "round";
      const dx = r.door.x, gap = 34, wy = r.top ? r.y + r.h : r.y, oy = r.top ? r.y : r.y + r.h;
      ctx.beginPath();
      ctx.moveTo(r.x, oy); ctx.lineTo(r.x + r.w, oy);
      ctx.moveTo(r.x, r.y); ctx.lineTo(r.x, r.y + r.h);
      ctx.moveTo(r.x + r.w, r.y); ctx.lineTo(r.x + r.w, r.y + r.h);
      ctx.moveTo(r.x, wy); ctx.lineTo(dx - gap, wy);
      ctx.moveTo(dx + gap, wy); ctx.lineTo(r.x + r.w, wy);
      ctx.stroke();
      ctx.lineCap = "butt";
      // label
      ctx.fillStyle = col; ctx.font = "800 15px system-ui"; ctx.textAlign = "left";
      ctx.fillText(r.name.replace("_", " ").toUpperCase(), r.x + 12, r.top ? r.y + 24 : r.y + r.h - 12);
      const open = tasks.filter((x) => ["READY", "PENDING", "RUNNING", "WAITING"].includes(x.status)).length;
      ctx.fillStyle = "#8a98b8"; ctx.font = "12px system-ui"; ctx.textAlign = "right";
      ctx.fillText(`${open} open`, r.x + r.w - 12, r.top ? r.y + 24 : r.y + r.h - 12);
      // console
      const cx = r.console.x, cy = r.console.y;
      ctx.fillStyle = "#2d3858"; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 3;
      rr(ctx, cx - 46, cy - 14, 92, 30, 6); ctx.fill(); ctx.stroke();
      const flick = busy ? 0.55 + 0.45 * Math.abs(Math.sin(t * 9)) : 0.35;
      ctx.fillStyle = rgba(col, flick); rr(ctx, cx - 36, cy - 9, 72, 18, 4); ctx.fill();
      if (busy) {
        ctx.shadowColor = col; ctx.shadowBlur = 20; ctx.fillStyle = rgba(col, 0.4);
        rr(ctx, cx - 36, cy - 9, 72, 18, 4); ctx.fill(); ctx.shadowBlur = 0;
        ctx.fillStyle = "rgba(0,0,0,.35)";
        for (let i = 0; i < 3; i++) ctx.fillRect(cx - 30, cy - 6 + ((t * 30 + i * 6) % 16), 60 * Math.random() + 4, 1.5);
      }
      // task markers
      const waiting = tasks.filter((x) => x.status === "WAITING");
      const ready = tasks.filter((x) => x.status === "READY");
      const mx = cx + 62, my = cy - 4;
      if (waiting.length) {
        const pulse = 1 + Math.sin(t * 6) * 0.15;
        ctx.save(); ctx.translate(mx, my - 18); ctx.scale(pulse, pulse);
        ctx.shadowColor = "#ff4d5e"; ctx.shadowBlur = 18;
        ctx.fillStyle = "#ff4d5e"; ctx.beginPath(); ctx.moveTo(0, -16); ctx.lineTo(15, 11); ctx.lineTo(-15, 11); ctx.closePath(); ctx.fill();
        ctx.shadowBlur = 0; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 2.5; ctx.stroke();
        ctx.fillStyle = "#fff"; ctx.font = "900 16px system-ui"; ctx.textAlign = "center"; ctx.fillText("!", 0, 8);
        ctx.restore();
        ctx.fillStyle = "#ff9aa5"; ctx.font = "700 11px system-ui"; ctx.textAlign = "center";
        ctx.fillText(`${waiting.length} need${waiting.length > 1 ? "" : "s"} you`, mx, my + 12);
        const sp = this.toScreen(mx, my - 18);
        this.hits.push({ type: "waiting", x: sp.x - 20, y: sp.y - 20, w: 40, h: 44, task: waiting[0], room: r.name, tasks: waiting });
      } else if (ready.length) {
        const b = Math.sin(t * 5) * 4;
        ctx.fillStyle = "#ffd23f"; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 2.5;
        ctx.beginPath(); ctx.moveTo(mx - 9, my - 26 + b); ctx.lineTo(mx + 9, my - 26 + b); ctx.lineTo(mx, my - 12 + b); ctx.closePath(); ctx.fill(); ctx.stroke();
        ctx.fillStyle = "#ffd23f"; ctx.font = "700 11px system-ui"; ctx.textAlign = "center";
        ctx.fillText(`${ready.length} ready`, mx, my + 6);
      }
      const s0 = this.toScreen(r.x, r.y), s1 = this.toScreen(r.x + r.w, r.y + r.h);
      this.hits.push({ type: "room", x: s0.x, y: s0.y, w: s1.x - s0.x, h: s1.y - s0.y, room: r.name, tasks });
    }

    drawAirlock(ctx, a, t) {
      ctx.fillStyle = "#1c2338"; ctx.fillRect(a.x, a.y, a.w, a.h);
      ctx.save(); ctx.beginPath(); ctx.rect(a.x, a.y, 16, a.h); ctx.clip();
      for (let i = -2; i < 12; i++) { ctx.fillStyle = i % 2 ? "#ffd23f" : "#1c1c1c"; ctx.beginPath(); ctx.moveTo(a.x, a.y + i * 14); ctx.lineTo(a.x + 16, a.y + i * 14 - 10); ctx.lineTo(a.x + 16, a.y + i * 14 + 4); ctx.lineTo(a.x, a.y + i * 14 + 14); ctx.fill(); }
      ctx.restore();
      ctx.fillStyle = "#8a98b8"; ctx.font = "800 12px system-ui"; ctx.textAlign = "center";
      ctx.fillText("AIRLOCK", a.x + a.w / 2 + 6, a.y - 8);
    }

    drawBridge(ctx, b, t, p) {
      ctx.fillStyle = "rgba(56,225,255,.06)"; ctx.fillRect(b.x, b.y, b.w, b.h);
      // window
      ctx.fillStyle = "#0a1330"; ctx.strokeStyle = "#4a5a82"; ctx.lineWidth = 6;
      ctx.beginPath(); ctx.moveTo(b.x + b.w - 10, b.y + 30); ctx.quadraticCurveTo(b.x + b.w + 50, b.y + b.h / 2, b.x + b.w - 10, b.y + b.h - 30); ctx.closePath(); ctx.fill(); ctx.stroke();
      for (let i = 0; i < 8; i++) { ctx.fillStyle = "#dfe8ff"; ctx.fillRect(b.x + b.w + ((i * 7 + t * 20) % 30) - 8, b.y + 50 + i * 16, 2, 2); }
      // captain chair
      ctx.fillStyle = "#6b2fbb"; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 3;
      rr(ctx, b.x + 40, b.y + 90, 34, 40, 8); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#8a98b8"; ctx.font = "800 12px system-ui"; ctx.textAlign = "center";
      ctx.fillText("BRIDGE", b.x + b.w / 2, b.y - 8);
      ctx.fillStyle = "#cfd8ee"; ctx.font = "11px system-ui";
      ctx.fillText(`€${(p.spent || 0).toFixed(2)} / €${(p.budget_eur || 0).toFixed(0)}`, b.x + b.w / 2, b.y + b.h + 16);
    }

    drawCrewmate(ctx, c, t, p) {
      const working = c.state === "working";
      drawCrew(ctx, c.x, c.y, c.color, {
        facing: c.facing, phase: c.phase, walking: c.state === "walking" || c.state === "leaving",
        working, alpha: c.alpha, hat: c.kind === "pm" ? "captain" : null, scale: c.kind === "pm" ? 1.12 : 1,
      });
      if (c.kind === "pm") {
        ctx.fillStyle = "#e6ecf8"; ctx.font = "700 11px system-ui"; ctx.textAlign = "center";
        ctx.fillText("PM", c.x, c.y + 16);
        if (p.status === "PAUSED" && c.state === "idle") {
          ctx.fillStyle = "#ffc53d"; ctx.font = "800 16px system-ui";
          ctx.fillText("z", c.x + 20, c.y - 62 - (t * 10 % 12)); ctx.fillText("Z", c.x + 30, c.y - 74 - (t * 10 % 12));
        }
      }
      if (working && c.started) {
        const elapsed = (Date.now() - c.started) / 1000;
        const frac = Math.min(0.95, elapsed / 40);
        ctx.strokeStyle = "rgba(255,255,255,.18)"; ctx.lineWidth = 4;
        ctx.beginPath(); ctx.arc(c.x, c.y - 72, 10, 0, Math.PI * 2); ctx.stroke();
        ctx.strokeStyle = c.color; ctx.beginPath(); ctx.arc(c.x, c.y - 72, 10, -Math.PI / 2, -Math.PI / 2 + frac * Math.PI * 2); ctx.stroke();
        for (let i = 0; i < 3; i++) {
          const a = t * 4 + i * 2.1;
          ctx.fillStyle = rgba(c.color, 0.8); ctx.fillRect(c.x + 18 + Math.cos(a) * 6, c.y - 40 + Math.sin(a) * 6, 3, 3);
        }
      }
      if (c.state === "result") {
        const ok = c.result === "ok", y = c.y - 78 - Math.min(10, (c.phase % 3) * 8);
        ctx.fillStyle = ok ? "#3ddc84" : "#ff4d5e"; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(c.x, y, 13, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 3.5; ctx.beginPath();
        if (ok) { ctx.moveTo(c.x - 6, y); ctx.lineTo(c.x - 1, y + 5); ctx.lineTo(c.x + 7, y - 5); }
        else { ctx.moveTo(c.x - 5, y - 5); ctx.lineTo(c.x + 5, y + 5); ctx.moveTo(c.x + 5, y - 5); ctx.lineTo(c.x - 5, y + 5); }
        ctx.stroke();
      }
      if (c.kind === "worker" && c.task && (working || c.state === "walking") && now() < (c.bubbleUntil || 0)) this.bubble(ctx, c.x, c.y - 92, c.task);
      const sp = this.toScreen(c.x, c.y - 30);
      const { s } = this.view();
      this.hits.push({ type: "crew", x: sp.x - 18 * s, y: sp.y - 30 * s, w: 36 * s, h: 56 * s, crew: c });
    }

    bubble(ctx, x, y, text) {
      ctx.font = "600 12px system-ui";
      const lines = wrap(ctx, text, 170, 2);
      const w = Math.max(...lines.map((l) => ctx.measureText(l).width)) + 20, h = lines.length * 15 + 12;
      ctx.fillStyle = "rgba(255,255,255,.96)"; ctx.strokeStyle = OUTLINE; ctx.lineWidth = 2;
      rr(ctx, x - w / 2, y - h, w, h, 9); ctx.fill(); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(x - 6, y - 1); ctx.lineTo(x, y + 8); ctx.lineTo(x + 6, y - 1); ctx.fillStyle = "#fff"; ctx.fill();
      ctx.fillStyle = "#111"; ctx.textAlign = "center";
      lines.forEach((l, i) => ctx.fillText(l, x, y - h + 18 + i * 15));
    }

    // ---------- DOM overlays + input
    renderTitle() {
      if (this.mode === "fleet") {
        const n = this.projects().length;
        this.el.title.innerHTML = `Fleet<small>${n} ship${n === 1 ? "" : "s"} · click a ship to board</small>`;
      } else {
        const p = this.projects().find((x) => x.id === this.selected);
        if (p) this.el.title.innerHTML = `${esc(p.name)}<small>${esc(p.status)} · ${p.progress || 0}% done</small>`;
      }
    }

    renderLegend() {
      this.el.legend.innerHTML = Object.entries(DEPT_COLORS)
        .map(([d, c]) => `<span><i data-bg="${c}"></i>${d.replace("_", " ")}</span>`).join("");
      this.el.legend.querySelectorAll("[data-bg]").forEach((e) => { e.style.background = e.dataset.bg; });
    }

    renderTaskList() {
      const p = this.projects().find((x) => x.id === this.selected);
      if (!p) return;
      const order = { WAITING: 0, RUNNING: 1, READY: 2, PENDING: 3, FAILED: 4, COMPLETED: 5, CANCELLED: 6 };
      const tasks = [...(p.tasks || [])].sort((a, b) => order[a.status] - order[b.status]);
      const collapsed = this.el.tasks.classList.contains("collapsed");
      this.el.tasks.innerHTML = `
        <h4>TASKS ▾</h4>
        <div class="body">
          <div class="progress-label">TOTAL TASKS COMPLETED</div>
          <div class="bar green"><i data-pct="${p.progress || 0}"></i></div>
          ${tasks.length ? tasks.map((x) => `
            <div class="t ${x.status}" data-task="${esc(x.id)}" title="${esc(x.status)}">
              <span class="room">${esc(x.department.replace("_", " "))}:</span>
              <span>${esc(trunc(x.description, 60))}${x.status === "WAITING" ? " — needs you" : ""}</span>
            </div>`).join("") : `<div class="muted">No tasks yet. The orchestrator plans them when the work loop runs.</div>`}
        </div>`;
      this.el.tasks.classList.toggle("collapsed", collapsed);
      this.el.tasks.querySelectorAll("[data-pct]").forEach((e) => { e.style.width = e.dataset.pct + "%"; });
    }

    pos(e) { const r = this.canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    hit(pt) {
      const order = ["crew", "waiting", "mothership", "ship", "room"];
      for (const type of order) {
        for (let i = this.hits.length - 1; i >= 0; i--) {
          const h = this.hits[i];
          if (h.type === type && pt.x >= h.x && pt.x <= h.x + h.w && pt.y >= h.y && pt.y <= h.y + h.h) return h;
        }
      }
      return null;
    }
    onMove(e) { this.mouse = this.pos(e); }
    onClick(e) {
      if (this.justDragged) { this.justDragged = false; return; }   // it was a swipe, not a tap
      const h = this.hit(this.pos(e));
      if (!h) return;
      if (h.type === "mothership") { if (this.h.onMothership) this.h.onMothership(); }
      else if (h.type === "ship") { if (!h.p.demo || this.demo.on) this.showShip(h.id); }
      else if (h.type === "waiting" && this.h.onTask && !String(h.task.id).startsWith("demo")) this.h.onTask(h.task.id);
      else if (h.type === "crew" && h.crew.taskId && this.h.onTask && !String(h.crew.id).startsWith("demo")) this.h.onTask(h.crew.taskId);
      else if (h.type === "room" && this.h.onRoom && this.selected !== "demo_ship") this.h.onRoom(this.selected, h.room);
    }
    updateTip() {
      const tip = this.el.tip;
      const h = this.mouse ? this.hit(this.mouse) : null;
      this.hover = h;
      this.canvas.style.cursor = h && h.type !== "room" ? "pointer" : h ? "help" : "default";
      if (!h) { tip.classList.add("hidden"); return; }
      let html = "";
      if (h.type === "mothership") {
        html = `<b>Master Orchestrator</b>Head of the company<br><span class="muted">Click to talk to it</span>`;
      } else if (h.type === "ship") {
        const p = h.p, c = p.counts || {};
        html = `<b>${esc(p.name)}</b>${esc(p.status)} · priority ${p.priority || "-"}<br>
          ${c.COMPLETED || 0} done · ${c.READY || 0} ready · ${c.PENDING || 0} pending · ${c.WAITING || 0} waiting<br>
          Budget €${(p.spent || 0).toFixed(2)} of €${(p.budget_eur || 0).toFixed(2)}<br><span class="muted">Click to board</span>`;
      } else if (h.type === "crew") {
        const c = h.crew;
        html = c.kind === "pm" ? `<b>Project Manager</b>Patrols rooms with open work`
          : `<b>${esc(c.id)}</b>${esc(c.dept)} · ${esc(c.model || "model pending")}<br>${esc(trunc(c.task, 140))}<br>
             <span class="muted">${c.state === "result" ? (c.result === "ok" ? "✔ completed" : "✖ failed") : esc(c.state)}</span>`;
      } else if (h.type === "waiting") {
        html = `<b>Needs you (${h.tasks.length})</b>${h.tasks.map((x) => "• " + esc(trunc(x.description, 70))).join("<br>")}<br><span class="muted">Click to review</span>`;
      } else if (h.type === "room") {
        const open = h.tasks.filter((x) => !["COMPLETED", "CANCELLED"].includes(x.status));
        html = `<b>${esc(h.room.replace("_", " "))}</b>${open.length} open · ${h.tasks.length} total<br><span class="muted">Click for this room's tasks</span>`;
      }
      tip.innerHTML = html;
      tip.classList.remove("hidden");
      const x = Math.min(this.mouse.x + 16, this.w - tip.offsetWidth - 8);
      const y = Math.min(this.mouse.y + 16, this.hgt - tip.offsetHeight - 8);
      tip.style.left = x + "px"; tip.style.top = y + "px";
    }
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  window.FleetView = FleetView;
  window.drawCrew = drawCrew;
})();
