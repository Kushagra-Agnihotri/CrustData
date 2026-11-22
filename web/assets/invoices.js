/* Invoice Desk: every document, its decision and top reason; detail with evidence and the PO match panel. */
(function () {
  const { h, raw, api, post, inr, icon, badge } = App;
  let filter = null;

  async function list(f) {
    if (f !== undefined) filter = f;
    const invs = await api("/invoices");
    const n = (d) => invs.filter((i) => i.decision === d).length;
    const shown = invs.filter((i) => !filter || i.decision === filter);
    const opts = [[null, "All", invs.length], ["PAY_ELIGIBLE", "Eligible", n("PAY_ELIGIBLE")], ["HOLD", "Held", n("HOLD")], ["BLOCK", "Blocked", n("BLOCK")]];
    App.render(h`
      <div class="page-head"><div><h1>Invoice desk</h1>
        <p>Every document from the inbox, the decision, and the reason. Open one to see the evidence, the purchase order it was matched against, and the original.</p></div>
        <div class="seg" role="group" aria-label="Filter by decision">${opts.map(([k, l, c]) => h`<button type="button" data-f="${k || ""}" aria-pressed="${String(filter === k)}">${l}<span class="n">${c}</span></button>`)}</div>
      </div>
      ${invs.length ? h`<div class="panel table-wrap"><table>
        <thead><tr><th>Decision</th><th>Vendor</th><th>Document</th><th>PO</th><th class="num">Total</th><th>Top reason</th></tr></thead>
        <tbody>${shown.map((i) => h`<tr class="clickable" tabindex="0" data-id="${i.id}">
          <td>${badge(i.decision)}</td>
          <td>${i.vendor ? h`<strong>${i.vendor}</strong>` : h`<span>${i.vendor_name_raw || "Unknown sender"}</span><span class="sub">not an approved vendor</span>`}</td>
          <td>${i.invoice_no || (i.doc_kind === "bank_change" ? "Bank-change email" : "—")}<span class="sub">${i.file}${i.invoice_date ? ` · ${App.date(i.invoice_date)}` : ""}</span></td>
          <td>${i.po_number || h`<span class="muted">—</span>`}</td>
          <td class="num">${i.total ? inr(i.total) : "—"}</td>
          <td class="reason-cell">${i.top_reason ? i.top_reason.message : h`<span class="muted">All checks passed</span>`}</td>
        </tr>`)}</tbody></table></div>`
      : h`<div class="panel empty"><h3>No documents yet</h3><p>Start the desk on this week's inbox from <a href="#/">Home</a>.</p></div>`}`);
    App.main().querySelectorAll(".seg button").forEach((b) => (b.onclick = () => { location.hash = b.dataset.f ? `#/invoices/f/${b.dataset.f}` : "#/invoices"; }));
    App.main().querySelectorAll("tr[data-id]").forEach((tr) => {
      const go = () => (location.hash = `#/invoices/${tr.dataset.id}`);
      tr.onclick = (e) => { if (!e.target.closest("a")) go(); };
      tr.onkeydown = (e) => { if (e.key === "Enter") go(); };
    });
  }

  function reasonsView(rs) {
    return h`${rs.map((r, idx) => h`<details class="reason" ${r.severity !== "OK" && idx < 3 ? "open" : ""}>
      <summary>${App.sevIcon(r.severity)}<div><div>${r.message}</div><div class="code">${r.code}${r.facts?.length ? ` · ${r.facts.length} source${r.facts.length > 1 ? "s" : ""}` : ""}</div></div>${icon("chev", "chev")}</summary>
      <div class="evidence">${r.facts?.length ? r.facts.map(App.fact)
        : h`<div class="fact"><div class="kv">${/can't verify|cannot verify/i.test(r.message) ? "No record settles this. Shown as “can't verify” rather than guessed." : "Worked out from the fields read from the document (right) and our vendor master."}</div></div>`}</div>
    </details>`)}`;
  }

  function fraudCompare(d) {
    const codes = d.reasons.map((r) => r.code);
    if (!codes.some((c) => ["BANK_CHANGE_FRAUD", "SENDER_DOMAIN_MISMATCH", "REPLY_TO_MISMATCH"].includes(c))) return "";
    const dom = (e) => (e || "").split("@")[1] || "—";
    const f = d.fields, onFile = d.vendor?.domain;
    const sender = dom(f.sender_email), reply = f.reply_to ? dom(f.reply_to) : null;
    return h`<div class="compare" style="margin-top:6px">
        <div><div class="lbl">Domain on file for ${d.vendor?.legal_name || "this vendor"}</div><div class="val good">${onFile || "—"}</div>
          <div class="small muted">Phone on file ${d.vendor?.phone_on_file || "—"} · our vendor master</div></div>
        <div><div class="lbl">This email came from</div><div class="val ${sender !== onFile ? "bad" : ""}">${sender}</div>
          <div class="small muted">From ${f.sender_email || "—"}${reply ? h` · Reply-To <span class="${reply !== onFile ? "bad" : ""}">${f.reply_to}</span>` : ""}</div></div>
      </div>`;
  }

  function poPanel(p) {
    if (!p) return h`<p class="muted small">This document has no line items to match.</p>`;
    if (!p.po) return h`<div class="note held">${p.po_number_raw ? h`<strong>${p.po_number_raw}</strong> is not in our purchase orders.` : "The invoice cites no purchase order."} Nothing to match its lines against.</div>`;
    const po = p.po;
    return h`<div class="row small" style="margin-bottom:10px">
        <span>Matched against <a href="#/books/pos/${encodeURIComponent(po.po_number)}"><strong>${po.po_number}</strong></a>, raised ${App.date(po.po_date)} on ${po.vendor}</span>
        <span class="spacer"></span><span class="src"><span class="tag ours">our records</span>${p.has_grn ? `${p.grns.length} goods receipt${p.grns.length > 1 ? "s" : ""}` : "no goods receipt yet (2-way match)"}</span></div>
      <div class="table-wrap"><table class="po-table">
        <thead>
          <tr class="sup"><th colspan="3">On the invoice</th><th colspan="4" class="side">On our purchase order${p.has_grn ? " and goods receipt" : ""}</th></tr>
          <tr><th>Item</th><th class="num">Qty</th><th class="num">Rate</th><th class="side">Item</th><th class="num">Ordered</th><th class="num">Already invoiced</th>${p.has_grn ? h`<th class="num">Received</th>` : ""}<th class="num">PO rate</th></tr></thead>
        <tbody>${p.rows.map((r) => {
          const codes = r.flags.map((f) => f.code);
          const qtyBad = codes.includes("OVER_BILLED") || codes.includes("NOT_RECEIVED");
          const priceBad = codes.includes("PRICE_VARIANCE");
          const i = r.invoice, o = r.po;
          return h`<tr class="${r.flags.length ? "flagged" : ""}">
            <td>${i ? h`${i.item}${r.flags.map((f) => h`<span class="line-flag">${f.message}</span>`)}` : h`<span class="muted">not billed</span>`}</td>
            <td class="num ${qtyBad ? "bad" : ""}">${i ? App.qty(i.qty) : ""}</td>
            <td class="num ${priceBad ? "bad" : ""}">${i ? inr(i.unit_price, true) : ""}</td>
            <td class="side">${o ? o.item : h`<span class="muted">not on the PO</span>`}</td>
            <td class="num ${codes.includes("OVER_BILLED") ? "bad" : ""}">${o ? App.qty(o.qty_ordered) : ""}</td>
            <td class="num">${o ? App.qty(o.qty_invoiced) : ""}</td>
            ${p.has_grn ? h`<td class="num ${codes.includes("NOT_RECEIVED") ? "bad" : ""}">${r.received !== null ? App.qty(r.received) : "—"}</td>` : ""}
            <td class="num ${priceBad ? "bad" : ""}">${o ? inr(o.unit_price, true) : ""}</td></tr>`;
        })}</tbody></table></div>`;
  }

  async function detail(id) {
    const d = await api(`/invoices/${id}`);
    const x = App.dec(d.decision);
    const f = d.fields;
    const fld = (k, v) => (v === null || v === undefined || v === "" ? "" : h`<dt>${k}</dt><dd>${v}</dd>`);
    App.render(h`
      <div class="crumbs"><a href="#/invoices">Invoice desk</a> / ${d.file}</div>
      <div class="page-head"><div>
        <h1>${d.vendor_name || f.vendor_name_raw || "Unknown sender"} · ${f.invoice_no_raw || (d.doc_kind === "bank_change" ? "bank-change request" : d.file)}</h1>
        <p>${f.total ? h`<strong class="tnum">${inr(f.total)}</strong> · ` : ""}${f.invoice_date ? `dated ${App.date(f.invoice_date)} · ` : ""}${d.source_kind === "email" || d.file.endsWith(".eml") ? "email" : "document"} received in the accounts inbox
          ${d.vendor ? h` · ${App.vendorLink(d.vendor.id, "vendor record")}` : ""}</p></div></div>

      <div class="grid-2">
        <div class="stack">
          <div class="panel verdict ${x.cls}">
            <div class="row">${badge(d.decision, true)}</div>
            ${d.explanation ? h`<p class="explain">${d.explanation.split(" | ")[0].replace(/ Nothing is paid until a person reviews it\.$/, "")}${d.explanation.includes(" | ") ? "" : ""}</p>` : ""}
            ${d.decision !== "PAY_ELIGIBLE" && d.decision ? h`<p class="small" style="color:var(--ink-2)">Nothing is paid until a person reviews it.</p>` : ""}
            ${fraudCompare(d)}
          </div>

          <div class="panel panel-pad">
            <div class="section-head"><h2>Reasons and evidence</h2><p>${d.reasons.filter((r) => r.severity !== "OK").length} problem(s) · ${d.reasons.filter((r) => r.severity === "OK").length} checks passed</p></div>
            ${reasonsView(d.reasons)}
          </div>

          ${d.doc_kind === "invoice" ? h`<div class="panel panel-pad" id="po">
            <div class="section-head"><h2>Purchase order match</h2></div>
            ${poPanel(d.po_panel)}
          </div>` : ""}

          ${d.instalments.length ? h`<div class="panel panel-pad"><div class="section-head"><h2>Payments</h2></div>
            <table><thead><tr><th>#</th><th class="num">Amount</th><th>Status</th><th>Approved by</th><th>Bank ref</th></tr></thead>
            <tbody>${d.instalments.map((i) => h`<tr><td>${i.instalment_no}</td><td class="num">${inr(i.amount)}</td><td>${i.status}</td><td>${i.approved_by || "—"}</td><td class="mono">${i.provider_ref || "—"}</td></tr>`)}</tbody></table></div>` : ""}
        </div>

        <div class="stack">
          <div class="panel panel-pad">
            <h3 style="margin-bottom:12px">Actions</h3>
            ${d.state === "HELD" ? h`<form id="releaseForm" class="stack">
                <p class="small muted">Release only if you've settled the reason above yourself. Your name and note go in the audit ledger.</p>
                <label class="field"><span>Your name (approver)</span><input type="text" id="relWho" autocomplete="name"></label>
                <label class="field"><span>Why it's OK to pay</span><input type="text" id="relNote"></label>
                <div class="row"><button class="btn primary" type="submit" id="relBtn">Release with note</button>
                <button class="btn" type="button" id="recheckBtn">${icon("refresh")}Re-check</button></div>
                <div class="err" id="relErr"></div></form>`
              : d.state === "BLOCKED" ? h`<p class="small muted" style="margin-bottom:10px">Blocked documents can't be released. Fix the root cause (for example the vendor master or a verified bank change) and re-check.</p>
                <button class="btn" id="recheckBtn">${icon("refresh")}Re-check</button>`
              : h`<p class="small muted">${d.decision === "PAY_ELIGIBLE" ? h`Eligible. It goes into the <a href="#/run">payment run</a>.` : "No action needed."}</p>`}
          </div>

          <div class="panel panel-pad">
            <h3 style="margin-bottom:12px">Read from the document</h3>
            <dl class="fields">
              ${fld("Vendor as written", f.vendor_name_raw)}${fld("GSTIN", f.gstin_raw)}
              ${fld("Invoice no", f.invoice_no_raw ? `${f.invoice_no_raw} → ${f.invoice_no_norm}` : null)}
              ${fld("Invoice date", f.invoice_date && App.date(f.invoice_date))}${fld("PO", f.po_number_raw)}
              ${fld("Sub-total", f.subtotal !== null ? inr(f.subtotal, true) : null)}${fld("Tax", f.tax !== null ? inr(f.tax, true) : null)}
              ${fld("Total", f.total !== null ? inr(f.total, true) : null)}
              ${fld("Bank", f.bank_account ? `${f.bank_account}${f.ifsc_raw ? " · " + f.ifsc_raw : ""}` : null)}${fld("Beneficiary", f.beneficiary_raw)}
              ${fld("From", f.sender_email)}${fld("Reply-To", f.reply_to)}
              ${fld("Signed by", f.signatory ? `${f.signatory}${f.signatory_title ? ", " + f.signatory_title : ""}` : null)}
              ${fld("MSME clock from", f.accepted_date && App.date(f.accepted_date))}${fld("Due", f.due_date && App.date(f.due_date))}
              ${fld("MSME deadline", f.msme_deadline ? `${App.date(f.msme_deadline)} · ${App.countdown(f.msme_deadline)}` : null)}
              ${fld("Outstanding", f.outstanding !== null && f.outstanding !== undefined ? inr(f.outstanding) : null)}
            </dl>
            <p class="small muted" style="margin-top:12px">Read by ${d.extraction_method || "—"} · sha256 <span class="mono">${(d.sha256 || "").slice(0, 16)}…</span></p>
          </div>

          ${d.lines.length ? "" : ""}
          <div class="panel panel-pad"><details class="plain"><summary>Original document</summary>
            <pre class="doc" style="margin-top:10px">${d.original || "File not found"}</pre></details></div>

          <div class="panel panel-pad"><h3 style="margin-bottom:8px">State history</h3>
            <ul class="timeline">${d.events.map((e) => h`<li><span class="muted tnum">${App.dateTime(e.at)}</span><span>${e.from_state ? `${e.from_state} → ` : ""}<strong>${e.to_state}</strong> <span class="muted">· ${e.actor}${e.detail?.note ? ` · “${e.detail.note}”` : ""}</span></span></li>`)}</ul></div>
        </div>
      </div>`);

    const rb = document.getElementById("recheckBtn");
    if (rb) rb.onclick = async () => { const r = await App.busy(rb, () => post(`/invoices/${id}/recheck`, { actor: "web" })); App.toast(`Re-checked: ${r.state}.`); detail(id); };
    const form = document.getElementById("releaseForm");
    if (form) form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = document.getElementById("relErr"); err.textContent = "";
      const who = document.getElementById("relWho").value.trim(), note = document.getElementById("relNote").value.trim();
      if (!who || !note) { err.textContent = "Your name and a note are both required. They go in the audit ledger."; return; }
      try { await App.busy(document.getElementById("relBtn"), () => post(`/invoices/${id}/release`, { approver: who, note })); }
      catch (x) { err.textContent = x.message; return; }
      App.toast(`Released by ${who}. Re-plan the payment run to include it.`); detail(id);
    });
  }

  App.route("invoices", async (args) => {
    if (args[0] === "f") return list(args[1] || null);
    if (args[0]) return detail(args[0]);
    return list();
  });
})();
