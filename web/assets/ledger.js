/* Audit Ledger: events, calls and cost, payout reconciliation, auditor export. */
(function () {
  const { h, api, inr, icon } = App;
  const TABS = [["recon", "Payout reconciliation"], ["events", "Events"], ["calls", "Calls and cost"]];

  App.route("ledger", async (args) => {
    const tab = TABS.some((t) => t[0] === args[0]) ? args[0] : "recon";
    const c = await api("/ledger/calls");
    const t = c.totals;
    const docs = c.per_document.length || 1;
    const recOk = c.reconciliation.every((r) => r.paid_instalments === r.provider_payouts);
    let body;
    if (tab === "recon") {
      body = h`<div class="note ${recOk ? "ok" : "block"}" style="margin-bottom:14px">${recOk ? "No rupee paid twice. Every paid instalment maps to exactly one bank payout, matched by its idempotency key." : "Mismatch: some paid instalments don't map to exactly one payout. Investigate before the next run."}</div>
        <div class="panel table-wrap"><table><thead><tr><th>Vendor and invoice</th><th class="num">Total</th><th class="num">Paid</th><th class="num">Outstanding</th><th class="num">Paid instalments</th><th class="num">Bank payouts</th><th>Check</th></tr></thead>
        <tbody>${c.reconciliation.map((r) => h`<tr><td>${App.vendorLink(r.vendor_id, r.vendor)}<span class="sub"><a href="#/invoices/${r.id}">${r.invoice_no_raw}</a></span></td>
          <td class="num">${inr(r.total)}</td><td class="num">${inr(r.paid)}</td><td class="num">${inr(r.outstanding)}</td><td class="num">${r.paid_instalments}</td><td class="num">${r.provider_payouts}</td>
          <td>${r.paid_instalments === r.provider_payouts ? h`<span class="badge ok">${icon("check")}Matches</span>` : h`<span class="badge block">${icon("x")}Mismatch</span>`}</td></tr>`)}</tbody></table></div>`;
    } else if (tab === "events") {
      const ev = await api("/ledger/events");
      body = h`<p class="caption">Append-only: the database refuses UPDATE and DELETE on events and facts. Newest first.</p>
        <div class="panel table-wrap"><table><thead><tr><th>#</th><th>When</th><th>What</th><th>Change</th><th>By</th><th>Detail</th></tr></thead>
        <tbody>${ev.map((e) => h`<tr><td class="num">${e.seq}</td><td class="nowrap tnum">${App.dateTime(e.at)}</td><td>${e.entity_type.replace(/_/g, " ")}</td>
          <td class="nowrap">${e.from_state ? `${e.from_state} → ` : ""}<strong>${e.to_state}</strong></td><td>${e.actor}</td>
          <td class="small muted">${Object.keys(e.detail || {}).length ? JSON.stringify(e.detail).slice(0, 140) : ""}</td></tr>`)}</tbody></table></div>`;
    } else {
      body = h`<div class="grid-2"><div class="panel table-wrap"><table><thead><tr><th>When</th><th>Tool</th><th>Endpoint</th><th>Mode</th><th class="num">Credits</th><th class="num">₹</th><th class="num">ms</th><th>OK</th></tr></thead>
        <tbody>${c.calls.map((x) => h`<tr><td class="nowrap tnum">${App.time(x.at)}</td><td>${x.tool}</td><td class="small mono">${(x.endpoint || "").slice(0, 48)}</td>
          <td>${x.mode === "live" ? h`<span class="src"><span class="tag live">live</span></span>` : h`<span class="src"><span class="tag mock">${x.mode || "mock"}</span></span>`}</td>
          <td class="num">${x.credits || 0}</td><td class="num">${(x.est_cost_inr || 0).toFixed(2)}</td><td class="num">${x.latency_ms}</td><td>${x.ok ? "yes" : h`<span class="err">${x.error || "failed"}</span>`}</td></tr>`)}</tbody></table></div>
        <div class="panel table-wrap"><table><thead><tr><th>Document</th><th class="num">Calls</th><th class="num">Credits</th><th class="num">₹</th></tr></thead>
        <tbody>${c.per_document.map((d) => h`<tr><td><a href="#/invoices/${d.id}">${d.case_id || (d.source_path || "").split("/").pop()}</a></td><td class="num">${d.calls}</td><td class="num">${d.credits}</td><td class="num">${d.est_inr.toFixed(2)}</td></tr>`)}</tbody></table></div></div>`;
    }
    App.render(h`
      <div class="page-head"><div><h1>Audit ledger</h1><p>Every decision, call and payment, traced to a source with a date. Replayable line by line.</p></div>
        <a class="btn" href="/api/ledger/export" download="auditor_export.csv">${icon("download")}Auditor CSV</a></div>
      <div class="report-figs" style="margin-bottom:24px;grid-template-columns:repeat(4,minmax(0,1fr))">
        <div><div class="fig">${t.n}</div><div class="lbl">external and model calls · ${t.fails || 0} failed</div></div>
        <div><div class="fig">${Number(t.credits).toFixed(1)}</div><div class="lbl">Crustdata credits · limit ${c.credit_limit} per run</div></div>
        <div><div class="fig">₹${Number(t.inr).toFixed(2)}</div><div class="lbl">estimated cost · ₹${(t.inr / docs).toFixed(2)} per document</div></div>
        <div><div class="fig">${recOk ? "Once" : "Check"}</div><div class="lbl">${recOk ? "each payment, reconciled with the bank" : "reconciliation mismatch"}</div></div>
      </div>
      <div class="tabs" role="tablist">${TABS.map(([k, l]) => h`<button role="tab" aria-selected="${String(k === tab)}" data-tab="${k}">${l}</button>`)}</div>
      ${body}`);
    App.main().querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => (location.hash = `#/ledger/${b.dataset.tab}`)));
  });
})();
