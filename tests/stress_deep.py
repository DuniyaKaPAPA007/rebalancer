"""Behavioural properties -- idempotence, thrashing, 200-week run.\n\nChalao:  python tests/stress_deep.py\n"""
import copy, random, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import rebalancer.config as cfgmod
from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason
from rebalancer.planner import build_plan
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent))
from stress_weeks import Book, BASE as WBASE

BASE = cfgmod.load(__import__('pathlib').Path(__file__).resolve().parents[1] / 'config.yaml')
BASE["risk"]["max_single_order_value_inr"] = 10_00_00_000
BASE["risk"]["max_turnover_pct"] = 9.9
BASE["risk"]["min_market_cap_cr"] = 0
BASE["portfolio"]["n_stocks"] = "auto"
BASE["portfolio"]["exit_rank_threshold"] = "auto"

FAILS=[]
def chk(n,c,d=""):
    if not c: FAILS.append((n,d))
    print(f"  {'[OK]' if c else '[FAIL]'} {n}" + (f"  -- {d}" if not c and d else ""))

def plan_for(names, book, px, cfg=None):
    cfg = cfg or copy.deepcopy(BASE)
    held = book.positions()
    allsym = set(names) | {h.symbol for h in held}
    wl=[TargetName(rank=i+1,symbol=s,ref_ltp=px[s],market_cap_cr=25000.0) for i,s in enumerate(names)]
    sec={s:f"H{s}" for s in allsym}; ltp={s:px[s] for s in allsym}
    circ={s:CircuitInfo(symbol=s,ltp=px[s],upper=px[s]*1.2,lower=px[s]*0.8,
                        prev_close=px[s],volume=int(5_00_00_000/px[s])) for s in allsym}
    return build_plan(run_id="X",holdings=held,free_cash=book.cash,watchlist=wl,
                      ltp=ltp,security_ids=sec,cfg=cfg,circuit=circ)

def execute(plan, book, px, cfg, fill=1.0, rng=None):
    rng = rng or random.Random(0)
    for o in [o for o in plan.orders if o.side is Side.SELL]:
        book.apply(o, o.qty if rng.random()<=fill else int(o.qty*0.5), px[o.symbol], cfg)
    for o in [o for o in plan.orders if o.side is Side.BUY]:
        if book.cash < o.value*1.001: continue
        book.apply(o, o.qty if rng.random()<=fill else int(o.qty*0.5), px[o.symbol], cfg)

U=[f"S{i:03d}" for i in range(40)]
cfg=copy.deepcopy(BASE)

print("\n=== G. IDEMPOTENCE -- kuch nahi badla toh plan khaali hona chahiye ===")
px={s:round(100+i*7.3,2) for i,s in enumerate(U)}
names=U[:11]
book=Book(10_00_000)
for i in range(3):
    p=plan_for(names,book,px); execute(p,book,px,cfg); book.settle_day()
p2=plan_for(names,book,px)
big=[o for o in p2.orders if o.value > p2.slice_value*0.02]
chk("G1 3 baar wahi list -> chhote-mote adjust hi bache",
    len(big)<=1, f"{len(big)} bade order: {[(o.symbol,round(o.value)) for o in big[:3]]}")
p3=plan_for(names,book,px)
chk("G2 plan dobara banane se result nahi badalta",
    len(p2.orders)==len(p3.orders), f"{len(p2.orders)} vs {len(p3.orders)}")

print("\n=== H. THRASHING -- A,B,A,B list badalne par ===")
A,B = U[:11], U[5:16]          # 6 naam common
book=Book(10_00_000); churns=[]
for i in range(12):
    p=plan_for(A if i%2==0 else B, book, px)
    churns.append(p.churn_pct); execute(p,book,px,cfg); book.settle_day()
common=set(A)&set(B)
p=plan_for(A,book,px)
touched={o.symbol for o in p.orders}
chk("H1 common naam poore nahi bikte", not any(
    o.symbol in common and o.reason is Reason.EXIT for o in p.orders),
    str([o.symbol for o in p.orders if o.reason is Reason.EXIT]))
chk("H2 churn 100% se neeche rehta", max(churns[2:])<0.95, f"max {max(churns[2:])*100:.0f}%")
print(f"       churn har hafte: {[f'{c*100:.0f}%' for c in churns]}")

print("\n=== I. LONG RUN -- 200 hafte, kharab fills ===")
rng=random.Random(9); book=Book(10_00_000); pos_hist=[]
for wk in range(200):
    for s in U: px[s]=max(1.0,round(px[s]*(1+rng.gauss(0,0.06)),2))
    book.settle_day()
    p=plan_for(rng.sample(U,11),book,px)
    if p.blockers: continue
    execute(p,book,px,cfg,fill=0.55,rng=rng)
    pos_hist.append(len([1 for v in book.pos.values() if v[0]>0]))
chk("I1 positions unbounded nahi badhte", max(pos_hist)<40, f"max {max(pos_hist)}")
chk("I2 cash kabhi negative nahi", book.cash>=-1, f"{book.cash:.2f}")
chk("I3 koi negative qty nahi", all(v[0]>=0 for v in book.pos.values()))
print(f"       positions: shuru {pos_hist[0]}, max {max(pos_hist)}, aakhir {pos_hist[-1]}")
print(f"       final NAV Rs.{book.value(px)+book.cash:,.0f} | cash Rs.{book.cash:,.0f}")

print("\n=== J. PRICE SHOCK ===")
book=Book(10_00_000); px2={s:100.0 for s in U}
p=plan_for(U[:11],book,px2); execute(p,book,px2,cfg); book.settle_day()
for s in U[:5]: px2[s]=5.0        # -95%
for s in U[5:11]: px2[s]=400.0    # +300%
p=plan_for(U[:11],book,px2)
chk("J1 90% crash + 4x rally -> plan banta hai", not p.blockers, str(p.blockers)[:90])
chk("J2 koi qty<=0 nahi", all(o.qty>0 for o in p.orders))
buys=[o for o in p.orders if o.side is Side.BUY]
sells=[o for o in p.orders if o.side is Side.SELL]
need=sum(o.value for o in buys)*1.001
got=book.cash+sum(o.value for o in sells)*0.999
chk("J3 cash ke andar rehta hai", need<=got+1, f"{need:.0f} > {got:.0f}")

print("\n=== K. WATCHLIST SIZE badalta rahe ===")
book=Book(10_00_000); ok=True; msgs=[]
for n in [3,20,5,33,1,11,40,2]:
    p=plan_for(U[:n],book,px2)
    if p.blockers: ok=False; msgs.append(f"n={n}: {p.blockers[0][:60]}")
    else: execute(p,book,px2,cfg); book.settle_day()
chk("K1 3->20->5->33->1->11->40->2 sab chalta hai", ok, "; ".join(msgs))
chk("K2 cash positive", book.cash>=-1, f"{book.cash:.2f}")

print("\n" + "="*70)
print(f"  {'SAB PASS' if not FAILS else f'{len(FAILS)} FAIL'}")
for n,d in FAILS: print(f"    FAIL: {n}  {d}")
