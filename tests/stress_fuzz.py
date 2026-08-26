"""Planner fuzz -- 12,000 random scenarios.

Chalao:  python tests/stress_fuzz.py
"""
import copy, random, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
import rebalancer.config as cfgmod
from rebalancer.models import Position, TargetName, CircuitInfo, Side, Reason
from rebalancer.planner import build_plan

BASE = cfgmod.load(__import__('pathlib').Path(__file__).resolve().parents[1] / 'config.yaml')
BASE["risk"]["max_single_order_value_inr"] = 10_00_00_000
BASE["risk"]["max_turnover_pct"] = 9.9
BASE["risk"]["min_market_cap_cr"] = 0
random.seed(777)

bad_cash=[]; bad_qty=[]; bad_wash=[]; bad_dup=[]; blocked=0; ok=0
for it in range(12000):
    n_wl   = random.randint(2, 30)
    n_hold = random.randint(0, 12)
    overlap= random.random()
    cfg = copy.deepcopy(BASE)
    cfg["portfolio"]["n_stocks"] = random.choice(["auto", min(n_wl, random.randint(1,20))])
    cfg["portfolio"]["exit_rank_threshold"] = "auto"
    cfg["portfolio"]["use_overflow_slot"] = random.random() < .7
    cfg["portfolio"]["drift_band_pct"] = random.choice([0.0,0.0,0.05,0.20])
    cfg["portfolio"]["cash_reserve_pct"] = random.choice([0.0,0.01,0.03])
    cfg["portfolio"]["partial_list_mode"] = random.choice(["full","fixed_slots"])

    wl_syms=[f"W{i}" for i in range(n_wl)]
    pool = wl_syms + [f"X{i}" for i in range(8)]
    hold_syms = random.sample(pool, min(n_hold, len(pool)))
    prices={s: round(random.uniform(12, 4000),2) for s in set(pool)}
    holds=[]
    for s in hold_syms:
        q=random.randint(1, 4000)
        a=q if random.random()<.8 else random.randint(0,q)
        holds.append(Position(symbol=s,security_id=f"H{s}",total_qty=q,
                              available_qty=a,avg_price=prices[s]*random.uniform(.6,1.4)))
    cash=round(random.uniform(0, 60_00_000),2)
    wl=[TargetName(rank=i+1,symbol=s,ref_ltp=prices[s],market_cap_cr=25000.0)
        for i,s in enumerate(wl_syms)]
    allsym=set(pool)
    sec={s:f"H{s}" for s in allsym}
    circ={s:CircuitInfo(symbol=s,ltp=prices[s],upper=prices[s]*1.2,lower=prices[s]*.8,
                        prev_close=prices[s],volume=10_00_000) for s in allsym}
    p=build_plan(run_id=f"F{it}",holdings=holds,free_cash=cash,watchlist=wl,ltp=prices,
                 security_ids=sec,cfg=cfg,circuit=circ)
    if p.blockers: blocked+=1; continue
    ok+=1

    buys=[o for o in p.orders if o.side is Side.BUY]
    sells=[o for o in p.orders if o.side is Side.SELL]
    # 1. cash conservation: buy value <= cash + sell proceeds (costs ke baad)
    proceeds = sum(o.value for o in sells)*(1-cfg["costs"]["est_sell_cost_pct"]) \
               - cfg["costs"]["dp_charge_per_scrip_inr"]*len(sells)
    need = sum(o.value for o in buys)*(1+cfg["costs"]["est_buy_cost_pct"])
    if need > cash + proceeds + 1.0:
        bad_cash.append((it, need-(cash+proceeds)))
    # 2. qty sanity
    for o in p.orders:
        if o.qty<=0: bad_qty.append((it,o.symbol,o.qty))
    # 3. same symbol buy AND sell (wash)
    bs={o.symbol for o in buys}; ss={o.symbol for o in sells}
    if bs & ss: bad_wash.append((it, sorted(bs&ss)))
    # 4. same symbol do baar same side
    for side,lst in (("B",buys),("S",sells)):
        syms=[o.symbol for o in lst]
        if len(syms)!=len(set(syms)): bad_dup.append((it,side))
    # 5. oversell -- sellable se zyada bech to nahi rahe
    hq={h.symbol:h.available_qty for h in holds}
    for o in sells:
        if o.qty > hq.get(o.symbol,0): bad_cash.append((it,f"OVERSELL {o.symbol}"))

print(f"chale: {ok} plans ok, {blocked} blocked")
print(f"  CASH se zyada maanga : {len(bad_cash)}  {bad_cash[:4]}")
print(f"  qty <= 0             : {len(bad_qty)}  {bad_qty[:4]}")
print(f"  WASH trade           : {len(bad_wash)}  {bad_wash[:4]}")
print(f"  duplicate order      : {len(bad_dup)}  {bad_dup[:4]}")
