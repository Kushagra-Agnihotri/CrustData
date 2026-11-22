/* Vendor 360: vendor table -> identity, live facts, trust and priority (separate), bank history, bank-change checklist. */
(function () {
  const { h, api, post, inr, icon, badge } = App;
  const MSME = { micro: "Micro", small: "Small", medium: "Medium", none: "—" };
  const STATUS = { active: "Active", on_hold: "On hold", blocked: "Blocked" };

  async function list() {
    const vs = await api("/vendors");
    App.render(h`
      <div class="page-head"><div><h1>Vendors</h1><p>Our approved vendor master, with trust (is it safe to pay at all?) and priority (who first?) kept separate on purpose.</p></div></div>
      <div class="panel table-wrap"><table>
        <thead><tr><th>Vendor</th><th>GSTIN</th><th>MSME</th><th class="num">Trust</th><th class="num">Priority</th><th>Status</th><th class="num">Open dues</th><th>Needs you</th></tr></thead>
        <tbody>${vs.map((v) => h`<tr class="clickable" tabindex="0" data-id="${v.id}">
          <td><strong>${v.legal_name}</strong><span class="sub">${v.city || ""}${v.is_critical ? " · critical supplier" : ""}</span></td>
          <td class="mono">${v.gstin}</td>
          <td>${MSME[v.msme_category]}${v.is_trader ? h`<span class="sub">trader</span>` : ""}</td>
          <td class="num">${v.trust_score !== null ? v.trust_score.toFixed(2) : "—"}</td>
          <td class="num">${v.priority_score !== null ? v.priority_score.toFixed(2) : "—"}</td>
          <td>${v.status === "active" ? STATUS.active : h`<span class="badge held">${icon("pause")}${STATUS[v.status]}</span>`}</td>
          <td class="num">${inr(v.open_dues)}</td>
          <td class="small">${[v.pending_bank_change ? "Bank change to verify" : "", v.held_docs ? `${v.held_docs} held/blocked document${v.held_docs > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ") || h`<span class="muted">—</span>`}</td>
        </tr>`)}</tbody></table></div>`);
    App.main().querySelectorAll("tr[data-id]").forEach((tr) => {
      const go = () => (location.hash = `#/vendors/${tr.dataset.id}`);
      tr.onclick = go; tr.onkeydown = (e) => { if (e.key === "Enter") go(); };
    });
  }

  function breakdown(title, score, rows, note) {
    return h`<div class="panel panel-pad">
      <div class="section-head"><h2>${title}</h2><span class="big-score">${score !== null && score !== undefined ? score.toFixed(2) : "—"}</span></div>
      <p class="caption">${note}</p>
      ${rows.map((r) => h`<div class="score">
        <div>${r.verified ? "" : h`<span class="muted" title="can't verify">? </span>`}<strong>${r.signal.replace(/_/g, " ")}</strong> <span class="muted small">weight ${r.weight}</span>
          <div class="small muted">${r.note}</div>
          ${r.facts?.length ? h`<details class="plain"><summary class="small">Evidence (${r.facts.length})</summary><div class="stack" style="margin-top:6px">${r.facts.map(App.fact)}</div></details>` : ""}</div>
        <div class="meter" aria-hidden="true"><span style="width:${Math.round(r.value * 100)}%"></span></div>
        <div class="num small">+${Number(r.contribution).toFixed(3)}</div></div>`)}
    </div>`;
  }

  async function detail(id) {
    const d = await api(`/vendors/${id}`);
    const v = d.vendor;
    const pending = d.bank_accounts.filter((a) => a.status === "pending_verification");
    App.render(h`
      <div class="crumbs"><a href="#/vendors">Vendors</a> / ${v.legal_name}</div>
      <div class="page-head"><div><h1>${v.legal_name}</h1>
        <p>${MSME[v.msme_category] !== "—" ? `MSME ${MSME[v.msme_category].toLowerCase()}${v.is_trader ? " (trader, 45-day rule doesn't apply)" : ""} · ` : ""}${v.city}, ${v.state} · ${v.credit_days} days credit${v.discount_pct ? ` · ${v.discount_pct}/${v.discount_days} discount` : ""} · open dues ${inr(d.open_dues)}</p></div>
        <span class="chip">${STATUS[v.status]}</span></div>

      ${pending.map((a) => h`<div class="panel panel-pad" style="border-color:var(--held-rule);background:var(--held-tint);margin-bottom:24px">
        <h2 style="margin-bottom:8px">Bank change waiting for verification</h2>
        <p style="color:#5f3d00">New account <strong>${a.account}</strong> · ${a.ifsc}${a.bank_name ? ` · ${a.bank_name}` : ""}, proposed by <strong>${a.proposed_by || "an email"}</strong>. An email can only propose a change. A different person approves it after checking out of band.</p>
        <ol style="color:#5f3d00;margin:10px 0 14px;padding-left:20px">
          <li>Call <strong>${v.phone_on_file}</strong>, the number already on file, never one from the email.</li>
          <li>Ask for a contact you already know, and read back the new account number.</li>
          <li>Confirm the reason for the change.</li></ol>
        <form class="row" data-acct="${a.id}" style="align-items:flex-end">
          <label class="field" style="width:220px"><span>Your name (checker)</span><input type="text" name="who"></label>
          <label class="field" style="flex:1;min-width:200px"><span>Note</span><input type="text" name="note"></label>
          <label class="check" style="flex-basis:100%"><input type="checkbox" name="called"> I called ${v.phone_on_file} and the vendor confirmed this change</label>
          <button class="btn primary" type="submit" name="approve">Approve change</button>
          <button class="btn danger" type="button" data-reject>Reject change</button>
          <span class="err" style="flex-basis:100%"></span>
        </form></div>`)}

      ${v.status !== "active" ? h`<form class="panel panel-pad row" id="liftForm" style="margin-bottom:24px;align-items:flex-end">
          <div style="flex-basis:100%"><strong>${v.legal_name} is ${STATUS[v.status].toLowerCase()}.</strong> <span class="muted">Its eligible invoices stay out of payment runs until the hold is lifted.</span></div>
          <label class="field" style="width:220px"><span>Your name</span><input type="text" name="who"></label>
          <label class="field" style="flex:1"><span>Why</span><input type="text" name="note"></label>
          <button class="btn" type="submit">Lift hold</button><span class="err" style="flex-basis:100%"></span></form>` : ""}

      <div class="grid-even">
        <div class="panel panel-pad"><h2 style="margin-bottom:12px">Identity</h2>
          <dl class="fields">
            <dt>GSTIN</dt><dd class="mono">${v.gstin}</dd><dt>PAN</dt><dd class="mono">${v.pan || "—"}</dd><dt>Udyam</dt><dd class="mono">${v.udyam_no || "—"}</dd>
            <dt>Domain</dt><dd>${v.domain || "—"}</dd><dt>Phone on file</dt><dd>${v.phone_on_file || "—"}</dd>
            <dt>Written agreement</dt><dd>${v.has_written_agreement ? "Yes (45-day MSME limit)" : "No (15-day MSME limit)"}</dd>
            <dt>Supplier since</dt><dd>${App.date(v.relationship_since)}</dd>
            ${v.aliases?.length ? h`<dt>Also invoices as</dt><dd>${v.aliases.join(", ")}</dd>` : ""}
          </dl>
          <p class="src" style="margin-top:12px"><span class="tag ours">our records</span> vendor master</p>
        </div>
        <div class="panel panel-pad"><h2 style="margin-bottom:12px">Public facts</h2>
          ${d.facts.length ? h`<div class="stack">${d.facts.slice(0, 8).map(App.fact)}</div>` : h`<p class="muted">No live lookups recorded for this vendor yet.</p>`}
        </div>
      </div>

      <section class="grid-even">
        ${breakdown("Trust", v.trust_score, d.trust_breakdown, `Is it safe to pay at all? Below ${d.trust_gate} the invoice is held. Unverifiable signals score 0 and are marked “?”.`)}
        ${breakdown("Priority", v.priority_score, d.priority_breakdown, "Who gets paid first when cash is short. It never makes an unsafe payment safe.")}
      </section>

      <section><div class="section-head"><h2>Bank accounts</h2></div>
        <div class="panel table-wrap"><table><thead><tr><th>Status</th><th>Account</th><th>IFSC</th><th>Beneficiary</th><th>Valid from</th><th>Valid to</th><th>Proposed by</th><th>Approved by</th><th>Note</th></tr></thead>
        <tbody>${d.bank_accounts.map((a) => h`<tr><td>${a.status.replace("_", " ")}</td><td class="mono">${a.account}</td><td class="mono">${a.ifsc}</td><td>${a.beneficiary_name || "—"}</td>
          <td>${a.valid_from ? App.date(a.valid_from.slice(0, 10)) : "—"}</td><td>${a.valid_to ? App.date(a.valid_to.slice(0, 10)) : "—"}</td><td>${a.proposed_by || "—"}</td><td>${a.approved_by || "—"}</td><td class="small">${a.verification_note || ""}</td></tr>`)}</tbody></table></div></section>

      <section class="grid-even">
        <div><div class="section-head"><h2>Invoices</h2></div><div class="panel table-wrap"><table><thead><tr><th>Document</th><th>Decision</th><th class="num">Total</th><th class="num">Outstanding</th></tr></thead>
          <tbody>${d.invoices.map((i) => h`<tr><td><a href="#/invoices/${i.id}">${i.invoice_no_raw || i.doc_kind.replace("_", " ")}</a><span class="sub">${i.top_reason ? i.top_reason.message.slice(0, 90) : ""}</span></td><td>${badge(i.decision)}</td><td class="num">${inr(i.total)}</td><td class="num">${inr(i.outstanding)}</td></tr>`)}</tbody></table></div></div>
        <div><div class="section-head"><h2>Purchase orders</h2></div><div class="panel table-wrap"><table><thead><tr><th>PO</th><th>Date</th><th>Status</th><th class="num">Value</th></tr></thead>
          <tbody>${d.pos.length ? d.pos.map((p) => h`<tr><td>${App.poLink(p.po_number)}</td><td>${App.date(p.po_date)}</td><td>${p.status.replace(/_/g, " ").toLowerCase()}</td><td class="num">${inr(p.value)}</td></tr>`) : h`<tr><td colspan="4" class="muted">No purchase orders.</td></tr>`}</tbody></table></div></div>
      </section>`);

    App.main().querySelectorAll("form[data-acct]").forEach((f) => {
      const err = f.querySelector(".err");
      f.addEventListener("submit", async (e) => {
        e.preventDefault(); err.textContent = "";
        try { await App.busy(f.querySelector("[name=approve]"), () => post(`/bank-accounts/${f.dataset.acct}/approve`, { approver: f.who.value, called_phone: f.called.checked, note: f.note.value })); }
        catch (x) { err.textContent = x.message; return; }
        App.toast("Bank change approved. Re-check the vendor's held invoices."); detail(id);
      });
      f.querySelector("[data-reject]").onclick = async (e) => {
        err.textContent = "";
        try { await App.busy(e.currentTarget, () => post(`/bank-accounts/${f.dataset.acct}/reject`, { approver: f.who.value, note: f.note.value || "rejected" })); }
        catch (x) { err.textContent = x.message; return; }
        App.toast("Bank change rejected."); detail(id);
      };
    });
    const lf = document.getElementById("liftForm");
    if (lf) lf.addEventListener("submit", async (e) => {
      e.preventDefault(); const err = lf.querySelector(".err"); err.textContent = "";
      try { await App.busy(lf.querySelector("button"), () => post(`/vendors/${id}/status`, { status: "active", approver: lf.who.value, note: lf.note.value })); }
      catch (x) { err.textContent = x.message; return; }
      App.toast("Hold lifted."); detail(id);
    });
  }

  App.route("vendors", async (args) => (args[0] ? detail(args[0]) : list()));
})();
