/* Payables Desk — core: API, formatting, icons, router. No dependencies, works offline. */
(function () {
  const App = (window.App = { routes: {}, cfg: null });

  // ---------- API
  App.api = async function (path, opts = {}) {
    const init = { method: opts.method || (opts.body ? "POST" : "GET"), headers: {} };
    if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    const r = await fetch("/api" + path, init);
    const ct = r.headers.get("content-type") || "";
    const data = ct.includes("json") ? await r.json() : await r.text();
    if (!r.ok) throw new Error((data && data.detail) || r.statusText || "Request failed");
    return data;
  };
  App.post = (path, body = {}) => App.api(path, { method: "POST", body });

  // ---------- escaping + tiny templating
  const esc = (App.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])));
  class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
  App.raw = (s) => new Raw(s);
  App.h = function (strings, ...vals) {
    let out = "";
    strings.forEach((s, i) => {
      out += s;
      if (i < vals.length) {
        const v = vals[i];
        if (v instanceof Raw) out += v.s;
        else if (Array.isArray(v)) out += v.map((x) => (x instanceof Raw ? x.s : esc(x))).join("");
        else if (v === null || v === undefined || v === false) out += "";
        else out += esc(v);
      }
    });
    return new Raw(out);
  };
  const h = App.h;

  // ---------- money (integer paise in, ₹ with Indian grouping out)
  const grp = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  const grp2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  App.inr = (paise, decimals = false) => {
    if (paise === null || paise === undefined) return "—";
    const neg = paise < 0; const r = Math.abs(paise) / 100;
    return (neg ? "−" : "") + "₹" + (decimals ? grp2.format(r) : grp.format(Math.round(r)));
  };
  App.inrShort = (paise) => {
    if (paise === null || paise === undefined) return "—";
    const r = paise / 100;
    if (Math.abs(r) >= 1e7) return "₹" + (r / 1e7).toFixed(2) + " Cr";
    if (Math.abs(r) >= 1e5) return "₹" + (r / 1e5).toFixed(2) + "L";
    return App.inr(paise);
  };
  App.qty = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 }));

  // ---------- dates (22 Nov 2026, 15:40 IST)
  const dFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Kolkata" });
  const dtFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
  const tFmt = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
  const parse = (s) => {
    if (!s) return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(s + "T12:00:00+05:30");
    const d = new Date(s); return isNaN(d) ? null : d;
  };
  App.date = (s) => { const d = parse(s); return d ? dFmt.format(d) : (s || "—"); };
  App.dateTime = (s) => { const d = parse(s); return d ? dtFmt.format(d).replace(",", "") : (s || "—"); };
  App.time = (s) => { const d = parse(s); return d ? tFmt.format(d) : ""; };
  App.daysLeft = (s) => {
    const d = parse(s); if (!d || !App.cfg) return null;
    const run = parse(App.cfg.run_date);
    return Math.round((d - run) / 86400000);
  };
  App.countdown = (s) => {
    const n = App.daysLeft(s); if (n === null) return "";
    return n > 1 ? `${n} days left` : n === 1 ? "1 day left" : n === 0 ? "due today" : `${-n} days overdue`;
  };

  // ---------- icons (authored, 1.75 stroke, 24 grid)
  const I = {
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    pause: '<circle cx="12" cy="12" r="9"/><path d="M10 9v6M14 9v6"/>',
    block: '<circle cx="12" cy="12" r="9"/><path d="M6 6l12 12"/>',
    okc: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.8 2.8L16 10"/>',
    chev: '<path d="M9 6l6 6-6 6"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    back: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    doc: '<path d="M7 3h7l5 5v13H7z"/><path d="M14 3v5h5M10 13h6M10 17h6"/>',
    search: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
    bank: '<path d="M4 10h16M5 10v8M9.5 10v8M14.5 10v8M19 10v8M3 20h18M12 3l9 5H3z"/>',
    phone: '<path d="M6 3h3l2 5-2.5 1.5a11 11 0 0 0 6 6L16 13l5 2v3a3 3 0 0 1-3 3A16 16 0 0 1 3 6a3 3 0 0 1 3-3z"/>',
    dot: '<circle cx="12" cy="12" r="3.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.5-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5L20 16M20 20v-4h-4"/>',
    edit: '<path d="M4 20h4L19 9l-4-4L4 16z"/>',
    send: '<path d="M4 12l16-8-6 16-2-7z"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
  };
  App.icon = (name, cls = "") => App.raw(`<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${I[name] || ""}</svg>`);

  // ---------- decisions: word + colour + icon, never colour alone
  const DEC = {
    PAY_ELIGIBLE: { cls: "ok", word: "Eligible", ico: "okc" },
    HOLD: { cls: "held", word: "Held", ico: "pause" },
    BLOCK: { cls: "block", word: "Blocked", ico: "block" },
  };
  App.dec = (d) => DEC[d] || { cls: "none", word: "Not decided", ico: "dot" };
  App.badge = (d, lg = false) => { const x = App.dec(d); return h`<span class="badge ${x.cls}${lg ? " lg" : ""}">${App.icon(x.ico)}${x.word}</span>`; };
  App.sevIcon = (sev) => (sev === "BLOCK" ? App.icon("block", "ico block") : sev === "HOLD" ? App.icon("pause", "ico held") : App.icon("okc", "ico ok"));
  App.TIER = { 0: "Tier 0 · must pay", 1: "Tier 1 · early-payment discount", 2: "Tier 2 · by priority" };

  // ---------- provenance: live vs recorded vs mock vs ours, always visible
  App.prov = function (f) {
    const p = f.provenance;
    const when = App.dateTime(f.retrieved_at);
    const label = p === "live" ? `live · retrieved ${when}` : p === "recorded" ? `live, recorded ${when}` : p === "mock" ? "mock data" :
      p === "document" ? "from the document" : "our records";
    const ref = (f.source_ref || "").length > 90 ? f.source_ref.slice(0, 90) + "…" : f.source_ref;
    return h`<div class="src"><span class="tag ${p}">${label}</span><span>${f.source_type}</span>${ref ? h`<span>· ${ref}</span>` : ""}${p === "mock" ? h`<span>· fixture ${when}</span>` : ""}</div>`;
  };
  App.factValue = (v) => {
    if (v === null || v === undefined) return "—";
    if (typeof v === "object") { const s = JSON.stringify(v); return s.length > 220 ? s.slice(0, 220) + "…" : s; }
    return String(v);
  };
  App.fact = (f) => h`<div class="fact ${f.provenance === "mock" ? "mock" : ""}"><div class="kv"><b>${f.key}</b> = ${App.factValue(f.value)}</div>${App.prov(f)}</div>`;

  App.vendorLink = (id, name) => (id ? h`<a href="#/vendors/${id}">${name}</a>` : h`${name || "—"}`);
  App.poLink = (po) => (po ? h`<a href="#/books/pos/${encodeURIComponent(po)}">${po}</a>` : "—");

  // ---------- toast
  let tt;
  App.toast = (msg, error = false) => {
    const el = document.getElementById("toast");
    el.textContent = msg; el.className = "toast show" + (error ? " error" : "");
    clearTimeout(tt); tt = setTimeout(() => (el.className = "toast" + (error ? " error" : "")), error ? 5200 : 3000);
  };

  // busy button helper
  App.busy = async (btn, fn) => {
    const old = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="spin"></span>' + old;
    try { return await fn(); } catch (e) { App.toast(e.message, true); throw e; } finally { btn.disabled = false; btn.innerHTML = old; }
  };

  // ---------- router
  App.route = (name, fn) => (App.routes[name] = fn);
  App.main = () => document.getElementById("main");
  App.render = (html) => { App.main().innerHTML = String(html); };
  let timers = [];
  App.every = (ms, fn) => { const t = setInterval(fn, ms); timers.push(t); return t; };
  App.clearTimers = () => { timers.forEach(clearInterval); timers = []; };

  async function navigate() {
    App.clearTimers();
    const hash = location.hash.replace(/^#\/?/, "");
    const [name, ...rest] = hash.split("/");
    const key = name || "home";
    document.querySelectorAll("[data-nav]").forEach((a) => { if (a.dataset.nav === key) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
    const fn = App.routes[key] || App.routes.home;
    App.render(h`<div class="loading">Loading…</div>`);
    try { await fn(rest.map(decodeURIComponent)); }
    catch (e) { App.render(h`<div class="panel empty"><h3>Couldn't load this page</h3><p>${e.message}</p></div>`); console.error(e); }
    if (!location.hash.includes("#keep")) window.scrollTo(0, 0);
  }

  App.start = async function () {
    try { App.cfg = await App.api("/config"); } catch (e) { App.cfg = { run_date: "2026-11-22" }; }
    const c = App.cfg;
    const chip = (label, mode) => `<span class="chip ${mode === "live" ? "live" : "mock"}" title="${label} source mode"><span class="dot"></span>${label} ${mode === "live" ? "live" : "mock"}</span>`;
    document.getElementById("modes").innerHTML =
      `<span class="chip">Run date ${App.date(c.run_date)}</span>` + chip("Crustdata", c.crustdata_mode) + chip("IFSC", c.ifsc_mode) +
      `<span class="chip">Payments ${esc(c.payment_provider || "mock")} · test</span>`;
    window.addEventListener("hashchange", navigate);
    navigate();
  };
})();
