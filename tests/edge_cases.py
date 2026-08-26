"""Live-path edge cases -- POORI app, nakli Dhan ke against.

Ye pytest nahi hai, standalone script hai (har case ke liye asli server
uthata hai, isliye dheema hai). Chalane ka tareeka:

    python -m uvicorn --app-dir tests _mock_dhan:app --port 8991 &
    python tests/edge_cases.py

Jo cases yahan cover hote hain -- inme se kai asli bug nikal chuke hain:
  * khaali portfolio (200+[], 500 no-data, {"data":[]}, null, qty 0)
  * cash 0 / Rs.500 / Rs.5,000 / Rs.10 crore
  * holdings hain par cash zero
  * quote API fail
  * auth fail aur Dhan outage -> app ko NAKLI data NAHI dena chahiye
"""
import base64, json, subprocess, sys, time, requests, os, shutil, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCK = os.environ.get("EDGE_MOCK", "http://127.0.0.1:8991")
PORT = int(os.environ.get("EDGE_PORT", 8901))
B = f"http://127.0.0.1:{PORT}"

def jwt(days=30, cid="1100112233"):
    h=base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip('=')
    p=base64.urlsafe_b64encode(json.dumps({"exp":int(time.time())+days*86400,
        "dhanClientId":cid}).encode()).decode().rstrip('=')
    return f"{h}.{p}.sig"

def set_mock(**kw):
    requests.post(f"{MOCK}/v2/_set/quote/ok", timeout=5)   # default reset
    for k,v in kw.items():
        requests.post(f"{MOCK}/v2/_set/{k}/{v}", timeout=5)

def backup_cfg():
    import shutil as _sh
    _sh.copy(str(ROOT/"config.yaml"), str(ROOT/"config.yaml.bak"))


def seed_scrip_master():
    """Nakli NSE scrip master -- taaki container bina internet ke bhi test ho."""
    import csv as _csv
    cache = ROOT/".cache"/"scrip_master.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    syms = ["HFCL","STLTECH","DIACABS","SWANDEF","CUPID","SHILPAMED","SKYGOLD",
            "RUBICON","AVALON","SANSERA","E2E","PGEL","KAYNES","CPPLUS",
            "WELCORP","CEMPRO","ATHERENERG","KIRLOSENG","SCHNEIDER","APARINDS",
            "RRKABEL","SYRMA","ADANIENSOL","ACUTAAS","EMMVEE","POWERINDIA",
            "THERMAX","AMBER","DIXON","SUZLON","BSE","CDSL","HAL"]
    with cache.open("w", newline="") as f:
        w=_csv.writer(f)
        w.writerow(["EXCH_ID","SEGMENT","SECURITY_ID","TRADING_SYMBOL",
                    "INSTRUMENT","SERIES","ISIN"])
        for i,s in enumerate(syms, 10000):
            w.writerow(["NSE","E",str(i),s,"EQUITY","EQ",f"INE{i:09d}"])
    # download skip karne ke liye mtime abhi ka rakho
    return cache

def start():
    backup_cfg()
    seed_scrip_master()
    # config ko mock par point karo
    c=(ROOT/"config.yaml").read_text()
    c=c.replace("base_url: https://api.dhan.co/v2", f"base_url: {MOCK}/v2")
    (ROOT/"config.yaml").write_text(c)
    for f in ("runs.db",): (ROOT/f).unlink(missing_ok=True)
    shutil.rmtree(ROOT/"plans", ignore_errors=True)
    env=dict(os.environ, DHAN_CLIENT_ID="1100112233", DHAN_ACCESS_TOKEN=jwt())
    p=subprocess.Popen([sys.executable,"-m","uvicorn","web.api:app","--host","127.0.0.1",
                        "--port",str(PORT)], cwd=ROOT, env=env,
                       stdout=open("/tmp/edge_srv.log","w"), stderr=subprocess.STDOUT)
    for _ in range(40):
        try:
            requests.get(f"{B}/api/health", timeout=2); return p
        except Exception: time.sleep(0.4)
    raise RuntimeError("server start nahi hua")

def call(m, path, **kw):
    r = getattr(requests, m)(f"{B}{path}", timeout=90, **kw)
    try: body=r.json()
    except Exception: body=r.text[:200]
    return r.status_code, body

RESULTS=[]
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  -- {detail}" if detail and not cond else ""))

try:
    CASES = [
        ("Khaali portfolio (200 + [])",           dict(holdings="empty_list", cash=500000)),
        ("Khaali portfolio (500 no-data)",        dict(holdings="no_data_500", cash=500000)),
        ("Khaali portfolio ({'data':[]})",        dict(holdings="wrapped_empty", cash=500000)),
        ("holdings null aaya",                    dict(holdings="null", cash=500000)),
        ("qty 0 wali row",                        dict(holdings="zero_qty", cash=500000)),
        ("Khaali + cash bhi 0",                   dict(holdings="empty_list", cash=0)),
        ("Khaali + bahut kam cash (Rs.500)",      dict(holdings="empty_list", cash=500)),
        ("Khaali + Rs.5,000",                     dict(holdings="empty_list", cash=5000)),
        ("Khaali + Rs.10 crore",                  dict(holdings="empty_list", cash=100000000)),
        ("4 holdings + normal cash",              dict(holdings="n4", cash=500000)),
        ("4 holdings + zero cash",                dict(holdings="n4", cash=0)),
        ("Quote API fail",                        dict(holdings="empty_list", cash=500000, quote="fail")),
        ("Auth fail beech mein",                  dict(holdings="auth_fail", cash=500000)),
        ("Dhan outage (503)",                     dict(holdings="outage", cash=500000)),
    ]
    # ye cases mein plan ka FAIL hona hi sahi hai
    EXPECT_PLAN_FAIL = {"Auth fail beech mein", "Dhan outage (503)", "Quote API fail"}
    EXPECT_HOLD_FAIL = {"Auth fail beech mein", "Dhan outage (503)"}
    EXPECT_EXEC_BLOCK = {"Khaali + Rs.10 crore"}
    EXPECT_NO_ORDERS  = {"Khaali + cash bhi 0"}
    for name, mock in CASES:
        print(f"\n=== {name} ===")
        set_mock(**mock)
        srv = start()                     # har case fresh server par
        sc, h = call("get","/api/health")
        check(f"{name}: health 200", sc==200, f"got {sc}")
        expect_live = name not in EXPECT_HOLD_FAIL
        check(f"{name}: mode sahi", (h.get("mode")=="live")==expect_live,
              f"mode={h.get('mode')} msg={h.get('autodetect_msg','')[:80]}")
        sc, hold = call("get","/api/holdings")
        if name in EXPECT_HOLD_FAIL:
            check(f"{name}: /holdings ne SAAF mana kiya (nakli data nahi diya)",
                  sc==502, f"HTTP {sc}: {str(hold)[:140]}")
            check(f"{name}: nakli portfolio NAHI dikhaya",
                  sc==502 and "holdings" not in str(hold)[:40], str(hold)[:100])
        else:
            check(f"{name}: /holdings chala", sc==200, f"HTTP {sc}: {hold}")
        if sc==200:
            check(f"{name}: cash sahi", abs(hold.get('cash',-1)-mock['cash'])<1,
                  f"expected {mock['cash']} got {hold.get('cash')}")
        with open(ROOT/"watchlist.csv","rb") as f:
            sc, wl = call("post","/api/watchlist", files={"file":("w.csv",f,"text/csv")})
        check(f"{name}: watchlist upload", sc==200, str(wl)[:120])
        sc, plan = call("post","/api/plan")
        ok = sc==200
        if name in EXPECT_NO_ORDERS:
            check(f"{name}: khaali account par saaf message", sc==200 and plan.get("blockers"),
                  f"HTTP {sc}")
            if sc==200:
                check(f"{name}: message samajh aata hai",
                      any("khaali" in b.lower() for b in plan.get("blockers",[])),
                      str(plan.get("blockers"))[:150])
            srv.terminate(); srv.wait(timeout=10); continue
        if name in EXPECT_PLAN_FAIL:
            check(f"{name}: plan ne SAAF mana kiya (500 nahi)", sc in (400,502,503),
                  f"HTTP {sc}: {str(plan)[:160]}")
            srv.terminate(); srv.wait(timeout=10)
            continue
        check(f"{name}: plan bana", ok, f"HTTP {sc}: {str(plan)[:200]}")
        if ok:
            buys=[o for o in plan["orders"] if o["side"]=="BUY"]
            sells=[o for o in plan["orders"] if o["side"]=="SELL"]
            bv=sum(o["value"] for o in buys)
            check(f"{name}: NAV theek", plan["nav"]>=0, str(plan["nav"]))
            check(f"{name}: cash se zyada nahi", bv <= mock['cash'] + sum(o['value'] for o in sells) + 1,
                  f"buy {bv:.0f} vs cash {mock['cash']}")
            check(f"{name}: koi qty<=0 nahi", all(o["qty"]>0 for o in plan["orders"]))
            sc2, dry = call("post","/api/execute", json={"mode":"dry"})
            if name in EXPECT_EXEC_BLOCK:
                check(f"{name}: rehearsal ne SAAF roka (500 nahi)", sc2==400,
                      f"HTTP {sc2}: {str(dry)[:160]}")
            else:
                check(f"{name}: rehearsal chala", sc2==200, f"HTTP {sc2}: {str(dry)[:150]}")
            # asli execution bina "haan" ke kabhi nahi
            sc3, _ = call("post","/api/execute", json={"mode":"real","confirm":"yes"})
            check(f"{name}: bina 'haan' asli execution ROKA", sc3==400, f"HTTP {sc3}")
        srv.terminate(); srv.wait(timeout=10)
finally:
    import shutil as _sh; _sh.copy(str(ROOT / "config.yaml.bak"), ROOT/"config.yaml")

print("\n" + "="*70)
bad=[r for r in RESULTS if not r[1]]
print(f"  {len(RESULTS)-len(bad)}/{len(RESULTS)} pass, {len(bad)} FAIL")
for n,_,d in bad: print(f"    FAIL: {n}  {d}")
