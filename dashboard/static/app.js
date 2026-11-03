// Charmer dashboard: live refresh every 5 s from /api/state, sparklines and the hourly bar chart as inline SVG.
// No library, no build step. Colours come from tokens.css through CSS variables only.
(() => {
  "use strict";
  const SVG = "http://www.w3.org/2000/svg";
  const SERIES = 6;
  const SHOWN = 5; // the chart names the five busiest pairs; the rest add up as "Other"
  const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  let state = null;
  // the chart range lives in the URL (?range=48h), so it survives a reload and can be shared
  let range = new URLSearchParams(location.search).get("range") === "48h" ? "48h" : "today";
  let lastOk = Date.now();

  const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
  const compact = (v) => {
    if (v == null) return "—";
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
    if (a >= 1e5) return (v / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
    return nf.format(v);
  };
  const el = (name, attrs = {}) => {
    const n = document.createElementNS(SVG, name);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  };

  // "Today" starts at midnight in the dashboard's zone (state.tz), never the viewer's own clock
  function windowed(series) {
    if (!series || !state) return series || [];
    if (range === "48h") return series;
    const hours = (state.by_pair && state.by_pair.hours) || [];
    const from = hours.findIndex((h) => h >= state.by_pair.today_from);
    return from < 0 ? series : series.slice(from);
  }
  const clock = (opts) => new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: state ? state.tz : undefined, ...opts });

  function spark(svg, raw) {
    const values = windowed(raw);
    svg.replaceChildren();
    if (!values.length || values.every((v) => !v)) {
      svg.append(el("path", { class: "line", d: "M0 30 L120 30" }));
      return;
    }
    const max = Math.max(...values, 1);
    const step = values.length > 1 ? 120 / (values.length - 1) : 120;
    const pts = values.map((v, i) => [i * step, 30 - (v / max) * 26]);
    const line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    svg.append(el("path", { class: "area", d: `${line} L120 32 L0 32 Z` }), el("path", { class: "line", d: line }));
  }

  function chart(box, legend, byPair) {
    const all = (byPair.series || []).map((s) => ({ ...s, values: windowed(s.values) }))
      .map((s) => ({ ...s, total: s.values.reduce((a, v) => a + v, 0) })).filter((s) => s.total > 0)
      .sort((a, b) => b.total - a.total);
    const series = all.slice(0, SHOWN).map((s, i) => ({ ...s, colour: `var(--c-series-${(i % SERIES) + 1})` }));
    if (all.length > SHOWN) {
      const rest = all.slice(SHOWN);
      series.push({ pair: `Other (${rest.length})`, colour: "var(--c-nokey)",
        values: rest[0].values.map((_, i) => rest.reduce((a, s) => a + s.values[i], 0)) });
    }
    box.querySelector("svg")?.remove();
    box.querySelector(".chart-tip")?.remove();
    legend.replaceChildren();
    const empty = box.querySelector(".empty");
    if (!series.length) {
      if (!empty) {
        const p = document.createElement("p");
        p.className = "empty";
        p.textContent = "No calls in this window. They appear here as soon as consumers use the pool.";
        box.append(p);
      }
      box.setAttribute("aria-label", "Requests per hour: none in this window");
      return;
    }
    empty?.remove();
    const n = series[0].values.length;
    const totals = Array.from({ length: n }, (_, i) => series.reduce((a, s) => a + s.values[i], 0));
    const max = Math.max(...totals, 1);
    // drawn at the box's real pixel size, so axis text is never stretched
    const W = Math.max(240, Math.round(box.clientWidth)), H = Math.max(120, Math.round(box.clientHeight));
    const padL = 40, padB = 22, gap = 2;
    const bw = (W - padL) / n;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, "aria-hidden": "true" });
    const hours = windowed(byPair.hours || []);
    const hourFmt = clock(range === "48h" ? { weekday: "short" } : {});
    const hover = el("rect", { class: "hover", x: 0, y: 0, width: Math.max(1, bw), height: H - padB, visibility: "hidden" });
    svg.append(hover);
    [0, Math.floor((n - 1) / 2), n - 1].filter((v, i, a) => a.indexOf(v) === i).forEach((i) => {
      if (hours[i] == null) return;
      const t = el("text", { class: "axis", x: padL + i * bw + bw / 2, y: H - 4, "text-anchor": i === n - 1 ? "end" : i === 0 ? "start" : "middle" });
      t.textContent = hourFmt.format(new Date(hours[i] * 1000));
      svg.append(t);
    });
    for (const f of [0, 0.5, 1]) {
      const y = H - padB - f * (H - padB - 6);
      svg.append(el("line", { class: "gridline", x1: padL, x2: W, y1: y, y2: y }));
      const t = el("text", { class: "axis", x: padL - 6, y: y + 3, "text-anchor": "end" });
      t.textContent = compact(Math.round(max * f));
      svg.append(t);
    }
    totals.forEach((_, i) => {
      let y = H - padB;
      series.forEach((s) => {
        const h = (s.values[i] / max) * (H - padB - 6);
        if (h <= 0) return;
        y -= h;
        svg.append(el("rect", { class: "bar", x: padL + i * bw + gap / 2, y, width: Math.max(1, bw - gap), height: h, style: `fill: ${s.colour}` }));
      });
    });
    box.append(svg);

    // hover (or touch) a column: its hour, its total and each pair's share
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    box.append(tip);
    const span = clock();
    const day = clock(range === "48h" ? { weekday: "short" } : {});
    const show = (e) => {
      const i = Math.floor((e.clientX - box.getBoundingClientRect().left - padL) / bw);
      if (i < 0 || i >= n || hours[i] == null) { hide(); return; }
      hover.setAttribute("x", padL + i * bw);
      hover.setAttribute("visibility", "visible");
      const head = document.createElement("strong");
      head.textContent = `${day.format(new Date(hours[i] * 1000))}–${span.format(new Date((hours[i] + 3600) * 1000))} · ${nf.format(totals[i])} requests`;
      const list = document.createElement("ul");
      series.filter((s) => s.values[i]).forEach((s) => {
        const li = document.createElement("li");
        const key = document.createElement("span");
        key.className = "key";
        key.style.setProperty("--k", s.colour);
        const v = document.createElement("span");
        v.className = "v tnum";
        v.textContent = nf.format(s.values[i]);
        li.append(key, document.createTextNode(s.pair), v);
        list.append(li);
      });
      tip.replaceChildren(head, list);
      tip.hidden = false;
      const x = padL + i * bw + bw / 2;
      tip.style.left = `${Math.min(Math.max(0, x - tip.offsetWidth / 2), W - tip.offsetWidth)}px`;
    };
    const hide = () => { tip.hidden = true; hover.setAttribute("visibility", "hidden"); };
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", hide);

    const peak = totals.indexOf(Math.max(...totals));
    box.setAttribute("aria-label", `Requests per hour, stacked by pair: ${nf.format(totals.reduce((a, v) => a + v, 0))} in this window, `
      + `busiest hour ${hourFmt.format(new Date(hours[peak] * 1000))} with ${nf.format(totals[peak])}`);
    series.forEach((s) => {
      const li = document.createElement("li");
      const key = document.createElement("span");
      key.className = "key";
      key.style.setProperty("--k", s.colour);
      li.append(key, document.createTextNode(s.pair));
      legend.append(li);
    });
  }

  function render() {
    if (!state) return;
    document.querySelectorAll("[data-bind]").forEach((node) => {
      const v = get(state, node.dataset.bind);
      let text;
      if (node.dataset.format === "num") text = compact(v);
      else if (node.dataset.format === "pct") text = v == null ? "—" : `${Math.round(v)}%`;
      else if (node.dataset.format === "tightest") text = v ? `tightest: ${v}` : "no day limits set";
      else text = v == null ? "—" : String(v);
      if (node.textContent !== text) {
        node.textContent = text;
        node.classList.add("is-updated");
        setTimeout(() => node.classList.remove("is-updated"), 600);
      }
    });
    document.querySelectorAll("[data-delta]").forEach((node) => {
      const v = get(state, node.dataset.delta);
      node.replaceChildren();
      if (v == null) {
        const s = document.createElement("span");
        s.className = "faint";
        s.textContent = "No data for yesterday";
        node.append(s);
      } else {
        const s = document.createElement("span");
        s.className = v >= 0 ? "up" : "down";
        s.textContent = `${v >= 0 ? "+" : ""}${Math.round(v)}%`;
        node.append(s, document.createTextNode(" vs this time yesterday"));
      }
    });
    document.querySelectorAll("[data-spark]").forEach((svg) => spark(svg, get(state, svg.dataset.spark)));
    const box = document.querySelector('[data-chart="by_pair"]');
    if (box) chart(box, document.querySelector('[data-legend="by_pair"]'), state.by_pair || {});
    const lite = document.querySelector("[data-bind-litellm]");
    if (lite && state.litellm) {
      const up = state.litellm.up;
      lite.className = "chip " + (up === false ? "chip-pulled" : up ? "chip-serving" : "chip-nokey");
      lite.lastChild.textContent = up === false ? "LiteLLM unreachable" : up ? "LiteLLM up" : "LiteLLM checking";
    }
  }

  // the label changes only when live turns stale or back (it is a status region: one announcement each way)
  function tick() {
    const u = document.querySelector("[data-updated]");
    if (!u) return;
    const stale = Date.now() - lastOk > 15000;
    const text = stale ? `paused · last update ${clock({ second: "2-digit" }).format(new Date(lastOk))}` : "live";
    if (u.textContent !== text) u.textContent = text;
    u.classList.toggle("is-stale", stale);
  }

  async function refresh() {
    if (document.hidden) return;
    try {
      const r = await fetch("/api/state", { headers: { accept: "application/json" }, credentials: "same-origin" });
      if (r.status === 401) { location.href = "/login"; return; }
      if (!r.ok) return;
      state = await r.json();
      lastOk = Date.now();
      render();
    } catch (_) { /* keep the last numbers; the "updated" label shows their age */ }
  }

  document.querySelectorAll("[data-range]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.range === range));
    b.addEventListener("click", () => {
      range = b.dataset.range;
      const url = new URL(location.href);
      if (range === "48h") url.searchParams.set("range", "48h"); else url.searchParams.delete("range");
      history.replaceState(null, "", url);
      document.querySelectorAll("[data-range]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      render();
    });
  });

  // Calls: a filter applies as soon as it changes (the Filter button stays for a page without script)
  document.querySelectorAll("form[data-autosubmit] select").forEach((s) => s.addEventListener("change", () => s.form.requestSubmit()));

  const initial = document.getElementById("initial-state");
  if (initial) {
    try { state = JSON.parse(initial.textContent); render(); } catch (_) { /* fetched below */ }
  }
  if (document.querySelector("[data-bind], [data-spark], [data-chart]")) {
    setInterval(refresh, 5000);
    setInterval(tick, 1000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  }
})();
