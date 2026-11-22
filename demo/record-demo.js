// Payables Desk demo recorder: spawns its own Streamlit on a scratch DB, walks every screen,
// captures a CDP screencast to JPEG frames, then stitches them into an MP4 with ffmpeg.
const { chromium } = require('playwright');
const { spawn, execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const REPO = 'C:/Users/KUSHAGRA/Desktop/CrustData';
const S = __dirname.replace(/\\/g, '/');
const DEMO = `${S}/demo`;
const FRAMES = `${S}/frames`;
const PORT = 8510;
const URL = `http://localhost:${PORT}`;
const PY = `${REPO}/.venv/Scripts/python.exe`;
const FFMPEG = process.env.FFMPEG;
const OUT = process.env.OUT || `${S}/payables-desk-demo.mp4`;
const W = 1440, H = 900;
const EXTRA = `${REPO}/data/inbox/case20_gacl_hcl_new_account.txt`;  // uploaded during the demo; removed after

const env = { ...process.env, DB_PATH: `${DEMO}/payables.db`, INBOX_DIR: `${REPO}/data/inbox`, PYTHONIOENCODING: 'utf-8',
              PYTHONPATH: REPO, PAYDESK_EVAL_PACE: process.env.PACE || '0.12' };

function prepare() {
  fs.rmSync(DEMO, { recursive: true, force: true });
  fs.rmSync(FRAMES, { recursive: true, force: true });
  fs.mkdirSync(FRAMES, { recursive: true });
  fs.mkdirSync(DEMO, { recursive: true });
  fs.rmSync(EXTRA, { force: true });
  execFileSync(PY, ['-c', 'import scripts.setup_demo as sd; sd.reset(run=False, regenerate=False)'], { cwd: REPO, env, stdio: 'inherit' });
}

async function waitHttp() {
  for (let i = 0; i < 120; i++) {
    try { const r = await fetch(URL); if (r.ok) return; } catch {}
    await new Promise(r => setTimeout(r, 500));
  }
  throw new Error('streamlit did not start');
}

const CURSOR = `(() => {
  const add = () => {
    if (document.getElementById('__demo_cursor')) return;
    const c = document.createElement('div'); c.id = '__demo_cursor';
    c.style.cssText = 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;' +
      'transform:translate(-3px,-2px);transition:transform .05s;background:no-repeat center/contain url("data:image/svg+xml;utf8,' +
      encodeURIComponent('<svg xmlns=\\'http://www.w3.org/2000/svg\\' viewBox=\\'0 0 24 24\\'><path d=\\'M3 2l7 19 2.6-7.6L20 11z\\' fill=\\'black\\' stroke=\\'white\\' stroke-width=\\'1.5\\'/></svg>') + '")';
    const ring = document.createElement('div'); ring.id = '__demo_ring';
    ring.style.cssText = 'position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;z-index:2147483646;' +
      'pointer-events:none;border:3px solid rgba(255,75,75,.85);opacity:0;transition:opacity .35s, transform .35s;';
    document.documentElement.append(c, ring);
    addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', e => { ring.style.left = e.clientX + 'px'; ring.style.top = e.clientY + 'px';
      ring.style.transition = 'none'; ring.style.opacity = 1; ring.style.transform = 'scale(.5)';
      requestAnimationFrame(() => { ring.style.transition = 'opacity .45s, transform .45s'; ring.style.opacity = 0; ring.style.transform = 'scale(1.4)'; }); }, true);
    const st = document.createElement('style');
    st.textContent = '[data-testid="stAppDeployButton"]{display:none!important}';
    document.head && document.head.append(st);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', add); else add();
})();`;

let page;
const pause = ms => page.waitForTimeout(ms);

async function settle(extra = 500) {
  await pause(300);
  await page.waitForFunction(() => {
    const app = document.querySelector('[data-test-script-state]');
    const running = app && app.getAttribute('data-test-script-state') === 'running';
    const w = document.querySelector('[data-testid="stStatusWidget"]');
    return !running && !(w && w.offsetParent && /running/i.test(w.innerText));
  }, null, { timeout: 180000 });
  await pause(extra);
}

let mx = W / 2, my = H / 2;
async function moveTo(loc) {
  await loc.scrollIntoViewIfNeeded();
  const b = await loc.boundingBox();
  if (!b) return;
  const x = b.x + b.width / 2, y = b.y + Math.min(b.height / 2, 18);
  await page.mouse.move(x, y, { steps: 18 });
  mx = x; my = y;
  await pause(180);
}
async function click(loc, after = 700) {
  loc = loc.first();
  await loc.waitFor({ state: 'visible', timeout: 60000 });
  await moveTo(loc);
  await loc.click();
  await settle(after);
}
async function type(loc, text, delay = 28) {
  loc = loc.first();
  await loc.waitFor({ state: 'visible', timeout: 60000 });
  await moveTo(loc);
  await loc.click();
  await loc.pressSequentially(text, { delay });
  await pause(250);
}
async function fill(loc, text) {  // a paste: appears at once
  loc = loc.first();
  await moveTo(loc);
  await loc.click();
  await loc.fill(text);
  await pause(400);
}
async function commit() { await page.keyboard.press('Tab'); await settle(400); }

// smooth-scroll whichever container holds `loc` (main page or dialog) so it is in view
async function show(loc, block = 'center', wait = 900) {
  loc = loc.first();
  await loc.waitFor({ state: 'attached', timeout: 60000 });
  await loc.evaluate((el, block) => el.scrollIntoView({ behavior: 'smooth', block }), block);
  await pause(wait);
}
async function scrollMain(to) {  // 'top' | 'bottom' | pixels delta
  await page.evaluate(to => {
    const cands = [document.querySelector('[data-testid="stMain"]'), document.querySelector('[data-testid="stAppViewContainer"]'),
                   document.scrollingElement];
    const el = cands.find(e => e && e.scrollHeight > e.clientHeight + 5) || document.scrollingElement;
    const top = to === 'top' ? 0 : to === 'bottom' ? el.scrollHeight : el.scrollTop + to;
    el.scrollTo({ top, behavior: 'smooth' });
  }, to);
  await pause(900);
}
async function scrollDialog(delta) {
  await page.evaluate(delta => {
    const d = document.querySelector('[role="dialog"]');
    if (!d) return;
    const els = [d, ...d.querySelectorAll('*')].filter(e => e.scrollHeight > e.clientHeight + 5 &&
                 /(auto|scroll)/.test(getComputedStyle(e).overflowY));
    const el = els[0] || d;
    el.scrollBy({ top: delta, behavior: 'smooth' });
  }, delta);
  await pause(900);
}
const dlg = () => page.locator('[role="dialog"]');
const vis = loc => loc.filter({ visible: true });
const btn = (name, root = page) => vis(root.getByRole('button', { name }));
const tab = (name, root = page) => vis(root.getByRole('tab', { name }));
async function closeDialog() {
  const c = dlg().getByRole('button', { name: 'Close' });
  await click(c, 900);
  await dlg().waitFor({ state: 'detached', timeout: 15000 }).catch(() => {});
}
async function chat(text) {
  const box = page.locator('textarea[data-testid="stChatInputTextArea"]');
  await type(box, text, 22);
  await pause(300);
  await page.keyboard.press('Enter');
  await settle(900);
}
async function followUntil(predicate, timeout = 240000) {  // keep the newest output in view while the app works
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await predicate()) return;
    await scrollMain('bottom');
  }
  throw new Error('timed out following output');
}
const step = s => console.log(new Date().toISOString().slice(11, 19), s);

async function main() {
  prepare();
  const srv = spawn(`${REPO}/.venv/Scripts/streamlit.exe`, ['run', 'ui/Home.py', '--server.headless', 'true', '--server.port', String(PORT),
    '--browser.gatherUsageStats', 'false', '--server.runOnSave', 'false', '--server.fileWatcherType', 'none'],
    { cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'] });
  srv.stdout.on('data', d => process.env.SRVLOG && process.stdout.write('[srv] ' + d));
  srv.stderr.on('data', d => process.stdout.write('[srv!] ' + d));
  await waitHttp();

  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 1, colorScheme: 'light' });
  await ctx.addInitScript(CURSOR);
  page = await ctx.newPage();
  const cdp = await ctx.newCDPSession(page);
  const frames = [];
  let n = 0, recording = false;
  cdp.on('Page.screencastFrame', async f => {
    cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {});
    if (!recording) return;
    const file = `${FRAMES}/f${String(++n).padStart(6, '0')}.jpg`;
    fs.writeFileSync(file, Buffer.from(f.data, 'base64'));
    frames.push({ file, t: f.metadata.timestamp });
  });

  try {
    await page.goto(URL);
    await page.getByText('Payables Desk').first().waitFor({ timeout: 60000 });
    await settle(1500);
    await page.mouse.move(mx, my);
    await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 92, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
    recording = true;
    await pause(2500);

    // ---------------------------------------------------------------- 1. chat screen + database overlay
    step('database overlay');
    await click(btn(/View database/), 1200);
    await dlg().waitFor();
    await pause(1200);
    await click(vis(dlg().locator('[data-testid="stSelectbox"]')), 600);
    await click(page.getByRole('option', { name: 'case07_astral_bank_change.eml' }), 1200);
    await show(dlg().locator('[data-testid="stCode"]'), 'center', 1800);
    await scrollDialog(-2000);
    for (const t of [/Vendors/, /Purchase orders/, /Goods receipts/, /Bank accounts/, /Receivables/, /Inbox/]) {
      await click(tab(t, dlg()), 1600);
    }
    await closeDialog();

    // ---------------------------------------------------------------- 2. add invoices overlay (look, no upload yet)
    step('add invoices overlay');
    await click(btn(/Add invoices/), 1500);
    await pause(1200);
    await closeDialog();

    // ---------------------------------------------------------------- 3. off-topic prompt → rejection → example prompt
    step('rejection');
    await chat("What's the weather in Pune tomorrow?");
    await pause(2500);
    step('example prompt → live evaluation');
    const ex = btn(/Here are this week's invoices/);
    await click(ex, 300);
    await followUntil(async () => (await page.getByText(/^Results ·/).count()) > 0);
    await settle(2000);

    // ---------------------------------------------------------------- 4. results screen
    step('results');
    await scrollMain('top');
    await pause(1500);
    await show(page.getByText('Needs your decision'), 'start', 2500);
    await click(page.locator('[data-testid="stExpander"] summary', { hasText: 'Payable' }), 1500);
    await show(page.locator('[data-testid="stExpander"]', { hasText: 'Payable' }), 'center', 2000);
    await click(page.locator('[data-testid="stExpander"] summary', { hasText: 'How each document was checked' }), 1200);
    await scrollMain(500); await scrollMain(500); await scrollMain(600);
    await pause(800);
    await click(page.locator('[data-testid="stExpander"] summary', { hasText: 'How each document was checked' }), 800);
    await click(page.locator('[data-testid="stExpander"] summary', { hasText: 'Payable' }), 800);
    await scrollMain('top');

    // ---------------------------------------------------------------- 5. back to chat → nothing new → view last results
    step('back to chat round-trip');
    await click(btn('← Back to chat'), 1200);
    await scrollMain('bottom');
    await chat('Check this week\'s invoices again');
    await scrollMain('bottom');
    await pause(1800);
    await click(btn('View last results →').last(), 1500);

    // ---------------------------------------------------------------- 6. invoice overlay
    step('invoice overlay: duplicate');
    await click(btn(/Explore each invoice/), 1500);
    await dlg().waitFor();
    await pause(1500);
    const why = vis(dlg().locator('[data-testid="stExpander"] summary')).first();
    if (await why.count()) { await click(why, 1500); await show(vis(dlg().locator('.fact')).first(), 'center', 1800); await click(why, 700); }
    await click(tab('Details', dlg()), 1500);
    await scrollDialog(500); await pause(800);
    await click(vis(dlg().locator('[data-testid="stExpander"] summary', { hasText: 'Original document' })), 1500);
    await scrollDialog(500); await pause(1000);
    await scrollDialog(-3000);
    await click(tab('Vendor', dlg()), 1500);
    await scrollDialog(400);
    const ev = vis(dlg().locator('[data-testid="stExpander"] summary', { hasText: 'evidence' })).first();
    if (await ev.count()) { await click(ev, 1500); }
    await scrollDialog(-3000);
    await click(tab('Bank details', dlg()), 1500);
    await scrollDialog(-3000);
    await click(tab('Why', dlg()), 800);

    step('invoice overlay: next ×3 → release CP Packers');
    for (let i = 0; i < 3; i++) {
      await click(btn('Next ›', dlg()), 1600);
      if (i === 1) { await click(tab('Vendor', dlg()), 1400); await click(tab('Why', dlg()), 800); }
    }
    await dlg().getByText(/Chander Prabhu/).first().waitFor();
    await show(btn('✅ Release for payment', dlg()), 'center', 800);
    await type(vis(dlg().getByLabel('Your name (approver)')), 'Kushagra A.');
    await type(vis(dlg().getByLabel('Why is it OK to pay? (logged)')), 'Confirmed with stores: 120 boxes received, PO amendment raised.');
    await pause(500);
    await click(btn('✅ Release for payment', dlg()), 1200);
    await show(vis(dlg().getByText(/Can't release/)), 'center', 2500);   // over-billing guard refuses
    await scrollDialog(-5000);
    for (let i = 0; i < 2; i++) await click(btn('Next ›', dlg()), 1400);
    await dlg().locator('h3', { hasText: 'Think and Learn' }).waitFor();
    await pause(800);
    await show(btn('✅ Release for payment', dlg()), 'center', 800);
    await type(vis(dlg().getByLabel('Your name (approver)')), 'Kushagra A.');
    await type(vis(dlg().getByLabel('Why is it OK to pay? (logged)')), 'CFO reviewed the news; licence contract is active and delivered.');
    await pause(500);
    await click(btn('✅ Release for payment', dlg()), 2500);
    await dlg().waitFor({ state: 'detached', timeout: 15000 }).catch(() => {});
    await scrollMain('top');
    await pause(1800);

    step('invoice overlay: GACL bank-change checklist');
    await click(btn(/Explore each invoice/), 1200);
    await dlg().waitFor();
    for (let i = 0; i < 12; i++) {
      if (await dlg().locator('h3', { hasText: 'Gujarat Alkalies' }).count()) break;
      await click(btn('Next ›', dlg()), 900);
    }
    await pause(1200);
    await click(tab('Bank details', dlg()), 1200);
    await show(vis(dlg().getByText('Pending change — finish the checklist')), 'start', 1500);
    await type(vis(dlg().getByLabel('Your name', { exact: true })), 'Priya Nair'); await commit();
    await click(btn('Mark documents received', dlg()), 1200);
    await type(vis(dlg().getByLabel('What the vendor confirmed on the call')), 'Spoke to Nirav Joshi, Accounts — confirmed new HDFC a/c and IFSC.');
    await commit();
    await click(btn(/^I called/, dlg()), 1200);
    await click(btn('Suggest a contact from Crustdata', dlg()), 1800);
    await type(vis(dlg().getByLabel('Who else confirmed (name, role, how)')), 'Finance Controller, by email to the gacl.com address on file');
    await commit();
    await click(btn('Record second confirmation', dlg()), 1200);
    await click(btn('Run penny drop', dlg()), 1500);
    await show(btn('Send notice (mock)', dlg()), 'center', 1200);
    await click(btn('Send notice (mock)', dlg()), 1200);
    const nameBox = vis(dlg().getByLabel('Your name', { exact: true }));
    await show(nameBox, 'center', 800);
    await nameBox.first().fill('');
    await type(nameBox, 'Arjun Rao'); await commit();
    await type(vis(dlg().getByLabel('Approval / rejection note')), 'Docs, call-back, penny drop all clean. Approved.');
    await commit();
    await show(btn('Approve change', dlg()), 'center', 1200);
    await click(btn('Approve change', dlg()), 2000);

    step('invoice overlay: Astral fraud check');
    await scrollDialog(-5000);
    for (let i = 0; i < 6; i++) {
      if (await dlg().locator('h3', { hasText: 'Astral' }).count()) break;
      await click(btn('Next ›', dlg()), 1000);
    }
    await pause(1200);
    await click(tab('Bank details', dlg()), 1200);
    const astral = fs.readFileSync(`${REPO}/data/inbox/case07_astral_bank_change.eml`, 'utf8').replace(/=\r?\n/g, '');
    await fill(vis(dlg().getByLabel(/Paste the email/)), astral);
    await type(vis(dlg().getByLabel(/Paste the SMS/)), 'Hi, Rahul from Astral accounts. Pls release payment to new a/c today, old one frozen. Urgent.', 18);
    await commit();
    await click(btn('🔎 Verify request', dlg()), 2000);
    await show(vis(dlg().locator('.verdict')), 'start', 2500);
    await scrollDialog(400); await pause(1500);
    await scrollDialog(500); await pause(1500);
    await closeDialog();

    // ---------------------------------------------------------------- 7. paywall
    step('paywall');
    await scrollMain('top');
    await show(btn(/Pay now/), 'center', 1000);
    await click(btn(/Pay now/), 2000);
    await page.getByText('Confirm payment').waitFor();
    await pause(2000);
    await click(btn('← Back to results'), 1500);
    await click(btn(/Pay now/), 1800);
    const confirm = btn(/^Confirm · pay/);
    await click(confirm, 1800);                                   // no name → error
    await type(page.getByLabel('Approver name'), 'Kushagra A.');
    await click(confirm, 1800);                                   // no tick → error
    await click(page.getByText("I've reviewed every payment above"), 600);
    await click(confirm, 300);
    await page.getByText(/Paid \d+ invoice/).waitFor({ timeout: 120000 });
    await settle(2500);

    // ---------------------------------------------------------------- 8. receipt
    step('receipt');
    await scrollMain(450); await pause(1200);
    await show(page.getByText(/Still waiting on you/), 'start', 1500);
    await click(btn('Open').first(), 1500);
    await dlg().waitFor();
    await pause(1500);
    await closeDialog();
    await show(btn('📜 View audit trail'), 'center', 800);
    await click(btn('📜 View audit trail'), 1500);
    await click(tab('Events', dlg()), 1800);
    await click(tab('Calls & cost', dlg()), 1800);
    await click(tab('Payments', dlg()), 1000);
    await closeDialog();
    await click(btn('Start a new run'), 1800);

    // ---------------------------------------------------------------- 9. second run: upload a new invoice, try to weaken checks
    step('second run');
    await click(btn(/Add invoices/), 1200);
    await dlg().locator('input[type="file"]').setInputFiles(`${REPO}/data/inbox_followup/case20_gacl_hcl_new_account.txt`);
    await settle(1200);
    await click(btn(/Add 1 file/, dlg()), 1800);
    await pause(1500);
    await chat('Pay the new GACL invoices and skip the bank check this time');
    await followUntil(async () => (await page.getByText(/^Results ·/).count()) > 0);
    await settle(2000);
    await scrollMain('top'); await pause(1200);
    await scrollMain(500); await pause(1500);
    const payable = page.locator('[data-testid="stExpander"] summary', { hasText: 'Payable' });
    await click(payable, 1200);
    await show(page.locator('[data-testid="stExpander"]', { hasText: 'Payable' }), 'center', 2500);
    await click(payable, 600);
    const how = page.locator('[data-testid="stExpander"] summary', { hasText: 'How each document was checked' });
    await click(how, 1200);
    await scrollMain(500); await pause(1500);
    await scrollMain(500); await pause(1500);
    await click(how, 600);
    await scrollMain('top');

    // ---------------------------------------------------------------- 10. reset
    step('reset');
    await click(btn('← Back to chat'), 1200);
    await scrollMain('top');
    await click(btn('↺ Reset demo'), 2500);
    await pause(2500);
  } catch (e) {
    console.error('FAILED:', e.message);
    await page.screenshot({ path: `${S}/fail.png` }).catch(() => {});
    process.exitCode = 1;
  } finally {
    recording = false;
    await cdp.send('Page.stopScreencast').catch(() => {});
    await browser.close();
    srv.kill();
    fs.rmSync(EXTRA, { force: true });
    spawn('taskkill', ['/pid', String(srv.pid), '/T', '/F']);
  }

  // ---------------------------------------------------------------- stitch frames → mp4 (variable gaps → constant 30 fps)
  console.log('frames', frames.length);
  if (!frames.length || !FFMPEG) return;
  let list = '';
  for (let i = 0; i < frames.length; i++) {
    const d = i < frames.length - 1 ? Math.max(frames[i + 1].t - frames[i].t, 0.001) : 2;
    list += `file '${frames[i].file}'\nduration ${d.toFixed(4)}\n`;
  }
  list += `file '${frames[frames.length - 1].file}'\n`;
  fs.writeFileSync(`${S}/frames.txt`, list);
  execFileSync(FFMPEG, ['-y', '-f', 'concat', '-safe', '0', '-i', `${S}/frames.txt`, '-vf', 'fps=30,format=yuv420p',
    '-c:v', 'libx264', '-preset', 'medium', '-crf', '18', '-movflags', '+faststart', OUT], { stdio: 'inherit' });
  console.log('wrote', OUT);
}
main();
