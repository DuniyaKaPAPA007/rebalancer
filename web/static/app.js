/* Weekly Rebalancer -- frontend */
"use strict";

const $  = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
let PLAN = null, HEALTH = null, WL = null;

/* ---------------- helpers ---------------- */
function inr(x, dec = 0) {
  if (x == null || isNaN(x)) return "—";
  const neg = x < 0; x = Math.abs(x);
  let s = dec ? x.toFixed(dec) : Math.round(x).toString();
  let frac = ""; if (dec) { const p = s.split("."); s = p[0]; frac = "." + p[1]; }
  if (s.length > 3) {
    let head = s.slice(0, -3), tail = s.slice(-3), out = [];
    while (head.length > 2) { out.unshift(head.slice(-2)); head = head.slice(0, -2); }
    if (head) out.unshift(head);
    s = out.join(",") + "," + tail;
  }
  return (neg ? "-₹" : "₹") + s + frac;
}
const cr = x => x >= 1e7 ? (x / 1e7).toFixed(2) + " cr" : (x / 1e5).toFixed(2) + " L";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { /* ignore */ }
  if (!r.ok) throw new Error(body && body.detail ? body.detail : `HTTP ${r.status}`);
  return body;
}
function banner(kind, icon, html) {
  return `<div class="banner ${kind}"><span class="bi">${icon}</span><div>${html}</div></div>`;
}
function toast(msg, kind="info"){
  const wrap = $("#toastWrap"); if(!wrap) return;
  const el = document.createElement("div");
  el.className = "toast";
  const icons = {info:"✦", good:"✓", warn:"!", bad:"✕"};
  el.innerHTML = `<span>${icons[kind]||"✦"}</span><span>${esc(msg)}</span>`;
  if(kind==="good") el.style.background = "var(--good)";
  if(kind==="bad") el.style.background = "var(--critical)";
  if(kind==="warn") el.style.background = "#92400e";
  wrap.appendChild(el);
  setTimeout(()=>{ el.style.opacity="0"; el.style.transform="translateY(6px)"; }, 2600);
  setTimeout(()=> el.remove(), 3000);
}
function busy(btn, on, label) {
  btn.disabled = on;
  if (on) { btn.dataset.old = btn.innerHTML; btn.innerHTML = `<span class="spin"></span>${label || "…"}`; }
  else if (btn.dataset.old) btn.innerHTML = btn.dataset.old;
}
// --- tiny chart helper (no dependency, fallback if Chart.js missing) ---
function donutChart(canvasId, labels, values, colors){
  const c = document.getElementById(canvasId); if(!c) return;
  const ctx = c.getContext("2d");
  // try Chart.js if available
  if(window.Chart){
    if(c._chart) c._chart.destroy();
    c._chart = new Chart(ctx, {
      type:"doughnut",
      data:{ labels, datasets:[{ data:values, backgroundColor:colors, borderWidth:0, hoverOffset:4 }]},
      options:{
        cutout:"62%", plugins:{ legend:{display:false}, tooltip:{ callbacks:{ label:(ctx)=> `${ctx.label}: ${inr(ctx.parsed)}` } } },
        animation:{ duration:400, easing:"easeOutQuart" }
      }
    });
    return;
  }
  // fallback: simple SVG donut via canvas arc
  const total = values.reduce((a,b)=>a+b,0) || 1;
  const dpr = window.devicePixelRatio||1;
  const w=c.width=dpr*180, h=c.height=dpr*180;
  c.style.width="180px"; c.style.height="180px";
  ctx.clearRect(0,0,w,h); ctx.save(); ctx.scale(dpr,dpr);
  let ang=-Math.PI/2;
  const cx=90,cy=90,r=70, r2=42;
  labels.forEach((_,i)=>{
    const v=values[i]; const a= v/total* Math.PI*2;
    ctx.beginPath(); ctx.moveTo(cx,cy); ctx.arc(cx,cy,r,ang,ang+a); ctx.closePath();
    ctx.fillStyle=colors[i%colors.length]; ctx.fill();
    ang+=a;
  });
  // hole
  ctx.globalCompositeOperation="destination-out";
  ctx.beginPath(); ctx.arc(cx,cy,r2,0,Math.PI*2); ctx.fill();
  ctx.restore();
}
function barSpark(canvasId, labels, values, color){
  const c=document.getElementById(canvasId); if(!c) return;
  const ctx=c.getContext("2d");
  if(window.Chart){
    if(c._chart) c._chart.destroy();
    c._chart = new Chart(ctx, {
      type:"bar",
      data:{ labels, datasets:[{ data:values, backgroundColor:color, borderRadius:6, barThickness:14 }]},
      options:{
        indexAxis:"y", plugins:{ legend:{display:false}, tooltip:{ callbacks:{ label:(ctx)=> `${ctx.label}: ${ctx.parsed.x>0?"+":""}${inr(ctx.parsed.x)}` } } },
        scales:{ x:{ grid:{color:"rgba(148,163,184,.15)"}, ticks:{ callback:(v)=> inr(v) } }, y:{ grid:{display:false} } },
        animation:{ duration:300 }
      }
    });
    return;
  }
}

/* ---------------- tabs ---------------- */
function showTab(id) {
  $$("nav.tabs button").forEach(b => b.classList.toggle("active", b.dataset.tab === id));
  $$(".panel").forEach(p => p.classList.toggle("active", p.id === id));
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (id === "tPlan") loadDeploy();
  if (id === "tPort") loadPortfolio();
  if (id === "tCfg") loadConfig();
}
$$("nav.tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));

/* ---------------- theme ---------------- */
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.dataset.theme;
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("rebalTheme", next);
};

/* ---------------- health ---------------- */
async function refreshHealth() {
  try {
    HEALTH = await api("/api/health");
  } catch (e) {
    $("#healthBadge").className = "badge bad";
    $("#healthBadge").innerHTML = `<span class="dot"></span><span>server band hai</span>`;
    return;
  }
  const b = $("#healthBadge");
  const paper = HEALTH.mode === "paper";
  b.className = "badge " + (paper ? "demo" : (HEALTH.broker_ok ? "ok" : "bad"));
  b.innerHTML = `<span class="dot"></span><span>${esc(HEALTH.broker_msg)}</span>`;
  $$("#modeSw button").forEach(x => x.classList.toggle("on", x.dataset.mode === HEALTH.mode));
  renderHome();
}

$$("#modeSw button").forEach(btn => btn.onclick = async () => {
  const mode = btn.dataset.mode;
  if (mode === HEALTH?.mode) return;
  if (mode === "live" && !confirm("Live mode = asli paisa.\n\nPlan banane se abhi bhi kuch nahi jaata,\npar Execute tab se asli order ja sakte hain.\n\nAage badhein?")) return;
  try {
    await api("/api/mode", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }) });
    PLAN = null; $("#planWrap").classList.add("hidden"); $("#planEmpty").classList.remove("hidden");
    $("#execWrap").classList.add("hidden"); $("#execEmpty").classList.remove("hidden");
    $("#planPill").classList.add("hidden");
    await refreshHealth();
  } catch (e) { alert(e.message); }
});

/* ---------------- home ---------------- */
function renderHome() {
  if (!HEALTH) return;
  const paper = HEALTH.mode === "paper";
  if (HEALTH.creds_broken && !HEALTH.mode_chosen_by_user) {
    const b = banner("block", "✕",
      `<b>Dhan se connect nahi ho pa raha — app ruk gayi hai.</b>` +
      `<br><br>${esc(HEALTH.creds_broken)}` +
      `<br><br>Yahan nakli portfolio dikha kar tumhe dhoka dena sabse khatarnaak hota — ` +
      `tum us par asli trade kar dete. Isliye kuch nahi dikha rahe.` +
      `<br><br><button class="btn danger" onclick="showTab('tConn')">1 · Connect se check karo</button>`);
    $("#homeBanner").innerHTML = b;
    ["#uploadGate", "#planGate"].forEach(id => { const e = $(id); if (e) e.innerHTML = b; });
    $("#homeStats").innerHTML = "";
    return;
  }
  const auto = HEALTH.autodetect_msg
    ? banner(HEALTH.mode === "live" ? "good" : "warn",
             HEALTH.mode === "live" ? "✓" : "!",
             esc(HEALTH.autodetect_msg)) : "";
  $("#homeBanner").innerHTML = auto + (paper
    ? banner(HEALTH.creds ? "block" : "info", HEALTH.creds ? "!" : "i",
        HEALTH.creds
        ? `<b>Dhyan do — plan NAKLI paise par banega.</b> Tumhare credentials to hain, par app abhi Demo mode mein hai. Asli Dhan balance par plan banane ke liye upar <b>Live</b> dabao.`
        : `<b>Demo mode chalu hai.</b> Portfolio nakli hai, koi order market mein nahi jaayega. Poori app aise hi try kar sakte ho.${HEALTH.creds ? " Asli trading ke liye upar <b>Live</b> dabao." : " Live mode ke liye pehle Connection tab se credentials jodo."}`)
    : banner("block", "!", `<b>Live mode.</b> NAV tumhare asli Dhan account se aa raha hai. Execute tab se asli orders jaayenge — plan aur rehearsal abhi bhi safe hain.`));

  const steps = [
    { k: "Watchlist", v: HEALTH.watchlist_loaded ? `${HEALTH.wl_count} stocks` : "nahi hai", ok: HEALTH.watchlist_loaded, m: HEALTH.wl_name || "CSV upload karo" },
    { k: "Plan", v: HEALTH.has_plan ? "ban gaya" : "nahi bana", ok: HEALTH.has_plan, m: HEALTH.has_plan ? "Plan tab dekho" : "watchlist ke baad" },
    { k: "Broker", v: paper ? "Demo" : (HEALTH.broker_ok ? "Connected" : "Nahi juda"), ok: paper || HEALTH.broker_ok, m: paper ? "nakli data" : "Dhan" },
  ];
  renderGates();
  $("#homeStats").innerHTML = steps.map(s =>
    `<div class="stat"><div class="k">${s.k}</div><div class="v" style="color:${s.ok ? "var(--good)" : "var(--ink3)"}">${esc(s.v)}</div><div class="m">${esc(s.m)}</div></div>`).join("");
}

/* ---------------- step-1 gate ---------------- */
function renderGates() {
  if (!HEALTH) return;
  if (HEALTH.creds_broken && !HEALTH.mode_chosen_by_user) return;  // renderHome sambhal raha hai
  const paper = HEALTH.mode === "paper";
  let g = "";
  if (paper) {
    g = banner(HEALTH.creds ? "block" : "warn", "!",
      HEALTH.creds
        ? `<b>Ruko — abhi Demo mode hai.</b> Tumhare credentials to hain par app unhe use nahi kar rahi. Aage jo bhi plan banega wo <b>nakli ₹1 crore</b> par banega, tumhare asli Dhan balance par nahi.` +
          `<br><br><button class="btn danger" onclick="showTab('tConn')">1 · Connect par jao</button>`
        : `<b>Pehla step baaki hai.</b> Dhan se connect nahi hue — plan nakli paise par banega. Asli balance par chalana hai toh pehle connect karo.` +
          `<br><br><button class="btn primary" onclick="showTab('tConn')">1 · Connect par jao</button>`);
  }
  const u = $("#uploadGate"), p = $("#planGate");
  if (u) u.innerHTML = g;
  if (p) p.innerHTML = g;
}

/* ---------------- upload ---------------- */
const drop = $("#drop"), fileInput = $("#fileInput");
drop.onclick = () => fileInput.click();
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => {
  e.preventDefault(); drop.classList.remove("over");
  if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
};
fileInput.onchange = e => { if (e.target.files.length) upload(e.target.files[0]); };

async function upload(file) {
  if (!/\.csv$/i.test(file.name)) { $("#upMsg").innerHTML = banner("block", "!", "Sirf .csv file chalegi."); return; }
  $("#upMsg").innerHTML = banner("info", "…", `<b>${esc(file.name)}</b> padh rahe hain…`);
  const fd = new FormData(); fd.append("file", file);
  try {
    WL = await api("/api/watchlist", { method: "POST", body: fd });
  } catch (e) {
    $("#upMsg").innerHTML = banner("block", "!", `<b>Upload fail:</b> ${esc(e.message)}`);
    drop.classList.remove("loaded");
    return;
  }
  drop.classList.add("loaded");
  drop.querySelector(".t").textContent = file.name;
  drop.querySelector(".h").textContent = `${WL.count} stocks mile — dusri file chahiye toh click karo`;

  let msg = "";
  if (WL.format === "backtest") {
    msg += banner("info", "i",
      `<b>Backtest file pehchaan li.</b> Ismein poori history hai — maine sabse aakhri period <b>${esc(WL.period)}</b> uthaya hai, wahi strategy ki abhi ki list hai. Purane periods rebalance ke liye bekaar hain.` +
      (WL.prev_period ? `<br><br>Demo portfolio pichhle period <b>${esc(WL.prev_period)}</b> se bana diya hai — isse asli rebalance dikhega (kuch naam rahenge, kuch jaayenge, kuch aayenge).` : ""));
  }
  msg += banner("good", "✓", `<b>${WL.count} stocks</b> mil gaye. Top ${WL.n_stocks} mein paisa barabar bantega, ${WL.n_stocks + 1}va slot bache hue cash ke liye hai.`);
  if (WL.warnings.length)
    msg += banner("warn", "!", `<b>Dhyan do:</b><ul>${WL.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`);
  $("#upMsg").innerHTML = msg;

  const rows = WL.stocks.map(s => {
    const tag = s.in_top ? `<span class="tag TOPUP">TOP ${WL.n_stocks}</span>`
      : s.overflow ? `<span class="tag OVERFLOW">n+1</span>`
        : `<span class="tag gray">reserve</span>`;
    return `<tr><td class="num">${s.rank}</td><td class="l sym">${esc(s.symbol)}</td>
      <td class="l muted">${esc(s.name || "")}</td>
      <td class="num">${s.ltp ? inr(s.ltp, 2) : "—"}</td>
      <td class="num">${s.mcap_cr ? "₹" + cr(s.mcap_cr * 1e7) : "—"}</td>
      <td>${tag}</td></tr>`;
  }).join("");
  $("#wlTable").innerHTML =
    `<thead><tr><th>S.No</th><th class="l">Symbol</th><th class="l">Naam</th><th>LTP</th><th>Mkt cap</th><th>Slot</th></tr></thead><tbody>${rows}</tbody>`;
  $("#wlSub").innerHTML = `${esc(WL.filename)} — ${WL.count} naam` +
    (WL.format === "backtest"
      ? ` <span class="tag OVERFLOW">BACKTEST</span> period ${esc(WL.period)}`
      : ` <span class="tag gray">SCREENER</span>`);
  $("#wlCard").classList.remove("hidden");
  $("#slotCard").classList.remove("hidden");
  $("#ofChk").checked = WL.use_overflow !== false;
  $$("#slotMode button").forEach(b => b.classList.toggle("on",
    (b.dataset.sm === "auto") === (WL.n_override == null)));
  $("#slotN").classList.toggle("hidden", WL.n_override == null);
  if (WL.n_override != null) $("#slotN").value = WL.n_override;
  renderSlots({ n_stocks: WL.n_stocks, auto: WL.n_override == null,
                use_overflow: WL.use_overflow, list_len: WL.count });
  refreshHealth();
}

/* ---------------- slots ---------------- */
function renderSlots(d) {
  if (!d || !d.list_len) { $("#slotOut").innerHTML = ""; return; }
  const n = d.n_stocks, of = d.use_overflow && d.list_len > n;
  const wt = 100 / n;
  $("#slotOut").innerHTML = banner("good", "✓",
    `<b>${n} stock</b> mein barabar paisa — har ek <b>${wt.toFixed(wt < 1 ? 2 : 1)}%</b>` +
    (of ? `, aur bacha hua ${d.list_len}va naam (n+1 slot) mein.` : `.`) +
    (d.auto ? ` <span class="muted">(Auto — list ke ${d.list_len} naam se nikaala)</span>` : ``));
}

async function pushSlots(mode, n) {
  try {
    const r = await api("/api/slots", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, n, use_overflow: $("#ofChk").checked })
    });
    renderSlots(r);
    PLAN = null;
    $("#planWrap").classList.add("hidden"); $("#planEmpty").classList.remove("hidden");
    $("#execWrap").classList.add("hidden"); $("#execEmpty").classList.remove("hidden");
    $("#planPill").classList.add("hidden");
  } catch (e) { $("#slotOut").innerHTML = banner("block", "!", esc(e.message)); }
}

$$("#slotMode button").forEach(b => b.onclick = () => {
  const fixed = b.dataset.sm === "fixed";
  $$("#slotMode button").forEach(x => x.classList.toggle("on", x === b));
  $("#slotN").classList.toggle("hidden", !fixed);
  if (fixed) {
    if (!$("#slotN").value) $("#slotN").value = WL ? WL.n_stocks : 10;
    $("#slotN").focus();
    pushSlots("fixed", parseInt($("#slotN").value, 10));
  } else pushSlots("auto", null);
});
let slotT;
$("#slotN").oninput = () => {
  clearTimeout(slotT);
  slotT = setTimeout(() => {
    const v = parseInt($("#slotN").value, 10);
    if (v > 0) pushSlots("fixed", v);
  }, 450);
};
$("#ofChk").onchange = () => {
  const fixed = $('#slotMode button[data-sm="fixed"]').classList.contains("on");
  pushSlots(fixed ? "fixed" : "auto", fixed ? parseInt($("#slotN").value, 10) : null);
};
$("#toPlanBtn").onclick = () => { showTab("tPlan"); makePlan(); };

/* ---------------- deploy budget ---------------- */
let DEP = null, depT = null;

function depModeNow() {
  const b = $("#depMode button.on");
  return b ? b.dataset.dm : "all";
}

function renderDeploy(d) {
  DEP = d || DEP;
  if (!DEP) return;
  const mode = DEP.mode === "pct" ? "pct" : DEP.mode === "amount" ? "amount" : "all";
  $$("#depMode button").forEach(b => b.classList.toggle("on", b.dataset.dm === mode));
  $("#depPct").classList.toggle("hidden", mode !== "pct");
  $("#depAmt").classList.toggle("hidden", mode !== "amount");
  $("#depSliderWrap").classList.toggle("hidden", mode !== "pct");

  const pv = DEP.preview, nav = DEP.nav;
  const bar = $("#depBar");
  if (pv && nav > 0) {
    const eqp = Math.max(0, Math.min(100, pv.equity / nav * 100));
    bar.classList.remove("hidden");
    bar.querySelector(".eq").style.width = eqp + "%";
    bar.querySelector(".cs").style.width = (100 - eqp) + "%";
    bar.querySelector(".eq").textContent = eqp >= 12 ? `stocks ${eqp.toFixed(0)}%` : "";
    bar.querySelector(".cs").textContent = (100 - eqp) >= 12 ? `cash ${(100 - eqp).toFixed(0)}%` : "";
  } else bar.classList.add("hidden");

  let h = "";
  if (DEP.nav_error)
    h += banner("warn", "!", `<b>Capital nahi aayi:</b> ${esc(DEP.nav_error)}<br>` +
      `<span class="muted">Setting phir bhi save ho gayi — plan banate waqt lag jaayegi.</span>`);
  else if (!nav)
    h += banner("info", "i", "Capital abhi nahi aayi. Connect tab se Dhan jodo, phir yahan " +
      "asli number dikhega.");

  if (pv && nav > 0) {
    const kind = mode === "all" ? "good" : "info";
    h += banner(kind, mode === "all" ? "✓" : "i",
      `<b>${inr(pv.equity)}</b> stocks mein jaayega, <b>${inr(pv.cash)}</b> cash mein rahega.` +
      `<br><span class="muted">Capital ${inr(nav)} — ${esc(DEP.nav_source || "")}` +
      (DEP.holdings_value ? ` (stocks ${inr(DEP.holdings_value)} + cash ${inr(DEP.free_cash)})` : "") +
      ` · setting: ${esc(pv.label)}</span>` +
      (pv.capped ? `<br><b>Note:</b> itna paisa hai nahi / ${DEP.reserve_pct.toFixed(1)}% reserve ` +
        `nikal kar — jitna ho sakta tha utna hi liya.` : ""));
    if (DEP.holdings_value > pv.equity + 1)
      h += banner("warn", "!",
        `Abhi stocks mein <b>${inr(DEP.holdings_value)}</b> laga hua hai, aur budget ` +
        `<b>${inr(pv.equity)}</b> ka hai. Plan is farak ko <b>bechkar</b> poora karega ` +
        `(<b>${inr(DEP.holdings_value - pv.equity)}</b> nikalega). Ye jaanbujh kar ho raha hai — ` +
        `agar nahi chahiye toh budget badha do.`);
  }
  $("#depOut").innerHTML = h;
}

async function loadDeploy() {
  try { renderDeploy(await api("/api/deploy")); }
  catch (e) { $("#depOut").innerHTML = banner("warn", "!", esc(e.message)); }
}

async function pushDeploy(mode, pct, amount) {
  try {
    const r = await api("/api/deploy", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, pct, amount })
    });
    // budget badla = purana plan jhootha. Server ne bhi hata diya hai.
    PLAN = null;
    $("#planWrap").classList.add("hidden"); $("#planEmpty").classList.remove("hidden");
    $("#execWrap").classList.add("hidden"); $("#execEmpty").classList.remove("hidden");
    $("#planPill").classList.add("hidden");
    renderDeploy(r);
  } catch (e) { $("#depOut").innerHTML = banner("block", "!", esc(e.message)); }
}

function pushDeployNow() {
  const m = depModeNow();
  if (m === "pct") {
    const v = parseFloat($("#depPct").value);
    if (!(v >= 0 && v <= 100)) {
      $("#depOut").innerHTML = banner("block", "!", "Percent 0 se 100 ke beech likho.");
      return;
    }
    pushDeploy("pct", v, null);
  } else if (m === "amount") {
    const v = parseFloat(String($("#depAmt").value).replace(/[,\s₹]/g, ""));
    if (!(v >= 0)) {
      $("#depOut").innerHTML = banner("block", "!", "Rupees mein ek number likho.");
      return;
    }
    pushDeploy("amount", null, v);
  } else pushDeploy("all", null, null);
}

$$("#depMode button").forEach(b => b.onclick = () => {
  $$("#depMode button").forEach(x => x.classList.toggle("on", x === b));
  const m = b.dataset.dm;
  $("#depPct").classList.toggle("hidden", m !== "pct");
  $("#depAmt").classList.toggle("hidden", m !== "amount");
  $("#depSliderWrap").classList.toggle("hidden", m !== "pct");
  if (m === "pct") {
    if (!$("#depPct").value) $("#depPct").value = DEP && DEP.mode === "pct" ? DEP.pct : 60;
    $("#depSlider").value = Math.round(parseFloat($("#depPct").value) || 0);
    $("#depPct").focus();
  } else if (m === "amount") {
    if (!$("#depAmt").value && DEP && DEP.nav) $("#depAmt").value = Math.round(DEP.nav / 2);
    $("#depAmt").focus();
  }
  pushDeployNow();
});

$("#depSlider").oninput = () => {
  $("#depPct").value = $("#depSlider").value;
  clearTimeout(depT); depT = setTimeout(pushDeployNow, 300);
};
$("#depPct").oninput = () => {
  const v = parseFloat($("#depPct").value);
  if (v >= 0 && v <= 100) $("#depSlider").value = Math.round(v);
  clearTimeout(depT); depT = setTimeout(pushDeployNow, 450);
};
$("#depAmt").oninput = () => { clearTimeout(depT); depT = setTimeout(pushDeployNow, 550); };

/* ---------------- plan ---------------- */
$("#mkPlanBtn").onclick = () => makePlan();
$("#rePlanBtn").onclick = () => makePlan();
$("#toExecBtn").onclick = () => showTab("tExec");

async function makePlan() {
  const btn = $("#mkPlanBtn");
  busy(btn, true, "Plan bana rahe hain…");
  $("#planBanners").innerHTML = banner("info", "…", "Portfolio aur live prices la rahe hain…");
  $("#planEmpty").classList.remove("hidden"); $("#planWrap").classList.add("hidden");
  try {
    PLAN = await api("/api/plan", { method: "POST" });
  } catch (e) {
    busy(btn, false);
    $("#planEmpty").classList.remove("hidden");
    $("#planWrap").classList.remove("hidden");
    $("#planBanners").innerHTML = banner("block", "!", `<b>Plan nahi ban paya:</b> ${esc(e.message)}`);
    return;
  }
  busy(btn, false);
  PLAN._loadedAt = Date.now();
  renderPlan();
  refreshHealth();
}

function renderPlan() {
  const p = PLAN;
  $("#planEmpty").classList.add("hidden");
  $("#planWrap").classList.remove("hidden");
  $("#planPill").classList.remove("hidden");
  $("#planPill").textContent = p.orders.length;

  let bn = "";
  if (p.is_liquidation)
    bn += banner("block", "!", `<b>SAB BECHO plan.</b> Ye normal rebalance nahi hai -- ` +
      `poora portfolio bik jaayega aur paisa 100% cash mein aa jaayega. ` +
      `Execute karne ke liye <code>sab bech do</code> likhna padega.`);
  if (p.mode === "paper") bn += banner("info", "i", "<b>Demo plan</b> — nakli portfolio par bana hai.");
  if (p.blockers.length)
    bn += banner("block", "✕", `<b>BLOCKED — ye plan execute nahi hoga.</b><ul>${p.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>`);
  const ps = p.price_source || {};
  if (ps.fallback && Object.keys(ps.fallback).length)
    bn += banner("warn", "!",
      `<b>Prices Dhan se nahi aaye.</b> ` +
      Object.entries(ps.fallback).map(([k, v]) => `<b>${esc(k)}</b> se ${v} naam`).join(", ") +
      (ps.age_min ? ` &middot; sabse purana <b>${ps.age_min} min</b>` : "") +
      `<br><span class="muted">Ye delayed prices hain. LIMIT order in par lag raha hai &mdash; buffer bada karne ki soch lo.</span>`);
  if (p.warnings.length)
    bn += banner("warn", "!", `<b>${p.warnings.length} warning</b> — padh lo, rukavat nahi hai.<ul>${p.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>`);
  if (!p.blockers.length && !p.warnings.length)
    bn += banner("good", "✓", "Koi warning nahi. Plan saaf hai.");
  $("#planBanners").innerHTML = bn;

  const sells = p.orders.filter(o => o.side === "SELL");
  const buys = p.orders.filter(o => o.side === "BUY");
  const sv = sells.reduce((a, o) => a + o.value, 0);
  const bv = buys.reduce((a, o) => a + o.value, 0);
  const keeps = sells.filter(o => o.reason === "TRIM").length +
                buys.filter(o => o.reason === "TOPUP").length;

  const wpct = p.nav ? (p.slice_value / p.nav * 100) : 0;
  const mr = p.min_required;
  let stats = [
    ["NAV", inr(p.nav), esc(p.capital_source || "portfolio + cash")],
    ["Slots", `${p.slots}`, `${p.list_len} naam mein se` + (p.auto ? " (auto)" : "")],
    ["Har stock", inr(p.slice_value), `${wpct.toFixed(wpct < 1 ? 2 : 1)}% each`],
    ["Free cash", inr(p.free_cash), p.mode === "paper" ? "demo" : "Dhan /fundlimit se"],
    ["Stocks mein", inr(p.target_equity), esc(p.deploy_label || "poori capital")],
    ["Cash mein rahega", inr(p.cash_after), "market se bahar"],
    ["Bechna", inr(sv), `${sells.length} orders`, "sell"],
    ["Kharidna", inr(bv), `${buys.length} orders`, "buy"],
    ["Rakhe ja rahe", keeps, "bina beche adjust"],
    ["Churn", (p.churn_pct * 100).toFixed(1) + "%", "NAV ka kitna bika"],
  ];
  if (mr) {
    const need = mr.min_nav || mr.min_investable;
    const allocated = mr.allocated_nav || p.nav;
    const ok = allocated >= need - 1;
    stats.push(["Minimum NAV", inr(need), ok ? "✅ aapka NAV kaafi hai" : `⚠️ kam hai — ${inr(need - allocated)} aur chahiye` ]);
    stats.push(["Min / stock", inr(mr.min_slice), `har stock me ≥${mr.min_slice ? Math.ceil(mr.min_slice) : ""} (₹${mr.min_trade_val} min)` ]);
  }
  $("#planStats").innerHTML = stats.map(([k, v, m, cls]) =>
    `<div class="stat ${cls || ""}"><div class="k">${k}</div><div class="v num">${v}</div><div class="m">${m}</div></div>`).join("");

  // Minimum capital banner - always show, highlight if allocated < required
  if (mr) {
    const needNav = mr.min_nav;
    const needInv = mr.min_investable;
    const alloc = mr.allocated_investable;
    const per = mr.per_stock || [];
    const details = per.slice(0,5).map(x=>`${esc(x.symbol)} ₹${Math.round(x.price).toLocaleString('en-IN')}×${x.min_qty}=₹${Math.round(x.min_value).toLocaleString('en-IN')}`).join(', ') + (per.length>5?' ...':'');
    const short = alloc < needInv -1
      ? banner("block", "✕", `<b>Capital kam hai!</b> Top ${mr.n} stocks me har ek me kam se kam 1 valid order (₹${mr.min_trade_val} min) ke liye <b>kam se kam ${inr(needInv)} stocks me + reserve = NAV ${inr(needNav)}</b> chahiye.<br>`
        + `Aapne sirf <b>${inr(alloc)} stocks me / NAV ${inr(p.nav)}</b> allocate kiya — isiliye <b>${mr.n - buys.length} stocks me paisa nahi laga (8/10 jaisa)</b>.<br>`
        + `<span class="muted">Per-stock min: ${details}. Deploy % badhao ya capital badhao ya n kam karo.</span>`)
      : banner("good", "✓", `<b>Capital kaafi hai.</b> Top ${mr.n} stocks ke liye minimum <b>${inr(needInv)} stocks me / NAV ${inr(needNav)}</b> chahiye, aapke paas <b>${inr(alloc)} / ${inr(p.nav)}</b> hai — sab ${mr.n} buy banenge (S.No 1-${mr.n}).<br><span class="muted">Per-stock: ${details}</span>`);
    $("#planBanners").innerHTML += short;
  }

  // --- charts: allocation + cost ---
  try{
    const palette = ["#2563eb","#7c3aed","#059669","#ea580c","#0ea5e9","#eab308","#f43f5e","#14b8a6","#f97316","#8b5cf6","#6366f1","#10b981"];
    const allocLabels = buys.map(o=>o.symbol);
    const allocVals = buys.map(o=>o.value);
    if(allocVals.length){
      donutChart("allocChart", allocLabels, allocVals, palette.slice(0, allocLabels.length));
      $("#allocLegend").innerHTML = allocLabels.map((l,i)=>`<span><i style="background:${palette[i%palette.length]}"></i>${esc(l)} ${inr(allocVals[i])}</span>`).join("");
    } else {
      $("#allocLegend").innerHTML = `<span class="muted">Koi BUY nahi — allocation chart nahi</span>`;
    }
    const c = p.costs||{};
    const costVals = [c.stt||0, c.dp_charges||0, (c.stamp_duty||0)+(c.txn_charges||0)+(c.sebi_fees||0)+(c.gst||0)];
    if(costVals.reduce((a,b)=>a+b,0)>1){
      donutChart("costChart", ["STT","DP","Other"], costVals, ["#ef4444","#f59e0b","#64748b"]);
      $("#costHint").textContent = "STT sabse bada hissa — churn kam karo toh bachega";
    } else {
      $("#costHint").textContent = "";
    }
  }catch(e){ /* chart optional */ }

  // table search + copy handlers (attach once)
  const hookSearch = (inputId, tableId) => {
    const inp = document.getElementById(inputId);
    if(!inp || inp._hooked) return;
    inp._hooked=true;
    inp.addEventListener("input", ()=>{
      const q = inp.value.trim().toLowerCase();
      const rows = document.querySelectorAll(`#${tableId} tbody tr`);
      rows.forEach(tr=>{
        const txt = tr.textContent.toLowerCase();
        tr.style.display = !q || txt.includes(q) ? "" : "none";
      });
    });
  };
  hookSearch("sellSearch","sellTable");
  hookSearch("buySearch","buyTable");
  hookSearch("portSearch","portTable");
  const hookCopy = (btnId, tableId) => {
    const b=document.getElementById(btnId);
    if(!b || b._hooked) return;
    b._hooked=true;
    b.onclick=()=>{
      const t=document.getElementById(tableId);
      if(!t) return;
      const txt = Array.from(t.querySelectorAll("tr")).map(tr=> Array.from(tr.cells).map(td=>td.textContent.trim()).join("\t")).join("\n");
      navigator.clipboard.writeText(txt).then(()=>toast("Copied ✓","good")).catch(()=>toast("Copy fail","bad"));
    };
  };
  hookCopy("sellCopyBtn","sellTable");
  hookCopy("buyCopyBtn","buyTable");
  // export handlers
  const doExport = (type) => {
    if(!PLAN) return;
    if(type==="csv"){
      const rows = [["Side","Symbol","Qty","Price","Limit","Value","Reason","Note"]];
      PLAN.orders.forEach(o=> rows.push([o.side,o.symbol,o.qty,o.ref_price,o.limit_price,o.value,o.reason,o.note]));
      const csv = rows.map(r=> r.map(v=> `"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
      const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([csv],{type:"text/csv"})); a.download=`plan-${PLAN.run_id}.csv`; a.click();
      toast("CSV downloaded","good");
    } else {
      const j = JSON.stringify(PLAN,null,2);
      const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([j],{type:"application/json"})); a.download=`plan-${PLAN.run_id}.json`; a.click();
      toast("JSON downloaded","good");
    }
  };
  const eb=document.getElementById("exportBtn");
  const ej=document.getElementById("exportJsonBtn");
  if(eb && !eb._hooked){ eb._hooked=true; eb.onclick=()=>doExport("csv"); }
  if(ej && !ej._hooked){ ej._hooked=true; ej.onclick=()=>doExport("json"); }

  const LBL = { EXIT:"OUT", ENTRY:"NEW", TOPUP:"ADD", TRIM:"TRIM",
                OVERFLOW:"n+1", OVERFLOW_TRIM:"n+1 trim" };
  const rowsFor = list => list.length ? list.map((o, idx) => {
    const r = o.reason;
    return `<tr>
      <td class="num">${idx+1}</td>
      <td class="l"><span class="tag ${r}">${LBL[r] || r}</span></td>
      <td class="l sym">${esc(o.symbol)}</td>
      <td class="num">${o.qty.toLocaleString("en-IN")}</td>
      <td class="num">${inr(o.ref_price, 2)}</td>
      <td class="num">${o.limit_price ? inr(o.limit_price, 2) : "MKT"}</td>
      <td class="num"><b>${inr(o.value)}</b></td>
      <td class="l muted">${esc(o.note)}</td></tr>`;
  }).join("") : `<tr><td colspan="8" class="l muted" style="padding:18px 10px">Kuch nahi.</td></tr>`;

  const head = `<thead><tr><th>S.No</th><th class="l">Kya</th><th class="l">Symbol</th><th>Qty</th><th>Price</th><th>Limit</th><th>Value</th><th class="l">Kyun</th></tr></thead>`;
  const foot = (n, v) => n ? `<tfoot><tr><td colspan="6" class="l">${n} orders</td><td class="num">${inr(v)}</td><td></td></tr></tfoot>` : "";

  $("#sellTable").innerHTML = head + `<tbody>${rowsFor(sells)}</tbody>` + foot(sells.length, sv);
  $("#buyTable").innerHTML = head + `<tbody>${rowsFor(buys)}</tbody>` + foot(buys.length, bv);

  const nOut = sells.filter(o => o.reason === "EXIT").length;
  const nTrim = sells.filter(o => o.reason === "TRIM").length;
  $("#sellSub").innerHTML = `<b>${nTrim}</b> stock sirf trim ho rahe hain (nayi list mein bhi hain, bech nahi rahe) · <b>${nOut}</b> poora exit`;
  const touched = new Set(p.orders.map(o => o.symbol));
  const untouched = (p.held_in_list || []).filter(s => !touched.has(s));
  if (untouched.length)
    $("#planBanners").innerHTML += banner("good", "✓",
      `<b>${untouched.length} stock bilkul chhue hi nahi ja rahe</b> — nayi list mein hain aur weight already sahi hai, toh koi order nahi: <b>${untouched.map(esc).join(", ")}</b>`);
  $("#buySub").innerHTML = `<b>${buys.filter(o => o.reason === "ENTRY").length}</b> nayi entry · <b>${buys.filter(o => o.reason === "TOPUP").length}</b> top-up · <b>${buys.filter(o => o.reason.startsWith("OVERFLOW")).length}</b> n+1 slot`;
  // Allocation mismatch warning + S.No already shows count
  const wantSlots = p.slots || 0;
  const gotBuys = buys.length;
  const skipped = p.skipped || [];
  // warn if fewer buys than slots (excluding holds that are within band)
  // count targets that should have buys but don't
  if (wantSlots > 0 && gotBuys < wantSlots) {
    const miss = wantSlots - gotBuys;
    const reason = skipped.length ? ` Skipped: ${skipped.slice(0,5).map(s=>`${esc(s.symbol)} (${esc(s.reason.slice(0,60))})`).join(', ')}${skipped.length>5?'...':''}` : '';
    $("#planBanners").innerHTML += banner("warn", "!",
      `<b>Allocation adhura:</b> ${wantSlots} stocks me baantna tha, par sirf <b>${gotBuys}</b> me buy order bana (${miss} stocks me paisa nahi laga).`+
      `<br><span class="muted">Wajah: ${miss} stocks ka qty min_trade (₹${(p.costs && p.costs.min_trade) || 500}) se kam ya price/circuit issue. ${reason}</span>`+
      `<br><span class="muted">S.No column se dekho kaunse chhute. Agar cash kam hai toh deploy % badhao ya capital badhao.</span>`);
  }
  if (skipped.length) {
    $("#planBanners").innerHTML += banner("info", "i",
      `<b>${skipped.length} skipped</b> — ye orders isliye nahi bane:<ul>${skipped.map(s=>`<li><b>${esc(s.symbol)}</b>: ${esc(s.reason)}</li>`).join('')}</ul>`);
  }

  const c = p.costs || {};
  const money = v => inr(v || 0);
  $("#costTable").innerHTML = `<thead><tr><th>S.No</th><th class="l">Kharcha</th><th>Amount</th></tr></thead><tbody>
    <tr><td class="num">1</td><td class="l">STT (0.1% dono taraf)</td><td class="num">${money(c.stt)}</td></tr>
    <tr><td class="num">2</td><td class="l">DP charges (har sell scrip)</td><td class="num">${money(c.dp_charges)}</td></tr>
    <tr><td class="num">3</td><td class="l">Stamp + txn + SEBI + GST</td><td class="num">${money((c.stamp_duty||0)+(c.txn_charges||0)+(c.sebi_fees||0)+(c.gst||0))}</td></tr>
    <tr><td class="num">4</td><td class="l">Brokerage (Dhan delivery = 0)</td><td class="num">${money(c.brokerage)}</td></tr>
    </tbody><tfoot><tr><td></td><td class="l">Total — is baar</td><td class="num">${money(c.total)}${p.nav ? ` <span class="muted">(${((c.total || 0) / p.nav * 100).toFixed(2)}% of NAV)</span>` : ""}</td></tr></tfoot>`;
  const an = c.annual;
  if (an) {
    const hot = an.pct_of_nav >= 3;
    $("#costTable").insertAdjacentHTML("afterend", banner(hot ? "warn" : "info", hot ? "!" : "i",
      `<b>Isi rate par saal bhar mein: ${inr(an.yearly)}</b> — tumhare capital ka <b>${an.pct_of_nav.toFixed(2)}%</b>.` +
      `<br><span class="muted">${an.per_year} rebalance/saal maan kar (config: <code>rebalances_per_year</code>). Ismein slippage aur 20% STCG tax abhi bhi shaamil nahi hai.</span>` +
      (hot ? `<br><br>Strategy ko har saal <b>${an.pct_of_nav.toFixed(1)}%</b> sirf kharcha nikaalne ke liye kamana padega. Churn kam karo ya rebalance kam baar karo.` : "")));
  }
  $("#rawPlan").textContent = p.text;

  $("#execEmpty").classList.add("hidden");
  $("#execWrap").classList.remove("hidden");
  $("#dryOut").innerHTML = ""; $("#realOut").innerHTML = "";
  $("#confirmBox").value = ""; $("#realBtn").disabled = true;
  $("#confirmBox").placeholder = wantWord() + " likho";
  $("#realBtn").textContent = p.is_liquidation ? "SAB BECH DO" : "Asli orders bhejo";
  renderRealGuard();
}

/* ---------------- execute ---------------- */
function planAgeMin() {
  if (!PLAN) return 0;
  const base = PLAN.age_sec || 0;
  return (base + (Date.now() - (PLAN._loadedAt || Date.now())) / 1000) / 60;
}
function renderRealGuard() {
  const p = PLAN; if (!p) return;
  let h = "";
  const lim = p.max_age_min || 0, age = planAgeMin();
  if (lim > 0 && age > lim)
    h += banner("block", "!",
      `<b>Ye plan ${age.toFixed(0)} minute purana hai</b> (limit ${lim} min). ` +
      `Iske LIMIT prices us waqt ke hain -- ab order bharega hi nahi. ` +
      `<br><br><button class="btn primary" onclick="showTab('tPlan');document.querySelector('#rePlanBtn').click()">Naya plan banao</button>`);
  if (p.mode === "paper")
    h = banner("info", "i", "<b>Demo mode</b> — yahan 'asli' dabane par bhi order market mein nahi jaayega. Sach mein trade karne ke liye upar Live chuno.");
  else if (p.blockers.length)
    h = banner("block", "✕", "Plan BLOCKED hai. Execute band hai jab tak blockers theek nahi hote.");
  else if (p.is_liquidation)
    h = banner("block", "!", `<b>Ye POORA PORTFOLIO bech dega.</b> ${p.orders.length} scrip, ` +
      `${inr(p.orders.reduce((a, o) => a + o.value, 0))}. Iske baad sab cash mein hoga. ` +
      `Confirm box mein <code>sab bech do</code> likhna padega.`);
  else
    h = banner("warn", "!", `<b>Ye asli paisa lagayega.</b> ${p.orders.length} orders — ${inr(p.orders.filter(o => o.side === "SELL").reduce((a, o) => a + o.value, 0))} bechna, ${inr(p.orders.filter(o => o.side === "BUY").reduce((a, o) => a + o.value, 0))} kharidna. Pehle SELL jaayenge, fill hone ke baad BUY.`);
  $("#realGuard").innerHTML = h;
}

function wantWord() { return PLAN && PLAN.is_liquidation ? "sab bech do" : "haan"; }
$("#confirmBox").oninput = e => {
  const ok = e.target.value.trim().toLowerCase() === wantWord()
    && PLAN && !(PLAN.blockers || []).length;
  $("#realBtn").disabled = !ok;
};

function execResult(r, el) {
  const f = r.failed || [];
  let h = f.length
    ? banner("block", "✕", `<b>${f.length} order fail hue.</b><ul>${f.map(x => `<li>${esc(x.symbol || "")} — ${esc(x.error || x.reason || "")}</li>`).join("")}</ul>`)
    : banner("good", "✓", r.dry_run
      ? `<b>Rehearsal poori — koi dikkat nahi.</b> Market mein kuch nahi gaya.`
      : `<b>Execution poori hui.</b>${r.paper ? " (demo — asli order nahi gaya)" : ""}`);
  h += `<div class="stats" style="margin-top:12px">
    <div class="stat sell"><div class="k">SELL</div><div class="v num">${(r.sells || []).length}</div><div class="m">orders</div></div>
    <div class="stat buy"><div class="k">BUY</div><div class="v num">${(r.buys || []).length}</div><div class="m">orders</div></div>
    <div class="stat"><div class="k">Fail</div><div class="v num" style="color:${f.length ? "var(--critical)" : "var(--good)"}">${f.length}</div><div class="m">orders</div></div>
  </div>`;
  el.innerHTML = h;
}

$("#dryBtn").onclick = async () => {
  const b = $("#dryBtn"); busy(b, true, "Chal raha hai…");
  try {
    const r = await api("/api/execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "dry" }) });
    execResult(r, $("#dryOut"));
  } catch (e) { $("#dryOut").innerHTML = banner("block", "!", esc(e.message)); }
  busy(b, false);
};

$("#realBtn").onclick = async () => {
  const liq = PLAN && PLAN.is_liquidation;
  if (!confirm(liq
      ? "AAKHRI CONFIRMATION\n\nPOORA PORTFOLIO bik jaayega.\nAsli orders Dhan par jaayenge.\n\nAage badhein?"
      : "Aakhri confirmation.\n\nAsli orders Dhan par jaayenge.\n\nAage badhein?")) return;
  const b = $("#realBtn"); busy(b, true, "Orders ja rahe hain…");
  try {
    const r = await api("/api/execute", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode: "real", confirm: $("#confirmBox").value }) });
    execResult(r, $("#realOut"));
    $("#confirmBox").value = "";
  } catch (e) { $("#realOut").innerHTML = banner("block", "!", esc(e.message)); }
  busy(b, false); b.disabled = true;
};

/* ---------------- portfolio ---------------- */
async function loadPortfolio() {
  $("#portSub").textContent = "Load kar rahe hain…";
  let d;
  try { d = await api("/api/holdings"); }
  catch (e) {
    $("#portSub").textContent = "";
    $("#capCard").classList.add("hidden");
    $("#portStats").innerHTML = "";
    $("#portTable").innerHTML = "";
    $("#srcBanner").innerHTML = banner("block", "✕",
      `<b>Portfolio nahi aaya.</b><br><br>${esc(e.message).replace(/\n/g, "<br>")}`);
    return;
  }
  $("#portSub").textContent = esc(d.source || "");
  $("#capCard").classList.toggle("hidden", !d.is_demo);
  if (d.is_demo && d.demo_capital) {
    $("#capIn").value = Math.round(d.demo_capital);
    $("#capHint").textContent = `abhi ${inr(d.demo_capital)}`;
  }
  $("#srcBanner").innerHTML = d.is_demo
    ? banner("warn", "!", `<b>Ye paisa asli nahi hai.</b> Demo ka nakli portfolio hai — capital upar se badal sakte ho. Live mode mein NAV seedha Dhan se aata hai: holdings <code>/holdings</code> se, cash <code>/fundlimit</code> se.`)
    : banner("good", "✓", `<b>Ye seedha Dhan se aaya hai.</b> Holdings <code>/holdings</code> API se, free cash <code>/fundlimit</code> API se. Koi number app mein likha hua nahi hai.`);
  const pnl = d.holdings.reduce((a, h) => a + h.pnl, 0);
  $("#portStats").innerHTML = [
    ["Stocks ki value", inr(d.total), `${d.holdings.length} scrips`],
    ["Free cash", inr(d.cash), "kharidne ke liye"],
    ["Total NAV", inr(d.total + d.cash), "stocks + cash"],
    ["Unrealised P&L", inr(pnl), pnl >= 0 ? "faayda" : "nuksaan"],
  ].map(([k, v, m], i) =>
    `<div class="stat"><div class="k">${k}</div><div class="v num" ${i === 3 ? `style="color:${pnl >= 0 ? "var(--good)" : "var(--sell)"}"` : ""}>${v}</div><div class="m">${m}</div></div>`).join("");

  // Portfolio charts — allocation donut + P&L bar
  try{
    const palette = ["#2563eb","#7c3aed","#059669","#ea580c","#0ea5e9","#eab308","#f43f5e","#14b8a6","#6366f1","#f97316","#10b981","#84cc16"];
    if(d.holdings.length){
      const labs = d.holdings.map(h=>h.symbol);
      const vals = d.holdings.map(h=>h.value);
      donutChart("portAllocChart", labs, vals, palette.slice(0,labs.length));
      const lg = document.getElementById("portAllocLegend");
      if(lg) lg.innerHTML = labs.map((l,i)=>`<span><i style="background:${palette[i%palette.length]}"></i>${esc(l)} ${inr(vals[i])}</span>`).join("");
      const pnlLabs = d.holdings.map(h=>h.symbol);
      const pnlVals = d.holdings.map(h=>h.pnl);
      // use bar chart for P&L via canvas
      const col = pnlVals.map(v=> v>=0 ? "#10b981" : "#ef4444");
      // simple bar via Chart.js if available - use line bar
      if(window.Chart){
        const c=document.getElementById("portPnlChart");
        if(c){ if(c._chart) c._chart.destroy();
          c._chart = new Chart(c.getContext("2d"),{
            type:"bar",
            data:{ labels:pnlLabs, datasets:[{ data:pnlVals, backgroundColor:col, borderRadius:6 }]},
            options:{ indexAxis:"y", plugins:{ legend:{display:false}}, scales:{ x:{ grid:{color:"rgba(148,163,184,.12)"}, ticks:{ callback:v=>inr(v)}}, y:{ grid:{display:false}}}, animation:{duration:400}}
          });
        }
      }
    } else {
      const lg=document.getElementById("portAllocLegend");
      if(lg) lg.innerHTML = `<span class="muted">Koi holding nahi</span>`;
    }
  }catch(e){}

  // portfolio search hook
  const ps=document.getElementById("portSearch");
  if(ps && !ps._hooked){
    ps._hooked=true;
    ps.addEventListener("input",()=>{
      const q=ps.value.trim().toLowerCase();
      document.querySelectorAll("#portTable tbody tr").forEach(tr=>{
        tr.style.display = !q || tr.textContent.toLowerCase().includes(q) ? "" : "none";
      });
    });
  }

  $("#portTable").innerHTML =
    `<thead><tr><th>S.No</th><th class="l">Symbol</th><th>Qty</th><th>Bech sakte</th><th>Avg</th><th>LTP</th><th>Value</th><th>P&L</th><th>Weight</th></tr></thead><tbody>` +
    (d.holdings.length ? d.holdings.map((h, idx) => `<tr>
      <td class="num">${idx+1}</td>
      <td class="l sym">${esc(h.symbol)}</td>
      <td class="num">${h.qty.toLocaleString("en-IN")}</td>
      <td class="num">${h.available.toLocaleString("en-IN")}${h.available < h.qty ? ' <span class="tag gray">T+1</span>' : ""}</td>
      <td class="num">${inr(h.avg, 2)}</td><td class="num">${inr(h.ltp, 2)}</td>
      <td class="num"><b>${inr(h.value)}</b></td>
      <td class="num ${h.pnl >= 0 ? "pos" : "neg"}">${inr(h.pnl)} <span class="muted">(${h.pnl_pct >= 0 ? "+" : ""}${h.pnl_pct.toFixed(1)}%)</span></td>
      <td class="num">${h.weight.toFixed(1)}% <span class="wbar"><i style="width:${Math.min(h.weight * 2.5, 100)}%"></i></span></td></tr>`).join("")
      : `<tr><td colspan="9" class="l muted" style="padding:18px 10px">Koi holding nahi.</td></tr>`) + `</tbody>`;

  try {
    const r = await api("/api/runs");
    $("#runsTable").innerHTML = `<thead><tr><th>S.No</th><th class="l">Run</th><th class="l">Kab</th><th class="l">Status</th><th>NAV</th></tr></thead><tbody>` +
      (r.runs.length ? r.runs.map((x, idx) => `<tr><td class="num">${idx+1}</td><td class="l sym">${esc(x.run_id)}</td>
        <td class="l muted">${esc((x.created_at || "").replace("T", " ").slice(0, 16))}</td>
        <td class="l"><span class="tag ${x.status === "DONE" ? "keep" : x.status === "BLOCKED" ? "OUT" : "gray"}">${esc(x.status)}</span></td>
        <td class="num">${inr(x.nav)}</td></tr>`).join("")
        : `<tr><td colspan="5" class="l muted" style="padding:18px 10px">Abhi koi run nahi hua.</td></tr>`) + `</tbody>`;
    // NAV history line chart
    try{
      if(r.runs && r.runs.length>1 && window.Chart){
        const rev = [...r.runs].reverse();
        const labels = rev.map(x=> (x.run_id||"").slice(1,9));
        const vals = rev.map(x=> x.nav||0);
        const c=document.getElementById("navChart");
        if(c){
          if(c._chart) c._chart.destroy();
          c._chart = new Chart(c.getContext("2d"),{
            type:"line",
            data:{ labels, datasets:[{ data:vals, borderColor:"#2563eb", backgroundColor:"rgba(37,99,235,.08)", fill:true, tension:.4, pointRadius:3, pointBackgroundColor:"#2563eb" }]},
            options:{
              plugins:{ legend:{display:false}, tooltip:{ callbacks:{ label:(ctx)=> inr(ctx.parsed.y) } } },
              scales:{ x:{ grid:{display:false}, ticks:{ maxTicksLimit:6 }}, y:{ grid:{ color:"rgba(148,163,184,.12)"}, ticks:{ callback:v=>inr(v)} } },
              animation:{ duration:400}
            }
          });
        }
      }
    }catch(e){}
  } catch (e) { /* ignore */ }
}
$("#portRefresh").onclick = loadPortfolio;

$("#sellAllBtn").onclick = async () => {
  if (!confirm("POORA PORTFOLIO bechne ka plan banayein?\n\nAbhi sirf plan banega -- koi order nahi jaayega.\nExecute tab par jaakar 'sab bech do' likhna padega.")) return;
  const b = $("#sellAllBtn"); busy(b, true, "Plan bana rahe hain...");
  try {
    PLAN = await api("/api/plan/sell-all", { method: "POST" });
    $("#sellAllOut").innerHTML = banner("good", "\u2713",
      `<b>Plan ban gaya</b> -- ${PLAN.orders.length} scrip, ${inr(PLAN.orders.reduce((a, o) => a + o.value, 0))}.` +
      `<br><br><button class="btn danger" onclick="showTab('tPlan')">Plan dekho &rarr;</button>`);
    renderPlan();
  } catch (e) {
    $("#sellAllOut").innerHTML = banner("block", "\u2715",
      `<b>Nahi bana:</b><br>${esc(e.message).replace(/\n/g, "<br>")}`);
  }
  busy(b, false);
};

$("#capBtn").onclick = async () => {
  const v = parseFloat(($("#capIn").value || "").replace(/[^0-9.]/g, ""));
  if (!(v > 0)) { $("#capHint").textContent = "number daalo"; return; }
  const b = $("#capBtn"); busy(b, true, "…");
  try {
    const r = await api("/api/demo/capital", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ capital: v })
    });
    $("#capHint").textContent = `stocks ${inr(r.stocks_value)} + cash ${inr(r.cash)}`;
    PLAN = null;
    $("#planWrap").classList.add("hidden"); $("#planEmpty").classList.remove("hidden");
    $("#execWrap").classList.add("hidden"); $("#execEmpty").classList.remove("hidden");
    $("#planPill").classList.add("hidden");
    await loadPortfolio();
  } catch (e) { $("#capHint").textContent = e.message; }
  busy(b, false);
};

/* ---------------- config ---------------- */
async function loadConfig() {
  let c;
  try { c = await api("/api/config"); } catch (e) { return; }
  const labels = {
    n_stocks: "Kitne stocks mein barabar baantna",
    exit_rank_threshold: "Rank isse neeche gaya toh exit",
    partial_list_mode: "List chhoti ho toh (full = poora paisa lagao)",
    use_overflow_slot: "Bacha paisa n+1 stock mein",
    drift_band_pct: "Drift band (0 = har baar exact barabar)",
    cash_reserve_pct: "Cash reserve",
    max_weight_per_stock_pct: "Ek stock max weight",
    rank_by: "Rank kis hisaab se",
    max_turnover_pct: "Churn isse zyada ho toh ruk jao",
    max_single_order_value_inr: "Ek order max kitne ka",
    min_price_inr: "Isse saste share nahi",
    min_market_cap_cr: "Isse chhota mkt cap (₹ cr) → warning",
    max_pct_of_traded_value: "Order us din ke volume ka max %",
    narrow_band_warn_pct: "Circuit band isse patla → warning",
    stale_ltp_tolerance: "CSV ka LTP live se itna alag → purani list",
    allowed_window: "Rebalance ka time window (IST)",
  };
  const sec = (title, obj) => `<h3 style="font-size:14px;margin:20px 0 8px;color:var(--ink2)">${title}</h3>
    <table><thead><tr><th>S.No</th><th class="l">Setting</th><th>Value</th></tr></thead><tbody>` + Object.entries(obj).map(([k, v], idx) =>
    `<tr><td class="num">${idx+1}</td><td class="l">${esc(labels[k] || k)}<div class="muted" style="font-size:11.5px">${esc(k)}</div></td>
     <td class="num"><b>${esc(Array.isArray(v) ? v.join(" – ") : (v === null ? "off" : v))}</b></td></tr>`).join("") + `</tbody></table>`;
  $("#cfgOut").innerHTML = sec("Portfolio", c.portfolio) + sec("Risk guards", c.risk) + sec("Costs", c.costs) + sec("Execution", c.execution);
}

/* ---------------- boot ---------------- */
refreshHealth();
setInterval(refreshHealth, 30000);

/* ---------------- connection / credentials ---------------- */
const ICO = { ok: "✓", bad: "✕", warn: "!", pend: "" };

function chkRow(name, state, msg, detail) {
  return `<div class="chk ${state}"><div class="ico">${ICO[state] ?? ""}</div>
    <div class="body"><div class="nm">${esc(name)}</div>
    <div class="ms">${esc(msg || "")}</div>
    ${detail ? `<div class="dt">${esc(detail)}</div>` : ""}</div></div>`;
}

const STEP_NAMES = ["Format", "Token", "Dhan se baat", "Portfolio"];

function setConnDot(state) {
  const d = $("#connDot");
  d.className = "dot2" + (state ? " " + state : "");
}

$("#eyeBtn").onclick = () => {
  const i = $("#cTok"), show = i.type === "password";
  i.type = show ? "text" : "password";
  $("#eyeBtn").classList.toggle("on", show);
};

async function loadCreds() {
  let d;
  try { d = await api("/api/creds"); } catch (e) { return; }
  CREDS = d;
  if (d.client_id) $("#cId").value = d.client_id;
  let b = "";
  if (d.has_creds) {
    const ti = d.token_info || {};
    const exp = ti.expired
      ? `<b class="neg">Token expire ho chuka hai</b> (${esc(ti.expires_at || "")}). Naya banao.`
      : (ti.days_left != null
        ? `Token <b>${ti.days_left} din</b> aur chalega (${esc(ti.expires_at || "")}).`
        : "");
    b = banner(ti.expired ? "block" : "info", ti.expired ? "!" : "i",
      `<b>Credentials mile</b> — client <b>${esc(d.client_id)}</b>, token ${esc(d.masked)}` +
      `<br><span class="muted">source: ${esc(d.source || "?")}</span>${exp ? "<br>" + exp : ""}` +
      `<br><br>Pakka check karne ke liye <b>Check karo</b> dabao — token yahan bhara hua nahi hai, wo file se uthega.`);
    setConnDot(ti.expired ? "bad" : (VERIFIED ? "ok" : null));
  } else {
    b = banner("warn", "!", `<b>Abhi koi credentials nahi hain.</b> App Demo mode mein chalegi. ` +
      `Asli trading ke liye niche client ID aur token daal kar <b>Check karo</b> dabao.`);
    setConnDot("demo");
  }
  $("#connBanner").innerHTML = b;
}
let CREDS = null, VERIFIED = false;

$("#verifyBtn").onclick = async () => {
  const btn = $("#verifyBtn");
  const cid = $("#cId").value.trim(), tok = $("#cTok").value.trim();
  $("#cId").classList.remove("good", "bad");
  $("#cTok").classList.remove("good", "bad");
  $("#connResult").innerHTML = "";

  // sab steps pehle "pending", pehla "running" -- taaki progress dikhe
  const list = $("#checkList");
  list.classList.remove("hidden");
  list.innerHTML = STEP_NAMES.map((n, i) =>
    chkRow(n, i === 0 ? "run" : "pend", i === 0 ? "check kar rahe hain…" : "ruko…")).join("");
  busy(btn, true, "Check kar rahe hain…");
  setConnDot(null);

  // spinner ko aage badhao jab tak server jawaab de
  let cur = 0;
  const tick = setInterval(() => {
    if (cur >= STEP_NAMES.length - 1) return;
    cur++;
    list.innerHTML = STEP_NAMES.map((n, i) =>
      chkRow(n, i < cur ? "ok" : (i === cur ? "run" : "pend"),
        i < cur ? "theek hai" : (i === cur ? "check kar rahe hain…" : "ruko…"))).join("");
  }, 700);

  let r;
  try {
    r = await api("/api/creds/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_id: cid, access_token: tok, save: $("#saveChk").checked })
    });
  } catch (e) {
    clearInterval(tick);
    busy(btn, false);
    list.innerHTML = chkRow("Error", "bad", e.message);
    setConnDot("bad");
    return;
  }
  clearInterval(tick);
  busy(btn, false);

  // asli natije ek-ek karke reveal karo
  const got = r.steps || [];
  const rowState = st => st.ok === true ? "ok" : st.ok === false ? "bad" : "warn";
  for (let i = 0; i <= got.length; i++) {
    list.innerHTML =
      got.slice(0, i).map(st => chkRow(st.name, rowState(st), st.msg, st.detail)).join("") +
      (i < got.length ? chkRow(got[i].name, "run", "check kar rahe hain…") : "") +
      STEP_NAMES.filter(n => !got.some(g => g.name === n))
        .map(n => chkRow(n, "pend", "skip")).join("");
    await new Promise(res => setTimeout(res, i < got.length ? 340 : 0));
  }

  if (r.ok) {
    VERIFIED = true;
    setConnDot("ok");
    $("#cId").classList.add("good");
    if (tok) $("#cTok").classList.add("good");
    const ti = r.token_info || {};
    $("#connResult").innerHTML = banner("good", "✓",
      `<b>Credentials verify ho gaye.</b>` +
      `<br>Client <b>${esc(r.client_id)}</b> · token ${esc(r.masked)}` +
      (r.cash != null ? `<br>Free cash: <b>${inr(r.cash)}</b>` : "") +
      (r.holdings != null ? ` · Holdings: <b>${r.holdings}</b> scrip` : "") +
      (ti.days_left != null ? `<br>Token ${ti.days_left} din aur chalega` : "") +
      (r.saved ? `<br><br><b>creds.bat</b> mein save kar diya — agli baar khud uth jaayega.` :
        (r.save_error ? `<br><br><span class="neg">Save nahi hua: ${esc(r.save_error)}</span>` :
          `<br><br><span class="muted">Save nahi kiya (checkbox off tha) — sirf is session ke liye chalega.</span>`)) +
      `<br><br>Ab upar <b>Live</b> mode chun sakte ho.`);
    $("#cTok").value = "";
    await loadCreds();
    await refreshHealth();
    renderGates();
  } else {
    VERIFIED = false;
    setConnDot("bad");
    const failed = got.find(st => st.ok === false);
    if (failed && failed.name === "Format") $("#cId").classList.add("bad");
    if (failed && (failed.name === "Token" || failed.name === "Dhan se baat"))
      $("#cTok").classList.add("bad");
    $("#connResult").innerHTML = banner("block", "✕",
      `<b>Verify nahi hua.</b> Upar jo laal hai wahi asli wajah hai.` +
      (failed ? `<br><br><b>${esc(failed.name)}:</b> ${esc(failed.msg)}` +
        (failed.detail ? `<br><span class="muted">${esc(failed.detail)}</span>` : "") : ""));
  }
};

$("#pxTestBtn").onclick = async () => {
  const b = $("#pxTestBtn"); busy(b, true, "Test kar rahe hain...");
  $("#pxOut").innerHTML = "";
  try {
    const r = await api("/api/prices/test", { method: "POST" });
    let h = "";
    for (const s of r.results) {
      const rows = (s.sample || []).map((x, idx) =>
        `<tr><td class="num">${idx+1}</td><td class="l sym">${esc(x.symbol)}</td><td class="num">${inr(x.ltp, 2)}</td>` +
        `<td class="num">${x.volume ? x.volume.toLocaleString("en-IN") : "--"}</td>` +
        `<td>${x.circuit ? '<span class="tag keep">haan</span>' : '<span class="tag gray">nahi</span>'}</td></tr>`).join("");
      h += banner(s.ok ? "good" : "block", s.ok ? "\u2713" : "\u2715",
        `<b>${esc(s.name)}</b> -- ${esc(s.msg)}` +
        (s.ok && s.age_min != null ? `<br>Price <b>${s.age_min} minute</b> purana hai.` : "") +
        (s.ok && s.age_min == null ? `<br><span class="muted">Price kitna purana hai, ye source nahi batata.</span>` : "") +
        (s.ok && s.circuits === 0 ? `<br><span class="muted">Circuit limits nahi milte is source se.</span>` : "") +
        (rows ? `<table style="margin-top:10px"><thead><tr><th>S.No</th><th class="l">Symbol</th><th>LTP</th><th>Volume</th><th>Circuit</th></tr></thead><tbody>${rows}</tbody></table>` : ""));
    }
    if (!r.any_ok)
      h += banner("warn", "!", `<b>Koi bhi free source nahi chala.</b> Aise mein Dhan ka Data API hi ekmatra rasta hai, ya <code>config.yaml</code> mein <code>prices.fallback</code> badal kar dekho.`);
    $("#pxOut").innerHTML = h;
  } catch (e) {
    $("#pxOut").innerHTML = banner("block", "\u2715", esc(e.message).replace(/\n/g, "<br>"));
  }
  busy(b, false);
};

$("#clearBtn").onclick = async () => {
  if (!confirm("Saved credentials hata dein?\n\ncreds.bat ka naam badal kar creds.bat.removed kar diya jaayega.\nApp Demo mode par aa jaayegi.")) return;
  try {
    await api("/api/creds/clear", { method: "POST" });
    VERIFIED = false;
    $("#cId").value = ""; $("#cTok").value = "";
    $("#checkList").classList.add("hidden");
    $("#connResult").innerHTML = "";
    await loadCreds(); await refreshHealth();
  } catch (e) { alert(e.message); }
};

/* ---- extra world-class handlers ---- */
$("#runsRefreshBtn") && ($("#runsRefreshBtn").onclick = ()=> loadPortfolio());
document.addEventListener("keydown", (e)=>{
  if((e.ctrlKey||e.metaKey) && e.key.toLowerCase()==="k"){
    e.preventDefault(); toast("Quick: 1 Connect • 2 Watchlist • 3 Plan • 4 Execute","info");
    const t = prompt("Go to: 1=Connect, 2=Watchlist, 3=Plan, 4=Execute, H=Home, P=Portfolio");
    if(t==="1") showTab("tConn"); if(t==="2") showTab("tUpload"); if(t==="3") showTab("tPlan"); if(t==="4") showTab("tExec"); if(t && t.toLowerCase()==="h") showTab("tHome"); if(t && t.toLowerCase()==="p") showTab("tPort");
  }
  if(e.key==="?" && !e.ctrlKey && !e.metaKey){ toast("Shortcuts: ⌘K quick, 1-4 tabs, ? help","info"); }
});
// toast on key actions (wrap original functions)
const _origUpload = upload;
upload = async function(file){
  const r = await _origUpload(file);
  if(WL) toast(`${WL.count} stocks loaded ✓`,"good");
  return r;
};
const _origMakePlan = makePlan;
makePlan = async function(){
  const r = await _origMakePlan();
  if(PLAN && !PLAN.blockers.length) toast(`Plan ready: ${PLAN.orders.length} orders`,"good");
  else if(PLAN && PLAN.blockers.length) toast("Plan blocked","bad");
  return r;
};
// hide cmd hint after 8s
setTimeout(()=>{ const h=document.getElementById("cmdHint"); if(h) h.style.opacity="0"; setTimeout(()=>h&&h.remove(), 600); }, 8000);

const _origShowTab = showTab;
showTab = function (id) { _origShowTab(id); if (id === "tConn") loadCreds(); if(id==="tPort") loadPortfolio(); };
window.showTab = showTab;
$$("nav.tabs button").forEach(b => b.onclick = () => showTab(b.dataset.tab));

loadCreds();
toast("Welcome — world-class rebalancer ready ✦","info");
