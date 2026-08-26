"""Asli market ki mushkil situations -- T+1, split, delisting, circuit.\n\nChalao:  python tests/stress_hard.py\n"""
import copy, random, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import rebalancer.config as cfgmod
from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason
from rebalancer.planner import build_plan, build_liquidation_plan

BASE = cfgmod.load(__import__('pathlib').Path(__file__).resolve().parents[1] / 'config.yaml')
BASE["risk"]["max_single_order_value_inr"] = 10_00_00_000
BASE["risk"]["max_turnover_pct"] = 9.9
BASE["risk"]["min_market_cap_cr"] = 0
BASE["portfolio"]["n_stocks"] = "auto"
BASE["portfolio"]["exit_rank_threshold"] = "auto"

FAILS = []
def chk(name, cond, detail=""):
    if not cond: FAILS.append((name, detail))
    print(f"  {'[OK]' if cond else '[FAIL]'} {name}" + (f"  -- {detail}" if not cond and detail else ""))

def mk(names, holds, cash, px=None, cfg=None, circ_over=None):
    cfg = cfg or copy.deepcopy(BASE)
    allsym = set(names) | {h[0] for h in holds}
    px = px or {s: 100.0 for s in allsym}
    wl = [TargetName(rank=i+1, symbol=s, ref_ltp=px.get(s,100.0), market_cap_cr=25000.0)
          for i,s in enumerate(names)]
    hs = [Position(symbol=s, security_id=f"H{s}", total_qty=q, available_qty=a,
                   avg_price=px.get(s,100.0)) for s,q,a in holds]
    sec = {s: f"H{s}" for s in allsym}
    ltp = {s: px.get(s,100.0) for s in allsym}
    circ = {s: CircuitInfo(symbol=s, ltp=ltp[s], upper=ltp[s]*1.2, lower=ltp[s]*0.8,
                           prev_close=ltp[s], volume=int(5_00_00_000/max(ltp[s],1)))
            for s in allsym}
    if circ_over: circ.update(circ_over)
    return build_plan(run_id="T", holdings=hs, free_cash=cash, watchlist=wl,
                      ltp=ltp, security_ids=sec, cfg=cfg, circuit=circ)

N = [f"T{i}" for i in range(1,12)]
print("\n=== A. T+1 UNSETTLED ===")
p = mk(N, [("OLD",1000,0)], 5_00_000)
chk("A1 poora unsettled -> SELL nahi banta", not any(o.symbol=="OLD" for o in p.orders))
chk("A1 skipped mein aaya", any(s.symbol=="OLD" for s in p.skipped), str(p.skipped))
p = mk(N, [("OLD",1000,300)], 5_00_000)
o = next((o for o in p.orders if o.symbol=="OLD"), None)
chk("A2 aadha settled -> sirf 300 bikta", o and o.qty==300, f"{o.qty if o else None}")
chk("A2 warning aayi", any("unsettled" in w for w in p.warnings))
p = mk(N[:5]+["HELD"], [("HELD",5000,100)], 1_00_000)
o = next((o for o in p.orders if o.symbol=="HELD" and o.side is Side.SELL), None)
chk("A3 TRIM bhi sellable tak hi", (o is None) or o.qty<=100, f"{o.qty if o else 'no order'}")

print("\n=== B. CORPORATE ACTIONS ===")
# split: qty 2x, price 0.5x -- value same
px = {s:100.0 for s in N}; px["SPLIT"]=50.0
p = mk(N[:10]+["SPLIT"], [("SPLIT",2000,2000)], 5_00_000, px=px)
chk("B1 split ke baad plan banta hai", not p.blockers, str(p.blockers))
# bonus: qty badha, avg gira -- app ko farak nahi padna chahiye
p = mk(N, [("T1",9999,9999)], 5_00_000)
chk("B2 bonus se qty phool gayi -> TRIM aata hai",
    any(o.symbol=="T1" and o.reason is Reason.TRIM for o in p.orders))
# delisting: hold hai par price nahi
try:
    cfg=copy.deepcopy(BASE)
    hs=[Position(symbol="DEAD",security_id="HDEAD",total_qty=100,available_qty=100,avg_price=50)]
    wl=[TargetName(rank=i+1,symbol=s,ref_ltp=100.0,market_cap_cr=25000.0) for i,s in enumerate(N)]
    sec={s:f"H{s}" for s in N+["DEAD"]}; ltp={s:100.0 for s in N}; ltp["DEAD"]=0.0
    circ={s:CircuitInfo(symbol=s,ltp=100.0,upper=120,lower=80,prev_close=100,volume=500000) for s in N}
    p=build_plan(run_id="D",holdings=hs,free_cash=5_00_000,watchlist=wl,ltp=ltp,
                 security_ids=sec,cfg=cfg,circuit=circ)
    chk("B3 delisted scrip -> BLOCK, crash nahi", bool(p.blockers), "koi blocker nahi aaya")
    chk("B3 blocker mein naam hai", any("DEAD" in b for b in p.blockers), str(p.blockers))
except Exception as e:
    chk("B3 delisted scrip", False, f"CRASH: {type(e).__name__}: {e}")

print("\n=== C. CIRCUIT LOCK ===")
lock = {"T1": CircuitInfo(symbol="T1",ltp=120.0,upper=120.0,lower=80.0,prev_close=100.0,volume=500000)}
p = mk(N, [], 10_00_000, circ_over=lock)
chk("C1 upper circuit par BUY -> warning", any("UPPER CIRCUIT" in w for w in p.warnings), str(p.warnings)[:100])
chk("C1 order phir bhi banta hai (choice user ka)", any(o.symbol=="T1" for o in p.orders))
lock2 = {"OUTX": CircuitInfo(symbol="OUTX",ltp=80.0,upper=120.0,lower=80.0,prev_close=100.0,volume=500000)}
p = mk(N, [("OUTX",500,500)], 5_00_000, circ_over=lock2)
chk("C2 lower circuit par SELL -> warning", any("LOWER CIRCUIT" in w for w in p.warnings))

print("\n=== D. PAISE ke edge ===")
p = mk(N, [], -50_000)
chk("D1 negative cash -> crash nahi", isinstance(p.orders, list))
chk("D1 negative cash mein koi BUY nahi", not [o for o in p.orders if o.side is Side.BUY], str(p.orders[:2]))
px={s:100.0 for s in N}; px["T1"]=9_00_000.0
p = mk(N, [], 10_00_000, px=px)
chk("D2 1 share > slice -> crash nahi", isinstance(p.orders, list))
chk("D2 warning aayi", any("share" in w.lower() for w in p.warnings), str(p.warnings)[:120])
p = mk(N, [], 0.01)
chk("D3 1 paisa cash -> koi order nahi", len(p.orders)==0 or all(o.qty>0 for o in p.orders))
p = mk(N, [], 1e12)
chk("D4 Rs.1 lakh crore -> qty overflow nahi", all(o.qty>0 and o.qty<10**12 for o in p.orders))

print("\n=== E. WATCHLIST pathologies ===")
p = mk(["A","A","B","C"], [], 5_00_000)
chk("E1 duplicate symbol -> BLOCK", any("duplicate" in b.lower() for b in p.blockers), str(p.blockers))
p = mk(["ONLY"], [], 5_00_000)
chk("E2 sirf 1 naam -> chalta hai", not p.blockers, str(p.blockers))
chk("E2 100% ek hi mein", len([o for o in p.orders if o.side is Side.BUY])==1)
p = mk([f"U{i}" for i in range(33)], [], 5_00_000)   # 33 alag naam
chk("E3 33 naam -> sab handle", not p.blockers and len(p.blockers)==0, str(p.blockers)[:80])

print("\n=== F. SAB BECHO edge ===")
cfg=copy.deepcopy(BASE)
hs=[Position(symbol="X",security_id="HX",total_qty=100,available_qty=0,avg_price=50)]
p=build_liquidation_plan(run_id="L",holdings=hs,free_cash=0,ltp={"X":100.0},
                         security_ids={"X":"HX"},cfg=cfg,circuit={})
chk("F1 sab unsettled -> BLOCK saaf message", bool(p.blockers) and "T+1" in str(p.blockers), str(p.blockers))
p=build_liquidation_plan(run_id="L",holdings=[],free_cash=1000,ltp={},
                         security_ids={},cfg=cfg,circuit={})
chk("F2 khaali portfolio -> BLOCK", bool(p.blockers))
hs=[Position(symbol="Y",security_id="HY",total_qty=100,available_qty=100,avg_price=50)]
p=build_liquidation_plan(run_id="L",holdings=hs,free_cash=0,ltp={"Y":0.0},
                         security_ids={"Y":"HY"},cfg=cfg,circuit={})
chk("F3 price 0 -> BLOCK, bina price ke nahi bechte", bool(p.blockers), str(p.blockers))

print("\n" + "="*70)
print(f"  {'SAB PASS' if not FAILS else f'{len(FAILS)} FAIL'}")
for n,d in FAILS: print(f"    FAIL: {n}  {d}")
