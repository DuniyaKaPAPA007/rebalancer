"""Executor + DB + CSV + config -- yahan asli paisa jaata hai.\n\nChalao:  python tests/stress_exec.py\n"""
import copy, sys, tempfile, pathlib
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import rebalancer.config as cfgmod
import rebalancer.watchlist as wlmod
from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason, PlannedOrder, Plan
from rebalancer.planner import build_plan
from rebalancer.executor import Executor
from rebalancer.store import Store
from rebalancer.dhan import DhanError

FAILS=[]
def chk(n,c,d=""):
    if not c: FAILS.append((n,d))
    print(f"  {'[OK]' if c else '[FAIL]'} {n}" + (f"  -- {d}" if not c and d else ""))

CFG = cfgmod.load(__import__('pathlib').Path(__file__).resolve().parents[1] / 'config.yaml')
CFG["execution"]["phase_gap_sec"]=0
CFG["execution"]["fill_poll_interval_sec"]=0
CFG["execution"]["market_fallback_after_sec"]=0
CFG["risk"]["max_single_order_value_inr"]=10_00_00_000

class FakeBroker:
    def __init__(s, cash=10_00_000, fail_after=None, reject=(), dup_check=True):
        s.cash=cash; s.sent=[]; s.n=0; s.fail_after=fail_after
        s.reject=set(reject); s.dup_check=dup_check; s.by_corr={}
    def available_cash(s): return s.cash
    def holdings(s): return []
    def ltp(s, ids, **k): return {}
    def find_order_by_correlation(s, cid):
        return s.by_corr.get(cid) if s.dup_check else None
    def place_order(s, *, security_id, side, qty, correlation_id=None, **kw):
        sym = kw.get("symbol") or security_id
        if s.fail_after is not None and len(s.sent) >= s.fail_after:
            raise DhanError("broker gir gaya", 500, {"errorType":"Internal"})
        if security_id in s.reject:
            raise DhanError("RMS reject", 400, {"errorType":"RMS","errorMessage":"funds kam"})
        s.n+=1; oid=f"O{s.n:04d}"
        rec={"orderId":oid,"orderStatus":"TRADED","filledQty":qty,
             "averageTradedPrice":kw.get("price") or 100.0}
        s.sent.append((security_id, side, qty, correlation_id))
        if correlation_id: s.by_corr[correlation_id]=rec
        return rec
    def order(s, oid): return {"orderId":oid,"orderStatus":"TRADED","filledQty":1,"averageTradedPrice":100.0}
    def all_orders(s): return []
    def modify_to_market(s,*a,**k): return {}
    def cancel(s,*a,**k): return {}

_RUN=[0]
def mkplan(n=5, cash=10_00_000):
    _RUN[0]+=1
    names=[f"P{i}" for i in range(n)]
    px={s:100.0 for s in names}
    wl=[TargetName(rank=i+1,symbol=s,ref_ltp=100.0,market_cap_cr=25000.0) for i,s in enumerate(names)]
    circ={s:CircuitInfo(symbol=s,ltp=100.0,upper=120,lower=80,prev_close=100,volume=5_000_000) for s in names}
    return build_plan(run_id=f"E{_RUN[0]}",holdings=[],free_cash=cash,watchlist=wl,ltp=px,
                      security_ids={s:f"H{s}" for s in names},cfg=copy.deepcopy(CFG),circuit=circ)

tmp = pathlib.Path(tempfile.mkdtemp())

print("\n=== L. DUPLICATE ORDER GUARD (sabse zaroori) ===")
p=mkplan()
db=Store(str(tmp/"a.db")); db.save_run(p.run_id,"2026-08-20","PLANNED",p.nav,p.free_cash,p.slice_value,"{}")
b=FakeBroker()
r1=Executor(b,db,copy.deepcopy(CFG),dry_run=False).run(p)
first=len(b.sent)
r2=Executor(b,db,copy.deepcopy(CFG),dry_run=False).run(p)     # WAHI plan dobara
chk("L1 pehli baar order gaye", first>0, f"{first}")
chk("L2 DOBARA chalane par naye order NAHI gaye", len(b.sent)==first,
    f"pehle {first}, ab {len(b.sent)} -- {len(b.sent)-first} DUPLICATE!")

print("\n=== M. BROKER BEECH MEIN GIR GAYA ===")
p=mkplan(); db=Store(str(tmp/"b.db"))
db.save_run(p.run_id,"2026-08-20","PLANNED",p.nav,p.free_cash,p.slice_value,"{}")
b=FakeBroker(fail_after=2)
try:
    r=Executor(b,db,copy.deepcopy(CFG),dry_run=False).run(p)
    chk("M1 crash nahi hua", True)
    chk("M2 failed list mein aaya", len(r.get("failed") or [])>0, str(r.get("failed"))[:100])
    chk("M3 2 order gaye the wahi rahe", len(b.sent)==2, f"{len(b.sent)}")
except Exception as e:
    chk("M1 crash nahi hua", False, f"{type(e).__name__}: {e}")

print("\n=== N. RMS REJECT (funds kam) ===")
p=mkplan(); db=Store(str(tmp/"c.db"))
db.save_run(p.run_id,"2026-08-20","PLANNED",p.nav,p.free_cash,p.slice_value,"{}")
b=FakeBroker(reject={"HP1","HP3"})
r=Executor(b,db,copy.deepcopy(CFG),dry_run=False).run(p)
chk("N1 baaki order phir bhi gaye", len(b.sent)>=2, f"{len(b.sent)}")
chk("N2 reject hue failed mein", len(r.get("failed") or [])==2, str(len(r.get('failed') or [])))

print("\n=== O. CASH RE-CHECK (sell fill nahi hua) ===")
p=mkplan(cash=10_00_000); db=Store(str(tmp/"d.db"))
db.save_run(p.run_id,"2026-08-20","PLANNED",p.nav,p.free_cash,p.slice_value,"{}")
b=FakeBroker(cash=50_000)      # broker ke paas plan se bahut kam
r=Executor(b,db,copy.deepcopy(CFG),dry_run=False).run(p)
spent=sum(q*100.0 for _,sd,q,_ in b.sent if sd=="BUY")
chk("O1 asli cash se zyada nahi kharida", spent<=50_000*1.01, f"kharida Rs.{spent:,.0f}, cash Rs.50,000")

print("\n=== P. CSV parsing -- kachra input ===")
cases=[("khaali file","",True),
       ("sirf header","rank,symbol\n",True),
       ("symbol column nahi","a,b\n1,2\n",True),
       ("khaali symbol","rank,symbol\n1,\n2,HFCL\n",False),
       ("BOM + spaces","﻿rank, symbol \n 1 , HFCL \n",False),
       ("bada symbol","rank,symbol\n1,"+"A"*300+"\n",False),
       ("comma in quotes",'rank,symbol,name\n1,HFCL,"Ltd, India"\n',False),
       ("ek hi row","rank,symbol\n1,HFCL\n",False)]
for name,body,should_raise in cases:
    f=tmp/f"w_{abs(hash(name))}.csv"; f.write_text(body,encoding="utf-8")
    try:
        r=wlmod.read(f); raised=False; n=len(r)
    except Exception as e:
        raised=True; n=0; err=type(e).__name__
    if should_raise:
        chk(f"P: {name} -> saaf error", raised, "exception nahi aaya")
    else:
        chk(f"P: {name} -> padh liya ({n} naam)", not raised and n>0,
            f"raised={raised}" if raised else f"{n} naam")

print("\n=== Q. CONFIG pathologies ===")
import yaml
def load_bad(**over):
    c=copy.deepcopy(cfgmod.load(__import__('pathlib').Path(__file__).resolve().parents[1] / 'config.yaml'))
    for k,v in over.items():
        sec="portfolio" if k in c["portfolio"] else ("risk" if k in c["risk"] else "costs")
        c[sec][k]=v
    f=tmp/"cfg.yaml"; f.write_text(yaml.safe_dump(c))
    return cfgmod.load(f)
for name,kw,should_raise in [
    ("n_stocks = 0", dict(n_stocks=0), True),
    ("n_stocks = -5", dict(n_stocks=-5), True),
    ("cash_reserve 0.9", dict(cash_reserve_pct=0.9), True),
    ("cash_reserve -0.1", dict(cash_reserve_pct=-0.1), True),
    ("exit < n", dict(n_stocks=10, exit_rank_threshold=5), True),
    ("n=1 valid", dict(n_stocks=1, exit_rank_threshold=1), False),
    ("drift 0.5 valid", dict(drift_band_pct=0.5), False)]:
    try:
        load_bad(**kw); raised=False
    except Exception: raised=True
    chk(f"Q: {name}", raised==should_raise, f"raised={raised}, expected={should_raise}")

print("\n" + "="*70)
print(f"  {'SAB PASS' if not FAILS else f'{len(FAILS)} FAIL'}")
for n,d in FAILS: print(f"    FAIL: {n}  {d}")

print("\n=== R. PURANA PLAN (stale) ===")
import time as _t
p=mkplan(); db=Store(str(tmp/"e.db"))
db.save_run(p.run_id,"2026-08-20","PLANNED",p.nav,p.free_cash,p.slice_value,"{}")
b=FakeBroker()
p.created_ts = _t.time() - 3600            # 1 ghanta purana
try:
    Executor(b,db,copy.deepcopy(CFG),dry_run=False).run(p)
    chk("R1 purana plan ROKA gaya", False, "execute ho gaya!")
except RuntimeError as e:
    chk("R1 purana plan ROKA gaya", "purana" in str(e).lower(), str(e)[:90])
    chk("R2 ek bhi order nahi gaya", len(b.sent)==0, f"{len(b.sent)} gaye")
# rehearsal purane plan par bhi chalni chahiye (diagnostic hai)
b2=FakeBroker()
try:
    Executor(b2,db,copy.deepcopy(CFG),dry_run=True).run(p)
    chk("R3 rehearsal purane plan par bhi chalti hai", True)
except Exception as e:
    chk("R3 rehearsal purane plan par bhi chalti hai", False, str(e)[:80])
# taaza plan chalna chahiye
p2=mkplan(); db.save_run(p2.run_id,"2026-08-20","PLANNED",p2.nav,p2.free_cash,p2.slice_value,"{}")
b3=FakeBroker()
Executor(b3,db,copy.deepcopy(CFG),dry_run=False).run(p2)
chk("R4 taaza plan chalta hai", len(b3.sent)>0, f"{len(b3.sent)}")
# check band kar sakte ho
cfg0=copy.deepcopy(CFG); cfg0["execution"]["max_plan_age_min"]=0
p3=mkplan(); p3.created_ts=_t.time()-99999
db.save_run(p3.run_id,"2026-08-20","PLANNED",p3.nav,p3.free_cash,p3.slice_value,"{}")
b4=FakeBroker()
try:
    Executor(b4,db,cfg0,dry_run=False).run(p3)
    chk("R5 max_plan_age_min=0 -> check band", len(b4.sent)>0, "koi order nahi")
except Exception as e:
    chk("R5 max_plan_age_min=0 -> check band", False, str(e)[:80])

print("\n" + "="*70)
print(f"  FINAL: {'SAB PASS' if not FAILS else f'{len(FAILS)} FAIL'}")
for n,d in FAILS: print(f"    FAIL: {n}  {d}")
