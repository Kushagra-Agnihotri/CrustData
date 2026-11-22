/* Company Books: our internal records, read-only. */
(function () {
  const { h, api, inr } = App;
  const TABS = [["vendors", "Vendor master"], ["pos", "POs and goods receipts"], ["receivables", "Receivables"], ["bank", "Bank position"]];

  async function tabBody(tab, focus) {
    if (tab === "vendors") {
      const vs = await api("/books/vendors");
      return h`<div class="panel table-wrap"><table><thead><tr><th>Vendor</th><th>GSTIN</th><th>PAN</th><th>MSME</th><th>Agreement</th><th>Domain</th><th>Phone on file</th><th class="num">Credit days</th><th>Discount</th><th>Since</th></tr></thead>
        <tbody>${vs.map((v) => h`<tr><td>${App.vendorLink(v.id, v.legal_name)}${v.is_critical ? h`<span class="sub">critical supplier</span>` : ""}</td><td class="mono">${v.gstin}</td><td class="mono">${v.pan}</td>
          <td>${v.msme_category}${v.is_trader ? " · trader" : ""}</td><td>${v.has_written_agreement ? "written" : "none"}</td><td>${v.domain}</td><td class="nowrap">${v.phone_on_file}</td>
          <td class="num">${v.credit_days}</td><td>${v.discount_pct ? `${v.discount_pct}/${v.discount_days}` : "—"}</td><td class="nowrap">${App.date(v.relationship_since)}</td></tr>`)}</tbody></table></div>`;
    }
    if (tab === "pos") {
      const pos = await api("/books/pos");
      return h`<div class="stack">${pos.map((p) => h`<div class="panel panel-pad" id="po-${p.po_number}" ${focus === p.po_number ? 'data-focus="1"' : ""}>
        <div class="section-head"><h3>${p.po_number} · ${App.vendorLink(p.vendor_id, p.vendor)}</h3><span class="small muted">raised ${App.date(p.po_date)} · ${p.status.replace(/_/g, " ").toLowerCase()}</span></div>
        <table><thead><tr><th>#</th><th>Item</th><th class="num">Ordered</th><th class="num">Received (GRN)</th><th class="num">Invoiced</th><th class="num">Rate</th><th class="num">GST</th></tr></thead>
        <tbody>${p.lines.map((l) => h`<tr><td>${l.line_no}</td><td>${l.item}</td><td class="num">${App.qty(l.qty_ordered)} ${l.unit || ""}</td><td class="num">${App.qty(l.qty_received_grn)}</td><td class="num">${App.qty(l.qty_invoiced)}</td><td class="num">${inr(l.unit_price, true)}</td><td class="num">${l.gst_rate}%</td></tr>`)}</tbody></table>
        <p class="small muted" style="margin-top:10px">${p.grns.length ? `Goods receipts: ${p.grns.map((g) => `${g.grn_number} (${App.qty(g.qty)} on ${App.date(g.received_date)})`).join(", ")}` : "No goods receipt yet."}
          ${p.invoices.length ? h` · Invoices: ${p.invoices.map((i, k) => h`${k ? ", " : ""}<a href="#/invoices/${i.id}">${i.invoice_no_raw}</a>`)}` : ""}</p>
      </div>`)}</div>`;
    }
    if (tab === "receivables") {
      const rs = await api("/books/receivables");
      return h`<div class="panel table-wrap"><table><thead><tr><th>Customer</th><th>Invoice</th><th class="num">Amount</th><th>Due</th><th class="num">Confidence</th><th>Status</th></tr></thead>
        <tbody>${rs.map((r) => h`<tr><td>${r.customer}</td><td>${r.invoice_no}</td><td class="num">${inr(r.amount)}</td><td>${App.date(r.due_date)}</td><td class="num">${r.confidence !== null ? Math.round(r.confidence * 100) + "%" : "—"}</td><td>${r.status.toLowerCase()}</td></tr>`)}</tbody></table></div>
        <p class="small muted" style="margin-top:10px">Only open receipts due within the run's horizon with confidence of 70% or more count towards spendable cash, at amount × confidence.</p>`;
    }
    const b = await api("/books/bank");
    const c = b.cash;
    return h`<div class="grid-even"><div class="panel panel-pad"><h3 style="margin-bottom:12px">Bank position</h3>
        <dl class="fields"><dt>Balance</dt><dd class="tnum"><strong>${inr(c.bank_balance)}</strong></dd><dt>As of</dt><dd>${App.dateTime(c.balance_as_of)}</dd><dt>Source</dt><dd>${c.balance_source}</dd></dl>
        <h3 style="margin:18px 0 8px">Safety buffer, never spent</h3>
        <table><tbody>${c.buffer_items.map((x) => h`<tr><td>${x.label}</td><td class="num">${inr(x.amount)}</td></tr>`)}<tr><td><strong>Total</strong></td><td class="num"><strong>${inr(c.safety_buffer)}</strong></td></tr></tbody></table></div>
      <div class="panel panel-pad"><h3 style="margin-bottom:12px">Expected receipts</h3>
        ${c.receipts_counted.map((r) => h`<p class="small" style="margin-bottom:6px">Counted: ${r.customer} ${r.invoice_no}, ${inr(r.amount)} due ${App.date(r.due_date)} × ${Math.round(r.confidence * 100)}% = <strong>${inr(r.counted)}</strong></p>`)}
        ${c.receipts_not_counted.map((r) => h`<p class="small muted" style="margin-bottom:6px">Not counted: ${r.customer} ${r.invoice_no}, ${inr(r.amount)}: ${r.why_not}</p>`)}
        <hr class="rule"><p>Spendable = ${inr(c.bank_balance)} − ${inr(c.safety_buffer)} + ${inr(c.expected_receipts)} = <strong class="tnum">${inr(c.spendable)}</strong></p></div></div>`;
  }

  App.route("books", async (args) => {
    const tab = TABS.some((t) => t[0] === args[0]) ? args[0] : "vendors";
    const focus = args[1];
    App.render(h`<div class="page-head"><div><h1>Company books</h1><p>Our internal records (synthetic test data). The desk checks every invoice against these.</p></div>
      <span class="src"><span class="tag ours">our records</span></span></div>
      <div class="tabs" role="tablist">${TABS.map(([k, l]) => h`<button role="tab" aria-selected="${String(k === tab)}" data-tab="${k}">${l}</button>`)}</div>
      <div id="tabBody"><div class="loading">Loading…</div></div>`);
    App.main().querySelectorAll("[data-tab]").forEach((b) => (b.onclick = () => (location.hash = `#/books/${b.dataset.tab}`)));
    document.getElementById("tabBody").innerHTML = String(await tabBody(tab, focus));
    const f = App.main().querySelector("[data-focus]");
    if (f) { setTimeout(() => { f.scrollIntoView({ block: "center" }); f.classList.add("flash"); }, 50); }
  });
})();
