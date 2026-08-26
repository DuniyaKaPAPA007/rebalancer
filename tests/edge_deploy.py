"""DEPLOY BUDGET -- poori app ka HTTP flow, nakli Dhan ke against.

stress_deploy.py planner ka dimaag test karti hai. Ye file wo cheez test
karti hai jo user ASLI mein karega: button dabana, slider ghumana, ulta-pulta
number daalna, aur beech mein plan bana kar execute karne ki koshish.

Chalane ka tareeka:
    python -m uvicorn --app-dir tests _mock_dhan:app --port 8991 &
    python tests/edge_deploy.py
"""
import base64, json, subprocess, sys, time, os, shutil, pathlib, requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCK = os.environ.get("EDGE_MOCK", "http://127.0.0.1:8991")
PORT = int(os.environ.get("EDGE_PORT", 8903))
B = f"http://127.0.0.1:{PORT}"
CASH = 20_00_000

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}"
          + (f"  -- {detail}" if detail and not cond else ""))


def jwt(days=30, cid="1100112233"):
    h = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    p = base64.urlsafe_b64encode(json.dumps(
        {"exp": int(time.time()) + days * 86400, "dhanClientId": cid}
    ).encode()).decode().rstrip("=")
    return f"{h}.{p}.sig"


def set_mock(**kw):
    requests.post(f"{MOCK}/v2/_set/quote/ok", timeout=5)
    for k, v in kw.items():
        requests.post(f"{MOCK}/v2/_set/{k}/{v}", timeout=5)


def call(m, path, **kw):
    r = getattr(requests, m)(f"{B}{path}", timeout=90, **kw)
    try:
        body = r.json()
    except Exception:
        body = r.text[:300]
    return r.status_code, body


def dep(mode, pct=None, amount=None):
    return call("post", "/api/deploy",
                json={"mode": mode, "pct": pct, "amount": amount})


def start():
    shutil.copy(str(ROOT / "config.yaml"), str(ROOT / "config.yaml.bak"))
    c = (ROOT / "config.yaml").read_text()
    c = c.replace("base_url: https://api.dhan.co/v2", f"base_url: {MOCK}/v2")
    (ROOT / "config.yaml").write_text(c)
    (ROOT / "runs.db").unlink(missing_ok=True)
    shutil.rmtree(ROOT / "plans", ignore_errors=True)
    env = dict(os.environ, DHAN_CLIENT_ID="1100112233", DHAN_ACCESS_TOKEN=jwt())
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "web.api:app", "--host", "127.0.0.1",
         "--port", str(PORT)], cwd=ROOT, env=env,
        stdout=open("/tmp/edge_deploy_srv.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(40):
        try:
            requests.get(f"{B}/api/health", timeout=2)
            return p
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("server start nahi hua")


srv = None
try:
    set_mock(holdings="n4", cash=CASH)
    srv = start()
    sc, h = call("get", "/api/health")
    check("health 200", sc == 200, f"HTTP {sc}")
    check("live mode", h.get("mode") == "live", str(h.get("autodetect_msg"))[:90])

    with open(ROOT / "watchlist.csv", "rb") as f:
        sc, wl = call("post", "/api/watchlist", files={"file": ("w.csv", f, "text/csv")})
    check("watchlist upload", sc == 200, str(wl)[:120])

    # ---------------- 1. default state ---------------------------------
    sc, d = call("get", "/api/deploy")
    check("GET /api/deploy 200", sc == 200, f"HTTP {sc}: {str(d)[:160]}")
    check("default mode = all", d.get("mode") == "all", str(d.get("mode")))
    check("config se aaya", d.get("from_config") is True, str(d))
    NAV = d.get("nav") or 0
    check("NAV Dhan se aayi", NAV > 0, f"nav={NAV}")
    check("NAV source live", "Dhan" in str(d.get("nav_source")), str(d.get("nav_source")))
    check("preview hai", isinstance(d.get("preview"), dict), str(d)[:160])

    # ---------------- 2. GALAT input -- 400 aaye, 500 KABHI nahi --------
    BAD = [
        ("pct 150", dict(mode="pct", pct=150)),
        ("pct -1", dict(mode="pct", pct=-1)),
        ("pct 1e9", dict(mode="pct", pct=1e9)),
        ("pct missing", dict(mode="pct")),
        ("amount -5", dict(mode="amount", amount=-5)),
        ("amount missing", dict(mode="amount")),
        ("mode bakwaas", dict(mode="chandrayaan", pct=50)),
        ("mode khaali", dict(mode="")),
    ]
    for nm, body in BAD:
        sc, b = call("post", "/api/deploy", json=body)
        check(f"galat input '{nm}' -> saaf 400", sc == 400,
              f"HTTP {sc}: {str(b)[:140]}")
        check(f"galat input '{nm}' -> samajh aane wala message",
              sc == 400 and isinstance(b, dict) and len(str(b.get("detail", ""))) > 8,
              str(b)[:120])

    # galat input ke baad bhi setting purani hi honi chahiye
    sc, d = call("get", "/api/deploy")
    check("galat input ne setting nahi bigaadi", d.get("mode") == "all", str(d.get("mode")))

    # ---------------- 3. SEEMA ke input -- ye chalne chahiye ------------
    for nm, body, want_mode in [
        ("pct 0", dict(mode="pct", pct=0), "pct"),
        ("pct 100", dict(mode="pct", pct=100), "pct"),
        ("pct 0.5 (aadha percent)", dict(mode="pct", pct=0.5), "pct"),
        ("amount 0", dict(mode="amount", amount=0), "amount"),
        ("amount NAV se bada", dict(mode="amount", amount=NAV * 5), "amount"),
        ("amount 1 rupaya", dict(mode="amount", amount=1), "amount"),
        ("mode PCT capital", dict(mode="PCT", pct=40), "pct"),
        ("mode amt alias", dict(mode="amt", amount=10000), "amount"),
        ("wapas all", dict(mode="all"), "all"),
        ("wapas config", dict(mode="config"), "all"),
    ]:
        sc, b = call("post", "/api/deploy", json=body)
        check(f"seema '{nm}' chala", sc == 200, f"HTTP {sc}: {str(b)[:140]}")
        if sc == 200:
            check(f"seema '{nm}' mode sahi", b.get("mode") == want_mode,
                  f"got {b.get('mode')}")

    # ---------------- 4. 50% -> plan usi ke andar rahe ------------------
    sc, b = dep("pct", pct=50)
    check("50% set", sc == 200 and b["mode"] == "pct", str(b)[:120])
    eq50 = b["preview"]["equity"]
    check("50% preview ~ aadhi NAV", abs(eq50 - NAV * 0.5) < NAV * 0.02,
          f"{eq50:,.0f} vs {NAV*0.5:,.0f}")

    sc, plan = call("post", "/api/plan")
    check("50% par plan bana", sc == 200, f"HTTP {sc}: {str(plan)[:200]}")
    if sc == 200:
        te = plan["target_equity"]
        # Plan apni hi NAV par 50% le -- yahi asli invariant hai.
        check("plan mein target_equity = NAV ka 50%",
              abs(te - plan["nav"] * 0.5) < max(1.0, plan["nav"] * 0.001),
              f"{te:,.0f} vs {plan['nav']*0.5:,.0f}")
        check("deploy card aur plan ki NAV ek jaisi",
              abs(plan["nav"] - NAV) < max(1.0, NAV * 0.001),
              f"plan nav {plan['nav']:,.0f} vs card nav {NAV:,.0f}")
        check("cash_after theek", abs(plan["cash_after"] - (plan["nav"] - te)) < 1,
              str(plan["cash_after"]))
        check("deploy_label bharaa hua", bool(plan.get("deploy_label")), str(plan.get("deploy_label")))
        bv = sum(o["value"] for o in plan["orders"] if o["side"] == "BUY")
        hv = plan.get("holdings_value", 0)
        check("kharidi budget ke andar", bv <= te + 1,
              f"buy {bv:,.0f} vs target {te:,.0f} (holdings {hv:,.0f})")
        check("plan text mein deploy line hai", "Stocks mein" in plan.get("text", ""),
              plan.get("text", "")[:200])
        check("koi qty<=0 nahi", all(o["qty"] > 0 for o in plan["orders"]))
        syms = [(o["symbol"], o["side"]) for o in plan["orders"]]
        check("ek scrip ka ek hi order", len(syms) == len(set(s for s, _ in syms)),
              str(sorted(s for s, _ in syms)))

    # ---------------- 5. budget badalte hi purana plan mit jaaye ---------
    sc, b = dep("pct", pct=25)
    check("25% set", sc == 200, str(b)[:120])
    sc, e = call("post", "/api/execute", json={"mode": "dry"})
    check("budget badalne par purana plan hat gaya", sc == 400,
          f"HTTP {sc}: {str(e)[:160]} -- purane plan par execute nahi hona chahiye")

    # ---------------- 6. 0% -> sirf bechna ------------------------------
    sc, b = dep("pct", pct=0)
    check("0% set", sc == 200, str(b)[:120])
    sc, plan = call("post", "/api/plan")
    check("0% par 500 nahi aaya", sc in (200, 400), f"HTTP {sc}: {str(plan)[:200]}")
    if sc == 200:
        buys = [o for o in plan["orders"] if o["side"] == "BUY"]
        check("0% par ek bhi BUY nahi", not buys, str(buys)[:160])
        check("0% par target_equity 0", plan["target_equity"] == 0, str(plan["target_equity"]))
    elif sc == 400:
        check("0% par blocker samajh aata hai",
              "deploy" in str(plan).lower() or "churn" in str(plan).lower()
              or "bik raha" in str(plan).lower(), str(plan)[:200])

    # ---------------- 7. NAV se bada amount -> cap + warning -------------
    sc, b = dep("amount", amount=NAV * 10)
    check("bada amount 200", sc == 200, str(b)[:140])
    check("bada amount cap hua", b["preview"]["equity"] <= NAV + 1,
          f"{b['preview']['equity']:,.0f} vs NAV {NAV:,.0f}")
    check("bada amount capped flag", b["preview"].get("capped") is True, str(b["preview"]))
    sc, plan = call("post", "/api/plan")
    if sc == 200:
        check("bada amount par warning aayi",
              any("se bada hai" in w for w in plan["warnings"]),
              str(plan["warnings"])[:200])

    # ---------------- 8. slider spam -- 25 request thok do ---------------
    codes = set()
    for i in range(25):
        sc, _ = dep("pct", pct=i * 4)
        codes.add(sc)
    check("slider spam par sab 200", codes == {200}, str(codes))
    sc, d2 = call("get", "/api/deploy")
    check("spam ke baad state saaf", sc == 200 and d2["mode"] == "pct"
          and abs(d2["pct"] - 96) < 0.01, str(d2)[:140])

    # ---------------- 9. sell-all deploy ke saath bhi chale --------------
    sc, b = dep("pct", pct=60)
    sc, sa = call("post", "/api/plan/sell-all")
    check("sell-all chala", sc in (200, 400), f"HTTP {sc}: {str(sa)[:180]}")
    if sc == 200:
        check("sell-all mein sirf SELL", all(o["side"] == "SELL" for o in sa["orders"]),
              str([o["side"] for o in sa["orders"]]))
        check("sell-all target_equity 0", sa["target_equity"] == 0, str(sa["target_equity"]))

    # ---------------- 10. wapas 'all' -> purana behaviour ----------------
    sc, b = dep("all")
    check("wapas all", sc == 200 and b["mode"] == "all", str(b)[:120])
    sc, plan = call("post", "/api/plan")
    check("all par plan bana", sc == 200, f"HTTP {sc}: {str(plan)[:200]}")
    if sc == 200:
        check("all par target ~ NAV", plan["target_equity"] >= plan["nav"] * 0.9,
              f"{plan['target_equity']:,.0f} vs nav {plan['nav']:,.0f}")
        sc2, dry = call("post", "/api/execute", json={"mode": "dry"})
        check("rehearsal chali", sc2 in (200, 400), f"HTTP {sc2}: {str(dry)[:160]}")
        sc3, _ = call("post", "/api/execute", json={"mode": "real", "confirm": "yes"})
        check("bina 'haan' asli execution ROKA", sc3 == 400, f"HTTP {sc3}")

    # ---------------- 11. khaali portfolio + deploy ----------------------
    srv.terminate(); srv.wait(timeout=10)
    set_mock(holdings="empty_list", cash=CASH)
    srv = start()
    with open(ROOT / "watchlist.csv", "rb") as f:
        call("post", "/api/watchlist", files={"file": ("w.csv", f, "text/csv")})
    sc, b = dep("pct", pct=30)
    check("khaali portfolio: 30% set", sc == 200, str(b)[:140])
    sc, plan = call("post", "/api/plan")
    check("khaali portfolio: plan bana", sc == 200, f"HTTP {sc}: {str(plan)[:200]}")
    if sc == 200:
        bv = sum(o["value"] for o in plan["orders"] if o["side"] == "BUY")
        check("khaali portfolio: 30% se zyada nahi laga",
              bv <= plan["target_equity"] + 1,
              f"buy {bv:,.0f} vs target {plan['target_equity']:,.0f}")
        check("khaali portfolio: sab BUY", all(o["side"] == "BUY" for o in plan["orders"]))

    # ---------------- 12. cash 0 + deploy --------------------------------
    srv.terminate(); srv.wait(timeout=10)
    set_mock(holdings="n4", cash=0)
    srv = start()
    with open(ROOT / "watchlist.csv", "rb") as f:
        call("post", "/api/watchlist", files={"file": ("w.csv", f, "text/csv")})
    sc, b = dep("pct", pct=40)
    check("cash 0: 40% set", sc == 200, str(b)[:140])
    sc, plan = call("post", "/api/plan")
    check("cash 0: 500 nahi aaya", sc in (200, 400), f"HTTP {sc}: {str(plan)[:200]}")

finally:
    if srv:
        srv.terminate()
        try: srv.wait(timeout=10)
        except Exception: pass
    if (ROOT / "config.yaml.bak").exists():
        shutil.copy(str(ROOT / "config.yaml.bak"), str(ROOT / "config.yaml"))
        (ROOT / "config.yaml.bak").unlink()

print("\n" + "=" * 70)
bad = [r for r in RESULTS if not r[1]]
print(f"  {len(RESULTS)-len(bad)}/{len(RESULTS)} pass, {len(bad)} FAIL")
for n, _, d in bad:
    print(f"    FAIL: {n}  {d}")
sys.exit(1 if bad else 0)
