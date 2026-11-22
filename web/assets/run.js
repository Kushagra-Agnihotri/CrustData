/* Payment Run: the hero screen and the approval step. */
(function () {
  const { h, raw, api, post, inr, icon } = App;
  const edits = {}; // invoice_id -> paise

  function cashBar(run) {
    const c = run.cash;
    const total = c.bank_balance + c.expected_receipts;
    const computed = c.computed_spendable ?? c.spendable;
    const pct = (v) => Math.max(0, (v / total) * 100).toFixed(2) + "%";
    const alloc = Math.min(run.allocated, c.spendable);
    const free = Math.max(0, c.spendable - alloc);
    const capcut = Math.max(0, computed - c.spendable);
    const capped = capcut > 0;
    return h`<div class="panel cash">
      <div class="section-head"><h2>Cash for this run</h2><a class="small" href="#/books/bank">Where these numbers come from</a></div>
      <div class="cash-eq">
        <div class="term"><span class="lbl">Bank balance</span><span class="amt">${inr(c.bank_balance)}</span></div>
        <span class="op">−</span>
        <div class="term"><span class="lbl">Safety buffer, never spent</span><span class="amt">${inr(c.safety_buffer)}</span></div>
        <span class="op">+</span>
        <div class="term"><span class="lbl">Confident receipts (${c.receipts_counted.length})</span><span class="amt">${inr(c.expected_receipts)}</span></div>
        <span class="op">=</span>
        ${capped ? h`<div class="term capped"><span class="lbl">Cash would allow</span><span class="amt">${inr(computed)}</span></div>
          <div class="term result"><span class="lbl">Spendable, capped by your request</span><span class="amt">${inr(c.spendable)}</span></div>`
          : h`<div class="term result"><span class="lbl">Spendable</span><span class="amt">${inr(c.spendable)}</span></div>`}
      </div>
      <div class="bar" role="img" aria-label="${`Safety buffer ${inr(c.safety_buffer)}, allocated ${inr(alloc)}, unallocated ${inr(free)}${capped ? `, held back by your cap ${inr(capcut)}` : ""}`}">
        <span class="buffer" style="width:${pct(c.safety_buffer)}"></span>
        <span class="alloc" style="width:${pct(alloc)}"></span>
        <span class="free" style="width:${pct(free)}"></span>
        ${capped ? raw(`<span class="capcut" style="width:${pct(capcut)}"></span>`) : ""}
      </div>
      <div class="bar-legend">
        <span><i style="background:#d9d4c7"></i>Safety buffer ${inr(c.safety_buffer)}</span>
        <span><i style="background:var(--accent)"></i>Allocated now ${inr(alloc)}</span>
        <span><i style="background:#c4d0e4"></i>Spendable, not allocated ${inr(free)}</span>
        ${capped ? h`<span><i style="background:#ece8de;border:1px solid var(--rule-strong)"></i>Held back by your cap ${inr(capcut)}</span>` : ""}
        ${run.paid ? h`<span><strong>${inr(run.paid)}</strong> paid so far</span>` : ""}
      </div>
    </div>`;
  }

  const STATUS = { PAID: ["check", "Paid"], FAILED: ["x", "Failed"], SUBMITTED: ["send", "Sent to bank"], INTENT_WRITTEN: ["clock", "Sending"],
    APPROVED: ["check", "Approved"], PROPOSED: ["dot", "Proposed"], CANCELLED: ["x", "Cancelled"] };

  function row(x, editable) {
    const j = x.justification || {};
    const edited = edits[x.invoice_id] !== undefined && edits[x.invoice_id] !== x.amount;
    const max = x.inv_out ?? x.total;
    const [ico, word] = STATUS[x.status] || ["dot", x.status];
    return h`<tr class="alloc-row" id="inst-${x.id}">
      <td style="width:26%">${App.vendorLink(x.vendor_id, x.vendor)}
        <span class="sub"><a href="#/invoices/${x.invoice_id}">${x.invoice_no_raw}</a> · total ${inr(x.total)}</span>
        ${x.instalment_no > 1 ? h`<span class="sub">Instalment ${x.instalment_no}</span>` : ""}</td>
      <td>
        <div class="headline">${j.headline || ""}${x.approver_override ? h` <span class="edited">· set by approver</span>` : ""}</div>
        <ul class="why-list">${(j.reasons || []).filter((r) => r.code !== "SCORES").map((r) => h`<li>${r.text}</li>`)}</ul>
        ${j.remaining ? h`<div class="rest">Remaining ${inr(j.remaining.amount)} planned ${App.date(j.remaining.planned_date)}, ${j.remaining.because}.</div>` : ""}
      </td>
      <td class="num" style="width:230px">
        ${editable ? h`<div class="amount-edit" style="margin-left:auto">
            <label class="field"><span class="sr" style="position:absolute;left:-9999px">Amount now for ${x.vendor} ${x.invoice_no_raw}</span>
            <input type="number" min="0" max="${Math.floor(max / 100)}" step="1000" value="${Math.round((edits[x.invoice_id] ?? x.amount) / 100)}" data-inv="${x.invoice_id}" data-orig="${x.amount}"></label>
            <input type="range" min="0" max="${Math.floor(max / 100)}" step="1000" value="${Math.round((edits[x.invoice_id] ?? x.amount) / 100)}" data-range="${x.invoice_id}" aria-label="Adjust amount for ${x.vendor}">
            <div class="hint">of ${inr(max)} outstanding<br>min instalment ${inr(x.min)}</div>
            ${edited ? h`<div class="edited">Changed. Recompute to re-flow the rest.</div>` : ""}
          </div>`
        : h`<div style="font-weight:600;font-size:1.05rem">${inr(x.amount)}</div>
            <div class="status ${x.status}" style="justify-content:flex-end">${icon(ico)}${word}</div>
            ${x.provider_ref ? h`<div class="small muted">ref ${x.provider_ref}</div>` : ""}
            ${x.last_error ? h`<div class="small err">${x.last_error}</div>` : ""}`}
      </td></tr>`;
  }

  App.route("run", async function () {
    let run = await api("/runs/current");
    if (!run) {
      App.render(h`<div class="page-head"><div><h1>Payment run</h1></div></div>
        <div class="panel empty"><h3>No payment run yet</h3><p>Run the desk on this week's inbox from Home first, or plan a run from the invoices already eligible.</p>
        <div class="row" style="justify-content:center;margin-top:14px"><a class="btn" href="#/">Go to Home</a><button class="btn primary" id="planBtn">Plan this week's run</button></div></div>`);
      document.getElementById("planBtn").onclick = async (e) => { await App.busy(e.currentTarget, () => post("/runs/propose", {})); App.routes.run(); };
      return;
    }
    const editable = run.status === "PROPOSED";
    const tiers = [0, 1, 2].map((t) => ({ t, rows: run.instalments.filter((x) => x.tier === t) })).filter((g) => g.rows.length);
    const nEdits = Object.keys(edits).filter((k) => run.instalments.some((x) => x.invoice_id === k && x.amount !== edits[k])).length;

    App.render(h`
      <div class="page-head"><div>
        <h1>Payment run · ${App.date(run.run_date)}</h1>
        <p>Paid according to the cash we have: legal MSME dues and critical suppliers first, then discounts worth taking, then by priority in instalments. Every rupee has its reason.</p>
      </div>
      <div class="row"><span class="chip">${run.status === "PROPOSED" ? "Waiting for approval" : run.status === "DONE" ? "Done" : run.status.charAt(0) + run.status.slice(1).toLowerCase()}</span>
        ${run.approved_by ? h`<span class="chip">Approved by ${run.approved_by} · ${App.dateTime(run.approved_at)}</span>` : ""}</div></div>

      ${cashBar(run)}

      ${run.alerts?.length ? h`<div class="alerts" style="margin-top:18px">${run.alerts.map((a) => h`<div class="note ${a.level === "error" ? "block" : a.level === "warning" ? "held" : "info"}">${a.text}</div>`)}</div>` : ""}

      <section>
        <div class="section-head"><h2>Allocation</h2><p>${run.instalments.length} payment${run.instalments.length === 1 ? "" : "s"} · ${inr(run.allocated)}</p></div>
        <div class="panel table-wrap"><table>
          <thead><tr><th>Vendor and invoice</th><th>Why this amount</th><th class="num">Amount now</th></tr></thead>
          <tbody>${tiers.map((g) => h`<tr class="group"><td colspan="3">${App.TIER[g.t]} · ${inr(g.rows.reduce((s, x) => s + x.amount, 0))}</td></tr>${g.rows.map((x) => row(x, editable))}`)}</tbody>
        </table></div>
      </section>

      ${run.deferred?.length ? h`<section>
        <div class="section-head"><h2>Not paid in this run</h2><p>Eligible, but ₹0 now, and why</p></div>
        <div class="panel table-wrap"><table>
          <thead><tr><th>Vendor and invoice</th><th class="num">Outstanding</th><th>Why ₹0 now</th></tr></thead>
          <tbody>${run.deferred.map((d) => h`<tr><td><a href="#/invoices/${d.invoice_id}">${d.vendor}</a><span class="sub">${d.invoice_no}${d.tier !== null && d.tier !== undefined ? ` · ${App.TIER[d.tier]}` : ""}</span></td>
            <td class="num">${inr(d.outstanding)}</td><td class="reason-cell">${d.why}</td></tr>`)}</tbody>
        </table></div></section>` : ""}

      ${editable ? h`<form class="panel approve" id="approveForm">
          <button type="button" class="btn" id="recompute" ${nEdits ? "" : "disabled"}>${icon("refresh")}Recompute with my changes${nEdits ? ` (${nEdits})` : ""}</button>
          <span class="spacer"></span>
          <label class="field"><span class="sr" style="position:absolute;left:-9999px">Approver name</span><input type="text" id="approver" placeholder="Your name (approver)" autocomplete="name"></label>
          <label class="check"><input type="checkbox" id="reviewed"> I have reviewed every line</label>
          <button class="btn primary" type="submit" id="approveBtn">Approve run · ${inr(run.allocated)}</button>
          <span class="err" id="approveErr" style="flex-basis:100%;text-align:right"></span>
        </form>`
      : run.status === "APPROVED" || run.status === "EXECUTING" ? h`<div class="panel approve">
          <div><strong>Approved.</strong> <span class="muted">Each instalment is sent once with its own idempotency key, even through a crash.</span></div>
          <span class="spacer"></span><button class="btn primary" id="execBtn">Send approved payments (test mode)</button></div>`
      : run.status === "DONE" ? h`<div class="note ok" style="margin-top:24px">Run complete. Every instalment went out exactly once. Remaining balances roll to next week's run. <a href="#/ledger">Reconcile in the audit ledger</a> · <a href="#/">Final report</a></div>` : ""}
    `);

    // wiring
    App.main().querySelectorAll("input[data-inv]").forEach((inp) => {
      const rng = App.main().querySelector(`input[data-range="${inp.dataset.inv}"]`);
      const set = (v) => { edits[inp.dataset.inv] = Math.max(0, Math.round(Number(v) || 0)) * 100; inp.value = rng.value = Math.round(edits[inp.dataset.inv] / 100); markDirty(); };
      inp.addEventListener("change", () => set(inp.value));
      rng.addEventListener("input", () => { inp.value = rng.value; });
      rng.addEventListener("change", () => set(rng.value));
    });
    function markDirty() {
      const n = Object.keys(edits).filter((k) => run.instalments.some((x) => x.invoice_id === k && x.amount !== edits[k])).length;
      const b = document.getElementById("recompute");
      if (b) { b.disabled = !n; b.innerHTML = String(icon("refresh")) + "Recompute with my changes" + (n ? ` (${n})` : ""); }
    }
    const rc = document.getElementById("recompute");
    if (rc) rc.onclick = async () => {
      const overrides = {};
      for (const [k, v] of Object.entries(edits)) if (run.instalments.some((x) => x.invoice_id === k && x.amount !== v)) overrides[k] = v;
      await App.busy(rc, () => post("/runs/propose", { overrides, actor: "approver-edit" }));
      for (const k of Object.keys(edits)) delete edits[k];
      App.toast("Plan recomputed with your amounts."); App.routes.run();
    };
    const form = document.getElementById("approveForm");
    if (form) form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = document.getElementById("approveErr"); err.textContent = "";
      const pending = Object.keys(edits).some((k) => run.instalments.some((x) => x.invoice_id === k && x.amount !== edits[k]));
      if (pending) { err.textContent = "You changed amounts. Recompute first so the plan reflects them."; return; }
      const who = document.getElementById("approver").value.trim();
      if (!who) { err.textContent = "Enter your name. Approvals are logged against a named person."; return; }
      if (!document.getElementById("reviewed").checked) { err.textContent = "Tick “I have reviewed every line” to approve."; return; }
      try { await App.busy(document.getElementById("approveBtn"), () => post(`/runs/${run.id}/approve`, { approver: who, reviewed: true })); }
      catch (x) { err.textContent = x.message; return; }
      App.toast(`Run approved by ${who}.`); App.routes.run();
    });
    const ex = document.getElementById("execBtn");
    if (ex) ex.onclick = async () => {
      const r = await App.busy(ex, () => post(`/runs/${run.id}/execute`));
      App.toast(`${r.stats.paid} paid, ${r.stats.failed} failed${r.stats.recovered ? `, ${r.stats.recovered} recovered` : ""}.`); App.routes.run();
    };
  });
})();
