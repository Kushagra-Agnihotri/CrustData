/* Home: one guided flow. Request → job description → live checklist → summary → final report. */
(function () {
  const { h, raw, api, post, inr, inrShort, icon, badge } = App;
  const S = (App.homeState = App.homeState || { text: null, job: null, phase: "request", error: null });

  function stepCls(n, cur) { return n < cur ? "done" : n === cur ? "active" : "pending"; }
  function marker(n, cur) { return n < cur ? icon("check") : raw(`<span class="tnum">${n}</span>`); }

  function jobView(job) {
    if (!job) return "";
    if (job.status === "refused") {
      return h`<div class="refusal" role="alert">
        <h3>A request can tighten the desk's rules, not loosen them</h3>
        <ul>${job.refusals.map((r) => h`<li><q>${r.phrase}</q>: ${r.reason}</li>`)}</ul>
        <p class="small" style="margin-top:10px">Nothing was run and no decision changed. Edit the request, or use the approval screens, where your name is logged.</p>
      </div>`;
    }
    if (job.status === "needs_clarification") {
      return h`<div class="question"><strong>One question before I start.</strong> ${job.question}</div>`;
    }
    const vf = job.vendor_filter || {};
    return h`<div class="panel panel-pad">
      <dl class="job">
        <dt>Scope</dt><dd>${job.scope}</dd>
        <dt>I will</dt><dd><ul>${job.actions.map((a) => h`<li>${a}</li>`)}</ul></dd>
        <dt>Spend limit</dt><dd>${job.spend_cap ? h`<strong class="tnum">${inr(job.spend_cap)}</strong> for this run, or less if the cash we have is lower` : "The cash we have (bank balance − safety buffer + confident receipts)"}</dd>
        <dt>Vendors</dt><dd>${vf.include_ids?.length || vf.exclude_ids?.length || vf.msme_only ? job.understood.filter((u) => !u.startsWith("Spend")).join("; ") : "All approved vendors"}</dd>
        <dt>Approval</dt><dd>Required. No money moves until a named person approves the payment run.</dd>
      </dl>
      <p class="small muted" style="margin-top:12px">Read by ${job.method}. The rules decide what is payable; the request can only narrow them.</p>
    </div>`;
  }

  function checklist(st, planned) {
    const files = st.inbox_files || 0, docs = st.documents || 0, decided = st.decided || 0;
    const readDone = docs >= files && files > 0;
    const checkDone = !st.running && docs > 0 && decided >= docs;
    const items = [
      { label: `Reading documents from the accounts inbox`, detail: `${Math.min(docs, files)} of ${files}`, state: readDone ? "done" : "now" },
      { label: "Checking each invoice: PO and goods receipt, GSTIN, vendor, bank account, duplicates", detail: `${decided} of ${docs || files} decided`, state: checkDone ? "done" : readDone ? "now" : "wait" },
      { label: "Planning cash: who gets paid, how much, and why", detail: planned ? "proposed" : "", state: planned ? "done" : checkDone ? "now" : "wait" },
    ];
    return h`<ul class="checklist">${items.map((i) => h`<li class="${i.state}"><span class="st">${i.state === "done" ? icon("check") : i.state === "now" ? raw('<span class="spin"></span>') : icon("dot")}</span><span>${i.label}</span><span class="small muted tnum">${i.detail}</span></li>`)}</ul>
      ${st.error ? h`<p class="err" style="margin-top:8px">The run stopped: ${st.error}</p>` : ""}`;
  }

  async function summaryView(sum, run, invs) {
    const held = invs.filter((i) => i.decision === "HOLD"), blocked = invs.filter((i) => i.decision === "BLOCK");
    const spendable = run?.cash?.spendable;
    const status = run?.status;
    const need = status === "PROPOSED";
    const attention = [...blocked, ...held].slice(0, 8);
    return h`
      <p class="summary-line">
        <a class="ok" href="#/invoices/f/PAY_ELIGIBLE">${sum.eligible} eligible</a>,
        <a class="held" href="#/invoices/f/HOLD">${sum.held} held</a>,
        <a class="block" href="#/invoices/f/BLOCK">${sum.blocked} blocked</a>.
        ${run ? h`<a href="#/run">${inr(run.allocated)} proposed</a> ${run.spend_cap && run.spend_cap <= (run.cash.computed_spendable || 0) ? `within your ${inr(run.spend_cap)} limit` : `from ${inr(spendable)} we can spend this week`}.` : ""}
        ${need ? "Your approval is needed." : status === "DONE" ? "Paid and reconciled." : status ? `Run ${status.toLowerCase()}.` : ""}
      </p>
      ${attention.length ? h`<div class="panel panel-pad attention">
        <div class="section-head"><h3>Held or blocked, with the reason</h3><a class="small" href="#/invoices">All ${invs.length} documents</a></div>
        ${attention.map((i) => h`<div class="attention-item">
          ${badge(i.decision)}
          <div><a href="#/invoices/${i.id}"><strong>${i.vendor || i.vendor_name_raw || "Unknown sender"}</strong></a>
            <span class="muted small"> · ${i.invoice_no || (i.doc_kind === "bank_change" ? "bank-change email" : i.file)}</span>
            <div class="small" style="color:var(--ink-2);margin-top:2px">${i.top_reason ? i.top_reason.message : ""}</div></div>
          <span class="num tnum small">${i.total ? inr(i.total) : ""}</span>
        </div>`)}
      </div>` : ""}
      ${need ? h`<div class="row" style="margin-top:18px"><a class="btn primary" href="#/run">Review and approve the payment run ${icon("arrow")}</a></div>` : ""}`;
  }

  function reportView(sum, run, invs) {
    if (!run || run.status !== "DONE") return "";
    const paid = run.instalments.filter((x) => x.status === "PAID");
    const vendors = new Set(paid.map((x) => x.vendor_id));
    const stillHeld = invs.filter((i) => i.decision === "HOLD" || i.decision === "BLOCK");
    const cost = sum.cost || {};
    return h`
      <div class="report-figs">
        <div><div class="fig">${inr(run.paid)}</div><div class="lbl">paid in ${paid.length} instalment${paid.length === 1 ? "" : "s"} to ${vendors.size} vendor${vendors.size === 1 ? "" : "s"}, each exactly once</div></div>
        <div><div class="fig">${stillHeld.length}</div><div class="lbl">documents still held or blocked, waiting on you</div></div>
        <div><div class="fig">₹${(cost.inr || 0).toFixed(2)}</div><div class="lbl">cost of this run · ${cost.calls || 0} calls · ${(cost.credits || 0).toFixed(1)} credits</div></div>
      </div>
      ${run.deferred?.length ? h`<p class="small muted" style="margin-top:10px">${run.deferred.length} eligible invoice${run.deferred.length === 1 ? "" : "s"} roll to next week's run. <a href="#/run">See why</a>.</p>` : ""}
      <div class="row" style="margin-top:14px"><a class="btn" href="#/ledger">Open the audit ledger</a><a class="btn ghost" href="#/run">Payment run detail</a></div>`;
  }

  App.route("home", async function () {
    if (S.text === null) S.text = App.cfg.default_request || "";
    const [st, sum] = await Promise.all([api("/run/status"), api("/summary")]);
    const hasDocs = st.documents > 0;
    let run = null, invs = [];
    if (hasDocs) [run, invs] = await Promise.all([api("/runs/current"), api("/invoices")]);
    const planned = !!run;
    // which step are we on?
    let cur = 1;
    if (S.job && S.job.status === "ok") cur = 2;
    if (hasDocs || st.running) cur = 3;
    if (planned && !st.running) cur = 4;
    if (run && run.status === "DONE") cur = 5;
    const reqLocked = hasDocs || st.running;

    App.render(h`
      <div class="page-head"><div>
        <h1>This week's payables</h1>
        <p>Hand over the week's invoices and say what you want. The desk checks every one against our records and live sources, plans payments around our cash, and waits for your approval.</p>
      </div></div>
      <div class="home">
        <div class="flow-step ${stepCls(1, cur)}"><div class="marker">${marker(1, cur)}</div><div class="body">
          <h2>Your request</h2>
          <form id="reqForm" class="request-box stack">
            <label class="field"><span class="sr" style="position:absolute;left:-9999px">Request to the desk</span>
              <textarea id="reqText" rows="4" ${reqLocked ? "readonly" : ""}>${S.text}</textarea></label>
            ${reqLocked ? h`<p class="small muted">This week's run has started from this request. <button type="button" class="btn ghost sm" id="resetBtn">Reset test data and start over</button></p>`
              : h`<div class="row"><button class="btn primary" type="submit" id="readBtn">Read my request</button>
                <span class="small muted">Try “Don't spend more than ₹15 lakh this week”, or “only MSME vendors”.</span></div>`}
          </form>
          <div id="jobOut" style="margin-top:14px">${jobView(S.job)}</div>
        </div></div>

        <div class="flow-step ${stepCls(2, cur)}"><div class="marker">${marker(2, cur)}</div><div class="body">
          <h2>What the desk will do</h2>
          ${cur === 2 ? h`<p class="muted" style="margin-bottom:12px">${st.inbox_files} documents are waiting in the accounts inbox.</p>
            <button class="btn primary" id="startBtn">Start on this week's inbox</button>`
            : cur < 2 ? h`<p class="muted">Read your request first. I'll show what I understood before anything runs.</p>`
            : h`<p class="muted">${S.job?.spend_cap ? `Working within ${inr(S.job.spend_cap)}. ` : ""}Approval required before any payment.</p>`}
        </div></div>

        <div class="flow-step ${stepCls(3, cur)}"><div class="marker">${marker(3, cur)}</div><div class="body">
          <h2>Working through the inbox</h2>
          <div id="check">${cur >= 3 ? checklist(st, planned) : h`<p class="muted">Every document is read, matched and checked. You can watch it here.</p>`}</div>
        </div></div>

        <div class="flow-step ${stepCls(4, cur)}"><div class="marker">${marker(4, cur)}</div><div class="body">
          <h2>What's safe to pay</h2>
          <div id="sum">${cur >= 4 ? raw(String(await summaryView(sum, run, invs))) : h`<p class="muted">Eligible, held and blocked, each with its reason, and the proposed payment run.</p>`}</div>
        </div></div>

        <div class="flow-step ${stepCls(5, cur)}"><div class="marker">${marker(5, cur)}</div><div class="body">
          <h2>Final report</h2>
          ${cur >= 5 ? reportView(sum, run, invs) : h`<p class="muted">After you approve, payments go out once each. What was paid, what is still held and why, and what the run cost.</p>`}
        </div></div>
      </div>`);

    const form = document.getElementById("reqForm");
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (reqLocked) return;
      S.text = document.getElementById("reqText").value;
      await App.busy(document.getElementById("readBtn"), async () => { S.job = await post("/request", { text: S.text }); });
      App.routes.home();
    });
    document.getElementById("reqText").addEventListener("input", (e) => { S.text = e.target.value; });
    const reset = document.getElementById("resetBtn");
    if (reset) reset.addEventListener("click", async () => {
      if (!confirm("Reset the test database? This clears this week's decisions, payment run and ledger, and reloads our records.")) return;
      await App.busy(reset, () => post("/reset"));
      S.job = null; App.toast("Test data reset."); App.routes.home();
    });
    const start = document.getElementById("startBtn");
    if (start) start.addEventListener("click", async () => {
      await App.busy(start, () => post("/run"));
      App.routes.home();
    });

    // live checklist: poll while running, then propose the run with the request's cap
    if (st.running || (hasDocs && !planned)) {
      const poll = async () => {
        const s = await api("/run/status");
        const el = document.getElementById("check"); if (el) el.innerHTML = String(checklist(s, false));
        if (!s.running && !s.error) {
          App.clearTimers();
          try {
            const job = S.job && S.job.status === "ok" ? S.job : null;
            await post("/runs/propose", job ? { spend_cap: job.spend_cap, vendor_filter: job.vendor_filter } : {});
          } catch (e) { App.toast(e.message, true); }
          setTimeout(() => App.routes.home(), 350);
        }
      };
      App.every(450, poll);
    }
  });
})();
