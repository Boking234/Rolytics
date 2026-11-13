/* Roblox Analytics Agent: the app's screens. Plain JavaScript, no libraries. */
"use strict";

const TOKEN = document.querySelector('meta[name="token"]').content;
const $app = document.getElementById("app");
const STEPS = ["terms", "sharing", "key", "game", "agent", "discord", "done"];
let S = null;            // state from the app
let step = null;         // current setup step name
let tab = "today";
let draft = {};          // choices typed on the current setup screen
let pollTimer = null;
let lastUnread = 0;
let redoSteps = null;    // changing one thing from Settings: only these steps, then back home

// ------------------------------------------------------------ helpers ---
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
}
async function api(path, body) {
  const opts = {headers: {"X-Token": TOKEN}};
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch("/api/" + path, opts);
  try { return await r.json(); } catch (e) { return {ok: false, message: "The app didn't answer."}; }
}
function toast(text) {
  const t = document.getElementById("toast");
  t.textContent = text; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 3500);
}
function msg(r) {
  if (!r || !r.message) return "";
  return `<div class="msg ${r.ok ? "ok" : "err"}">${esc(r.message)}</div>`;
}
function openLink(url) { api("open", {url}).then(r => { if (!r.ok) toast(r.message); }); }
function gameIcon(g, size) {
  if (g && g.icon) return `<img src="${esc(g.icon)}" alt="" width="${size}" height="${size}">`;
  const letter = g && g.name ? g.name.trim()[0] : "?";
  return `<div class="gameicon" style="width:${size}px;height:${size}px">${esc(letter)}</div>`;
}
function statusClass(status) {
  return {"Act now": "act", "Watch": "watch", "All good": "good"}[status] || "none";
}
function statusMark(status) {
  return {"Act now": "!", "Watch": "~", "All good": "✓"}[status] || "·";
}
function fmtDay(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return d.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}
function fmtWhen(iso) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
}

// Tiny Markdown for the Terms and Privacy pages (headings, lists, tables, bold).
function md(text) {
  const lines = text.split("\n"); let html = ""; let list = false; let table = false;
  const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  for (const line of lines) {
    if (/^\|/.test(line)) {
      if (/^\|[-| ]+\|$/.test(line)) continue;
      const cells = line.split("|").slice(1, -1).map(c => inline(c.trim()));
      html += (table ? "" : "<table>") + "<tr>" + cells.map(c => `<td>${c}</td>`).join("") + "</tr>"; table = true; continue;
    } else if (table) { html += "</table>"; table = false; }
    if (/^- /.test(line)) { html += (list ? "" : "<ul>") + `<li>${inline(line.slice(2))}</li>`; list = true; continue; }
    if (list) { html += "</ul>"; list = false; }
    if (/^# /.test(line)) html += `<h2>${inline(line.slice(2))}</h2>`;
    else if (/^## /.test(line)) html += `<h3>${inline(line.slice(3))}</h3>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  return html + (list ? "</ul>" : "") + (table ? "</table>" : "");
}
async function showDoc(name) {
  const r = await fetch(name);
  document.getElementById("modal-body").innerHTML = md(await r.text());
  document.getElementById("modal").showModal();
}

// --------------------------------------------------------------- start ---
async function load() {
  S = await api("state");
  if (!S.setup_done) {
    if (!step) step = !S.terms_ok ? "terms" : !(S.key_saved || S.key_pending) ? "key" : !S.game ? "game" : "agent";
    renderSetup();
  } else {
    step = null;
    renderHome();
  }
}
function go(name) {
  if (redoSteps && !redoSteps.includes(name)) { redoSteps = null; step = null; draft = {}; return load(); }
  step = name; draft = {}; renderSetup(); window.scrollTo(0, 0);
}

// --------------------------------------------------------------- setup ---
function frame(inner, opts = {}) {
  const i = STEPS.indexOf(step);
  const bars = STEPS.slice(0, -1).map((_, k) => `<span class="${k <= i ? "on" : ""}"></span>`).join("");
  const back = redoSteps ? `<button class="btn link" onclick="redoSteps=null;step=null;load()">← Cancel</button>` : i > 0 && step !== "done" ? `<button class="btn link" onclick="go('${STEPS[i - 1]}')">← Back</button>` : "<span></span>";
  const demo = S.demo ? `<div class="demo-banner">Practice mode: made-up numbers, nothing is saved to your real settings.</div>` : "";
  return `${demo}<div class="steps">${bars}</div>
    ${step !== "done" ? `<div class="stepnum">Step ${i + 1} of ${STEPS.length - 1}</div>` : ""}
    ${inner}
    <div class="row spread" style="margin-top:18px">${back}${opts.next || ""}</div>`;
}

function renderSetup() {
  const views = {terms: viewTerms, sharing: viewSharing, key: viewKey, game: viewGame, agent: viewAgent, discord: viewDiscord, done: viewDone};
  $app.innerHTML = frame(...views[step]());
  if (step === "agent") loadAgents();
  if (step === "done") pollJob();
}

// Step 1: welcome, terms, age
function viewTerms() {
  const age = draft.age ?? S.age ?? "";
  const minor = age === "13to17";
  const ready = age && draft.agree && (!minor || draft.guardian);
  return [`
    <h1>Welcome to Roblox Analytics Agent</h1>
    <p class="muted">A daily check-up for your Roblox game. Here's the deal in plain words.</p>
    <div class="grid3">
      <div class="card"><h3>What it does</h3><p class="small">Reads your game's numbers from Roblox once a day and tells you the top 3 things that changed.</p></div>
      <div class="card"><h3>Stays on your PC</h3><p class="small">Your key, your game's data and your reports. No account, no email, no tracking.</p></div>
      <div class="card"><h3>Can go to the cloud</h3><p class="small">Only if you switch it on: anonymized lessons (next step) and Discord messages. Both are off now.</p></div>
    </div>
    <p class="small"><button class="btn link" onclick="showDoc('TERMS.md')">Read the Terms of Service</button> · <button class="btn link" onclick="showDoc('PRIVACY.md')">Read the Privacy Policy</button></p>
    <div class="card stack">
      <h3>How old are you?</h3>
      <p class="small muted">We only keep the age group, to switch features on or off. Nothing else about you.</p>
      ${[["under13", "Under 13", "The app works fully on your PC. Discord and cloud sharing stay off."],
         ["13to17", "13 to 17", "A parent or guardian needs to read the terms with you."],
         ["18plus", "18 or older", ""]].map(([v, l, n]) => `
        <label class="choice"><input type="radio" name="age" value="${v}" ${age === v ? "checked" : ""} onchange="draft.age='${v}';renderSetup()">
        <span><b>${l}</b>${n ? `<br><span class="small muted">${n}</span>` : ""}</span></label>`).join("")}
      ${minor ? `<label class="check"><input type="checkbox" ${draft.guardian ? "checked" : ""} onchange="draft.guardian=this.checked;renderSetup()">
        <span>A parent or guardian has read the Terms and Privacy Policy with me and agrees.</span></label>` : ""}
      <label class="check"><input type="checkbox" ${draft.agree ? "checked" : ""} onchange="draft.agree=this.checked;renderSetup()">
        <span>I agree to the Terms of Service and Privacy Policy.</span></label>
      ${draft.result ? msg(draft.result) : ""}
    </div>`,
    {next: `<button class="btn primary" ${ready ? "" : "disabled"} onclick="submitTerms()">Continue</button>`}];
}
async function submitTerms() {
  const r = await api("terms", {agree: !!draft.agree, age: draft.age ?? S.age, guardian_ok: !!draft.guardian});
  if (!r.ok) { draft.result = r; return renderSetup(); }
  S = await api("state"); go("sharing");
}

// Step 2: cloud sharing (off by default)
function viewSharing() {
  const kid = S.age === "under13";
  return [`
    <h1>Help other developers? (optional)</h1>
    <p class="muted">When lots of games hit the same problem, everyone's check-ups get smarter. You can share anonymized lessons to help. It's off unless you switch it on.</p>
    <div class="card">
      <div class="switch"><div><b>Share anonymized lessons</b><br><span class="small muted">${kid ? "Not available under 13." : "You can change this any time in Settings."}</span></div>
        <input type="checkbox" aria-label="Share anonymized lessons" ${S.share_lessons ? "checked" : ""} ${kid ? "disabled" : ""} onchange="toggleSharing(this.checked)"></div>
    </div>
    <div class="grid3" style="grid-template-columns:1fr 1fr">
      <div class="card"><h3>Would be shared</h3><ul class="list small">
        <li>The kind of problem, e.g. "crash rate up on phones"</li><li>How big it was, as a percentage</li>
        <li>Your genre and a rough size band</li><li>Whether a fix worked</li></ul></div>
      <div class="card"><h3>Never shared</h3><ul class="list small">
        <li>Your key</li><li>Your game's name or ID</li><li>Player names or IDs</li>
        <li>Exact Robux numbers</li><li>Your code</li></ul></div>
    </div>
    <p class="small muted">The community feature isn't live yet, so nothing uploads either way. When it launches, the app will tell you before anything is sent.</p>`,
    {next: `<button class="btn primary" onclick="go('key')">Continue</button>`}];
}
async function toggleSharing(on) {
  const r = await api("sharing", {on});
  toast(r.message); S = await api("state");
  if (step) renderSetup(); else renderHome();
}

// Step 3: Roblox API key with a picture guide
function viewKey() {
  const shots = [
    ["key-1", "Open the API Keys page and click <b>Create API Key</b>."],
    ["key-2", "Give it any name, e.g. <b>analytics-app</b>."],
    ["key-3", "Under Access Permissions pick <b>universe-analytics</b>, click Add API System, and choose your game."],
    ["key-4", "Tick only <b>universe.analytics:read</b>. Under Accepted IP Addresses add <b>0.0.0.0/0</b>."],
    ["key-5", "Click Save &amp; Generate Key, then <b>Copy Key to Clipboard</b>."],
  ];
  const r = draft.result;
  const ready = r && r.ok;
  return [`
    <h1>Connect to Roblox</h1>
    <p class="muted">The app needs a read-only key to see your game's numbers. It takes about 2 minutes.</p>
    <button class="btn" onclick="openLink('https://create.roblox.com/dashboard/credentials')">Open Roblox key page ↗</button>
    <div class="guide">${shots.map(([f, c], k) => `<figure><img src="${f}.svg" alt="Step ${k + 1}"><figcaption>${k + 1}. ${c}</figcaption></figure>`).join("")}</div>
    <p class="illus">Pictures are simplified drawings of Roblox's page, so yours may look a little different.</p>
    <div class="card stack">
      <h3>Paste your key</h3>
      ${S.key_saved && !draft.replace ? `<p>A key is already saved on this computer. <span class="muted small">(It's never shown.)</span></p>
        <div class="row"><button class="btn primary" onclick="keepKey()">Use the saved key</button><button class="btn" onclick="draft.replace=true;renderSetup()">Use a new key</button></div>` : `
      <input type="password" id="key" autocomplete="off" spellcheck="false" placeholder="Paste with Ctrl+V" aria-label="API key">
      <p class="small muted">Stays hidden and is saved only on this computer, after Roblox accepts it. It's never sent to your AI agent.</p>
      <div class="row"><button class="btn primary" onclick="submitKey()">Check key</button></div>`}
      ${r ? msg(r) : ""}
    </div>`,
    {next: `<button class="btn primary" ${ready ? "" : "disabled"} onclick="go('game')">Continue</button>`}];
}
async function submitKey() {
  const el = document.getElementById("key");
  const r = await api("key", {key: el.value});
  el.value = "";
  draft.result = r; renderSetup();
}
async function keepKey() { draft.result = await api("key/keep", {}); renderSetup(); }

// Step 4: pick the game, confirm details
function viewGame() {
  const g = S.game;
  const r = draft.result;
  const genre = draft.genre ?? (g && g.genre) ?? "";
  return [`
    <h1>Pick your game</h1>
    <p class="muted">Open your game on the Creator Dashboard and copy the link from the address bar. (The game's number works too.)</p>
    <div class="row" style="align-items:flex-start;gap:16px">
      <figure style="margin:0;width:260px;max-width:100%" class="guide"><img src="game-1.svg" alt="Copy the address bar link"></figure>
      <div style="flex:1;min-width:240px" class="stack">
        <button class="btn" onclick="openLink('https://create.roblox.com/dashboard/creations')">Open my creations ↗</button>
        <input type="text" id="link" placeholder="https://create.roblox.com/dashboard/creations/experiences/…" value="${esc(draft.link || "")}" aria-label="Game link">
        <button class="btn primary" onclick="submitGame()">Connect game</button>
      </div>
    </div>
    ${r ? msg(r) : ""}
    ${g && (!r || r.ok) ? `
    <div class="card stack" style="margin-top:14px">
      <div class="gamecard">${gameIcon(g, 48)}<div><b>${esc(g.name)}</b><br><span class="small muted">Universe ${esc(g.id)}</span></div></div>
      <div class="row"><label style="flex:1;min-width:200px">Genre<br>
        <select id="genre" onchange="draft.genre=this.value"><option value="">Choose…</option>${S.genres.map(x => `<option ${x === genre ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
        <label style="flex:2;min-width:220px">Main systems <span class="muted small">(optional)</span><br>
        <input type="text" id="systems" placeholder="e.g. pets, trading, rebirths" value="${esc(draft.systems ?? g.systems ?? "")}"></label></div>
      <div class="row"><label>Daily report at<br><input type="time" id="rt" value="${esc(S.report_time)}"></label>
        <label>Quiet hours from<br><input type="time" id="qs" value="${esc(S.quiet_start)}"></label>
        <label>to<br><input type="time" id="qe" value="${esc(S.quiet_end)}"></label></div>
      <p class="small muted">Reports during quiet hours wait in your inbox without a notification.</p>
    </div>` : ""}`,
    {next: `<button class="btn primary" ${g && (!r || r.ok) ? "" : "disabled"} onclick="submitDetails()">Continue</button>`}];
}
async function submitGame() {
  draft.link = document.getElementById("link").value;
  draft.result = {ok: true, message: "Checking with Roblox…"}; renderSetup();
  draft.result = await api("game", {link: draft.link});
  S = await api("state"); renderSetup();
}
async function submitDetails() {
  const r = await api("details", {genre: document.getElementById("genre").value, systems: document.getElementById("systems").value,
    report_time: document.getElementById("rt").value, quiet_start: document.getElementById("qs").value, quiet_end: document.getElementById("qe").value});
  if (!r.ok) { draft.result = r; return renderSetup(); }
  S = await api("state"); go("agent");
}

// Step 5: AI agent
let AGENTS = null;
function viewAgent() {
  const r = draft.result;
  const pick = draft.agent ?? S.agent ?? "";
  const list = AGENTS ? AGENTS.map(a => `
      <label class="choice"><input type="radio" name="agent" value="${a.key}" ${pick === a.key ? "checked" : ""} onchange="draft.agent='${a.key}';draft.result=null;renderSetup()">
      <span><b>${esc(a.label)}</b> ${a.found ? `<span class="pill good">Found on this PC</span>` : ""}</span></label>`).join("") : `<p class="muted">Looking for AI agents on this PC…</p>`;
  return [`
    <h1>Pick your AI helper</h1>
    <p class="muted">Your AI agent can read each check-up (a few hundred words, never your key) and help fix what it finds. It uses your own plan, like Claude Pro or Max.</p>
    <div class="stack">${list}
      <label class="choice"><input type="radio" name="agent" value="none" ${pick === "none" ? "checked" : ""} onchange="draft.agent='none';draft.result=null;renderSetup()">
      <span><b>No AI agent for now</b><br><span class="small muted">You still get the daily report and alerts in this app.</span></span></label>
    </div>
    ${r ? (r.manual ? `<div class="card"><p>Add this to your agent's MCP settings:</p><pre class="body">${esc(r.message)}</pre></div>` : msg(r)) : ""}
    ${pick && pick !== "none" && !(r && r.ok) ? `<p class="small muted" style="margin-top:10px">Not installed yet? <button class="btn link" onclick="openLink('https://claude.ai/download')">Get Claude ↗</button></p>` : ""}`,
    {next: pick && !(r && r.ok) ? `<button class="btn primary" onclick="submitAgent()">${pick === "none" ? "Continue" : "Connect"}</button>`
                                : `<button class="btn primary" ${r && r.ok ? "" : "disabled"} onclick="go('discord')">Continue</button>`}];
}
async function loadAgents() {
  if (AGENTS) return;
  AGENTS = await api("agents");
  if (step === "agent") renderSetup();
}
async function submitAgent() {
  const key = draft.agent ?? S.agent;
  const r = await api("agent", {key});
  if (key === "none" && r.ok) { S = await api("state"); return go("discord"); }
  draft.result = r; draft.agent = key; S = await api("state"); renderSetup();
}

// Step 6: Discord DMs (placeholder until the Discord stage is built)
function viewDiscord() {
  const kid = S.age === "under13";
  if (kid) return [`<h1>Discord messages</h1><div class="card"><p>Discord needs you to be 13 or older, so this stays off. Your reports will show up in this app's inbox and as desktop notifications.</p></div>`,
    {next: `<button class="btn primary" onclick="go('done')">Continue</button>`}];
  const shots = [["discord-1", "Click <b>Connect Discord</b>."], ["discord-2", "Approve in the Discord window."], ["discord-3", "Check your DMs for a welcome message."]];
  const r = draft.result;
  return [`
    <div class="row"><h1 style="margin:0">Get reports on your phone</h1><span class="pill info">Highly recommended</span></div>
    <p class="muted" style="margin-top:6px">Your daily top 3 and urgent alerts as Discord DMs, so you see them even when you're away from your PC. No Discord server needed.</p>
    <div class="guide">${shots.map(([f, c], k) => `<figure><img src="${f}.svg" alt="Step ${k + 1}"><figcaption>${k + 1}. ${c}</figcaption></figure>`).join("")}</div>
    <div class="card stack">
      <div class="row"><button class="btn primary" disabled>Connect Discord</button><span class="pill watch">Coming in the next update</span></div>
      <p class="small muted">Discord messages aren't built yet. Until then, everything arrives in this app's inbox and as desktop notifications.</p>
      <label class="check"><input type="checkbox" ${(draft.remind ?? S.discord === "remind") ? "checked" : ""} onchange="draft.remind=this.checked">
        <span>Remind me when Discord messages are ready</span></label>
      ${r ? msg(r) : ""}
    </div>`,
    {next: `<span class="row"><button class="btn link" onclick="submitDiscord(false)">Skip for now</button><button class="btn primary" onclick="submitDiscord(true)">Continue</button></span>`}];
}
async function submitDiscord(cont) {
  const remind = cont && (draft.remind ?? S.discord === "remind");
  const r = await api("discord", {choice: remind ? "remind" : "skipped"});
  if (!r.ok) { draft.result = r; return renderSetup(); }
  S = await api("state"); go("done");
}

// Done: finish and show the first report being made
function viewDone() {
  const startup = S.can_start_with_windows ? `<label class="check"><input type="checkbox" id="startup" checked>
      <span>Open this app in the background when my computer starts, so reports arrive on time.</span></label>` : "";
  return [`
    <h1>You're all set</h1>
    <div class="card stack">
      <div class="gamecard">${gameIcon(S.game, 48)}<div><b>${esc(S.game ? S.game.name : "")}</b><br><span class="small muted">Daily report at ${esc(S.report_time)}</span></div></div>
      <div id="job"><p class="muted">Press Finish to get your first report.</p></div>
      ${startup}
    </div>`,
    {next: `<button class="btn primary" id="finish" onclick="finish()">Finish</button>`}];
}
async function finish() {
  const el = document.getElementById("startup");
  const r = await api("finish", el ? {start_with_windows: el.checked} : {});
  if (!r.ok) { document.getElementById("job").innerHTML = msg(r); return; }
  document.getElementById("finish").outerHTML = `<button class="btn primary" onclick="load()">Go to my dashboard</button>`;
  pollJob(true);
}
async function pollJob(started) {
  const box = document.getElementById("job");
  if (!box || (!started && !(S.job && S.job.running))) return;
  const st = await api("state"); S = st;
  const j = st.job;
  const pct = j.total_steps ? Math.round(100 * j.done_steps / j.total_steps) : (j.running ? 10 : 100);
  box.innerHTML = j.error ? msg({ok: false, message: j.error})
    : `<p>${esc(j.phase || "Starting")}${j.running ? "…" : ""}</p><div class="bar"><div style="width:${pct}%"></div></div>
       ${j.running ? `<p class="small muted">The first time takes a few minutes. You can close this window; the app keeps working.</p>` : `<p class="msg ok">Your first report is ready.</p>`}`;
  if (j.running) setTimeout(() => pollJob(true), 1500);
}

// ---------------------------------------------------------------- home ---
async function renderHome() {
  const g = S.game || {};
  const demo = S.demo ? `<div class="demo-banner">Practice mode: made-up numbers.</div>` : "";
  $app.innerHTML = `${demo}
    <header class="top">${gameIcon(g, 40)}<div><div class="name">${esc(g.name)}</div><div class="small muted" id="sub"></div></div>
      <nav class="tabs">
        <button class="${tab === "today" ? "on" : ""}" onclick="setTab('today')">Today</button>
        <button class="${tab === "inbox" ? "on" : ""}" onclick="setTab('inbox')">Inbox${S.unread ? `<span class="badge">${S.unread}</span>` : ""}</button>
        <button class="${tab === "settings" ? "on" : ""}" onclick="setTab('settings')">Settings</button>
      </nav></header>
    <main id="main"><p class="loading">Loading…</p></main>`;
  lastUnread = S.unread;
  if (tab === "today") renderToday(); else if (tab === "inbox") renderInbox(); else renderSettings();
  schedulePoll();
}
function setTab(t) { tab = t; renderHome(); }

async function renderToday() {
  const t = await api("today");
  const j = S.job;
  const main = document.getElementById("main");
  if (!main) return;
  document.getElementById("sub").textContent = t.day ? `Data through ${fmtDay(t.day)}` : "";
  const cls = statusClass(t.status);
  const lines = {"Act now": "Something big needs a look today.", "Watch": "A few things to keep an eye on.",
                 "All good": t.items.length ? "Nothing worrying. Here's what improved." : `No unusual changes across ${t.checked} metrics.`,
                 "No data": t.waiting || j.running ? "Your first report is on its way." : "No data yet."}[t.status] || "";
  const busy = j.running ? `<div class="card"><p>${esc(j.phase)}…</p><div class="bar"><div style="width:${j.total_steps ? Math.round(100 * j.done_steps / j.total_steps) : 10}%"></div></div></div>` : "";
  const err = j.error && !j.running ? msg({ok: false, message: "Last update failed: " + j.error}) : "";
  main.innerHTML = `
    <div class="card status"><div class="dot ${cls}">${statusMark(t.status)}</div>
      <div><h1 style="margin:0">${esc(t.status)}</h1><p class="muted" style="margin:0">${esc(lines)}</p></div></div>
    ${busy}${err}
    ${t.items.map(it => itemCard(it)).join("")}
    ${(t.notes || []).length ? `<p class="small muted">${t.notes.map(esc).join("<br>")}</p>` : ""}
    ${t.weekly ? `<details class="card"><summary>This week's check-up</summary><pre class="body">${esc(t.weekly)}</pre></details>` : ""}
    <div class="row" style="margin-top:6px">
      <button class="btn" onclick="api('open-report', {})">All charts ↗</button>
      <button class="btn" onclick="runNow()" ${j.running ? "disabled" : ""}>Check now</button>
      ${S.agent ? `<button class="btn" onclick="copyPrompt()">Copy question for my AI</button>` : ""}
    </div>
    ${S.paused ? `<p class="msg err">Daily reports are paused. <button class="btn link" onclick="saveSetting({paused:false})">Resume</button></p>` : ""}`;
}
function itemCard(it) {
  const cls = it.good ? "good" : (it.severity === "act" ? "act" : "watch");
  const tag = it.good ? "Good news" : (it.severity === "act" ? "Act now" : "Watch");
  const marked = {done: "Marked as fixed. I'll check again in 3 days.", snooze: "Snoozed for 7 days.", wrong: "Marked as not a problem."}[it.marked];
  return `<div class="card item ${cls} ${it.marked ? "marked" : ""}">
    <div class="row spread"><span class="pill ${cls}">${tag}</span><span class="small muted">#${it.n}</span></div>
    <p class="title" style="margin:8px 0 4px">${esc(it.title)}</p>
    <details><summary>Details</summary><p class="small" style="margin-top:6px">${esc(it.detail)}</p></details>
    ${marked ? `<p class="small muted" style="margin:8px 0 0">${marked} <button class="btn link small" onclick="mark('${esc(it.key)}','open')">Undo</button></p>` :
      it.good ? "" : `<div class="row" style="margin-top:10px">
      <button class="btn small" onclick="mark('${esc(it.key)}','done')">✓ Fixed</button>
      <button class="btn small" onclick="mark('${esc(it.key)}','snooze')">Snooze 7 days</button>
      <button class="btn small" onclick="mark('${esc(it.key)}','wrong')">Not a problem</button></div>`}
  </div>`;
}
async function mark(key, action) {
  const r = await api("mark", {key, action});
  toast(r.message); renderToday();
}
async function runNow() { const r = await api("run", {}); toast(r.message); S = await api("state"); renderToday(); }
function copyPrompt() {
  const text = "Run my Roblox daily check-up";
  (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
    () => toast(`Copied. Paste it into your AI agent: "${text}"`), () => toast(`Ask your AI agent: "${text}"`));
}

async function renderInbox() {
  const items = await api("inbox");
  const main = document.getElementById("main");
  if (!main) return;
  document.getElementById("sub").textContent = "";
  main.innerHTML = `<div class="row spread"><h2>Inbox</h2>${items.some(i => !i.read) ? `<button class="btn link" onclick="readAll()">Mark all read</button>` : ""}</div>
    <div class="card">${items.length ? items.map(i => `
      <div class="inbox-row" onclick="openItem(${i.id}, this)">
        <span class="${i.read ? "read-dot" : "unread-dot"}"></span>
        <div style="flex:1"><div class="row spread"><b>${esc(i.title)}</b><span class="small muted">${esc(fmtWhen(i.created_at))}</span></div>
        <pre class="body" hidden>${esc(i.body)}</pre></div></div>`).join("")
      : `<p class="muted">Reports and alerts will show up here. The next one comes at ${esc(S.report_time)}.</p>`}</div>`;
}
async function openItem(id, row) {
  const pre = row.querySelector("pre"); pre.hidden = !pre.hidden;
  const dot = row.querySelector(".unread-dot");
  if (dot) { dot.className = "read-dot"; await api("read", {id}); S = await api("state"); updateBadge(); }
}
async function readAll() { await api("read", {}); S = await api("state"); renderHome(); }
function updateBadge() {
  const b = document.querySelector("nav.tabs button:nth-child(2)");
  if (b) b.innerHTML = "Inbox" + (S.unread ? `<span class="badge">${S.unread}</span>` : "");
}

function renderSettings() {
  const main = document.getElementById("main");
  document.getElementById("sub").textContent = "";
  const kid = S.age === "under13";
  const discordText = {blocked: "Not available under 13.", remind: "Coming in the next update. We'll remind you.",
                       skipped: "Coming in the next update.", unset: "Coming in the next update.", linked: "Connected."}[S.discord];
  main.innerHTML = `
    <div class="card"><h2>Reports</h2>
      <div class="switch"><span>Daily report at</span><input type="time" value="${esc(S.report_time)}" onchange="saveSetting({report_time:this.value})"></div>
      <div class="switch"><span>Quiet hours</span><span class="row"><input type="time" value="${esc(S.quiet_start)}" onchange="saveSetting({quiet_start:this.value})"> to
        <input type="time" value="${esc(S.quiet_end)}" onchange="saveSetting({quiet_end:this.value})"></span></div>
      <div class="switch"><div>Pause daily reports<br><span class="small muted">One click to stop, one to resume.</span></div><input type="checkbox" ${S.paused ? "checked" : ""} onchange="saveSetting({paused:this.checked})"></div>
      ${S.can_start_with_windows ? `<div class="switch"><span>Open when my computer starts</span><input type="checkbox" ${S.start_with_windows ? "checked" : ""} onchange="saveSetting({start_with_windows:this.checked})"></div>` : ""}
    </div>
    <div class="card"><h2>Sharing and messages</h2>
      <div class="switch"><div>Share anonymized lessons<br><span class="small muted">${kid ? "Not available under 13." : "Off unless you turn it on. Nothing uploads until the feature launches."}</span></div>
        <input type="checkbox" ${S.share_lessons ? "checked" : ""} ${kid ? "disabled" : ""} onchange="toggleSharing(this.checked)"></div>
      <div class="switch"><div>Discord messages<br><span class="small muted">${esc(discordText)}</span></div><button class="btn small" disabled>Connect</button></div>
    </div>
    <div class="card"><h2>Connections</h2>
      <div class="switch"><div>Game<br><span class="small muted">${esc(S.game ? S.game.name : "")}${S.game && S.game.genre ? " · " + esc(S.game.genre) : ""}</span></div><button class="btn small" onclick="redo('game')">Change</button></div>
      <div class="switch"><div>AI agent<br><span class="small muted">${esc(S.agent ? (AGENT_NAMES[S.agent] || S.agent) : "None")}</span></div><button class="btn small" onclick="redo('agent')">Change</button></div>
      <div class="switch"><div>Roblox key<br><span class="small muted">${S.key_saved ? "Saved on this computer. Never shown." : "Not saved."}</span></div>
        <span class="row"><button class="btn small" onclick="redo('key')">Replace</button><button class="btn small" onclick="forgetKey()">Remove</button></span></div>
    </div>
    <div class="card"><h2>About</h2>
      <p class="small">Version ${esc(S.version)} · Data folder: <code>${esc(S.data_folder)}</code></p>
      <p class="small"><button class="btn link" onclick="showDoc('TERMS.md')">Terms</button> · <button class="btn link" onclick="showDoc('PRIVACY.md')">Privacy</button> ·
        <button class="btn link" onclick="exportSettings()">Export settings</button> · <button class="btn link" onclick="openLink('https://github.com/Boking234/roblox-analytics-agent')">Source code ↗</button></p>
    </div>`;
}
const AGENT_NAMES = {"claude-code": "Claude Code", "claude-desktop": "Claude Desktop app", "codex": "OpenAI Codex CLI", "cursor": "Cursor",
                     "vscode": "VS Code (GitHub Copilot)", "gemini": "Gemini CLI", "windsurf": "Windsurf"};
async function saveSetting(changes) {
  const r = await api("settings", {changes});
  toast(r.message); S = await api("state");
  if (tab === "today") renderToday();
}
function redo(which) {
  redoSteps = which === "key" ? ["key", "game"] : [which];
  step = which; draft = {}; AGENTS = null; renderSetup();
}
async function forgetKey() {
  const r = await api("key/forget", {});
  toast(r.message); S = await api("state"); step = null; load();
}
async function exportSettings() {
  const data = await api("export-settings");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
  a.download = "roblox-analytics-agent-settings.json"; a.click();
}

// Keep the screen fresh: new reports, finished jobs.
function schedulePoll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (step) return;
    const wasRunning = S.job && S.job.running;
    S = await api("state");
    if (S.unread !== lastUnread) {
      if (S.unread > lastUnread) toast("New report in your inbox");
      lastUnread = S.unread; updateBadge();
    }
    if (tab === "today" && (wasRunning || S.job.running)) renderToday();
  }, 4000);
}

load();
