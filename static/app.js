// Saathi UI - vanilla JS, no build step. Merchant-facing journey + a "Behind the scenes" technical view.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (n) => "₹" + Number(n || 0).toLocaleString("en-IN");

const S = {
  status: null, merchant: null, lang: null, pick: null, scenario: null,
  caseId: null, data: null, msgs: [], spoken: null, speakOn: true, speakQueue: Promise.resolve(), rec: null,
  bts: false, lastLog: 0, lines: [], paused: false, auto: true, caseOnly: false, off: new Set(), clearedAt: 0, tab: "contracts",
};

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.json !== undefined) { o.body = JSON.stringify(o.json); o.headers["Content-Type"] = "application/json"; o.method = o.method || "POST"; }
  const r = await fetch(path, o);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3000);
}
function when(iso) {
  const d = new Date(Date.parse(iso) + 5.5 * 36e5);
  const day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][d.getUTCDay()];
  const mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()];
  return `${day} ${d.getUTCDate()} ${mon}, ${iso.slice(11, 16)}`;
}
function vtime(iso) {
  if (!iso) return "—";
  const day = Math.round((Date.parse(iso.slice(0, 10) + "T00:00:00+05:30") - Date.parse("2026-11-14T00:00:00+05:30")) / 864e5);
  return (day ? `D+${day} ` : "") + iso.slice(11, 23);
}
function fmtDur(ms) {
  const neg = ms < 0; ms = Math.abs(ms);
  const h = Math.floor(ms / 36e5), m = Math.floor((ms % 36e5) / 6e4);
  return (neg ? "-" : "") + (h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`);
}

// ------------------------------------------------------------------ screens
function show(name) {
  ["who", "lang", "talk"].forEach((n) => { $(`#s-${n}`).hidden = n !== name; });
  $("#change").hidden = name !== "talk";
}

async function loadStatus() {
  const s = await api("/api/status");
  S.status = s;
  $("#clock").textContent = "🕒 " + when(s.clock);
  $("#badges").innerHTML = `<span class="pillb ${s.sarvam}">Voice: ${s.sarvam}</span><span class="pillb ${s.gemini}">AI: ${s.gemini}</span>`;
  return s;
}
const scenarioFor = (mid) => Object.entries(S.status.scenarios).find(([, v]) => v.merchant_id === mid);

const userById = (id) => S.status.users.find((u) => u.id === id) || S.status.merchants.find((m) => m.id === id);

function renderWho() {
  $("#people").classList.add("two");
  $("#people").innerHTML = S.status.users.map((u) => {
    const merchant = u.type === "merchant";
    return `<button class="person" data-pick="${u.id}"><span class="icon">${merchant ? "🏪" : "👤"}</span>
      <span class="ptype">${merchant ? "Merchant" : "Paytm customer"}</span><b>${esc(u.name)}</b>
      <span>${merchant ? `${esc(u.business)} shop, ${esc(u.city)}` : `pays with Paytm UPI, ${esc(u.city)}`}</span></button>`;
  }).join("");
  show("who");
}
function renderLang() {
  const m = userById(S.pick);
  $("#hello").textContent = `Hello ${m.name}!`;
  $("#langs").innerHTML = Object.entries(S.status.languages).map(([k, v]) =>
    `<button class="lang2 ${k === m.lang ? "suggest" : ""}" data-lang="${k}"><b>${esc(v.label)}</b><span>${esc(v.name)}</span></button>`).join("");
  show("lang");
}
async function login(mid, lang) {
  const r = await api("/api/login", { json: { merchant_id: mid, lang } });
  // new person / new login: start with an empty chat, journey panel and behind-the-scenes trace + log
  S.clearedAt = r.log_from || S.lastLog; S.lastIntent = null;
  enterTalk(r.merchant, r.lang);
  $("#steps")._html = null; renderSteps(); renderLog(true);
}
function enterTalk(m, lang) {
  S.merchant = m; S.lang = lang; S.caseId = null; S.data = null; S.spoken = null; S.msgs = [];
  $("#avatar").textContent = m.name[0];
  $("#pname").textContent = m.name;
  $("#pshop").textContent = `${m.type === "customer" ? "Paytm customer" : m.business}, ${m.city} · ${S.status.languages[lang].name}`;
  $$("#uswitch button").forEach((b) => b.classList.toggle("on", b.dataset.switch === m.type));
  // example problems for this kind of user, in the chosen language
  const ids = m.type === "customer" ? ["S7"] : ["S6", "S8"];
  S.examples = ids.map((id) => {
    const sc = S.status.scenarios[id];
    return { id, title: sc.title.split("(")[0].trim(), text: sc.texts[lang] || sc.texts.en || sc.text };
  });
  S.scenario = ids[0];
  S.suggest = S.examples[0].text;
  $("#msgs").dataset.sig = ""; $("#msgs").innerHTML = ""; $("#text").value = "";
  $("#try")._html = ""; $("#try").innerHTML = "";
  show("talk");
  loadChat(); renderJourney();
}

// ------------------------------------------------------------------ conversation + voice
async function loadChat() {
  if (!S.merchant || S.workTimer) return;      // don't redraw while the "working" bubble is up
  const { messages } = await api(`/api/chat?merchant_id=${S.merchant.id}`);
  if (S.spoken === null) S.spoken = new Set(messages.map((m) => m.id));
  S.msgs = messages;
  const box = $("#msgs");
  const sig = messages.map((m) => m.id).join(",");
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = messages.length ? messages.map((m) => {
      const mine = m.direction === "in";
      return `<div class="b ${mine ? "me" : "them"}">
        ${mine ? "" : `<span class="who">Saathi</span>`}<div class="bt">${esc(m.text)}</div>
        ${m.text_en && m.text_en !== m.text ? `<div class="ben">${esc(m.text_en)}</div>` : ""}
        <div class="bm">${when(m.ts)}${mine ? "" : ` <button class="listen" data-say="${m.id}">🔊 Listen</button>`}</div></div>`;
    }).join("") : "";
    box.scrollTop = box.scrollHeight;
  }
  // suggestion buttons (no label) appear after Saathi has replied once, while no request is open
  const replied = messages.some((m) => m.direction === "out");
  const hasCase = messages.some((m) => m.case_id);
  const tryHtml = replied && !hasCase && S.examples
    ? S.examples.map((x, i) => `<button class="trybtn" data-try="${i}">“${esc(x.text)}”</button>`).join("") : "";
  if ($("#try")._html !== tryHtml) { $("#try")._html = tryHtml; $("#try").innerHTML = tryHtml; }
  for (const m of messages) {
    if (m.direction === "out" && !S.spoken.has(m.id)) { S.spoken.add(m.id); if (S.speakOn) speak(m.text, m.lang); }
  }
  const latest = messages.filter((m) => m.case_id).pop();
  if (latest && S.caseId !== latest.case_id) { S.caseId = latest.case_id; loadCase(); }
}

function speak(text, lang) {
  S.speakQueue = S.speakQueue.then(async () => {
    try {
      const r = await api("/api/tts", { json: { text, lang, speaker: S.voice } });
      if (r.audio) {
        const a = new Audio(`data:${r.mime};base64,${r.audio}`);
        await new Promise((res) => { a.onended = res; a.onerror = res; a.play().catch((e) => { toast("Tap 🔊 Listen to hear the reply"); res(); }); });
        return;
      }
      if (!("speechSynthesis" in window)) return;
      const u = new SpeechSynthesisUtterance(text);
      u.lang = r.lang_code;
      const v = speechSynthesis.getVoices().find((x) => x.lang.replace("_", "-").startsWith(r.lang_code.slice(0, 2)));
      if (v) u.voice = v;
      await new Promise((res) => { u.onend = res; u.onerror = res; speechSynthesis.speak(u); });
    } catch (e) { /* best effort */ }
  });
}

// "Saathi is working" bubble shown while a reply is on its way
const WORKING = ["Thinking…", "Checking your records…", "Working on your request…"];
function showWorking(mine) {
  stopWorking();
  const box = $("#msgs");
  if (mine) box.insertAdjacentHTML("beforeend", `<div class="b me pending"><div class="bt">${esc(mine)}</div></div>`);
  box.insertAdjacentHTML("beforeend", `<div class="b them working"><span class="who">Saathi</span><div class="bt"><span class="dots"><i></i><i></i><i></i></span> <span class="wtext">${WORKING[0]}</span></div></div>`);
  box.scrollTop = box.scrollHeight;
  let i = 0;
  S.workTimer = setInterval(() => { const w = $("#msgs .wtext"); if (w) w.textContent = WORKING[++i % WORKING.length]; }, 1400);
}
function stopWorking() {
  clearInterval(S.workTimer); S.workTimer = null;
  $$("#msgs .working, #msgs .pending").forEach((e) => e.remove());
  $("#msgs").dataset.sig = "";
}

async function sendText(text) {
  text = (text || "").trim();
  if (!text) return;
  $("#send").disabled = true;
  $("#text").value = "";
  showWorking(text);
  try {
    const ex = (S.examples || []).find((x) => Object.values(S.status.scenarios[x.id].texts).includes(text));
    const r = await api("/api/message", { json: { text, scenario_id: ex ? ex.id : null, focus: true } });
    $("#text").value = "";
    S.lastIntent = r.intent || "complaint";
    if (r.case) { S.caseId = r.case.id; S.data = null; }
  } catch (e) { toast(e.message); } finally { stopWorking(); $("#send").disabled = false; await loadChat(); loadCase(); }
}

async function toggleMic() {
  if (S.rec) { S.rec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const rec = new MediaRecorder(stream, MediaRecorder.isTypeSupported("audio/webm") ? { mimeType: "audio/webm" } : {});
    const chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      $("#mic").classList.remove("rec"); $("#micnote").textContent = "Listening to what you said…"; S.rec = null;
      const fd = new FormData();
      fd.append("audio", new Blob(chunks, { type: "audio/webm" }), "clip.webm");
      if (S.scenario) fd.append("scenario_id", S.scenario);
      fd.append("focus", "1");
      showWorking("🎤 …");
      try {
        const r = await api("/api/message", { method: "POST", body: fd });
        S.lastIntent = r.intent || "complaint";
        if (r.case) S.caseId = r.case.id;
      }
      catch (e) { toast(e.message); }
      stopWorking();
      $("#micnote").textContent = "Tap the mic and tell us your problem";
      await loadChat(); loadCase();
    };
    rec.start(); S.rec = rec;
    $("#mic").classList.add("rec"); $("#micnote").textContent = "I'm listening… tap again when you're done";
    setTimeout(() => S.rec === rec && rec.state === "recording" && rec.stop(), 29000);
  } catch (e) { toast("Microphone not available: " + e.message); }
}

// ------------------------------------------------------------------ the journey (plain words)
async function loadCase() {
  if (!S.caseId) return renderJourney();
  try { S.data = await api(`/api/cases/${S.caseId}`); } catch (e) { return; }
  if (!S.merchant || S.data.case.merchant_id !== S.merchant.id) S.data = null;
  renderJourney(); renderTeamActions(); if (S.bts) renderSteps();
}

const PROBLEM = {
  rent_after_return: (c) => `You returned your Soundbox, but ${money(c.anomaly && c.anomaly.amount)} rent was still taken.`,
  settlement_held: (c) => `Your ${money(c.anomaly && c.anomaly.amount)} payment to your bank is stuck.`,
  incentive_pending: (c) => `Your ${money(c.anomaly && c.anomaly.amount)} reward hasn't been paid yet.`,
  debited_not_credited: (c) => `Issue: your ${money(c.anomaly && c.anomaly.amount)} payment to ${c.anomaly ? c.anomaly.payee : "the shop"} shows failed, but the amount was debited.`,
  payment_check: (c) => `A customer says they paid ${money(firstAmount(c))}, but no money or alert reached you.`,
};
const firstAmount = (c) => ((c.parsed && c.parsed.amounts) || [{}])[0].said;
const CAUSE = {
  billing_event_missing: "Our warehouse got your Soundbox back, but our billing team was never told. That's why rent kept coming.",
  bank_no_response: "Your bank didn't confirm the transfer, so the money got stuck on the way.",
  soundbox_offline_alerts_not_delivered: "Your Soundbox lost its connection, so it couldn't announce payments. Don't worry: your customers' money was still reaching you.",
  campaign_batch_pending: "The reward payments haven't been sent out yet.",
  beneficiary_bank_timeout: "Likely cause: the shop's bank didn't respond in time after your bank had already taken the money.",
  tampered_qr: "The money has not come into your account. Please check the QR sticker at your shop, it may have been changed.",
};
const FIX = {
  stop_rental: () => "Stopped the Soundbox rent",
  post_credit: (a) => `Sent ${money(a.params.amount)} back to you`,
  requeue_settlement: (a) => `Sent your ${money(a.params.amount)} to your bank again`,
  remote_restart: () => "Restarted your Soundbox from our side",
  qr_replacement: () => "Blocked the old QR and ordered a new one for your shop",
  raise_reversal_request: () => "Escalated your case to the bank",
};
const DONE = {
  credit_posted: (k) => `${money(k.amount)} is back in your account.`,
  settlement_credited: (k) => `${money(k.amount)} has reached your bank account.`,
  heartbeat_received: () => "Your Soundbox is online and announcing payments again.",
  rental_stopped: () => "Rent has stopped.",
  reversal_credited: (k) => `${money(k.amount)} is back in your bank account.`,
  qr_registered: () => "Your new QR is working: a ₹1 test payment reached you.",
};
const CHECK_LABEL = { rental_stopped: "Rent stopped", credit_posted: "Money back in your account", settlement_credited: "Money in your bank",
  heartbeat_received: "Soundbox back online", reversal_credited: "Bank resolves the issue", qr_registered: "New QR active",
  test_payment_received: "₹1 test payment received" };

function journey(d) {
  const c = d.case, dx = c.diagnosis || {}, k = d.contract, st = c.status;
  const steps = [];
  const add = (state, title, text, extra = "") => steps.push({ state, title, text, extra });
  add("done", "We heard you", `“${esc(c.text_original)}”`);
  if (c.intent === "payment_check" && st === "AWAITING_UTR") {
    const utr = (S.status.scenarios.S6 || {}).utr || "";
    add("done", "We checked your account", `No ${money(firstAmount(c))} reached you in the last 15 minutes.`);
    add("ask", "Share the payment reference (UTR)", "Ask the customer for the 12-digit UTR number on their payment screen. Type it in the chat, or tap:",
      `<button class="utrbtn" data-utr="${esc(utr)}" data-case="${esc(c.id)}">${esc(utr.replace(/(\d{4})(?=\d)/g, "$1 "))}</button>`);
    add("todo", "We fix it", ""); add("todo", "Done", "");
    return steps;
  }
  if (c.intent === "explain_today") {
    const msg = [...S.msgs].reverse().find((m) => m.case_id === c.id && m.kind === "explain");
    add("done", "We explained your account", msg ? esc(msg.text_en || msg.text).replace(/\n/g, "<br>") : "");
    if (st === "AWAITING_CHOICE") {
      add("ask", "Fix the wrong charge?", "One charge looks wrong. Shall we fix it?",
        `<div class="jchoices">${(c.choices || []).map((x) => `<button class="jchoice" data-choice="${esc(x.anomaly_id)}" data-case="${esc(c.id)}">${esc(x.label)}</button>`).join("")}</div>`);
    }
    return steps;
  }
  if (st === "ANSWERED") { add("done", "We answered your question", "See the reply in the chat."); return steps; }
  if (st === "AWAITING_CHOICE") {
    add("ask", "Which one do you mean?", "You have two things pending. Tap the one you're asking about:",
      `<div class="jchoices">${(c.choices || []).map((x) => `<button class="jchoice" data-choice="${esc(x.anomaly_id)}" data-case="${esc(c.id)}">${esc(x.label.replace(/\s*\(.*\)$/, ""))}</button>`).join("")}</div>`);
    add("todo", "We fix it", ""); add("todo", "We check it really worked", ""); add("todo", "Done", "");
    return steps;
  }
  if (c.intent === "payout_request") {
    add("done", "We looked into it", "That account isn't linked to your shop, and we found no wrong charge to refund.");
    add("bad", "We can't do this", "To keep your money safe, we never send money to an unlinked account. Our team has been told, and someone will contact you if needed.");
    return steps;
  }
  const problem = (PROBLEM[c.issue_type] || (() => ""))(c);
  const cause = CAUSE[dx.cause] || dx.cause_label || "";
  if (!c.diagnosis) { add("wait", "Finding the problem", "One moment…"); return steps; }
  add("done", "We found the problem", [problem, cause].filter(Boolean).map(esc).join(" "));
  const ex = c.executed || [];
  if (st === "AWAITING_APPROVAL") add("wait", "We fix it", "A Paytm team member is checking this before we go ahead. This keeps your money safe.");
  else if (st === "NEEDS_HUMAN" || st === "REJECTED") add("wait", "A person will help", "This one needs a person from our team. They'll get back to you.");
  else if (ex.length) add("done", ex.some((a) => a.action === "raise_reversal_request") ? "We escalated it" : "We fixed it", ex.map((a) => (FIX[a.action] || (() => ""))(a)).filter(Boolean).join(". ") + ".");
  if (!k) { add("todo", "We check it really worked", ""); add("todo", "Done", ""); return steps; }
  const late = (k.ladder || []).length > 0;
  if (k.status === "CLOSED") {
    add("done", "We checked it really worked", "We confirmed it in Paytm's own records, not just on our word.");
    const main = k.checks.find((x) => DONE[x.name] && x.name !== "rental_stopped") || k.checks[0];
    add("done", "Done", (DONE[main.name] || (() => "All fixed."))(k) + (late ? " Sorry it took longer than promised." : ""));
  } else if (late) {
    const good = (k.ladder || []).some((s) => s.step === "compensate" && /goodwill/.test(s.result));
    const rbi = (k.ladder || []).filter((s) => s.step === "compensate").map((s) => +((s.result.match(/paid ₹(\d+)/) || [])[1] || 0)).reduce((a, b) => a + b, 0);
    const extra = rbi ? ` The RBI limit (T+5) has passed, so you get ₹100 for each day of delay: ${money(rbi)} added so far.`
      : good ? " We've added ₹50 to your account for the trouble." : "";
    add("warn", "Taking longer than we promised", `Our check showed it hasn't reached you yet, so a person is chasing it now.${extra} New time: ${when(k.deadline)}.`);
    add("todo", "Done", "");
  } else {
    const parts = k.checks.map((x) => `${x.status === "PASS" ? "✅" : "⏳"} ${CHECK_LABEL[x.name] || x.name}`).join("<br>");
    if (k.checks.some((x) => x.name === "reversal_credited"))
      add("wait", "Expected resolution", `By <b>${when(k.deadline)}</b>. We'll keep checking with the bank.<br><span class="small muted">${esc((k.promise && k.promise.basis) || "")}</span><br>${parts}`);
    else add("wait", "We're checking it really worked", `We'll confirm by <b>${when(k.deadline)}</b>.<br>${parts}`);
    add("todo", "Done", "");
  }
  return steps;
}

function renderJourney() {
  const track = $("#track");
  const has = !!S.data;
  $("#journey").hidden = !has;
  $("#s-talk").classList.toggle("solo", !has);
  if (!has) { track.innerHTML = ""; track._html = ""; return; }
  $("#jnone").hidden = true;
  const ICON = { done: "✓", wait: "", ask: "?", warn: "!", bad: "✕", todo: "" };
  const html = journey(S.data).map((s) => `<li class="j ${s.state}"><span class="jdot">${ICON[s.state]}</span>
    <div class="jbody"><b>${s.title}</b>${s.text ? `<p>${s.text}</p>` : ""}${s.extra}</div></li>`).join("");
  if (track._html !== html) { track._html = html; track.innerHTML = html; }
}

// team actions live in the demo strip: in real life a Paytm team member does these
function renderTeamActions() {
  const box = $("#teamact");
  const open = ((S.data && S.data.desk) || []).filter((d) => d.status === "OPEN");
  const html = open.map((d) => {
    const b = (a, l, cls = "") => `<button class="dbtn ${cls}" data-desk="${d.id}" data-act="${a}">${l}</button>`;
    return ({
      approval: `Paytm team: ${b("approve", "✓ Approve", "go")}${b("reject", "Reject")}`,
      escalation: `Paytm team: ${b("resolve", "✓ Fix the stuck refund", "go")}`,
      playbook_review: `Paytm team: ${b("approve", "✓ Save new fix", "go")}`,
      blocked: `Paytm team: ${b("resolve", "Confirm block")}`,
      handoff: `Paytm team: ${b("resolve", "Mark handled")}`,
    })[d.kind] || "";
  }).join(" ");
  if (box._html !== html) { box._html = html; box.innerHTML = html; }
}

// ------------------------------------------------------------------ behind the scenes: 7 steps
const STEP_NAMES = ["Understand", "Diagnose", "Safety check", "Act", "Contract", "Verify", "If fail: remediate"];
function renderSteps() {
  const d = S.data;
  $("#caseid").textContent = d ? `${d.case.id} · ${d.case.merchant_id} · ${d.case.status}` : "no request yet";
  if (!d) {
    const what = { greeting: "a greeting", thanks: "a thank-you", status: "a status question", other: "unclear" }[S.lastIntent];
    const html = `<div class="btsnote">No request is open, so none of the 7 steps ran.${what ? ` The last message was <b>${what}</b>: Saathi only replied, nothing was changed. See the <b>[match]</b> / <b>[llm]</b> lines in the log for how it decided.` : " Say something in the chat to start."}</div>`;
    if ($("#steps")._html !== html) { $("#steps")._html = html; $("#steps").innerHTML = html; }
    return;
  }
  const tr = d.trace;
  const st = d.case.status, k = d.contract;
  // why a step has not run (yet)
  const waitWhy = (n) => {
    if (st === "AWAITING_UTR") return n <= 5 ? "⏳ waiting for the UTR from the user" : "⏳ runs after the fix";
    if (st === "AWAITING_CHOICE") return n <= 5 ? "⏳ runs if the user taps the fix" : "⏳ runs after the fix";
    if (st === "ANSWERED") return "not needed: the question was answered";
    if (n === 6) return k ? "⏳ checks run as time passes: use Skip time" : "—";
    if (n === 7) return k && (k.status === "CLOSED") ? "not needed: the promise was kept" : "⏳ only if the promise is missed";
    return "—";
  };
  const html = STEP_NAMES.map((name, i) => {
    const evs = tr.filter((t) => t.step === i + 1);
    const ai = [...new Set(evs.map((e) => e.ai).filter((a) => a && a !== "none"))].join(", ");
    return `<li class="step ${evs.length ? "done open" : "todo"}"><div class="srow"><span class="sicon">${evs.length ? "✓" : i + 1}</span>
      <span class="sname">${name}</span><span class="stext">${evs.length ? esc(String(evs[evs.length - 1].result)) : esc(waitWhy(i + 1))}</span>${ai ? `<span class="aitag">AI: ${esc(ai)}</span>` : ""}</div>
      ${evs.length ? `<div class="sdetail">${evs.map((t) => `<div><b>${esc(t.title)}</b> · ${esc(typeof t.result === "string" ? t.result : JSON.stringify(t.result))}
        ${(t.evidence || []).map((x) => `<span class="ev">${esc(x)}</span>`).join("")}${t.ms ? ` <span class="ms">${t.ms.toFixed(1)} ms</span>` : ""}</div>`).join("")}</div>` : ""}</li>`;
  }).join("");
  const box = $("#steps");
  if (box._html !== html) { box._html = html; box.innerHTML = html; }
}

// ------------------------------------------------------------------ behind the scenes: log console
const COMPONENTS = ["auth", "mcp-client", "mcp-server", "db", "sarvam", "parse", "match", "cluster", "promise", "incident",
  "lifecycle", "bayes", "llm", "policy", "token", "gateway", "audit", "contract", "verify", "clock", "sim", "remediate",
  "notify", "learning", "human"];
const COLORS = {
  auth: "#7DD3FC", "mcp-client": "#60A5FA", "mcp-server": "#93C5FD", db: "#A5B4FC", sarvam: "#F9A8D4", parse: "#C4B5FD",
  match: "#5EEAD4", cluster: "#2DD4BF", promise: "#FCD34D", incident: "#FB923C", lifecycle: "#FDBA74", bayes: "#A3E635",
  llm: "#E879F9", policy: "#F87171", token: "#FCA5A5", gateway: "#38BDF8", audit: "#94A3B8", contract: "#FDE047",
  verify: "#4ADE80", clock: "#CBD5E1", sim: "#9CA3AF", remediate: "#FB7185", notify: "#67E8F9", learning: "#BEF264", human: "#FFFFFF",
};
function renderFilters() {
  $("#filters").innerHTML = COMPONENTS.map((c) => `<button class="fchip ${S.off.has(c) ? "off" : ""}" data-comp="${c}" style="color:${COLORS[c]}">${c}</button>`).join("");
}
function lineHtml(l) {
  const msg = esc(l.msg).replace(/\(mock\)/g, '<span class="mock">(mock)</span>');
  return `<div class="line ${l.level}"><span class="ts">${vtime(l.virtual_ts)}</span><span style="color:${COLORS[l.component] || "#fff"}">[${esc(l.component)}]</span><span class="m">${msg}</span></div>`;
}
const visible = (l) => l.id > S.clearedAt && !S.off.has(l.component) && !(S.caseOnly && S.caseId && l.case_id !== S.caseId);
function renderLog(full) {
  const box = $("#log"), lines = S.lines.filter(visible);
  if (full) box.innerHTML = lines.slice(-1500).map(lineHtml).join("");
  $("#logcount").textContent = `${lines.length} lines`;
  if (S.auto) box.scrollTop = box.scrollHeight;
}
async function pollLogs() {
  if (S.paused) return;
  try {
    const r = await api(`/api/logs?since=${S.lastLog}`);
    if (S.status) { S.status.clock = r.clock; $("#clock").textContent = "🕒 " + when(r.clock); }
    if (r.last_id < S.lastLog) { S.lines = []; S.lastLog = 0; S.clearedAt = 0; $("#log").innerHTML = ""; return; }
    if (!r.lines.length) return;
    S.lines.push(...r.lines);
    if (S.lines.length > 2000) S.lines = S.lines.slice(-2000);
    S.lastLog = r.lines[r.lines.length - 1].id;
    if (!S.bts) return;
    $("#log").insertAdjacentHTML("beforeend", r.lines.filter(visible).map(lineHtml).join(""));
    renderLog(false);
  } catch (e) { /* server restarting */ }
}

// ------------------------------------------------------------------ behind the scenes: tabs
const pill = (s) => `<span class="pill ${String(s).toLowerCase()}">${esc(s)}</span>`;
async function renderTab() {
  if (!S.bts) return;
  const body = $("#tabbody"), tab = S.tab;
  let html;
  try { html = await { contracts: tabContracts, incidents: tabIncidents, desk: tabDesk, audit: tabAudit, metrics: tabMetrics }[tab](); }
  catch (e) { html = `<div class="empty">${esc(e.message)}</div>`; }
  if (tab !== S.tab || body._html === tab + html) return;
  const open = new Set($$("details[open]", body).map((d) => d.dataset.k));
  body._html = tab + html; body.innerHTML = html;
  $$("details", body).forEach((d) => { if (open.has(d.dataset.k)) d.open = true; });
}
async function tabContracts() {
  const { contracts, clock } = await api("/api/contracts");
  if (!contracts.length) return `<div class="empty">No promises yet.</div>`;
  return contracts.map((k) => {
    const left = Date.parse(k.deadline) - Date.parse(clock);
    return `<div class="item"><div class="item-head"><span class="id">${k.id}</span>${pill(k.status)}<span class="small muted">${k.case_id} · ${k.merchant_id} · ${esc(k.playbook_id)}${k.promise && k.promise.basis ? ` · ${esc(k.promise.basis)}` : ""}</span>
      <span style="flex:1"></span>${["OPEN", "REMEDIATING"].includes(k.status) ? `<span class="countdown ${left < 0 ? "over" : ""}">${fmtDur(left)}</span>` : ""}</div>
      <div class="checks">${k.checks.map((c) => `<div class="check"><span class="dot ${c.status}">${c.status === "PASS" ? "✓" : c.status === "FAIL" ? "✗" : "…"}</span>
        <span class="mono">${esc(c.name)}(${esc(Object.values(c.params).join(", "))})</span><span class="small muted">${esc(c.detail || "pending")}</span>
        ${(c.evidence || []).map((e) => `<span class="ev">${esc(e)}</span>`).join("")}</div>`).join("")}</div>
      ${k.ladder && k.ladder.length ? `<details data-k="lad-${k.id}"><summary>Remediation ladder (${k.ladder.length})</summary><ol class="ladder">${k.ladder.map((s) => `<li><b>${esc(s.step)}</b> · ${esc(s.result)}</li>`).join("")}</ol></details>` : ""}
      ${k.proof ? `<details data-k="proof-${k.id}"><summary>Proof bundle</summary><pre class="proof">${esc(JSON.stringify(k.proof, null, 2))}</pre></details>` : ""}</div>`;
  }).join("");
}
async function tabIncidents() {
  const { incidents } = await api("/api/incidents");
  return incidents.map((i) => `<div class="item"><div class="item-head"><span class="id">${i.id}</span>${pill(i.status)}<span class="pill">${esc(i.source)}</span>
    <span style="flex:1"></span><b>${i.members}</b><span class="small muted">members</span></div><div><b>${esc(i.title)}</b></div>
    <div class="small mono">${esc(i.signature)}</div><div class="small muted">this session: ${i.session_members} ticket(s)</div></div>`).join("");
}
async function tabDesk() {
  const { items } = await api("/api/desk");
  if (!items.length) return `<div class="empty">Team desk is empty.</div>`;
  return items.map((d) => `<div class="item"><div class="item-head"><span class="id">${d.id}</span>${pill(d.status)}<span class="pill">${esc(d.kind)}</span>
    <span class="small muted">${d.case_id} · ${d.merchant_id}</span></div><div>${esc(d.title)}</div>
    ${d.status === "OPEN" ? `<div class="actions">${d.kind === "escalation" ? `<button class="btn sm ok" data-desk="${d.id}" data-act="resolve">Resolve</button>` :
      d.kind === "approval" || d.kind === "playbook_review" ? `<button class="btn sm ok" data-desk="${d.id}" data-act="approve">Approve</button><button class="btn sm light" data-desk="${d.id}" data-act="reject">Reject</button>` :
      `<button class="btn sm" data-desk="${d.id}" data-act="resolve">Close</button>`}</div>` : `<span class="small muted">${esc(d.resolution || "")}</span>`}
    <details data-k="desk-${d.id}"><summary>Proof pack</summary><pre class="proof">${esc(JSON.stringify(d.proof, null, 2))}</pre></details></div>`).join("");
}
async function tabAudit() {
  const { rows } = await api("/api/audit");
  return `<div class="actions"><button class="btn sm" id="verify-chain">Verify chain</button><span class="small muted" id="chain-res">hash = sha256(prev_hash + row)</span></div>` +
    (rows.length ? `<table class="t"><tr><th>#</th><th>time</th><th>case</th><th>action</th><th>hash</th></tr>` +
      rows.map((r) => `<tr><td>${r.seq}</td><td class="mono">${vtime(r.ts)}</td><td>${r.case_id}</td><td>${esc(r.action)}</td><td class="mono">${r.hash.slice(0, 12)}…</td></tr>`).join("") + `</table>` : `<div class="empty">No actions yet.</div>`);
}
async function tabMetrics() {
  const m = await api("/api/metrics");
  const pct = (o) => o.den ? `${Math.round((100 * o.num) / o.den)}%` : "—";
  const tile = (big, lbl, sub) => `<div class="tile"><div class="big">${big}</div><div class="lbl">${lbl}</div><div class="tiny muted">${sub}</div></div>`;
  return `<div class="tiles">
    ${tile(pct(m.verified_resolution_rate), "Verified resolution rate", `${m.verified_resolution_rate.num}/${m.verified_resolution_rate.den} closed by a passing check`)}
    ${tile(pct(m.promises_kept), "Promises kept", `${m.promises_kept.num}/${m.promises_kept.den} before the deadline`)}
    ${tile(`${m.gemini_calls.num} / ${m.gemini_calls.den}`, "Gemini calls vs cases", `live ${m.gemini_calls.live} · mock ${m.gemini_calls.mock}`)}
    ${tile(m.tickets_per_incident.den ? (m.tickets_per_incident.num / m.tickets_per_incident.den).toFixed(1) : "—", "Tickets per incident", `${m.tickets_per_incident.num} → ${m.tickets_per_incident.den}`)}</div>
    <div class="item small"><b>Autonomy rule:</b> ${esc(m.autonomy_rule)}</div>
    <table class="t"><tr><th>playbook</th><th>autonomy</th><th>verified</th></tr>${m.playbooks.map((p) => `<tr><td class="mono">${esc(p.id)}</td><td>${pill(p.autonomy)}</td><td>${p.wins}/${p.attempts}</td></tr>`).join("")}</table>`;
}
async function refreshDeskCount() {
  const { items } = await api("/api/desk");
  const n = items.filter((d) => d.status === "OPEN").length;
  $("#desk-n").hidden = !n; $("#desk-n").textContent = n;
}
function selectTab() { $$("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.tab === S.tab)); $("#tabbody")._html = ""; renderTab(); }

// ------------------------------------------------------------------ clicks
document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, .srow");
  if (!t) return;
  try {
    if (t.classList.contains("srow")) t.parentElement.classList.toggle("open");
    else if (t.dataset.pick) { S.pick = t.dataset.pick; renderLang(); }
    else if (t.dataset.go === "who") renderWho();
    else if (t.dataset.lang) await login(S.pick, t.dataset.lang);
    else if (t.id === "change") { await api("/api/logout", { json: {} }); S.merchant = null; S.data = null; renderTeamActions(); renderWho(); }
    else if (t.id === "mic") toggleMic();
    else if (t.id === "send") sendText($("#text").value);
    else if (t.dataset.try) sendText(S.examples[+t.dataset.try].text);
    else if (t.dataset.switch) {
      const id = S.status.users.find((u) => u.type === t.dataset.switch).id;
      if (!S.merchant || S.merchant.id !== id) await login(id, S.lang);
    } else if (t.dataset.utr) {
      t.disabled = true; showWorking(t.dataset.utr);
      try { await api("/api/utr", { json: { case_id: t.dataset.case, utr: t.dataset.utr } }); } finally { stopWorking(); }
      await loadChat(); loadCase();
    }
    else if (t.id === "speak") {
      S.speakOn = !S.speakOn; t.classList.toggle("on", S.speakOn); t.textContent = S.speakOn ? "🔊 Voice on" : "🔇 Voice off";
      if (!S.speakOn && "speechSynthesis" in window) speechSynthesis.cancel();
    } else if (t.dataset.say) { const m = S.msgs.find((x) => String(x.id) === t.dataset.say); if (m) speak(m.text, m.lang); }
    else if (t.dataset.choice) {
      t.disabled = true; showWorking(t.textContent.trim());
      try { await api("/api/choice", { json: { case_id: t.dataset.case, anomaly_id: t.dataset.choice } }); } finally { stopWorking(); }
      await loadChat(); loadCase();
    } else if (t.dataset.adv) {
      t.disabled = true;
      await api("/api/clock/advance", { json: { hours: +t.dataset.adv } });
      t.disabled = false; toast(`Skipped ${t.textContent.replace("+", "")}`); refresh();
    } else if (t.dataset.desk) {
      t.disabled = true;
      await api(`/api/desk/${t.dataset.desk}/${t.dataset.act}`, { json: {} });
      toast("Done"); refresh();
    } else if (t.id === "reset") {
      await api("/api/reset", { json: {} });
      Object.assign(S, { merchant: null, caseId: null, data: null, lines: [], lastLog: 0, clearedAt: 0 });
      $("#log").innerHTML = ""; renderTeamActions(); await loadStatus(); renderWho();
      toast("Started over");
    } else if (t.id === "techbtn") { S.bts = true; $("#bts").hidden = false; renderSteps(); renderLog(true); renderTab(); refreshDeskCount(); }
    else if (t.id === "btsclose") { S.bts = false; $("#bts").hidden = true; }
    else if (t.id === "spike") {
      t.disabled = true;
      const r = await api("/api/spike", { json: { n: 40 } });
      t.disabled = false; toast(`${r.cases} tickets grouped into ${r.incidents.length} incident`); S.tab = "incidents"; selectTab();
    } else if (t.dataset.comp) { S.off.has(t.dataset.comp) ? S.off.delete(t.dataset.comp) : S.off.add(t.dataset.comp); renderFilters(); renderLog(true); }
    else if (t.id === "lg-pause") { S.paused = !S.paused; t.classList.toggle("on", S.paused); }
    else if (t.id === "lg-auto") { S.auto = !S.auto; t.classList.toggle("on", S.auto); }
    else if (t.id === "lg-case") { S.caseOnly = !S.caseOnly; t.classList.toggle("on", S.caseOnly); renderLog(true); }
    else if (t.id === "lg-clear") { S.clearedAt = S.lastLog; renderLog(true); }
    else if (t.id === "lg-copy") { await navigator.clipboard.writeText(S.lines.filter(visible).map((l) => `${vtime(l.virtual_ts)} [${l.component}] ${l.msg}`).join("\n")); toast("Copied"); }
    else if (t.dataset.tab) { S.tab = t.dataset.tab; selectTab(); }
    else if (t.id === "verify-chain") {
      const r = await api("/api/audit/verify", { json: {} });
      $("#chain-res").innerHTML = r.ok ? `<b style="color:var(--pass)">✓ chain intact · ${r.rows} rows</b>` : `<b style="color:var(--fail)">✗ broken at #${r.broken_at}</b>`;
    }
  } catch (x) { toast(x.message); t.disabled = false; }
});
$("#text").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendText($("#text").value); } });

async function refresh() {
  await Promise.allSettled([loadChat(), loadCase(), renderTab(), S.bts ? refreshDeskCount() : null]);
}

// ------------------------------------------------------------------ boot
(async () => {
  const s = await loadStatus();
  renderFilters();
  S.voice = "kavya";
  try { S.voice = localStorage.getItem("saathi-voice") || "kavya"; } catch (e) { /* storage blocked */ }
  $("#voice").innerHTML = Object.entries(s.voices).map(([k, v]) => `<option value="${k}" ${k === S.voice ? "selected" : ""}>🗣 ${esc(v)}</option>`).join("");
  $("#voice").addEventListener("change", (e) => {
    S.voice = e.target.value;
    try { localStorage.setItem("saathi-voice", S.voice); } catch (x) { /* ignore */ }
    const last = [...S.msgs].reverse().find((m) => m.direction === "out");
    speak(last ? last.text : "Namaste! Main Saathi hoon.", last ? last.lang : "hinglish");   // preview the new voice
  });
  if ("speechSynthesis" in window) speechSynthesis.getVoices();
  $("#banklate").checked = !!s.bank_late;
  $("#banklate").addEventListener("change", async (e) => {
    await api("/api/sim/bank_late", { json: { on: e.target.checked } });
    toast(e.target.checked ? "Bank will miss the refund deadline (customer test)" : "Bank refunds on time");
  });
  if (s.session_merchant && userById(s.session_merchant)) enterTalk(userById(s.session_merchant), s.session_lang || "en");
  else renderWho();
  setInterval(pollLogs, 500);
  setInterval(refresh, 1500);
})();
